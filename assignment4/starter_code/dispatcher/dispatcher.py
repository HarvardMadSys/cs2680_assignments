"""dispatcher/dispatcher.py — the agent's client to the DISPATCHER (course infrastructure, not yours).

Your code imports it as `from dispatcher import Dispatcher, DispatcherError`; madsOpt.py builds
the Dispatcher and hands it to Agent(dispatcher).run(). See madsOpt.py for the call list.
"""

import json
import os
import sys
import threading
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the harness root (this file lives in dispatcher/)
sys.path.insert(0, HERE)

from evaluation_scripts.trace_logger import TraceLogger    # noqa: E402  (course trace format)

SEQ_DIR = os.path.join(HERE, ".tasks", "seq")
PATCH_DIR = os.path.join(HERE, ".tasks", "patches")
POLL_S = 1
MAX_LIVE = 25


def _log(msg: str):
    print(f"[madsOpt] {msg}", file=sys.stderr, flush=True)


class DispatcherError(Exception):
    """The dispatcher refused a call (too many live tasks, unknown task, no patch, ...)."""


class DispatcherShutdown(Exception):
    """The dispatcher ended the run."""


class TaskTraceLogger(TraceLogger):
    """The course TraceLogger, but writing to a fixed per-task file."""

    def __init__(self, path: str, enabled: bool):
        super().__init__(False)
        self.enabled = enabled
        if enabled:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._fh = open(path, "a", buffering=1)


class Dispatcher:
    def __init__(self, log_enabled: bool):
        self._lock = threading.Lock()
        self._n = 0
        self._loggers = {}
        self._log_enabled = log_enabled   # PATCH_DIR is created by the dispatcher host side

    # -- transport -----------------------------------------------------------
    def _wait_for(self, path: str) -> dict:
        while not os.path.exists(path):
            if os.path.exists(os.path.join(SEQ_DIR, "shutdown")):
                raise DispatcherShutdown("the dispatcher ended the run")
            time.sleep(POLL_S)
        with open(path) as f:
            return json.load(f)

    def _request(self, op: str, **fields) -> dict:
        with self._lock:                       # the lock only orders the request numbers
            n = self._n; self._n += 1
            req = {"n": n, "op": op, **fields}
            tmp = os.path.join(SEQ_DIR, f".req_{n}.json")
            with open(tmp, "w") as f:
                json.dump(req, f)
            os.replace(tmp, os.path.join(SEQ_DIR, f"req_{n}.json"))
        resp = self._wait_for(os.path.join(SEQ_DIR, f"resp_{n}.json"))   # outside the lock: other threads proceed
        if not resp.get("ok"):
            raise DispatcherError(resp.get("error", "dispatcher refused the request"))
        return resp

    # -- the API -------------------------------------------------------------
    def next_task(self):
        resp = self._request("next_task")
        task = resp.get("task")
        if task is not None:
            self.logger(task["task"]).run_start(task.get("workdir", "/app"))
            _log(f"task #{task['task']} opened (sandbox {task.get('sandbox_url') or '(none)'})")
        return task

    def patch_path(self, k: int) -> str:
        d = os.path.join(PATCH_DIR, str(k))   # created by the dispatcher when the task is opened
        return os.path.join(d, "patch.diff")

    def submit_patch(self, k: int, text: str) -> int:
        p = self.patch_path(k)
        with open(p, "w") as f:
            f.write(text)
        return len(text)

    def extract_patch(self, k: int) -> int:
        return int(self._request("extract", k=int(k)).get("bytes", 0))

    def evaluate(self, k: int) -> dict:
        """Grade the patch in task k's folder. Blocks THIS caller until the verdict is in
        (the grading runs in the background on the dispatcher; other threads are not blocked)."""
        resp = self._request("evaluate", k=int(k))
        if resp.get("pending"):
            resp = self._wait_for(os.path.join(SEQ_DIR, f"evalres_{int(k)}_{resp['attempt']}.json"))
        ev = {"tests_failed": resp["tests_failed"], "tests_total": resp["tests_total"], "attempt": resp["attempt"]}
        _log(f"task #{k} attempt {ev['attempt']}: {ev['tests_failed']}/{ev['tests_total']} tests failing")
        return ev

    def continue_task(self, k: int):
        self._request("continue", k=int(k))
        _log(f"task #{k}: continuing")

    def done(self, k: int, reason: str = "done", iterations=None) -> dict:
        resp = self._request("done", k=int(k))
        self.logger(k).run_end(reason, int(iterations or 0))
        self.logger(k).close()
        _log(f"task #{k} done ({reason}); final {resp.get('tests_failed')}/{resp.get('tests_total')} tests failing")
        return resp

    def logger(self, k: int) -> TraceLogger:
        if k not in self._loggers:
            self._loggers[k] = TaskTraceLogger(os.path.join("madsOpt_logs", str(k), "run.jsonl"), self._log_enabled)
        return self._loggers[k]
