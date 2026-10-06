#!/usr/bin/env python3
"""madsOpt.py — DISPATCHER driver of the CS2680 A3 harness, variant 2_parallel_sandbox.

    python madsOpt.py [--log]

This file is course infrastructure: the leaderboard grader runs its own copy of
it (only your src/ is taken from your submission). Everything that is yours
lives in src/ — see src/agentic_loop.py.

In this variant YOUR agent drives the task flow. The dispatcher only serves
requests and enforces the rules:

    dispatcher = Dispatcher(...)               # built here
    Agent(dispatcher).run()                    # your main loop (src/agentic_loop.py)

Calls your Agent may make (all block until the dispatcher answers; the client lives in
dispatcher/dispatcher.py: `from dispatcher import Dispatcher, DispatcherError`):
    task = dispatcher.next_task()        open a new task: its sandbox is started and the
                                         task dict returned ({problem_statement, requirements,
                                         interface, task (index k), sandbox_url, workdir});
                                         None when no tasks are left; raises DispatcherError
                                         when MAX_LIVE tasks are already live
    dispatcher.extract_patch(k)          convenience: `git add -N . && git diff` of sandbox k
                                         written to dispatcher.patch_path(k); returns its size
    dispatcher.submit_patch(k, text)     or write the patch yourself (same file)
    ev = dispatcher.evaluate(k)          grade the patch in the folder of task k on a pristine
                                         container: {"tests_failed": F, "tests_total": T,
                                         "attempt": a} — the task's OWN hidden tests, never
                                         their names; raises DispatcherError if no patch is there
    dispatcher.continue_task(k)          keep working on k: the sandbox and your edits stay,
                                         the submitted patch is deleted, attempt counter +1
    dispatcher.done(k, reason, iterations) task k is final (its LAST evaluation counts; a patch
                                         that was submitted but never evaluated is evaluated
                                         now; no patch at all = failed). Its sandbox and its
                                         patch folder are removed.
    dispatcher.logger(k)                 the TraceLogger for task k (madsOpt_logs/<k>/run.jsonl):
                                         log api_request / tool_call / tool_result to it
Rules enforced by the dispatcher (run_all.py, host side): at most MAX_LIVE (5) tasks live at
once; evaluation runs on pristine containers; results are keyed by task index; the whole run
must end within A3_RUN_LIMIT_S (4 h) of the first task being opened, after which the dispatcher
shuts the run down (DispatcherShutdown), finalizes open tasks as by done(k) and counts tasks
never opened as failed.

File handshake with evaluation_scripts/run_all.py, all under .tasks/seq/:
    agent -> host  req_<n>.json   {"n", "op", "k", ...}     (one at a time, n = 0,1,2,...)
    host  -> agent resp_<n>.json  {"n", "ok", ...payload | "error"}
    host  -> agent ready | shutdown ;  agent -> host  end   (agent finished)
Patch folders: .tasks/patches/<k>/patch.diff (visible to both sides).
Course API: run_all.py sets CS2680_BASE_URL to the egress proxy (http://a3proxy_<run>:3128/v1,
the agent container's only way to the API: https://api.cs2680.com itself is unreachable from
inside it); CS2680_API_KEY and the model ids CS2680_MODEL_EXPERT / _STANDARD / _STARTER come from
the host (run_all.py passes every CS2680_* variable into the agent container). Your Agent reads
them with os.environ — never a hard-coded URL.
Requires Python 3.10+, the `openai` package, and the stdlib.
"""

import argparse
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dispatcher import Dispatcher, DispatcherShutdown, SEQ_DIR, _log  # noqa: E402  (dispatcher client)
from src.agentic_loop import Agent                                # noqa: E402  (student code)


def main() -> int:
    parser = argparse.ArgumentParser(description="CS2680 A3 harness driver (dispatcher)")
    parser.add_argument("--log", action="store_true", help="write per-task JSONL traces to ./madsOpt_logs/<k>/")
    args = parser.parse_args()
    if not os.environ.get("CS2680_BASE_URL"):
        sys.exit("CS2680_BASE_URL is not set: run the agent with evaluation_scripts/run_all.py")
    dispatcher = Dispatcher(log_enabled=args.log)
    rc = 0
    try:
        Agent(dispatcher).run()
    except DispatcherShutdown:
        _log("shutdown by the dispatcher")
    except Exception as e:
        _log(f"Agent.run raised {type(e).__name__}: {e}")
        rc = 1
    finally:
        for lg in dispatcher._loggers.values():
            lg.close()
        tmp = os.path.join(SEQ_DIR, ".end.tmp")
        with open(tmp, "w") as f:
            f.write("end\n")
        os.replace(tmp, os.path.join(SEQ_DIR, "end"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
