#!/usr/bin/env python3
"""verdict.py: the host side of evaluate_one.sh, which decides whether a patch resolves its task.

The grading container applies the patch and runs the task's tests/test.sh, so the patch's code runs
there as root: anything inside that container, and any file it can write, may have been changed by
it. So nothing from there decides the verdict except the raw output of the task's test run: this
script, on the host, parses it with the task's own parser.py and applies test.sh's own rule (every
fail_to_pass and pass_to_pass test PASSED), with the task's real config.json, which the container
never sees (it gets config_for_container.json, without those two lists). The container's own
reward.txt and RESULT lines are ignored.

Run with `python3 -I -S` (no current directory, no site-packages on the path); the parser runs the
same way, in an empty directory. Files written by the container are read only if they are plain
files (no symlink, device or fifo is followed or opened) and not too large.

    verdict.py sanitize <config.json> <out.json>     config.json without the lists of required tests
    verdict.py decide --tests DIR --out OUT --patch FILE [--container DIR] [--timed-out] [--no-patch]

`decide` writes, in OUT (a folder the container never mounts): output.json (the host's parse),
run-script-stdout.txt and run-script-stderr.txt (plain copies of the container's raw test logs),
apply.log and post_apply_status.txt (from the blocks the container printed before the patch's code
could run), verdict.json {resolved, required, passed, passed_required, logs, note}, patch_flags.json
(what in the patch could hijack Python or the grading: flagged for staff, never refused), and
reward.txt ("1" or "0"); it appends the host's verdict to verifier_stdout.txt and prints 1 or 0.
"""

import argparse
import ast
import json
import os
import re
import stat
import subprocess
import sys
import tempfile

LOG_CAP = 512 * 1024 * 1024          # a raw test log larger than this is not parsed (the verdict is 0)
PARSER_TIMEOUT_S = 900               # the task's parser on a hostile log: never wait forever
BLOCKS = {"apply.log": "apply-log", "post_apply_status.txt": "status"}   # INNER's marked blocks


def sanitize(src, dst):
    config = json.load(open(src))
    for key in ("fail_to_pass", "pass_to_pass"):
        if key in config:
            config[key] = []
    with open(dst, "w") as f:
        json.dump(config, f)


def parse_tests(data):
    """As test.sh: a list, or a string holding JSON or a Python literal."""
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            pass
        try:
            return ast.literal_eval(data)
        except (ValueError, SyntaxError):
            return []
    return data if isinstance(data, list) else []


def copy_plain(src, dst, cap=LOG_CAP):
    """Copy src to dst if it is a plain file of at most `cap` bytes, never following a symlink or
    opening a device or fifo -> (copied, why not)."""
    try:
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        return False, "missing" if isinstance(e, FileNotFoundError) else f"not a plain file ({e.strerror})"
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return False, "not a plain file"
        if st.st_size > cap:
            return False, f"too large ({st.st_size} bytes)"
        with open(dst, "wb") as out:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                if out.tell() > cap:
                    return False, "too large"
    return True, ""


def marked_blocks(stdout_path):
    """The first apply-log and status blocks INNER printed, before test.sh (and the patch's code)
    started: printed lines cannot be taken back, so later output cannot change them. (The file is
    the docker client's output, written by the host.)"""
    found = {}
    try:
        with open(stdout_path, "rb") as f:
            head = f.read(64 * 1024 * 1024).decode("utf-8", "replace")
    except OSError:
        return found
    for name, tag in BLOCKS.items():
        m = re.search(rf"^@@a3-{tag}-begin@@\n(.*?)^@@a3-{tag}-end@@$", head, re.S | re.M)
        if m:
            found[name] = m.group(1)
    return found


STDLIB = set(getattr(sys, "stdlib_module_names", ())) - {"test", "this", "antigravity"}


def patch_flags(text):
    """What in the patch could hijack Python start-up or imports, or reach the grading itself:
    staff review these (the patch is graded all the same)."""
    flags = []
    created = re.findall(r"^diff --git a/(\S+) b/\S+\n(?:(?:old|new) mode \d+\n)*new file mode", text, re.M)
    for path in created:
        if "/" not in path and path.endswith(".py") and path[:-3] in STDLIB:
            flags.append(f"new top-level module {path} has the name of a standard-library module")
        elif path.count("/") == 1 and path.endswith("/__init__.py") and path.split("/")[0] in STDLIB:
            flags.append(f"new top-level package {path.split('/')[0]}/ has the name of a standard-library module")
    for path in re.findall(r"^diff --git a/(\S+) b/", text, re.M):
        base = os.path.basename(path)
        if base in ("sitecustomize.py", "usercustomize.py") or base.endswith(".pth"):
            flags.append(f"{path}: a file Python runs or reads at start-up")
    for what, rx in (("/logs/verifier (the grading output folder)", r"/logs/verifier"),
                     ("reward.txt (the verdict file)", r"reward\.txt"),
                     ("the hidden test files in /tests", r"(?<![\w./-])/tests/(?:config\.json|test\.sh|run_script\.sh|parser\.py)")):
        if re.search(rx, text):
            flags.append(f"the patch mentions {what}")
    return flags


def run_parser(tests_dir, stdout_copy, stderr_copy, out_json, log_path):
    """The task's parser.py on the host, isolated, in an empty directory -> (ok, note)."""
    with tempfile.TemporaryDirectory(prefix="a3verdict_") as cwd, open(log_path, "wb") as log:
        try:
            r = subprocess.run([sys.executable, "-I", "-S", os.path.join(tests_dir, "parser.py"),
                                stdout_copy, stderr_copy, out_json],
                               cwd=cwd, stdout=log, stderr=subprocess.STDOUT, timeout=PARSER_TIMEOUT_S,
                               env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        except subprocess.TimeoutExpired:
            return False, f"the parser took over {PARSER_TIMEOUT_S} s"
    if r.returncode != 0 or not os.path.exists(out_json):
        return False, f"the parser failed (exit {r.returncode})"
    return True, ""


def decide(args):
    out = args.out
    config = json.load(open(os.path.join(args.tests, "config.json")))
    required = set(parse_tests(config.get("fail_to_pass", []))) | set(parse_tests(config.get("pass_to_pass", [])))
    flags = []
    if not args.no_patch:
        try:
            flags = patch_flags(open(args.patch, encoding="utf-8", errors="replace").read())
        except OSError:
            pass
    json.dump(flags, open(os.path.join(out, "patch_flags.json"), "w"), indent=1)

    passed, note, logs = set(), "", False
    vs = os.path.join(out, "verifier_stdout.txt")
    if args.no_patch:
        note = "no patch"
    else:
        for name, text in marked_blocks(vs).items():
            with open(os.path.join(out, name), "w") as f:
                f.write(text)
        copies = {}
        for name in ("run-script-stdout.txt", "run-script-stderr.txt"):
            dst = os.path.join(out, name)
            ok, why = copy_plain(os.path.join(args.container, name), dst) if args.container else (False, "missing")
            if not ok and os.path.lexists(dst):
                os.unlink(dst)
            copies[name] = (ok, why)
        if all(ok for ok, _ in copies.values()):
            logs = True
            host_json = os.path.join(out, "output.json")
            ok, why = run_parser(args.tests, os.path.join(out, "run-script-stdout.txt"),
                                 os.path.join(out, "run-script-stderr.txt"), host_json, os.path.join(out, "host_parser.log"))
            if ok:
                try:
                    tests = json.load(open(host_json)).get("tests", [])
                    passed = {t.get("name", "") for t in tests if isinstance(t, dict) and t.get("status") == "PASSED"}
                except (OSError, ValueError, AttributeError) as e:
                    note = f"the parser's output could not be read ({e})"
            else:
                note = why
                if os.path.lexists(host_json):
                    os.unlink(host_json)
        else:
            bad = "; ".join(f"{n}: {why}" for n, (ok, why) in copies.items() if not ok)
            note = f"no test logs from the container ({bad}): the tests did not run, or did not finish"
        if args.timed_out:
            note = (note + "; " if note else "") + "the verifier timed out"

    resolved = bool(required) and required <= passed and not args.timed_out and not args.no_patch
    verdict = {"resolved": int(resolved), "required": len(required), "passed": len(passed),
               "passed_required": len(required & passed), "logs": logs, "note": note}
    json.dump(verdict, open(os.path.join(out, "verdict.json"), "w"), indent=1)
    with open(os.path.join(out, "reward.txt"), "w") as f:
        f.write(f"{int(resolved)}\n")
    lines = ["", "=== host verdict (evaluate_one.sh / verdict.py). Everything above comes from inside the grading",
             "=== container and decides nothing; the container never saw the lists of required tests.",
             f"Required tests: {len(required)}", f"Passed tests: {len(passed)}",
             f"Required tests that passed: {len(required & passed)}", "",
             "RESULT: PASSED" if resolved else "RESULT: FAILED"]
    if not resolved and required - passed and logs:
        lines.append(f"Missing tests: {sorted(required - passed)}")
    if note:
        lines.append(f"Note: {note}")
    if flags:
        lines.append("Patch flags (for staff review): " + "; ".join(flags))
    with open(vs, "a") as f:
        f.write("\n".join(lines) + "\n")
    print(int(resolved))


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sanitize")
    s.add_argument("src")
    s.add_argument("dst")
    d = sub.add_parser("decide")
    d.add_argument("--tests", required=True, help="the task's tests dir (config.json, parser.py)")
    d.add_argument("--out", required=True, help="the grading's output folder (never mounted in a container)")
    d.add_argument("--patch", required=True)
    d.add_argument("--container", help="what the grading container wrote (its /logs/verifier)")
    d.add_argument("--timed-out", action="store_true")
    d.add_argument("--no-patch", action="store_true")
    a = p.parse_args()
    if a.cmd == "sanitize":
        sanitize(a.src, a.dst)
    else:
        decide(a)


if __name__ == "__main__":
    main()
