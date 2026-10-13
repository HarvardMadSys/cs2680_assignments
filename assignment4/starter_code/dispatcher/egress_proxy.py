#!/usr/bin/env python3
"""egress_proxy.py — the agent container's only way to the internet.

Dispatcher-owned (CS2680 A3). evaluation_scripts/run_all.py runs this in a
container that sits on the normal bridge network AND on the private "egress"
network shared with the agent container; the agent itself is on internal
networks only (no gateway, no DNS to the outside). run_all.py points the
agent's OpenAI client here with CS2680_BASE_URL=http://<this container>:<port>/v1.

    python3 egress_proxy.py --port 3128 --upstream https://api.cs2680.com --session <id>

Behaviour:
- A reverse proxy, not a tunnel: the agent sends plain HTTP requests (on the
  private network only) and the proxy makes its own HTTPS request to the one
  --upstream, verifying the upstream's certificate. The agent never holds a
  connection or a TLS session to anything outside, so it cannot choose where
  its bytes go.
- Only requests for a path under /v1/ (the OpenAI-compatible API) are
  forwarded. CONNECT is refused (405); an absolute URL ("GET http://host/...",
  i.e. use as a forward proxy), any other path, or a path with "." / ".."
  segments is refused (403) before any outbound connection is made.
- The Host header is always the upstream's; hop-by-hop headers are dropped.
  Responses are passed back as they arrive (streamed replies work).
- With --session (run_all.py always gives it: dispatcher/session.py), every
  forwarded request carries the run's session id: an X-Session-Id header and a
  "cs2680-session/<id>" token at the end of the User-Agent. Whatever the agent
  sent for either (an X-Session-Id, a cs2680-session token) is replaced.
- One log line per request: method, path, status, bytes, seconds. Headers and
  bodies (the API key, prompts, replies) pass through but are never logged.
Stdlib only, one thread per client connection.
"""

import argparse
import http.client
import http.server
import re
import ssl
import sys
import time
import urllib.parse

UPSTREAM = None        # (scheme, host, port, Host header value), from --upstream
SESSION = None         # the run's session id, from --session
SESSION_OK = re.compile(r"[A-Za-z0-9._-]{1,120}")
PREFIX = "/v1/"
MAX_BODY = 64 << 20    # bytes of one request body
CONNECT_TIMEOUT_S = 30
READ_TIMEOUT_S = 900   # upstream silence (no byte) for this long ends the request
IDLE_TIMEOUT_S = 600   # an idle keep-alive connection from the agent is closed
HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate",
       "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length", "expect"}
SSL_CTX = ssl.create_default_context()


def _log(msg: str):
    print(f"[egress] {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


def _allowed_path(target: str) -> bool:
    if not target.startswith(PREFIX):
        return False
    path = urllib.parse.urlsplit(target).path
    segs = urllib.parse.unquote(path).replace("\\", "/").split("/")
    return not any(s in (".", "..") for s in segs)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cs2680-egress"
    sys_version = ""
    timeout = IDLE_TIMEOUT_S

    def log_message(self, fmt, *args):     # BaseHTTPRequestHandler's own logging: replaced by _log
        pass

    def _reply(self, code: int, text: str):
        body = (text + "\n").encode()
        self.close_connection = True       # the request body (if any) is left unread
        self.send_response_only(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_CONNECT(self):
        _log(f"{self.client_address[0]} REFUSED CONNECT {self.path[:120]!r}")
        self._reply(405, "Method Not Allowed: no tunnels; send API requests to CS2680_BASE_URL")

    def _body(self):
        """The request body (bytes), or None when there is none; raises ValueError when too large."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = b""
            while True:
                size = int(self.rfile.readline(1024).split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass                   # trailers
                    return body
                if len(body) + size > MAX_BODY:
                    raise ValueError("too large")
                body += self.rfile.read(size)
                self.rfile.readline(1024)      # CRLF after the chunk
        n = self.headers.get("Content-Length")
        if n is None:
            return None
        n = int(n)
        if n < 0 or n > MAX_BODY:
            raise ValueError("too large")
        return self.rfile.read(n)

    def _forward(self):
        peer, t0 = self.client_address[0], time.time()
        self._sent = 0
        shown = urllib.parse.urlsplit(self.path).path[:200]
        if not _allowed_path(self.path):
            _log(f"{peer} REFUSED {self.command} {self.path[:120]!r}")
            return self._reply(403, f"Forbidden: only {PREFIX}... paths of the course API are forwarded")
        try:
            body = self._body()
        except ValueError:
            return self._reply(413, "Request body too large or malformed")
        scheme, host, port, host_header = UPSTREAM
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=CONNECT_TIMEOUT_S, context=SSL_CTX)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT_S)
        try:
            try:
                conn.connect()
                conn.sock.settimeout(READ_TIMEOUT_S)
                drop = HOP | {t.strip().lower() for t in self.headers.get("Connection", "").split(",")}
                if SESSION:
                    drop |= {"x-session-id", "user-agent"}
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                conn.putheader("Host", host_header)
                for k, v in self.headers.items():
                    if k.lower() not in drop:
                        conn.putheader(k, v)
                if SESSION:
                    ua = re.sub(r"\s*cs2680-session/\S*", "", self.headers.get("User-Agent", "")).strip()
                    conn.putheader("User-Agent", f"{ua} cs2680-session/{SESSION}".strip())
                    conn.putheader("X-Session-Id", SESSION)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                _log(f"{peer} {self.command} {shown} upstream failed: {type(e).__name__}: {e}")
                return self._reply(502, f"Bad Gateway: {type(e).__name__}")
            self._relay(resp)
            _log(f"{peer} {self.command} {shown} -> {resp.status} {self._sent} B {time.time() - t0:.1f} s")
        except (OSError, http.client.HTTPException) as e:   # mid-response: the agent or the upstream went away
            self.close_connection = True
            _log(f"{peer} {self.command} {shown} broken after {self._sent} B: {type(e).__name__}")
        finally:
            conn.close()

    def _relay(self, resp):
        no_body = self.command == "HEAD" or resp.status in (204, 304) or 100 <= resp.status < 200
        drop = HOP | {t.strip().lower() for t in (resp.getheader("Connection") or "").split(",")}
        self.send_response_only(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() not in drop:
                self.send_header(k, v)
        chunked = False
        if no_body:
            if self.command == "HEAD" and resp.getheader("Content-Length") is not None:
                self.send_header("Content-Length", resp.getheader("Content-Length"))
        elif resp.length is not None:           # http.client: a Content-Length and not chunked
            self.send_header("Content-Length", str(resp.length))
        else:
            chunked = True
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if no_body:
            return
        while True:
            data = resp.read1(65536)
            if not data:
                break
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)
            self._sent += len(data)
        if chunked:
            self.wfile.write(b"0\r\n\r\n")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward


class Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


def main() -> int:
    global UPSTREAM, SESSION
    ap = argparse.ArgumentParser(description="CS2680 A3 egress proxy (reverse proxy to the course API)")
    ap.add_argument("--port", type=int, default=3128)
    ap.add_argument("--upstream", required=True,
                    help="the one origin requests are forwarded to, e.g. https://api.cs2680.com")
    ap.add_argument("--session", help="the run's session id, sent with every forwarded request")
    args = ap.parse_args()
    if args.session is not None and not SESSION_OK.fullmatch(args.session):
        ap.error(f"--session must be 1-120 of A-Z a-z 0-9 . _ -, not {args.session!r}")
    SESSION = args.session
    u = urllib.parse.urlsplit(args.upstream)
    if u.scheme not in ("https", "http") or not u.hostname or u.path not in ("", "/") or u.query:
        ap.error(f"--upstream must be an origin like https://api.cs2680.com, not {args.upstream!r}")
    port = u.port or (443 if u.scheme == "https" else 80)
    default = port == (443 if u.scheme == "https" else 80)
    UPSTREAM = (u.scheme, u.hostname, port, u.hostname if default else f"{u.hostname}:{port}")
    srv = Server(("0.0.0.0", args.port), Handler)
    _log(f"listening on {args.port}; forwarding {PREFIX}... to {u.scheme}://{UPSTREAM[3]}; "
         f"session {SESSION or '(none)'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
