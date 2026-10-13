#!/usr/bin/env python3
"""Bundle model_patch_<instance_id>.diff files into predictions.json for the
SWE-bench Pro evaluation script (scaleapi/SWE-bench_Pro-os).

Pro's swe_bench_pro_eval.py expects a JSON array:
    [{"instance_id": ..., "model_patch": ..., "prefix": ...}, ...]

agent_task_input.json (a mapping keyed by task key, normally the instance_id; a set may
list an instance twice under keys like <id>__2 — predictions then carry the key) is read from the
evaluation_scripts folder itself; the patches are read from — and
predictions.json written to — the agent repo root: the current working
directory, or $MADSOPT_REPO if set.
"""

import json
import os
import pathlib
import sys

script_dir = pathlib.Path(__file__).resolve().parent
repo_root = pathlib.Path(os.environ.get("MADSOPT_REPO", os.getcwd())).resolve()
tasks = json.load(open(script_dir / "agent_task_input.json"))

predictions = []
missing = []
for instance_id in tasks:
    p = repo_root / f"model_patch_{instance_id}.diff"
    if not p.exists():
        missing.append(instance_id)
        continue
    predictions.append({
        "instance_id": instance_id,
        "model_patch": p.read_text(),
        "prefix": "madsOpt",
    })

out = repo_root / "predictions.json"
json.dump(predictions, open(out, "w"), indent=2)
print(f"wrote {out} ({len(predictions)}/{len(tasks)} patches)")
if missing:
    print("missing patches for: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
