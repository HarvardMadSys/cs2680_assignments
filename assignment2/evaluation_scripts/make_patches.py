#!/usr/bin/env python3
"""Bundle model_patch_<instance_id>.diff files into patches.json for the
SWE-bench Pro evaluation script (scaleapi/SWE-bench_Pro-os).

Pro's swe_bench_pro_eval.py expects a JSON array:
    [{"instance_id": ..., "model_patch": ..., "prefix": ...}, ...]

agent_task_input.json (a mapping keyed by instance_id) is read from the
evaluation_scripts folder itself; the patches are read from — and
patches.json written to — the agent repo root: the current working
directory, or $MADSLOOP_REPO if set.
"""

import json
import os
import pathlib
import sys

script_dir = pathlib.Path(__file__).resolve().parent
repo_root = pathlib.Path(os.environ.get("MADSLOOP_REPO", os.getcwd())).resolve()
tasks = json.load(open(script_dir / "agent_task_input.json"))

patches = []
missing = []
for instance_id in tasks:
    p = repo_root / f"model_patch_{instance_id}.diff"
    if not p.exists():
        missing.append(instance_id)
        continue
    patches.append({
        "instance_id": instance_id,
        "model_patch": p.read_text(),
        "prefix": "madsLoop",
    })

out = repo_root / "patches.json"
json.dump(patches, open(out, "w"), indent=2)
print(f"wrote {out} ({len(patches)}/{len(tasks)} patches)")
if missing:
    print("missing patches for: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
