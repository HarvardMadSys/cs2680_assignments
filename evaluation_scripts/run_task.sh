#!/bin/bash
# Run madsLoop on one task from agent_task_input.json inside its task container
# (assignment section 2.3). Works with both image families:
#   - swebench/sweb.eval.* : no entrypoint, checkout at /testbed
#   - jefzda/sweap-images  : ENTRYPOINT=/bin/bash (so `docker run IMG bash -c`
#     would break — we override the entrypoint), checkout at /app
# The checkout directory is auto-detected inside the container, and known
# image quirks (a dead local pip mirror, PEP 668 "externally-managed-
# environment") are neutralized BEFORE your docker_env.sh runs, so a minimal
# `pip install -q openai` docker_env.sh works everywhere.
#
# Run from your agent repo root (the directory containing madsLoop.py), or
# set MADSLOOP_REPO to point at it:
#
#   export CS2680_API_KEY=hyi-...
#   bash evaluation_scripts/run_task.sh <task_index>
#
# agent_task_input.json is read from the evaluation_scripts folder itself,
# unless $TASKS_JSON names another task file (see mytest/).
#
# Produces:  model_patch_<instance_id>.diff  in the repo root
#            madsLoop_logs/                    copied back from the container
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Defaults to the tasks we ship; point TASKS_JSON at mytest/*.json to run
# one of your own tasks through the same path.
TASKS_JSON="${TASKS_JSON:-$SCRIPT_DIR/agent_task_input.json}"
REPO_ROOT="$(cd "${MADSLOOP_REPO:-$PWD}" && pwd)"
IDX="${1:?usage: run_task.sh <task_index>}"

if [[ ! -f "$REPO_ROOT/madsLoop.py" ]]; then
  echo "error: no madsLoop.py in $REPO_ROOT — run from your repo root or set MADSLOOP_REPO" >&2
  exit 1
fi

if [[ ! -f "$TASKS_JSON" ]]; then
  echo "error: no task file at $TASKS_JSON" >&2
  exit 1
fi

if [[ -z "${CS2680_API_KEY:-}" ]]; then
  echo "error: CS2680_API_KEY is not set — export it before running (see 'The model API')" >&2
  exit 1
fi

# On Apple Silicon set PLATFORM_FLAG="--platform linux/amd64"
PLATFORM_FLAG="${PLATFORM_FLAG:-}"

read -r INSTANCE_ID IMAGE < <(python3 -c "
import json
t = list(json.load(open('$TASKS_JSON')).values())[$IDX]
print(t['instance_id'], t['docker_image'])
")
python3 -c "
import json
t = list(json.load(open('$TASKS_JSON')).values())[$IDX]
open('$REPO_ROOT/.problem_$IDX.txt', 'w').write(t['problem_statement'])
"

echo "== task $IDX: $INSTANCE_ID" >&2
echo "== image: $IMAGE" >&2
if ! docker pull $PLATFORM_FLAG "$IMAGE" >&2; then
  # A mytest/ image you built locally has no registry to be pulled from. That
  # is fine, as long as the tag exists in the local Docker daemon.
  docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    echo "error: cannot pull $IMAGE, and no such image in the local daemon" >&2
    exit 1
  }
  echo "== using local image $IMAGE" >&2
fi

docker run --rm $PLATFORM_FLAG \
  --entrypoint bash \
  -v "$REPO_ROOT:/madsLoop" \
  -e CS2680_API_KEY="${CS2680_API_KEY:-}" \
  -e PROBLEM_FILE="/madsLoop/.problem_$IDX.txt" \
  -e LOG_DEST="/madsLoop/madsLoop_logs/$INSTANCE_ID" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  "$IMAGE" \
  -c 'set -e
      # The container runs as root but /madsLoop is the host user`s repo.
      # Whatever this script creates there must not be left root-owned
      # (the host cannot delete it) — chown on ANY exit, success or crash.
      trap "find /madsLoop ! -user \"$HOST_UID\" -exec chown \"$HOST_UID:$HOST_GID\" {} + 2>/dev/null || true" EXIT
      # --- container hygiene (image quirks, fixed before docker_env.sh) ---
      # Some images ship /etc/pip.conf pointing at a local offline mirror
      # (http://127.0.0.1:9876/) that is not running here -> "Connection
      # refused"; and some use a Debian-managed Python that refuses installs
      # per PEP 668. Env vars beat pip.conf, and are ignored by pips too old
      # to know them. Disposable container, so this is safe.
      export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
      export PIP_BREAK_SYSTEM_PACKAGES=1
      rm -f /etc/pip.conf 2>/dev/null || true
      # Do not litter root-owned __pycache__ into the mounted repo when
      # importing /madsLoop/madsLoop.py and src/ modules.
      export PYTHONDONTWRITEBYTECODE=1
      # the checkout is /testbed in SWE-bench images, /app in sweap-images
      WD=/testbed; [ -d /testbed/.git ] || WD=/app
      if [ ! -d "$WD/.git" ]; then
        echo "error: no repo checkout found at /testbed or /app in this image" >&2
        exit 1
      fi
      echo "== workdir in container: $WD" >&2
      # Some images ship a dirty checkout (e.g. flipt: go.work.sum modified at
      # baseline). Commit that state so the collected diff contains only the
      # agent`s changes.
      # (submodule-internal dirt is invisible to git diff and unstageable by
      # git add -A, so ignore it here; guard the commit and keep all output
      # off stdout, which is reserved for the final patch)
      if [ -n "$(git -C "$WD" status --porcelain --ignore-submodules)" ]; then
        echo "== note: baseline checkout is dirty; committing image state so the diff is only your changes" >&2
        git -C "$WD" -c user.email=eval@local -c user.name=eval add -A >&2 || true
        git -C "$WD" -c user.email=eval@local -c user.name=eval commit -qm "baseline image state" >&2 || \
          echo "== WARNING: baseline commit failed; continuing with dirty baseline" >&2
      fi
      # --- pick an interpreter for the HARNESS process -------------------
      # The harness code may use modern Python syntax while the image default
      # can be ancient (3.6/3.8). This selects who runs madsLoop.py ONLY — the
      # agent`s bash tool still uses the image`s default python, so repo and
      # test commands run in the task`s real environment.
      AGENT_PY=""
      for c in python3.13 python3.12 python3.11 python3.10 \
               /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
               /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
               /opt/miniconda3/bin/python /opt/conda/bin/python \
               python3 python; do
        if command -v "$c" >/dev/null 2>&1 && \
           "$c" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
          AGENT_PY="$(command -v "$c")"; break
        fi
      done
      if [ -z "$AGENT_PY" ]; then
        echo "== no Python >=3.10 in image; bootstrapping one with uv" >&2
        python3 -m pip install -q uv || pip install -q uv
        UV_BIN="$(command -v uv 2>/dev/null || true)"
        [ -n "$UV_BIN" ] || UV_BIN="$(python3 -c "from uv import find_uv_bin; print(find_uv_bin())" 2>/dev/null || true)"
        if [ -z "$UV_BIN" ]; then
          for u in /usr/local/bin/uv /usr/bin/uv /root/.local/bin/uv; do
            [ -x "$u" ] && UV_BIN="$u" && break
          done
        fi
        export UV_PYTHON_INSTALL_DIR=/tmp/uv-pythons
        "$UV_BIN" python install 3.12 >&2
        AGENT_PY="$("$UV_BIN" python find 3.12)"
      fi
      echo "== harness interpreter: $AGENT_PY ($("$AGENT_PY" -V 2>&1))" >&2
      # --- your environment setup, then the agent (same invocation grading uses) ---
      # Non-fatal: in non-Python images the default pip may be absent/broken;
      # ensure_openai below covers the harness interpreter regardless.
      bash /madsLoop/docker_env.sh || \
        echo "== WARNING: docker_env.sh failed; continuing (harness deps are ensured separately)" >&2
      # docker_env.sh installs into the image default python; make sure the
      # harness interpreter has the openai package too. Ladder: existing pip
      # -> ensurepip -> get-pip.py -> install with image pip into a dir on
      # PYTHONPATH (openai and its deps ship py3/abi3 wheels, so packages
      # installed by an older pip import fine on the newer interpreter).
      ensure_openai() {
        "$AGENT_PY" -c "import openai" 2>/dev/null && return 0
        if ! "$AGENT_PY" -m pip --version >/dev/null 2>&1; then
          "$AGENT_PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
        fi
        if ! "$AGENT_PY" -m pip --version >/dev/null 2>&1; then
          echo "== bootstrapping pip for $AGENT_PY via get-pip.py" >&2
          "$AGENT_PY" -c "import urllib.request as u; u.urlretrieve(\"https://bootstrap.pypa.io/get-pip.py\", \"/tmp/get-pip.py\")" \
            && "$AGENT_PY" /tmp/get-pip.py -q >/dev/null 2>&1 || true
        fi
        "$AGENT_PY" -m pip install -q "openai>=1.0" 2>/dev/null || true
        "$AGENT_PY" -c "import openai" 2>/dev/null && return 0
        echo "== falling back: image pip install into /tmp/agentdeps" >&2
        pip install -q --target /tmp/agentdeps "openai>=1.0"
        export PYTHONPATH="/tmp/agentdeps${PYTHONPATH:+:$PYTHONPATH}"
        "$AGENT_PY" -c "import openai"
      }
      ensure_openai
      cd /tmp
      "$AGENT_PY" /madsLoop/madsLoop.py -p "$(cat "$PROBLEM_FILE")" --log "$WD"
      if [ -d madsLoop_logs ]; then
        mkdir -p "$LOG_DEST" && cp -r madsLoop_logs/. "$LOG_DEST"/ && chown -R "$HOST_UID:$HOST_GID" /madsLoop/madsLoop_logs || true
      else
        echo "== WARNING: agent wrote no madsLoop_logs/ despite --log" >&2
      fi
      # Include files the agent CREATED: intent-to-add makes untracked
      # (non-gitignored) files show up in git diff with their content.
      git -C "$WD" add -N . >&2 2>/dev/null || true
      git -C "$WD" diff' \
  > "$REPO_ROOT/model_patch_${INSTANCE_ID}.diff"

rm -f "$REPO_ROOT/.problem_$IDX.txt"
echo "== patch written to model_patch_${INSTANCE_ID}.diff" >&2
head -20 "$REPO_ROOT/model_patch_${INSTANCE_ID}.diff" >&2
