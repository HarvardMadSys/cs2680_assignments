#!/bin/bash
# Run madsLoop on every task in agent_task_input.json (which lives next to this
# script), bundle the patches into patches.json, score them with the
# official SWE-bench Pro evaluation, and write a pass/fail summary to
# run_all_results.md. Run from your agent repo root, or set MADSLOOP_REPO.
# Export CS2680_API_KEY first — it is forwarded into every task container.
#
#   bash evaluation_scripts/run_all.sh [--workers N]
#
# --workers N (default 10, or $WORKERS) sets BOTH the number of task
# containers run concurrently AND the evaluation's --num_workers.
#
# Unlike a single task run, a failing task does not abort the batch: every
# task is attempted, failures are listed at the end, and the tasks that did
# produce a patch are still evaluated.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${MADSLOOP_REPO:-$PWD}" && pwd)"

WORKERS="${WORKERS:-10}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers) WORKERS="${2:?--workers needs a number}"; shift 2 ;;
    --workers=*) WORKERS="${1#*=}"; shift ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
[[ "$WORKERS" =~ ^[0-9]+$ && "$WORKERS" -ge 1 ]] || {
  echo "error: --workers must be a positive integer, got '$WORKERS'" >&2; exit 2; }

# Checked once here so a missing key fails now, not $N times inside the logs.
if [[ -z "${CS2680_API_KEY:-}" ]]; then
  echo "error: CS2680_API_KEY is not set — export it before running (see 'The model API')" >&2
  exit 1
fi

N=$(python3 -c "import json; print(len(json.load(open('$SCRIPT_DIR/agent_task_input.json'))))")
LOG_DIR="$REPO_ROOT/run_logs"
STATUS_DIR="$LOG_DIR/.status"
mkdir -p "$STATUS_DIR"
rm -f "$STATUS_DIR"/* 2>/dev/null || true

echo "== running $N tasks, $WORKERS at a time (logs: $LOG_DIR/task_<i>.log)" >&2
for i in $(seq 0 $((N - 1))); do
  # Throttle: hold at $WORKERS concurrent containers.
  while (( $(jobs -rp | wc -l) >= WORKERS )); do wait -n 2>/dev/null || true; done
  (
    bash "$SCRIPT_DIR/run_task.sh" "$i" > "$LOG_DIR/task_$i.log" 2>&1
    echo $? > "$STATUS_DIR/$i"
  ) &
done
wait

# Exit codes land in files rather than being collected from wait: the throttle
# above already reaped some children, so their status is no longer waitable.
failed=()
for i in $(seq 0 $((N - 1))); do
  [[ "$(cat "$STATUS_DIR/$i" 2>/dev/null || echo 1)" == "0" ]] || failed+=("$i")
done
if ((${#failed[@]})); then
  echo "== WARNING: ${#failed[@]} task(s) failed: ${failed[*]} (see $LOG_DIR/task_<i>.log)" >&2
fi

# Writes patches.json even when patches are missing, and exits 1 to say so.
python3 "$SCRIPT_DIR/make_patches.py" || \
  echo "== continuing to evaluation with the patches that exist" >&2

NUM_WORKERS="$WORKERS" bash "$SCRIPT_DIR/evaluate.sh"

python3 - "$SCRIPT_DIR/agent_task_input.json" \
           "$REPO_ROOT/pro_eval/eval_results.json" \
           "$REPO_ROOT/run_all_results.md" <<'EOF'
import json, sys

tasks = json.load(open(sys.argv[1]))
results = json.load(open(sys.argv[2]))

rows = [(iid, results.get(iid)) for iid in tasks]
passed = sum(1 for _, v in rows if v)

lines = ["# Evaluation results", "",
         f"{passed}/{len(rows)} passed", "",
         "| instance_id | result |", "| --- | --- |"]
lines += [f"| {iid} | {'PASS' if v else 'FAIL'} |" for iid, v in rows]

with open(sys.argv[3], "w") as f:
    f.write("\n".join(lines) + "\n")
print(f"wrote {sys.argv[3]} ({passed}/{len(rows)} passed)")
EOF
