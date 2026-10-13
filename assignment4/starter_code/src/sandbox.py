"""Sandbox — the agent's only door to the task repository.

The repository does not live in the agent's container. For every task,
evaluation_scripts/run_all.py starts a separate *sandbox* container from that
task's image and puts `sandbox_url` (and `workdir`, the checkout path inside
it) into the task JSON. Everything that touches the repo — running commands,
reading, writing — goes through a Sandbox object bound to that URL; local
open()/subprocess in this process would act on the agent container, which has
no repo.

    sb = Sandbox(task["sandbox_url"], task["workdir"])
    r  = sb.exec("pytest -q tests/x.py", timeout_s=300)   # {output, exit_code, timed_out}
    s  = sb.read_text("lib/foo.py")
    existed = sb.write_text("lib/foo.py", s.replace("a", "b"))

A Sandbox holds no connection state (one HTTP request per call), so one object
can be shared by several threads, or a second one can point at another
directory in the same container: Sandbox(sb.url, "/work/name").
Stdlib only (urllib).
"""

import json
import time
import urllib.error
import urllib.request

# exec(): the HTTP request waits this much longer than the command's own timeout_s
# (the server accepts timeout_s up to 900 s).
HTTP_TIMEOUT_MARGIN_S = 30
CONNECT_RETRIES = 3


class SandboxError(Exception):
    """The sandbox cannot be reached (gone or never came up)."""


class SandboxPathError(Exception):
    """A refusal from the server, with a readable message (path outside the
    repository and /work, missing file, a directory, ...)."""


class Sandbox:
    def __init__(self, url: str, workdir: str):
        if not url:
            raise SandboxError("task has no sandbox_url (no sandbox container "
                               "was started for it)")
        self.url = url.rstrip("/")
        self.workdir = workdir

    def __repr__(self):
        return f"Sandbox({self.url!r}, {self.workdir!r})"

    # -- transport ---------------------------------------------------------

    def _post(self, route: str, body: dict, http_timeout: float) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.url + route, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        for attempt in range(CONNECT_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                try:
                    msg = json.load(e).get("error", "")
                except Exception:
                    msg = ""
                if e.code == 400:
                    raise SandboxPathError(msg or "bad request")
                raise SandboxError(f"sandbox returned HTTP {e.code}: {msg}")
            except urllib.error.URLError as e:
                # Connection refused = nothing ran yet; safe to retry briefly
                # (the sandbox may still be starting). Anything else is final.
                if isinstance(e.reason, ConnectionRefusedError) and attempt < CONNECT_RETRIES - 1:
                    time.sleep(1)
                    continue
                raise SandboxError(f"cannot reach sandbox {self.url}: {e.reason}")
            except (ConnectionError, TimeoutError, OSError) as e:
                raise SandboxError(f"cannot reach sandbox {self.url}: {e}")
        raise SandboxError(f"cannot reach sandbox {self.url}")

    # -- the four primitives ----------------------------------------------

    def health(self) -> dict:
        with urllib.request.urlopen(self.url + "/health", timeout=10) as resp:
            return json.load(resp)

    def exec(self, command: str, timeout_s: int, cwd: str = None) -> dict:
        """Run `command` with sh -c in cwd (default: this sandbox's workdir).
        Returns {"output": str, "exit_code": int, "timed_out": bool}."""
        return self._post("/exec",
                          {"command": command, "timeout_s": int(timeout_s),
                           "cwd": cwd or self.workdir},
                          http_timeout=int(timeout_s) + HTTP_TIMEOUT_MARGIN_S)

    def read_text(self, path: str) -> str:
        """Whole file as text (relative paths resolve against workdir)."""
        return self._post("/read", {"path": path, "cwd": self.workdir},
                          http_timeout=60)["content"]

    def write_text(self, path: str, content: str) -> bool:
        """Write (creating parent dirs). Returns True if the file existed."""
        return self._post("/write", {"path": path, "content": content,
                                     "cwd": self.workdir},
                          http_timeout=60)["existed"]
