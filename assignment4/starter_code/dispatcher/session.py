"""session.py — the session id of one run_all.py run (CS2680 A3 harness).

Dispatcher-owned (course code). Every request the agent sends to the course API carries it:
run_all.py hands it to the egress proxy (dispatcher/egress_proxy.py --session), which sets the
X-Session-Id header and tags the User-Agent with "cs2680-session/<id>" on every request it
forwards. The agent container never sees it and cannot change it.

  - your runs:  <First>_<Last>_<UTC time the run started>, e.g. Jane_Doe_20261006T172408Z, from
                dispatcher/student_name.json (asked for once, in a terminal, and saved there)
  - gradings:   the grader's A3_SESSION_ID, sub_<submission>_<UTC time>[_c<copy>]

Names are kept to Latin letters, digits and "-" (José -> Jose, "Mary Ann" -> Mary-Ann,
O'Neil -> ONeil); a name with no Latin letters at all is asked for again, romanized.
Stdlib only; run_all.py loads it before it starts anything.
"""

import json
import os
import re
import sys
import time
import unicodedata

NAME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "student_name.json")
GRADER_SESSION = re.compile(r"sub_\d+_\d{8}T\d{6}Z(_c\d+)?")
MAX_PART = 40


def clean_name(raw) -> str:
    """Latin letters, digits and '-' only: accents dropped, apostrophes removed, spaces and any
    other separators made '-'. '' when nothing usable is left."""
    s = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.replace("'", "")).strip("-")
    return s[:MAX_PART].strip("-")


def _read():
    try:
        d = json.load(open(NAME_FILE))
        return clean_name(d.get("first_name", "")), clean_name(d.get("last_name", ""))
    except (OSError, ValueError, AttributeError):
        return "", ""


def _ask():
    print("run_all.py needs your name once; it is saved in dispatcher/student_name.json, and every\n"
          "run then sends <First>_<Last>_<time> as the session id of its course API requests.",
          file=sys.stderr)
    out = []
    for what in ("First name", "Last name"):
        v = ""
        while not v:
            try:
                v = clean_name(input(f"{what} (Latin letters): "))
            except EOFError:
                sys.exit("\nno name given: fill in dispatcher/student_name.json")
            if not v:
                print("  please type it in Latin letters (e.g. as on your Harvard ID)", file=sys.stderr)
        out.append(v)
    with open(NAME_FILE, "w") as f:
        json.dump({"first_name": out[0], "last_name": out[1]}, f, indent=2)
        f.write("\n")
    print(f"saved in {NAME_FILE}", file=sys.stderr)
    return out[0], out[1]


def session_id(now=None) -> str:
    """The grader's A3_SESSION_ID, else <First>_<Last>_<UTC now>. Exits (before anything has
    started) when there is no name and no terminal to ask in."""
    sid = os.environ.get("A3_SESSION_ID", "").strip()
    if sid:
        if not GRADER_SESSION.fullmatch(sid):
            sys.exit(f"A3_SESSION_ID={sid!r} is not a grader session id "
                     "(sub_<n>_<YYYYMMDDTHHMMSSZ>[_c<k>]); unset it to use your name")
        return sid
    first, last = _read()
    if not (first and last):
        if sys.stdin.isatty():
            first, last = _ask()
        else:
            sys.exit(f"no name in {NAME_FILE}: fill in first_name and last_name (Latin letters), "
                     "or run evaluation_scripts/run_all.py once in a terminal to be asked")
    return f"{first}_{last}_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}"
