#!/usr/bin/env python3
"""run_all.py — DISPATCHER runner for variant 2_parallel_sandbox (host side).

    export CS2680_API_KEY=...         # and the model ids (project_description.md, Part 1 Step 2)
    python3 evaluation_scripts/run_all.py [--limit N]

  CS2680_*        every CS2680_* variable is passed into the agent container as is, except
                  CS2680_BASE_URL: the agent gets http://a3proxy_<run>:3128/v1 (the egress proxy)
  A3_API_UPSTREAM the course API the egress proxy forwards to, default https://api.cs2680.com

  Session id: every course API request of a run carries <First>_<Last>_<UTC start time> (the egress
  proxy adds it; dispatcher/session.py), with your name from dispatcher/student_name.json: fill it
  in, or run this once in a terminal and answer. The id is printed and kept in run_logs/session_id.
  A3_SESSION_ID   the grader's id for a grading (sub_<n>_<UTC>[_c<k>]), used instead of the name

  --limit N       only the first N tasks can be opened (0 = preflight: start the agent, start
                  and health-check every task's sandbox, check egress, stop)
  A3_MAX_LIVE     tasks (sandboxes) live at once, default 5
  A3_MAX_GRADING  verifier containers running at once, default 3
  A3_EVAL_TIMEOUT seconds per grading, default 2100 (a hung test suite = all its tests failing)
  A3_RUN_LIMIT_S  seconds for the whole run, counted from the moment the first task is opened,
                  default 14400 (4 h); 0 = no limit (see "time limit" below)
  A3_DOCKER       docker command; default: `docker` if it works, else `sudo -n -E docker` (asks
                  for the sudo password once at the start and keeps sudo alive during the run)
  A3_CACHE        host-only cache dir, default <dir holding this harness>/.cache/cs2680_a3 — OUTSIDE
                  the harness, so the agent container (which mounts the harness) never sees it:
  A3_ASSETS         portable_python/, portable_python_musl/, wheels/, images/ (prepare_images.sh),
                    default $A3_CACHE/assets
  A3_HOSTLOG        host-only records (verdicts, task entries, patch snapshots), default $A3_CACHE/hostlogs
  SWEBENCH_PRO_OS   scaleapi/SWE-bench_Pro-os checkout (tests + solutions; prepare_images.sh fetches it),
                    default $A3_CACHE/SWE-bench_Pro-os

The AGENT drives the task flow (see madsOpt.py / dispatcher.py). This script starts the
infrastructure, then serves the agent's requests one at a time and enforces the rules:

  next_task   start sandbox k (next index), answer {problem_statement, requirements, interface,
              task=k, sandbox_url, workdir}; error when A3_MAX_LIVE are live; task=null at the end
  extract k   `git add -N . && git diff` of sandbox k -> .tasks/patches/<k>/patch.diff
  evaluate k  snapshot the submitted patch and grade it IN THE BACKGROUND on a pristine
              container (evaluate_one.sh); answer {pending: true} at once; when the grading ends,
              publish .tasks/seq/evalres_<k>_<a>.json = {task, attempt, tests_failed, tests_total}
              (that task's OWN hidden tests; never names)
  continue k  delete the submitted patch, keep the sandbox, attempt += 1 (refused while a
              grading of k is running)
  done k      final = last evaluation (an unevaluated submitted patch is graded first; no patch
              = failed); the answer is deferred until that grading ends; then the sandbox and
              the patch folder are removed
  time limit  A3_RUN_LIMIT_S after the first task was opened the run is stopped: `shutdown` is
              published (the agent's next dispatcher call raises DispatcherShutdown), every task
              still open is finalized as if done(k) had been called, tasks never opened count as
              failed; gradings already running still finish (grading is not the agent's time)

Verdicts live in this process's memory (Task.last) and in $A3_HOSTLOG, never in files the agent
container can reach; the repo-root copies (model_patch_<k>.diff, pro_eval/eval_results.json,
run_all_results.md) are written after the agent container is gone. So are the gradings' own
outputs (test names, test logs, expected values): evaluate_one.sh writes them to
$A3_HOSTLOG/.../pro_eval/<key>/ while the run lasts, and they are moved to pro_eval/<key>/ once the
agent container is gone, also when the run is stopped (move_gradings). Never handed to the agent:
instance_id, repo, base_commit, docker_image, sandbox_image.

Infrastructure: egress proxy a3proxy_<run> (the only container with internet; a reverse proxy that
forwards /v1/... to A3_API_UPSTREAM itself, no tunnels), agent container a3agent_<run> on two
--internal networks with CS2680_BASE_URL=http://a3proxy_<run>:3128/v1 (its only way to the API),
sandbox a3sbx_<run>_<k> per live task on the private network only, running src/sandbox_server.py
with a bind-mounted portable CPython. Both networks are created with no address of this host on
them (create_internal_network), so their containers cannot reach the host's own services either.
Gradings (evaluate_one.sh) run with no network.

Boundary with the agent container: it mounts this folder READ-ONLY at /madsOpt; only .tasks/ and
madsOpt_logs/ are writable mounts. The host never runs a file from that folder during or after the
run (evaluate_one.sh, make_predictions.py and the task list they read are copied to $A3_HOSTLOG
before the agent starts and run from there), touches .tasks/ only through directory fds without
following symlinks, and removes symlinks and special files from both writable folders afterwards.
"""

import argparse
import ast
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
TASKS_JSON = "evaluation_scripts/agent_task_input.json"
SEQ = ".tasks/seq"
PATCHES = ".tasks/patches"
CACHE = os.environ.get("A3_CACHE", os.path.join(os.path.dirname(ROOT), ".cache", "cs2680_a3"))
PRO_OS = os.environ.get("SWEBENCH_PRO_OS", os.path.join(CACHE, "SWE-bench_Pro-os"))
V2 = PRO_OS + "/v2/tasks"

ASSETS = os.environ.get("A3_ASSETS", os.path.join(CACHE, "assets"))
PORTABLE_PY = os.environ.get("A3_PORTABLE_PY", f"{ASSETS}/portable_python")
PORTABLE_PY_MUSL = os.environ.get("A3_PORTABLE_PY_MUSL", f"{ASSETS}/portable_python_musl")
AGENT_IMAGE = os.environ.get("A3_AGENT_IMAGE", "cs2680-a3-agent")
SBX_CPUS = os.environ.get("A3_SBX_CPUS", "1"); SBX_MEM = os.environ.get("A3_SBX_MEM", "4g")
SBX_PORT = 8000; PROXY_PORT = 3128
API_UPSTREAM = os.environ.get("A3_API_UPSTREAM", "https://api.cs2680.com")
MAX_LIVE = int(os.environ.get("A3_MAX_LIVE", "5"))
MAX_GRADING = int(os.environ.get("A3_MAX_GRADING", "3"))
EVAL_TIMEOUT = int(os.environ.get("A3_EVAL_TIMEOUT", "2100"))
RUN_LIMIT_S = int(os.environ.get("A3_RUN_LIMIT_S", str(4 * 3600)))   # whole run, from the first task opened

RUN = f"{int(time.time())}_{os.getpid()}"
NET, EGRESS, AGENT, PROXY = f"a3sbx_{RUN}", f"a3egress_{RUN}", f"a3agent_{RUN}", f"a3proxy_{RUN}"
API_BASE_URL = f"http://{PROXY}:{PROXY_PORT}/v1"   # the agent's CS2680_BASE_URL
HOSTLOG = os.path.join(os.environ.get("A3_HOSTLOG", os.path.join(CACHE, "hostlogs")), f"{os.path.basename(ROOT)}_{RUN}")
SCRIPTS = f"{HOSTLOG}/evaluation_scripts"   # host-only copies of the course scripts the host runs (snapshot_scripts)
GRADINGS = f"{HOSTLOG}/pro_eval"            # each task's latest grading while the run lasts (move_gradings)


# --- helpers -------------------------------------------------------------------------

def log(msg: str):
    line = f"== {msg}"
    try:
        print(line, file=sys.stderr, flush=True)
    except OSError:      # the terminal is gone (closed window = SIGHUP): keep going, the log file still gets it
        pass
    with open("run_logs/sequence.log", "a") as f:
        f.write(line + "\n")


DOCKER = ["docker"]   # set by resolve_docker(); every sh("docker", ...) goes through it
SESSION = None        # set by main(): this run's session id (dispatcher/session.py)


def resolve_docker():
    """Plain `docker` if this user may use it; else `sudo -n -E docker`: the password is asked
    once here, sudo is kept alive by a background thread (a later prompt would stall a run whose
    output is captured), and -E keeps the environment (`docker run -e CS2680_...` reads it).
    The choice is exported as A3_DOCKER for evaluate_one.sh."""
    global DOCKER
    if os.environ.get("A3_DOCKER"):
        DOCKER = os.environ["A3_DOCKER"].split()
    elif not shutil.which("docker"):
        sys.exit("error: docker not found on PATH (see Step 0 of the project description)")
    elif subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        DOCKER = ["docker"]
    elif shutil.which("sudo"):
        log("docker is not usable without root here: using sudo (asks for your password once)")
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            sys.exit("error: sudo failed; add yourself to the docker group instead: sudo usermod -aG docker $USER")
        if subprocess.run(["sudo", "-n", "-E", "docker", "info"], capture_output=True).returncode != 0:
            sys.exit("error: `sudo -E docker info` failed; add yourself to the docker group: sudo usermod -aG docker $USER")
        DOCKER = ["sudo", "-n", "-E", "docker"]
        def keep_sudo_alive():
            while True:
                time.sleep(60)
                subprocess.run(["sudo", "-n", "-v"], capture_output=True)
        threading.Thread(target=keep_sudo_alive, daemon=True).start()
    else:
        sys.exit("error: docker is not usable by this user and there is no sudo: sudo usermod -aG docker $USER")
    os.environ["A3_DOCKER"] = " ".join(DOCKER)


def sh(*cmd, check=True, capture=True, **kw) -> subprocess.CompletedProcess:
    if cmd and cmd[0] == "docker":
        cmd = (*DOCKER, *cmd[1:])
    return subprocess.run(list(cmd), check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, **kw)


def running(name: str) -> bool:
    r = sh("docker", "inspect", "-f", "{{.State.Running}}", name, check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


def have_image(img: str) -> bool:
    return sh("docker", "image", "inspect", img, check=False).returncode == 0


# --- the agent-writable folders ----------------------------------------------------------
# .tasks/ and madsOpt_logs/ are the only host folders the (root) agent container can write. Any
# entry in them may be a symlink to a host path or a device node, so the host never opens a path
# there: it works relative to the fds of .tasks/seq and .tasks/patches (opened before the agent
# starts), with O_NOFOLLOW, and reads plain files only. madsOpt_logs/ is not read during the run.

SEQ_FD = PATCHES_FD = -1


def open_task_dirs():
    global SEQ_FD, PATCHES_FD
    SEQ_FD = os.open(SEQ, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    PATCHES_FD = os.open(PATCHES, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def exists_at(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        return True
    except OSError:
        return False


def open_plain(dir_fd: int, name: str):
    """`name` in dir_fd opened for binary reading; None if missing, a symlink or not a plain file."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def write_at(dir_fd: int, name: str, data: bytes):
    """Atomically replace `name` in dir_fd (a symlink there is replaced, never followed)."""
    tmp = f".{name}.{os.urandom(6).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def seq_json(name: str, obj):
    write_at(SEQ_FD, name, json.dumps(obj).encode())


def patch_dir(k: int, create: bool = False) -> int:
    """fd of .tasks/patches/<k>; OSError if it is missing or not a real directory. Caller closes it."""
    if create:
        try:
            os.mkdir(str(k), dir_fd=PATCHES_FD)
        except FileExistsError:
            pass
    return os.open(str(k), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=PATCHES_FD)


def open_patch(k: int):
    """.tasks/patches/<k>/patch.diff opened for binary reading (see open_plain), or None."""
    try:
        d = patch_dir(k)
    except OSError:
        return None
    try:
        return open_plain(d, "patch.diff")
    finally:
        os.close(d)


def patch_size(k: int) -> int:
    f = open_patch(k)
    if f is None:
        return 0
    with f:
        return os.fstat(f.fileno()).st_size


def remove_patch(k: int):
    try:
        d = patch_dir(k)
    except OSError:
        return
    try:
        os.unlink("patch.diff", dir_fd=d)
    except FileNotFoundError:
        pass
    finally:
        os.close(d)


def remove_patch_dir(k: int):
    """rm -rf .tasks/patches/<k> without following symlinks (best effort)."""
    try:
        for _, dirs, files, d in os.fwalk(str(k), topdown=False, dir_fd=PATCHES_FD):
            for name in dirs + files:
                try:
                    os.rmdir(name, dir_fd=d)
                except OSError:
                    os.unlink(name, dir_fd=d)
        os.rmdir(str(k), dir_fd=PATCHES_FD)
    except OSError:
        pass


def drop_links():
    """After the agent container is gone: remove every symlink and special file (fifo, socket,
    device node) it left in its writable folders, so whatever reads or uploads them later only
    finds plain files."""
    for top in (".tasks", "madsOpt_logs"):
        for root, dirs, files in os.walk(top):
            for name in dirs + files:
                p = os.path.join(root, name)
                try:
                    m = os.lstat(p).st_mode
                    if not (stat.S_ISREG(m) or stat.S_ISDIR(m)):
                        os.unlink(p)
                except OSError:
                    pass


def move_gradings():
    """After the agent container is gone, also when the run was stopped: each task's latest grading
    (evaluate_one.sh's output: test names, test logs, expected values) from GRADINGS, which the
    agent container never mounts, to pro_eval/<key>/ in this folder."""
    if not os.path.isdir(GRADINGS):
        return
    os.makedirs("pro_eval", exist_ok=True)
    for key in sorted(os.listdir(GRADINGS)):
        src, dst = os.path.join(GRADINGS, key), os.path.join("pro_eval", key)
        try:
            if os.path.islink(src) or not os.path.isdir(src):
                continue
            if os.path.lexists(dst):
                log(f"pro_eval/{key} already exists; that task's grading stays in {src}")
                continue
            shutil.move(src, dst)
        except OSError as e:
            log(f"grading of task {key} not moved to pro_eval/ ({e}); it stays in {src}")
    try:
        os.rmdir(GRADINGS)
    except OSError:
        pass


def snapshot_scripts():
    """Copy the course scripts the host runs (evaluate_one.sh during the run, make_predictions.py
    after it) and the task list they read to the host-only $A3_HOSTLOG before the agent starts;
    they run from there, never from the folder the agent container mounts."""
    os.makedirs(SCRIPTS, exist_ok=True)
    for f in ("evaluate_one.sh", "make_predictions.py", "agent_task_input.json"):
        shutil.copy(f"evaluation_scripts/{f}", f"{SCRIPTS}/{f}")


def sbx(k: int) -> str:
    return f"a3sbx_{RUN}_{k}"


@dataclass
class Task:
    k: int
    key: str
    entry: dict
    live: bool = False
    wd: str = "/app"
    attempt: int = 0
    evaluated: bool = False          # the current submitted patch has a (finished) grading
    last: Optional[tuple] = None     # (resolved:int, tests_failed:int, tests_total:int)
    t_start: float = 0.0
    grading: Optional[subprocess.Popen] = None
    grading_attempt: int = 0
    grading_t0: float = 0.0
    queued: bool = False             # waiting for a grading slot
    finish_after_grading: Optional[int] = None   # request n whose `done` answer is deferred

    @property
    def iid(self): return self.entry["instance_id"]
    @property
    def sandbox_image(self): return self.entry.get("sandbox_image") or self.entry["docker_image"]
    @property
    def grade_image(self): return self.entry["docker_image"]


# --- setup ----------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=-1)
    return p.parse_args()


def preconditions():
    if not os.environ.get("CS2680_API_KEY"):
        sys.exit("error: export CS2680_API_KEY first")
    for d in (PORTABLE_PY, PORTABLE_PY_MUSL):
        if not os.access(f"{d}/bin/python3", os.X_OK):
            sys.exit(f"error: no portable python at {d} (set A3_ASSETS / A3_PORTABLE_PY / A3_PORTABLE_PY_MUSL)")
    if os.path.realpath(HOSTLOG).startswith(os.path.realpath(ROOT) + os.sep):
        sys.exit("error: A3_HOSTLOG must be outside this folder (the agent container mounts it)")
    old = [p for p in os.listdir(".") if p.startswith("model_patch_") or p in ("madsOpt_logs", "predictions.json", "pro_eval", "run_all_results.md")]
    old = [p for p in old if not (p == "madsOpt_logs" and os.path.isdir(p) and not os.listdir(p))]   # every run makes one
    if old:
        sys.exit(f"error: outputs of a previous run are still here ({', '.join(old[:3])}...); move them first")


def load_tasks(limit: int):
    data = json.load(open(TASKS_JSON))
    tasks = [Task(k, key, entry) for k, (key, entry) in enumerate(data.items())]
    os.makedirs(f"{HOSTLOG}/tasks", exist_ok=True)
    for t in tasks:
        json.dump(t.entry, open(f"{HOSTLOG}/tasks/{t.k}.json", "w"))
    n = len(tasks) if limit < 0 else min(limit, len(tasks))
    return tasks, n


def ensure_image(img: str, tarbase: str):
    if have_image(img):
        return
    tar = f"{ASSETS}/images/{tarbase}.tar"
    if os.path.isfile(tar):
        log(f"loading {img} from {tar}")
        if sh("docker", "load", "-q", "-i", tar, check=False).returncode == 0 and have_image(img):
            return
    log(f"pulling {img}")
    if sh("docker", "pull", "-q", img, check=False).returncode != 0:
        sys.exit(f"cannot get {img} — run evaluation_scripts/prepare_images.sh (with internet) or set A3_ASSETS to a dir with images/")


def ensure_images(tasks):
    for t in tasks:
        ensure_image(t.grade_image, t.iid)
        if t.sandbox_image != t.grade_image:
            ensure_image(t.sandbox_image, f"{t.iid}.sandbox")
    if not have_image(AGENT_IMAGE):
        tar = f"{ASSETS}/images/{AGENT_IMAGE}.tar"
        if os.path.isfile(tar):
            log(f"loading {AGENT_IMAGE} from {tar}"); sh("docker", "load", "-q", "-i", tar)
        else:
            if not os.path.isdir(f"{ASSETS}/wheels"):
                sys.exit(f"no {ASSETS}/wheels to build {AGENT_IMAGE} (pip download openai -d wheels)")
            log(f"building {AGENT_IMAGE} from dispatcher/agent.Dockerfile (context {ASSETS})")
            sh("docker", "build", "-q", "-f", "dispatcher/agent.Dockerfile", "-t", AGENT_IMAGE, ASSETS)


def create_internal_network(name: str):
    """An --internal network with no address of this host on it (gateway mode "isolated", Docker 28+):
    its containers reach each other, but not this host's own services (sshd, rpcbind, ...) through a
    gateway address. The host itself only ever reaches them with `docker exec`."""
    if sh("docker", "network", "create", "--internal", "-o", "com.docker.network.bridge.gateway_mode_ipv4=isolated",
          name, check=False).returncode != 0:
        log(f"warning: this Docker cannot isolate {name} from the host (needs Docker 28+); "
            f"its containers can reach this host's own services")
        sh("docker", "network", "create", "--internal", name)


def start_infra():
    create_internal_network(NET)
    create_internal_network(EGRESS)
    log(f"agent {AGENT} ({AGENT_IMAGE}); networks {NET}, {EGRESS}; API {API_BASE_URL} -> {API_UPSTREAM}; "
        f"max live {MAX_LIVE}, max grading {MAX_GRADING}, eval timeout {EVAL_TIMEOUT}s")
    sh("docker", "run", "-d", "--name", PROXY, "--network", "bridge", "-v", f"{ROOT}/dispatcher:/dispatcher:ro",
       AGENT_IMAGE, "python3", "-B", "/dispatcher/egress_proxy.py", "--port", str(PROXY_PORT), "--upstream", API_UPSTREAM,
       "--session", SESSION)
    sh("docker", "network", "connect", EGRESS, PROXY)
    host_id = f"{os.getuid()}:{os.getgid()}"
    script = (
        'trap "chown -R $HOST_ID /madsOpt/.tasks /madsOpt/madsOpt_logs 2>/dev/null" EXIT\n'
        'touch /madsOpt/.tasks/seq/ready\n'
        'cd /madsOpt/.tasks/trace\n'
        'python3 /madsOpt/madsOpt.py --log\n'
        'mkdir -p /madsOpt/madsOpt_logs && cp -r madsOpt_logs/. /madsOpt/madsOpt_logs/ 2>/dev/null || true\n')
    env = [*[x for v in sorted(os.environ) if v.startswith("CS2680_") and v != "CS2680_BASE_URL"
             for x in ("-e", v)],                                                            # key + model ids
           "-e", "MADSOPT_MAX_ITERATIONS",
           "-e", f"CS2680_BASE_URL={API_BASE_URL}",     # the egress proxy: the agent's only way to the API
           "-e", f"HOST_ID={host_id}", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    hidden = f"{HOSTLOG}/hidden.json"     # the task lists (instance ids, hidden test names) read as {} in the agent
    with open(hidden, "w") as f:
        f.write("{}\n")
    mounts = ["-v", f"{ROOT}:/madsOpt:ro",                      # read-only: the agent cannot touch the course files
              "-v", f"{ROOT}/.tasks:/madsOpt/.tasks", "-v", f"{ROOT}/madsOpt_logs:/madsOpt/madsOpt_logs",
              *[x for f in ("agent_task_input.json", "task_test.json")
                for x in ("-v", f"{hidden}:/madsOpt/evaluation_scripts/{f}:ro")]]
    sh("docker", "run", "-d", "--name", AGENT, "--network", EGRESS, *mounts, *env, AGENT_IMAGE, "bash", "-c", script)
    sh("docker", "network", "connect", NET, AGENT)
    while not exists_at(SEQ_FD, "ready"):
        if not running(AGENT):
            sys.exit("agent container died during setup (run_logs/agent.log)")
        time.sleep(1)


def cleanup():
    sh("docker", "exec", AGENT, "chown", "-R", f"{os.getuid()}:{os.getgid()}", "/madsOpt/.tasks", "/madsOpt/madsOpt_logs", check=False)
    for name, out in ((AGENT, "agent.log"), (PROXY, "proxy.log")):
        r = sh("docker", "logs", name, check=False)
        open(f"run_logs/{out}", "w").write((r.stdout or "") + (r.stderr or ""))
    ids = sh("docker", "ps", "-aq", "--filter", f"name=^a3sbx_{RUN}_", check=False).stdout.split()
    for c in ids + [AGENT, PROXY]:
        sh("docker", "rm", "-f", c, check=False)
    for net in (NET, EGRESS):
        sh("docker", "network", "rm", net, check=False)
    drop_links()


# --- sandboxes --------------------------------------------------------------------------

def start_sandbox(t: Task) -> str:
    """Start sandbox k; returns the repo path (workdir) or '' on failure."""
    inner = ('WD=/testbed; [ -d /testbed/.git ] || WD=/app\n'
             'PY=/opt/agentpy/bin/python3; [ -e /lib/ld-musl-x86_64.so.1 ] && PY=/opt/agentpy_musl/bin/python3\n'
             f'exec "$PY" -B /sandbox/sandbox_server.py --root "$WD" --port {SBX_PORT}')
    sh("docker", "run", "-d", "--name", sbx(t.k), "--network", NET, "--cpus", SBX_CPUS, "--memory", SBX_MEM,
       "-e", "GOPROXY=off", "-e", "GOSUMDB=off", "-e", "GOTOOLCHAIN=local",
       "-v", f"{ROOT}/src/sandbox_server.py:/sandbox/sandbox_server.py:ro", "-v", f"{PORTABLE_PY}:/opt/agentpy:ro", "-v", f"{PORTABLE_PY_MUSL}:/opt/agentpy_musl:ro",
       "--entrypoint", "bash", t.sandbox_image, "-c", inner)
    probe = f"import json,urllib.request as u;print(json.load(u.urlopen('http://{sbx(t.k)}:{SBX_PORT}/health',timeout=5))['root'])"
    for _ in range(30):   # health from INSIDE the agent container: the exact path the tools use
        r = sh("docker", "exec", AGENT, "python3", "-c", probe, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        if not running(sbx(t.k)):
            break
        time.sleep(2)
    return ""


def stop_sandbox(t: Task):
    r = sh("docker", "logs", sbx(t.k), check=False)
    open(f"run_logs/sandbox_{t.k}.log", "w").write((r.stdout or "") + (r.stderr or ""))
    sh("docker", "rm", "-f", sbx(t.k), check=False)


def extract_patch(t: Task) -> int:
    git = f"git -C '{t.wd}' -c safe.directory='*'"
    r = sh("docker", "exec", sbx(t.k), "sh", "-c", f"{git} add -N . && {git} diff", check=False)
    patch = r.stdout if r.returncode == 0 else ""
    d = patch_dir(t.k, create=True)
    try:
        write_at(d, "patch.diff", patch.encode())
    finally:
        os.close(d)
    return len(patch.encode())


# --- grading (background) -----------------------------------------------------------------

def total_tests(t: Task) -> int:
    c = json.load(open(f"{V2}/{t.iid}/tests/config.json"))
    n = 0
    for f in ("fail_to_pass", "pass_to_pass"):
        v = c.get(f, "[]"); v = ast.literal_eval(v) if isinstance(v, str) else v; n += len(v)
    return n


def start_grading(t: Task, n_done: Optional[int] = None):
    """Snapshot the submitted patch and grade it in the background (evaluate_one.sh)."""
    os.makedirs(f"{HOSTLOG}/patches", exist_ok=True)
    snap = f"{HOSTLOG}/patches/{t.k}_a{t.attempt}.diff"
    src = open_patch(t.k)
    with open(snap, "wb") as out:
        if src is not None:
            with src:
                shutil.copyfileobj(src, out)
    outdir = f"{GRADINGS}/{t.key}"     # not under ROOT: the agent container mounts it, and reads it live
    env = dict(os.environ, A3_EVAL_TIMEOUT=str(EVAL_TIMEOUT), SWEBENCH_PRO_OS=PRO_OS)   # the copy cannot find it relative to itself
    t.grading = subprocess.Popen(["bash", f"{SCRIPTS}/evaluate_one.sh", t.key, snap, outdir],
                                 stdout=subprocess.PIPE, stderr=open("run_logs/sequence.log", "a"), text=True, env=env)
    t.grading_attempt = t.attempt; t.grading_t0 = time.time(); t.queued = False
    if n_done is not None:
        t.finish_after_grading = n_done
    log(f"task {t.k} attempt {t.attempt}: grading started ({os.path.getsize(snap)} bytes)")


def finish_grading(t: Task):
    """Called when t.grading has exited: record the verdict and publish it to the agent."""
    out = (t.grading.stdout.read() or "").strip()
    res = 1 if out.endswith("1") else 0
    vs = f"{GRADINGS}/{t.key}/verifier_stdout.txt"
    text = open(vs, errors="replace").read() if os.path.exists(vs) else ""
    T = re.findall(r"Required tests: (\d+)", text); M = re.findall(r"Required tests that passed: (\d+)", text)
    if T:
        total, passed = int(T[-1]), int(M[-1]) if M else 0
    else:                       # verifier never ran the tests (apply/build failed, empty patch, timeout)
        total, passed = total_tests(t), 0
    if res == 1:
        passed = total
    failed = total - passed
    t.last = (res, failed, total); t.evaluated = True
    secs = int(time.time() - t.grading_t0)
    with open(f"{HOSTLOG}/evaluations.tsv", "a") as f:
        f.write(f"{t.k}\t{t.iid}\t{t.grading_attempt}\t{res}\t{failed}\t{total}\t{secs}\n")
    timed_out = os.path.exists(f"{GRADINGS}/{t.key}/TIMEOUT")
    log(f"task {t.k} attempt {t.grading_attempt}: {failed}/{total} tests failing, "
        f"{'RESOLVED' if res else 'unresolved'} ({secs}s{', TIMEOUT' if timed_out else ''})")
    seq_json(f"evalres_{t.k}_{t.grading_attempt}.json",
             {"task": t.k, "attempt": t.grading_attempt, "tests_failed": failed, "tests_total": total})
    t.grading = None


# --- the request loop ---------------------------------------------------------------------

class Server:
    def __init__(self, tasks, n_available):
        self.tasks = tasks; self.n_available = n_available
        self.next_k = 0; self.n = 0
        self.grade_queue = []          # tasks waiting for a grading slot
        self.t_first = None            # when the first task was opened: the whole-run clock starts here
        self.limit_note = ""           # set when A3_RUN_LIMIT_S stopped the run (goes into run_all_results.md)
        open(f"{HOSTLOG}/evaluations.tsv", "w").close()

    # -- state helpers
    def live(self): return [t for t in self.tasks if t.live]
    def grading_now(self): return sum(1 for t in self.tasks if t.grading is not None)

    def respond(self, n: int, payload: dict):
        payload["n"] = n
        seq_json(f"resp_{n}.json", payload)

    def finish_task(self, t: Task):
        """Final verdict already in t.last (or none -> grade empty). Remove sandbox + folder."""
        if t.last is None:
            t.evaluated = False
            start_grading(t)                          # empty patch -> evaluate_one answers 0 at once
            t.grading.wait(); finish_grading(t)
        stop_sandbox(t)
        remove_patch_dir(t.k)
        t.live = False
        with open("run_logs/task_times.tsv", "a") as f:
            f.write(f"{t.k}\t{t.iid}\t{t.t_start}\t{time.time()}\n")
        log(f"task {t.k}: DONE, final {t.last[1]}/{t.last[2]} tests failing; live={len(self.live())}")

    # -- the whole-run time limit
    def over_time(self) -> bool:
        """True once A3_RUN_LIMIT_S has passed since the first task was opened; then publishes
        `shutdown` so the agent stops (its next dispatcher call raises DispatcherShutdown)."""
        if RUN_LIMIT_S <= 0 or self.t_first is None or time.time() - self.t_first < RUN_LIMIT_S:
            return False
        open_now = [t.k for t in self.live()]
        unopened = [t.k for t in self.tasks[:self.n_available] if t.t_start == 0.0]
        self.limit_note = (f"Time limit reached: the run was stopped {RUN_LIMIT_S / 3600:g} h after the first task "
                           f"was opened. Tasks still open ({', '.join(map(str, open_now)) or 'none'}) were finalized as "
                           f"if done() had been called; tasks never opened ({', '.join(map(str, unopened)) or 'none'}) "
                           f"count as failed.")
        log(self.limit_note)
        write_at(SEQ_FD, "shutdown", b"")
        return True

    # -- background gradings
    def pump_gradings(self):
        for t in self.tasks:
            if t.grading is not None and t.grading.poll() is not None:
                finish_grading(t)
                if t.finish_after_grading is not None:       # a deferred done(k)
                    n = t.finish_after_grading; t.finish_after_grading = None
                    self.finish_task(t)
                    self.respond(n, {"ok": True, "tests_failed": t.last[1], "tests_total": t.last[2]})
        while self.grade_queue and self.grading_now() < MAX_GRADING:
            start_grading(self.grade_queue.pop(0))

    # -- requests
    def handle(self, n: int, req: dict):
        op = req.get("op"); k = req.get("k")
        t = self.tasks[int(k)] if isinstance(k, int) and 0 <= int(k) < len(self.tasks) else None
        if op == "next_task":
            if len(self.live()) >= MAX_LIVE:
                return self.respond(n, {"ok": False, "error": f"too many live tasks ({len(self.live())}): call done(k) on one first"})
            if self.next_k >= self.n_available:
                return self.respond(n, {"ok": True, "task": None})
            t = self.tasks[self.next_k]; self.next_k += 1
            log(f"task {t.k}: {t.key} — starting sandbox from {t.sandbox_image}")
            wd = start_sandbox(t)
            if not wd:
                log(f"task {t.k}: sandbox failed to start; handed over without one")
            t.live = True; t.wd = wd or "/app"; t.t_start = time.time(); t.attempt = 0; t.evaluated = False
            if self.t_first is None:
                self.t_first = t.t_start
                if RUN_LIMIT_S > 0:
                    log(f"time limit: the whole run must end within {RUN_LIMIT_S / 3600:g} h, by "
                        f"{time.strftime('%H:%M:%S UTC', time.gmtime(self.t_first + RUN_LIMIT_S))}")
            os.close(patch_dir(t.k, create=True))
            handed = {f: t.entry[f] for f in ("problem_statement", "requirements", "interface") if f in t.entry}
            handed.update(task=t.k, sandbox_url=f"http://{sbx(t.k)}:{SBX_PORT}" if wd else "", workdir=t.wd)
            return self.respond(n, {"ok": True, "task": handed})
        if t is None or not t.live:
            return self.respond(n, {"ok": False, "error": f"task {k} is not live"})
        if op == "extract":
            if not running(sbx(t.k)):
                return self.respond(n, {"ok": False, "error": f"sandbox of task {t.k} is not running"})
            return self.respond(n, {"ok": True, "bytes": extract_patch(t)})
        if op == "evaluate":
            if patch_size(t.k) == 0:
                return self.respond(n, {"ok": False, "error": f"no patch at .tasks/patches/{t.k}/patch.diff (call extract_patch or submit_patch first)"})
            if t.grading is not None or t.queued:
                return self.respond(n, {"ok": False, "error": f"an evaluation of task {t.k} is already running"})
            if self.grading_now() < MAX_GRADING:
                start_grading(t)
            else:
                t.queued = True; self.grade_queue.append(t); log(f"task {t.k}: grading queued ({self.grading_now()} running)")
            return self.respond(n, {"ok": True, "pending": True, "attempt": t.attempt})
        if op == "continue":
            if t.grading is not None or t.queued:
                return self.respond(n, {"ok": False, "error": f"an evaluation of task {t.k} is still running; wait for evaluate() to return"})
            remove_patch(t.k)
            t.attempt += 1; t.evaluated = False
            log(f"task {t.k}: agent continues (attempt {t.attempt})")
            return self.respond(n, {"ok": True, "attempt": t.attempt})
        if op == "done":
            if t.grading is not None or t.queued:
                return self.respond(n, {"ok": False, "error": f"an evaluation of task {t.k} is still running; wait for evaluate() to return"})
            if patch_size(t.k) > 0 and not t.evaluated:
                start_grading(t, n_done=n)             # answer deferred to pump_gradings()
                return
            self.finish_task(t)
            return self.respond(n, {"ok": True, "tests_failed": t.last[1], "tests_total": t.last[2]})
        return self.respond(n, {"ok": False, "error": f"unknown op '{op}'"})

    def serve(self):
        log(f"{self.n_available} task(s) available, at most {MAX_LIVE} live at once; waiting for the agent's requests")
        while True:
            self.pump_gradings()
            if self.over_time():
                break
            if exists_at(SEQ_FD, "end"):
                break
            if not running(AGENT):
                log("agent container died"); break
            f = open_plain(SEQ_FD, f"req_{self.n}.json")
            if f is None:
                time.sleep(0.5); continue
            try:
                with f:
                    req = json.loads(f.read(1 << 20))
                self.handle(self.n, req)
            except Exception as e:   # never let one bad request kill the run
                log(f"request {self.n} failed: {type(e).__name__}: {e}")
                self.respond(self.n, {"ok": False, "error": f"dispatcher error: {type(e).__name__}: {e}"})
            self.n += 1
        # wind down: let pending gradings finish, finalize what the agent left open
        while any(t.grading is not None for t in self.tasks) or self.grade_queue:
            self.pump_gradings(); time.sleep(1)
        for t in self.live():
            log(f"task {t.k}: still live at the end — finalizing")
            if patch_size(t.k) > 0 and not t.evaluated:
                start_grading(t); t.grading.wait(); finish_grading(t)
            self.finish_task(t)


# --- preflight ------------------------------------------------------------------------------

def preflight(tasks) -> int:
    bad = 0
    host = urllib.parse.urlsplit(API_UPSTREAM).hostname
    probe = f'''
import os, socket
base = os.environ.get("CS2680_BASE_URL", ""); proxy = ("{PROXY}", {PROXY_PORT}); host = "{host}"; ok = True
def status(req):   # the status code the proxy answers a raw request with
    s = socket.create_connection(proxy, timeout=20)
    try:
        s.sendall(req.encode()); line = s.recv(4096).split(b"\\r\\n", 1)[0].split()
        return int(line[1]) if len(line) > 1 and line[1].isdigit() else None
    finally: s.close()
try:
    import openai
    try: openai.OpenAI(base_url=base, api_key='preflight', max_retries=0).models.list(); print(f'egress: OpenAI SDK via {{base}} -> 200 (reachable)')
    except openai.APIStatusError as e: print(f'egress: OpenAI SDK via {{base}} -> HTTP {{e.status_code}} (reachable)')
    except openai.APIConnectionError as e: print(f'egress: OpenAI SDK via {{base}} FAILED: {{e}}'); ok = False
except ImportError: print('egress: openai not importable'); ok = False
for what, req, want in (("CONNECT github.com:443", "CONNECT github.com:443 HTTP/1.1\\r\\nHost: github.com:443\\r\\n\\r\\n", 405),
                        ("CONNECT {host}:443", "CONNECT {host}:443 HTTP/1.1\\r\\nHost: {host}:443\\r\\n\\r\\n", 405),
                        ("GET http://github.com/", "GET http://github.com/ HTTP/1.1\\r\\nHost: github.com\\r\\n\\r\\n", 403),
                        ("GET /admin", "GET /admin HTTP/1.1\\r\\nHost: {PROXY}\\r\\n\\r\\n", 403)):
    try: c = status(req)
    except OSError as e: c = type(e).__name__
    print(f'egress: {{what}} via proxy -> {{c}}', '(refused, good)' if c == want else 'NOT REFUSED'); ok &= c == want
for addr in ((host, 443), ('1.1.1.1', 443)):
    try: socket.create_connection(addr, timeout=5); print(f'egress: direct {{addr[0]}}:443 CONNECTED - agent is NOT isolated'); ok = False
    except OSError as e: print(f'egress: direct {{addr[0]}}:443 -> {{type(e).__name__}} (no route, good)')
print('EGRESS OK' if ok else 'EGRESS CHECK FAILED')
'''
    r = sh("docker", "exec", "-i", AGENT, "python3", "-", input=probe, check=False)
    open("run_logs/egress_check.log", "w").write(r.stdout + r.stderr)
    for line in r.stdout.splitlines():
        log(line)
    if "EGRESS OK" not in r.stdout:
        bad = 1
    for t in tasks:
        wd = start_sandbox(t)
        log(f"preflight {t.k}: {t.key} {'ok (repo at ' + wd + ')' if wd else 'FAILED (run_logs/sandbox_%d.log)' % t.k}")
        bad |= 0 if wd else 1
        if t.k == 0 and wd:
            chk = (f"timeout 5 bash -c 'echo > /dev/tcp/{PROXY}/{PROXY_PORT}' 2>/dev/null && echo PROXY_REACHABLE; "
                   "timeout 5 bash -c 'echo > /dev/tcp/1.1.1.1/443' 2>/dev/null && echo INTERNET_REACHABLE; echo checked")
            out = sh("docker", "exec", sbx(t.k), "bash", "-c", chk, check=False).stdout
            if "REACHABLE" in out:
                log(f"preflight: sandbox isolation FAILED: {out.strip()}"); bad = 1
            else:
                log("preflight: sandbox sees neither the proxy nor the internet (good)")
        stop_sandbox(t)
    return bad


# --- results ----------------------------------------------------------------------------------

def write_results(tasks, note: str = ""):
    """From this process's memory only, after the agent container is gone."""
    shutil.copy(f"{HOSTLOG}/evaluations.tsv", "run_logs/evaluations.tsv")
    os.makedirs("pro_eval", exist_ok=True)
    res = {t.key: bool(t.last and t.last[0] == 1) for t in tasks}
    json.dump(res, open("pro_eval/eval_results.json", "w"), indent=2)
    for t in tasks:
        snap = f"{HOSTLOG}/patches/{t.k}_a{t.grading_attempt}.diff"
        if t.last is not None and os.path.exists(snap):
            shutil.copy(snap, f"model_patch_{t.key}.diff")
    sh("python3", f"{SCRIPTS}/make_predictions.py", check=False)
    passed = sum(res.values())
    rows = [f"| {t.key} | {'PASS' if res[t.key] else 'FAIL'} | {t.last[1] if t.last else '-'}/{t.last[2] if t.last else '-'} |" for t in tasks]
    open("run_all_results.md", "w").write("\n".join(
        ["# Evaluation results", "", f"{passed}/{len(tasks)} passed", "", *([note, ""] if note else []),
         "| task | result | tests failing |", "| --- | --- | --- |", *rows]) + "\n")
    log(f"wrote run_all_results.md ({passed}/{len(tasks)} passed); host-only records in {HOSTLOG}")


def _stop_signal(signum, frame):
    raise KeyboardInterrupt   # SIGTERM / SIGHUP: handled exactly like Ctrl-C (see main)


def session_id() -> str:
    """dispatcher/session.py's id for this run, run from its source before anything is started (the
    agent container, which could only read that folder anyway, does not exist yet; no __pycache__)."""
    path = os.path.join(ROOT, "dispatcher", "session.py")
    ns = {"__name__": "a3_session", "__file__": path}
    exec(compile(open(path).read(), path, "exec"), ns)
    return ns["session_id"]()


def main() -> int:
    global SESSION
    args = parse_args()
    SESSION = session_id()                         # first: without a name, nothing is started
    preconditions()
    shutil.rmtree(".tasks", ignore_errors=True); shutil.rmtree("run_logs", ignore_errors=True)
    for d in (SEQ, ".tasks/trace", PATCHES, "run_logs", "madsOpt_logs"):
        os.makedirs(d, exist_ok=True)              # host-owned before the (root) agent container starts
    with open("run_logs/session_id", "w") as f:
        f.write(SESSION + "\n")
    log(f"session {SESSION} (sent with every course API request of this run)")
    resolve_docker()                               # after run_logs/ exists: its sudo fallback logs there
    os.makedirs(HOSTLOG, exist_ok=True)
    snapshot_scripts()
    open_task_dirs()
    tasks, n_available = load_tasks(args.limit)
    ensure_images(tasks)
    rc = 0
    # Stopping the run must remove what it started (agent, proxy, task containers, networks):
    # SIGTERM (kill, an IDE's stop button) and SIGHUP (closing the terminal) take the same path
    # as Ctrl-C, and once the cleanup has begun further signals are ignored, so a second Ctrl-C
    # cannot cut it short and leave an agent running (and calling the model) with nobody watching.
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, _stop_signal)
    interrupted = False
    server = None
    try:
        start_infra()
        if args.limit == 0:
            rc = preflight(tasks)
            write_at(SEQ_FD, "shutdown", b"")
            while running(AGENT):
                time.sleep(1)
            return rc
        server = Server(tasks, n_available)
        server.serve()
        write_at(SEQ_FD, "shutdown", b"")
        for _ in range(60):
            if not running(AGENT):
                break
            time.sleep(1)
    except KeyboardInterrupt:
        interrupted = True
        log("interrupted: stopping the agent and removing its containers and networks (please wait)")
    finally:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        cleanup()
        move_gradings()     # only now: the agent container, which mounts this folder, is gone
    if interrupted:
        log("stopped: containers and networks removed; no results written")
        return 130
    if n_available > 0:
        write_results(tasks, server.limit_note if server else "")
    return rc


if __name__ == "__main__":
    sys.exit(main())
