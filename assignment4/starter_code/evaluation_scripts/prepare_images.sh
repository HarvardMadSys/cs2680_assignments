#!/bin/bash
# ONE-TIME SETUP (needs internet; idempotent — everything already present is skipped).
# Prepares everything run_all.py needs for the tasks in agent_task_input.json:
#
#   bash evaluation_scripts/prepare_images.sh [--save] [agent_task_input.json]
#
#   A3_WARM_JOBS  parallel `go mod download`s for the blend of step 5 (default 16).
#   Docker: plain `docker` if it works for this user, else `sudo -n -E docker` (asks for the
#   sudo password once, keeps sudo alive while the script runs); A3_DOCKER overrides.
#   Everything goes to $A3_CACHE (default <dir holding this harness>/.cache/cs2680_a3), which
#   is OUTSIDE the harness: the agent container mounts the harness, never the cache.
#   0. $SWEBENCH_PRO_OS (default $A3_CACHE/SWE-bench_Pro-os): scaleapi/SWE-bench_Pro-os at a
#        pinned commit ($SWEBENCH_PRO_OS_COMMIT; git, shallow): v2/tasks/<id>/tests (grading,
#        run_all.py) and v2/tasks/<id>/solution (input of step 5). Kept if already there.
#   1. $A3_ASSETS/portable_python{,_musl}/  CPython 3.12 for the sandbox server (via uv);
#        A3_ASSETS defaults to $A3_CACHE/assets
#   2. $A3_ASSETS/wheels/                    openai wheels -> the agent image builds offline
#   3. every task's pristine `docker_image`  (docker pull)
#   4. cs2680-a3-agent                       (dispatcher/agent.Dockerfile)
#   5. every task's `sandbox_image`          pristine image + PRE-WARMED Go module cache:
#        Go resolves, online, on throwaway copies of the repo — the base tree, an agent-style
#        `go get` of the fix's direct requirements, and the tree with the reference fix
#        (SWE-bench_Pro-os/v2/tasks/<id>/solution/gold_patch.diff) applied — plus the repo's
#        current upstream go.sum as blend. Own-repo module entries added by the fix or the
#        blend are removed again (only those the base tree itself pins stay). /app untouched.
#        Lets an agent working offline `go get`/`go mod tidy`/`go test` the dependencies
#        the fix needs; the cache holds hundreds of modules and nothing points at the fix.
#        Verify with:  gold patch + tests/test.sh in the warm image with --network none.
#   --save  also `docker save` every image to $A3_ASSETS/images/ (<id>.tar,
#           <id>.sandbox.tar, cs2680-a3-agent.tar, SHA256SUMS) for machines without
#           internet: there, point A3_ASSETS at a copy and just run run_all.py.
set -euo pipefail
cd "$(dirname "$0")/.."
SAVE=0; TASKS=evaluation_scripts/agent_task_input.json
for a in "$@"; do case "$a" in --save) SAVE=1 ;; *) TASKS="$a" ;; esac; done
CACHE="${A3_CACHE:-$(dirname "$PWD")/.cache/cs2680_a3}"   # $PWD = the harness root (cd above)
ASSETS="${A3_ASSETS:-$CACHE/assets}"
AGENT_IMAGE="${A3_AGENT_IMAGE:-cs2680-a3-agent}"
SWE_DIR="${SWEBENCH_PRO_OS:-$CACHE/SWE-bench_Pro-os}"
SWE_URL="https://github.com/scaleapi/SWE-bench_Pro-os.git"
SWE_COMMIT="${SWEBENCH_PRO_OS_COMMIT:-66f92766bba642462d4bbe5479e83f91f9211862}"   # the task files were built on it
V2="$SWE_DIR/v2/tasks"
PY_VERSION=3.12
log() { echo "== $*" >&2; }
have() { docker image inspect "$1" >/dev/null 2>&1; }

# docker: plain if this user may use it, else through sudo (all docker calls below use this)
if [[ -n "${A3_DOCKER:-}" ]]; then read -r -a DOCKER <<< "$A3_DOCKER"
elif ! command -v docker >/dev/null; then
  echo "error: docker not found on PATH (see Step 0 of the project description)" >&2; exit 1
elif command docker info >/dev/null 2>&1; then DOCKER=(docker)
elif command -v sudo >/dev/null; then
  log "docker is not usable without root here: using sudo (asks for your password once)"
  sudo -v
  sudo -n -E docker info >/dev/null 2>&1 || { echo "error: \`sudo -E docker info\` failed; add yourself to the docker group: sudo usermod -aG docker \$USER" >&2; exit 1; }
  DOCKER=(sudo -n -E docker)
  ( while sleep 60; do sudo -n -v 2>/dev/null || exit 0; done ) & SUDO_KEEPALIVE=$!
  trap 'kill "$SUDO_KEEPALIVE" 2>/dev/null || true' EXIT
else
  echo "error: docker is not usable by this user and there is no sudo: sudo usermod -aG docker \$USER" >&2; exit 1
fi
docker() { command "${DOCKER[@]}" "$@"; }

# --- 0. SWE-bench_Pro-os (tests + reference solutions of the V2 tasks) --------------
if [[ ! -d "$V2" ]]; then
  [[ ! -e "$SWE_DIR" ]] || { echo "error: $SWE_DIR exists but has no v2/tasks; move it away or set SWEBENCH_PRO_OS" >&2; exit 1; }
  log "fetching scaleapi/SWE-bench_Pro-os @ ${SWE_COMMIT:0:7} -> $SWE_DIR"
  tmp="$SWE_DIR.partial"; rm -rf "$tmp"; mkdir -p "$(dirname "$SWE_DIR")"
  git init -q "$tmp"
  git -C "$tmp" fetch -q --depth 1 "$SWE_URL" "$SWE_COMMIT"
  git -C "$tmp" -c advice.detachedHead=false checkout -q FETCH_HEAD
  mv "$tmp" "$SWE_DIR"
fi
missing="$(python3 - "$TASKS" "$V2" <<'PYIN'
import json, os, sys
tasks, v2 = sys.argv[1:]
for t in json.load(open(tasks)).values():
    d = os.path.join(v2, t["instance_id"])
    need = ["tests/test.sh", "tests/config.json"] + (["solution/gold_patch.diff"] if t.get("sandbox_image") else [])
    for f in need:
        if not os.path.isfile(os.path.join(d, f)): print(f"{t['instance_id']}/{f}")
PYIN
)"
[[ -z "$missing" ]] || { echo "error: missing in $V2 (wrong SWE-bench_Pro-os checkout?):" >&2; echo "$missing" >&2; exit 1; }
log "SWE-bench_Pro-os ok: $SWE_DIR ($(git -C "$SWE_DIR" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout'))"

mkdir -p "$ASSETS/images" "$ASSETS/wheels"

# --- 1. portable pythons (glibc + musl) ------------------------------------------
if [[ ! -x "$ASSETS/portable_python/bin/python3" || ! -x "$ASSETS/portable_python_musl/bin/python3" ]]; then
  log "installing portable CPython $PY_VERSION (glibc + musl) with uv"
  UVTMP="$(mktemp -d)"; python3 -m pip install -q --target "$UVTMP" uv
  UV="$UVTMP/bin/uv"; export UV_PYTHON_INSTALL_DIR="$UVTMP/py"
  for flavor in gnu musl; do
    dest="$ASSETS/portable_python"; [[ $flavor == musl ]] && dest="${dest}_musl"
    [[ -x "$dest/bin/python3" ]] && continue
    PYTHONPATH="$UVTMP" "$UV" python install "cpython-$PY_VERSION-linux-x86_64-$flavor" --no-bin >/dev/null
    src="$(readlink -f "$(ls -d "$UV_PYTHON_INSTALL_DIR"/cpython-$PY_VERSION*-$flavor | head -1)")"
    rm -rf "$dest" && cp -a "$src" "$dest"
  done
  rm -rf "$UVTMP"
fi
log "portable pythons ok: $ASSETS/portable_python{,_musl}"

# --- 2. wheels for the agent image ------------------------------------------------
if ! ls "$ASSETS"/wheels/openai-*.whl >/dev/null 2>&1; then
  log "downloading openai wheels -> $ASSETS/wheels"
  python3 -m pip download -q "openai>=1.0" --python-version 3.11 --only-binary=:all: \
    --platform manylinux2014_x86_64 --platform any -d "$ASSETS/wheels"
fi

# --- 3. pristine task images --------------------------------------------------------
while read -r iid image; do
  have "$image" && continue
  if [[ -f "$ASSETS/images/$iid.tar" ]]; then log "loading $image"; docker load -q -i "$ASSETS/images/$iid.tar" >/dev/null
  else log "pulling $image"; docker pull -q "$image" >/dev/null; fi
done < <(python3 -c "import json,sys; [print(t['instance_id'], t['docker_image']) for t in json.load(open(sys.argv[1])).values()]" "$TASKS")

# --- 4. agent image ----------------------------------------------------------------
if ! have "$AGENT_IMAGE"; then
  log "building $AGENT_IMAGE (dispatcher/agent.Dockerfile, offline from $ASSETS/wheels)"
  docker build -q -f dispatcher/agent.Dockerfile -t "$AGENT_IMAGE" "$ASSETS" >/dev/null
fi

# --- 5. pre-warmed sandbox images ---------------------------------------------------
build_warm() {   # build_warm <iid> <repo> <base image> <tag>
  local iid=$1 repo=$2 base=$3 tag=$4 b; b="$(mktemp -d)"
  cp "$V2/$iid/solution/gold_patch.diff" "$b/gold.diff"
  # blend: every module@version in the repo's current upstream go.sum (hundreds), so the
  # fix's own additions do not stand out in the cache
  python3 - "$repo" "$b/modlist.txt" "$b/gold.diff" "$b/goget.txt" <<'PYIN'
import sys, urllib.request, re
repo, out, gold, goget = sys.argv[1:]
# direct requirements the fix adds/bumps in go.mod (what an agent would `go get`)
ingo, direct = False, []
for l in open(gold, errors='replace'):
    if l.startswith('+++ '): ingo = l.rstrip().endswith('b/go.mod')
    elif ingo and re.match(r'^\+\s+\S+ v\S+', l) and '// indirect' not in l and '=>' not in l:
        m, v = l[1:].split()[:2]; direct.append(f"{m}@{v}")
open(goget, 'w').write('\n'.join(direct) + '\n'); print(f"   fix direct requirements: {len(direct)}", file=sys.stderr)
try:
    txt = urllib.request.urlopen(f'https://raw.githubusercontent.com/{repo}/HEAD/go.sum', timeout=60).read().decode()
    own = repo.split('/')[-1].lower()   # never blend in modules of the task's own repository
    mods = sorted({f"{m}@{v.removesuffix('/go.mod')}" for m, v, _ in (l.split() for l in txt.splitlines() if len(l.split()) == 3)
                   if own not in m.lower()})
except Exception as e:
    print(f"   warning: upstream go.sum not fetched ({e}); no blend", file=sys.stderr); mods = []
open(out, 'w').write('\n'.join(mods) + '\n'); print(f"   blend: {len(mods)} upstream module versions", file=sys.stderr)
PYIN
  # The cache is filled by letting Go RESOLVE for real, online, on throwaway copies of the
  # repo: (1) the base tree — the image's own cache is incomplete even for its own go.sum
  # (zips only for packages its tests built); (2) the tree with the reference fix applied —
  # so `go get`/`go test` for the fix's dependencies work offline. `go list -deps -test`
  # loads every package and test without compiling. Then the blend list. /app is never touched.
  # Build inputs (gold.diff, modlist.txt) are BIND-MOUNTED into the RUN, never COPY'd: a
  # COPY would persist the reference patch in an image layer that anyone holding the
  # tarball could extract. GOCACHE is redirected so Go's index of the patched tree is not
  # persisted either. Everything created under /tmp/warm is deleted within the same layer.
  cat > "$b/Dockerfile" <<DOCKEREOF
FROM $base
RUN --mount=type=bind,source=.,target=/ctx,readonly set -e; \\
    export GOFLAGS= GOPROXY=https://proxy.golang.org,direct GOSUMDB=sum.golang.org GOTOOLCHAIN=local GOCACHE=/tmp/warm/gocache; \\
    W=/tmp/warm; mkdir -p \$W; cp -a /app \$W/base; OWN=\$(awk '/^module /{print \$2; exit}' /app/go.mod); MC=\$(go env GOMODCACHE); \\
    (cd \$W/base && go mod download all >/dev/null 2>&1; go list -deps -test ./... >/dev/null 2>&1) || echo "warning: base resolution incomplete"; \\
    find "\$MC/cache/download/\$OWN" -type f 2>/dev/null | sort > \$W/own_base.txt || true; \\
    cd \$W/base && git reset -q --hard && git clean -fdq; \\
    while read -r m; do [ -n "\$m" ] && { go get "\$m" >/dev/null 2>&1 || echo "warning: agent-style go get \$m failed"; }; done < /ctx/goget.txt; \\
    git reset -q --hard && git clean -fdq; \\
    if git apply /ctx/gold.diff; then echo "fix applied"; else echo "ERROR: fix does not apply"; exit 1; fi; \\
    (go mod download all >/dev/null 2>&1; go list -deps -test ./... >/dev/null 2>&1) || echo "warning: fix resolution incomplete"; \\
    mkdir -p \$W/blend && cd \$W/blend && go mod init blend >/dev/null 2>&1; \\
    touch \$W/blend.fail; if xargs -P 1 true </dev/null 2>/dev/null; then \\
      xargs -P ${A3_WARM_JOBS:-16} -n 1 sh -c 'go mod download "\$1" >/dev/null 2>>"\$0/blend.err" || echo "\$1" >> "\$0/blend.fail"' \$W < /ctx/modlist.txt || echo "warning: parallel blend download incomplete"; \\
    else while read -r m; do go mod download "\$m" >/dev/null 2>>\$W/blend.err || echo "\$m" >> \$W/blend.fail; done < /ctx/modlist.txt; fi; \\
    n=\$(wc -l < \$W/blend.fail); \\
    echo "blend: \$n of \$(wc -l < /ctx/modlist.txt) not downloadable: \$(head -c 200 \$W/blend.fail 2>/dev/null | tr '\\n' ' ')"; \\
    find "\$MC/cache/download/\$OWN" -type f 2>/dev/null | sort > \$W/own_after.txt || true; \\
    comm -13 \$W/own_base.txt \$W/own_after.txt | while read -r f; do rm -f "\$f"; done; rm -rf "\$MC/\$OWN"@*; \\
    echo "own-module entries: base \$(wc -l < \$W/own_base.txt), removed \$(comm -13 \$W/own_base.txt \$W/own_after.txt | wc -l) added by fix/blend"; \\
    cd / && rm -rf \$W; \\
    test -z "\$(git -C /app status --porcelain)" || (echo "/app modified by warming" && exit 1)
DOCKEREOF
  # keep only the RUN's own messages: step headers (BuildKit `#N [...]`, legacy `Step N/M :`) echo the
  # whole command, which itself contains every word matched below
  docker build -t "$tag" "$b" 2>&1 | grep -vE '^(#[0-9]+ \[|Step [0-9]+/)' \
    | grep -E "warning|blend:|fix applied|own-module|modified|ERROR|error" >&2 || true
  have "$tag" || { echo "error: build of $tag failed" >&2; rm -rf "$b"; return 1; }
  rm -rf "$b"
}
while read -r iid repo base tag; do
  [[ -n "$tag" ]] || continue
  have "$tag" && continue
  if [[ -f "$ASSETS/images/$iid.sandbox.tar" ]]; then log "loading $tag"; docker load -q -i "$ASSETS/images/$iid.sandbox.tar" >/dev/null; continue; fi
  [[ -f "$V2/$iid/solution/gold_patch.diff" ]] || { log "cannot build $tag: no $V2/$iid/solution/gold_patch.diff"; exit 1; }
  log "building $tag (pre-warmed Go module cache on $base)"; build_warm "$iid" "$repo" "$base" "$tag"
done < <(python3 -c "import json,sys; [print(t['instance_id'], t['repo'], t['docker_image'], t.get('sandbox_image','')) for t in json.load(open(sys.argv[1])).values()]" "$TASKS")
log "all images present"

# --- --save: tarballs for offline machines -------------------------------------------
if (( SAVE )); then
  while read -r iid image simage; do
    [[ -f "$ASSETS/images/$iid.tar" ]] || { log "saving $iid.tar"; docker save "$image" > "$ASSETS/images/$iid.tar.part" && mv "$ASSETS/images/$iid.tar.part" "$ASSETS/images/$iid.tar"; }
    [[ -z "$simage" || -f "$ASSETS/images/$iid.sandbox.tar" ]] || { log "saving $iid.sandbox.tar"; docker save "$simage" > "$ASSETS/images/$iid.sandbox.tar.part" && mv "$ASSETS/images/$iid.sandbox.tar.part" "$ASSETS/images/$iid.sandbox.tar"; }
  done < <(python3 -c "import json,sys; [print(t['instance_id'], t['docker_image'], t.get('sandbox_image','')) for t in json.load(open(sys.argv[1])).values()]" "$TASKS")
  [[ -f "$ASSETS/images/$AGENT_IMAGE.tar" ]] || { docker save "$AGENT_IMAGE" > "$ASSETS/images/$AGENT_IMAGE.tar.part" && mv "$ASSETS/images/$AGENT_IMAGE.tar.part" "$ASSETS/images/$AGENT_IMAGE.tar"; }
  (cd "$ASSETS/images" && sha256sum *.tar > SHA256SUMS)
  log "tarballs in $ASSETS/images: $(du -sh "$ASSETS/images" | cut -f1)"
fi
