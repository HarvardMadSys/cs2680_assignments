#!/usr/bin/env python3
"""sandbox_server.py — the agent's only door into a task container.

Yours to change, like the rest of src/. evaluation_scripts/run_all.py mounts this file
(src/sandbox_server.py) read-only into every task container and starts it as the container's main
process:

    python3 sandbox_server.py --root /app --port 8000

run_all.py waits for GET /health to answer JSON with "root" (the repository path) before it
hands the task to your agent; keep that working.

The agent (running in a separate container on the same private docker
network) drives the repository through four JSON-over-HTTP calls; nothing else
in the harness can reach the checkout:

    GET  /health                      -> {"ok": true, "root": "/app"}
    POST /exec   {command, timeout_s, cwd?}
                                      -> {"output", "exit_code", "timed_out"}
         sh -c <command> in cwd (default root), stdout+stderr merged, no stdin,
         whole process group killed at timeout_s (capped at MAX_TIMEOUT_S).
    POST /read   {path, cwd?}         -> {"content"}          | 400 {"error"}
    POST /write  {path, content, cwd?}-> {"existed": bool}    | 400 {"error"}
         Relative paths resolve against cwd (default root). File operations
         are confined to the repository root and to /work (e.g. for git
         worktrees); anything else is refused with a readable message.

Every request is served on its own thread, so several threads may use one
sandbox concurrently. Requires only the stdlib.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_TIMEOUT_S = 900          # longest timeout_s /exec accepts
DEFAULT_TIMEOUT_S = 300
MAX_OUTPUT_BYTES = 1_000_000  # head+tail kept; the agent truncates further
WORK_ROOT = "/work"           # extra allowed area besides the repository (e.g. git worktrees)

ROOT = "/app"                 # set from --root


class PathError(Exception):
    """Model-visible refusal (bad path, missing file, directory)."""


def _log(msg: str):
    print(f"[sandbox] {msg}", file=sys.stderr, flush=True)


def resolve(path: str, cwd: str) -> str:
    """Resolve a model-supplied path relative to cwd; confine it to ROOT or
    WORK_ROOT."""
    if not path:
        raise PathError("empty path")
    candidate = path if os.path.isabs(path) else os.path.join(cwd or ROOT, path)
    real = os.path.realpath(candidate)
    for allowed in (ROOT, WORK_ROOT):
        root = os.path.realpath(allowed)
        if real == root or real.startswith(root + os.sep):
            return real
    raise PathError(
        f"path {path!r} is outside the repository working directory; "
        "all file operations must stay inside it"
    )


class _BoundedCapture(threading.Thread):
    """Drain a pipe as it is produced, keeping only the first and last
    MAX_OUTPUT_BYTES/2 bytes. A command that prints gigabytes (grep -R over
    the whole filesystem, cat of a huge log) must never grow the server's
    memory — in a --memory-limited container that kills the sandbox."""

    def __init__(self, pipe):
        super().__init__(daemon=True)
        self.pipe = pipe
        self.half = MAX_OUTPUT_BYTES // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def run(self):
        for chunk in iter(lambda: self.pipe.read(65536), b""):
            self.total += len(chunk)
            if len(self.head) < self.half:
                need = self.half - len(self.head)
                self.head += chunk[:need]
                chunk = chunk[need:]
            if chunk:
                self.tail += chunk
                if len(self.tail) > self.half:
                    del self.tail[: len(self.tail) - self.half]
        self.pipe.close()

    def result(self) -> bytes:
        if self.total <= MAX_OUTPUT_BYTES:
            return bytes(self.head + self.tail)
        return (bytes(self.head) + f"\n\n[... sandbox cut {self.total} bytes of output, "
                f"middle elided ...]\n\n".encode() + bytes(self.tail))


def do_exec(command: str, timeout_s, cwd: str) -> dict:
    try:
        timeout = int(timeout_s) if timeout_s else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    timeout = max(1, min(timeout, MAX_TIMEOUT_S))
    cwd = cwd or ROOT
    if not os.path.isdir(cwd):
        raise PathError(f"working directory does not exist: {cwd}")
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,   # so a timeout can kill the whole group
    )
    capture = _BoundedCapture(proc.stdout)
    capture.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    capture.join(timeout=10)      # the pipe closes once every writer is dead
    return {
        "output": capture.result().decode("utf-8", errors="replace"),
        "exit_code": proc.returncode,
        "timed_out": timed_out,
    }


def do_read(path: str, cwd: str) -> dict:
    real = resolve(path, cwd)
    if not os.path.isfile(real):
        raise PathError(f"file not found: {path}")
    try:
        with open(real, "r", errors="replace") as f:
            return {"content": f.read()}
    except OSError as e:
        raise PathError(f"could not read {path}: {e}")


def do_write(path: str, content: str, cwd: str) -> dict:
    real = resolve(path, cwd)
    if os.path.isdir(real):
        raise PathError(f"{path} is a directory")
    existed = os.path.isfile(real)
    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "w") as f:
            f.write(content)
    except OSError as e:
        raise PathError(f"could not write {path}: {e}")
    return {"existed": existed}


class Handler(BaseHTTPRequestHandler):
    server_version = "cs2680-sandbox/1"

    def log_message(self, fmt, *args):   # one line per request, to stderr
        _log(fmt % args)

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True, "root": ROOT})
        self._send(404, {"error": f"unknown route {self.path}"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(req, dict):
                raise ValueError("body must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": f"bad request body: {e}"})
        cwd = req.get("cwd") or ROOT
        try:
            if self.path == "/exec":
                if "command" not in req:
                    raise PathError("missing required argument: command")
                return self._send(200, do_exec(req["command"], req.get("timeout_s"), cwd))
            if self.path == "/read":
                return self._send(200, do_read(req.get("path", ""), cwd))
            if self.path == "/write":
                if "content" not in req:
                    raise PathError("missing required argument: content")
                return self._send(200, do_write(req.get("path", ""), req["content"], cwd))
            return self._send(404, {"error": f"unknown route {self.path}"})
        except PathError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:  # never die on one bad request
            _log(f"internal error: {type(e).__name__}: {e}")
            return self._send(500, {"error": f"sandbox internal error: {type(e).__name__}: {e}"})


def main() -> int:
    global ROOT
    ap = argparse.ArgumentParser(description="CS2680 A3 sandbox exec server")
    ap.add_argument("--root", required=True, help="repository checkout (e.g. /app)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    ROOT = os.path.realpath(args.root)
    if not os.path.isdir(ROOT):
        _log(f"root does not exist: {ROOT}")
        return 2
    os.makedirs(WORK_ROOT, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    srv.daemon_threads = True
    _log(f"serving root={ROOT} on port {args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
