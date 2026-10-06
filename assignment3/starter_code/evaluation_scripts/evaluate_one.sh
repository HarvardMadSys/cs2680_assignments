#!/bin/bash
# Grade ONE patch with the SWE-bench Pro V2 verifier (same protocol as
# evaluate.sh, for a single instance): apply the patch to a PRISTINE
# ghcr.io/scaleapi/swe-bench_pro-v2:<instance_id> container, run the task's
# tests/test.sh with the tests dir mounted at /tests, read reward.txt. The container has
# no network (--network none): code from the patch runs next to the hidden tests.
#
#   bash evaluation_scripts/evaluate_one.sh <task_key> <patch_file> [out_dir]
#
# <task_key> is the task's key in agent_task_input.json (normally its instance_id; a set may
# list an instance twice under keys like <id>__2). The entry's `instance_id` decides the
# image and the tests dir. out_dir defaults to ./pro_eval/<task_key>.
# Prints "1" (resolved) or "0" on stdout; exit status 0 either way, 2 on a
# setup error (unknown instance, no verifier). An empty patch is "0" without
# starting a container. A3_EVAL_TIMEOUT (default 3300 s) bounds the verifier; on timeout the
# container is removed, reward is 0 and $OUT/TIMEOUT is created.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
KEY="$1"; PATCH="$2"; OUT="${3:-$PWD/pro_eval/$KEY}"
# The image: the task's sandbox_image when it has one (the pristine image plus the pre-warmed Go
# module cache the agent worked with: the container has no network to fetch modules), else docker_image.
read -r IID IMAGE < <(python3 -c "import json,sys; t=json.load(open(sys.argv[1])).get(sys.argv[2],{}); print(t.get('instance_id', sys.argv[2]), t.get('sandbox_image') or t.get('docker_image',''))" "$SCRIPT_DIR/agent_task_input.json" "$KEY")
[[ -n "$IMAGE" ]] || { echo "evaluate_one: unknown task $KEY" >&2; exit 2; }
CACHE="${A3_CACHE:-$(dirname "$ROOT")/.cache/cs2680_a3}"   # same default as run_all.py / prepare_images.sh
TDIR="${SWEBENCH_PRO_OS:-$CACHE/SWE-bench_Pro-os}/v2/tasks/$IID/tests"
[[ -f "$TDIR/test.sh" ]] || { echo "evaluate_one: no V2 verifier at $TDIR" >&2; exit 2; }

rm -rf "$OUT" && mkdir -p "$OUT/replay"
if [[ ! -s "$PATCH" ]] || ! grep -q '[^[:space:]]' "$PATCH"; then
  echo 0 > "$OUT/reward.txt"; echo "no patch" > "$OUT/verifier_stdout.txt"; echo 0; exit 0
fi
cp "$PATCH" "$OUT/replay/model.patch"

# Same apply ladder as evaluate.sh / v2 patch_replay; test.sh's own exit code
# is ignored, reward.txt is the verdict.
INNER='cd /app 2>/dev/null || cd /testbed
{ git apply --verbose /replay/model.patch || git apply --3way /replay/model.patch \
  || patch --fuzz=3 -p1 -i /replay/model.patch; } > /logs/verifier/apply.log 2>&1
echo "apply_rc=$?" >> /logs/verifier/apply.log
git status --short > /logs/verifier/post_apply_status.txt 2>&1 || true
bash /tests/test.sh
chown -R '"$(id -u):$(id -g)"' /logs/verifier 2>/dev/null || true'
# On timeout: `timeout` TERMs the docker client, which only forwards the signal into the
# container and keeps waiting (the container's shell defers it until the test process ends),
# so `-k 5` KILLs the client 5 s later and the container is then removed by name.
read -r -a DOCKER <<< "${A3_DOCKER:-docker}"   # run_all.py sets it (e.g. "sudo -n -E docker")
CNAME="a3grade_$$_$(date +%s%N)"
rc=0
timeout -k 5 "${A3_EVAL_TIMEOUT:-3300}" "${DOCKER[@]}" run --rm --name "$CNAME" --network none --entrypoint bash \
  -v "$TDIR:/tests:ro" -v "$OUT/replay:/replay:ro" -v "$OUT:/logs/verifier" \
  "$IMAGE" -c "$INNER" > "$OUT/verifier_stdout.txt" 2>&1 || rc=$?
if [[ $rc == 124 || $rc == 137 ]]; then
  "${DOCKER[@]}" rm -f "$CNAME" >/dev/null 2>&1 || true
  echo "TIMEOUT after ${A3_EVAL_TIMEOUT:-3300}s" > "$OUT/TIMEOUT"; echo "verifier timed out" >> "$OUT/verifier_stdout.txt"
fi
if [[ "$(cat "$OUT/reward.txt" 2>/dev/null | tr -d '[:space:]')" == 1 ]]; then echo 1; else echo 0; fi
