# Assignment 4 starter code

This is the Assignment 3 framework with three changes for Assignment 4. The assignment itself is on the course website.

- **Tasks.** `evaluation_scripts/agent_task_input.json` and `task_test.json` hold 25 SWE-bench Pro tasks, different from Assignment 3's and from the leaderboard's.
- **Up to 25 tasks live at once.** The dispatcher lets your agent keep all 25 tasks open (Assignment 3 allowed 5). Do not change the harness's settings: the leaderboard runs the harness as it is shipped.
- **Your server instead of the course API.** The egress proxy forwards your agent's model calls to `A3_API_UPSTREAM`, your server, and to nothing else. There is no default.

## Prerequisites

As in Assignment 3, a Linux x86_64 machine with Docker. In addition:

- **Disk.** The task images take about 40 GB to download and about 105 GB on disk, or about 145 GB if your Docker uses the containerd image store (`docker info` shows `io.containerd.snapshotter.v1`).
- **CPU and memory.** Each task container may use 1 CPU and 4 GB of memory, and with 25 tasks open they all run at once, next to up to 3 gradings.
- **Network.** The machine must reach the cluster's login node, so it must be on the Harvard network or the VPN.

## Pointing the harness at your server

The egress proxy runs in a container, and a container cannot reach the `localhost` of the machine it runs on. Open the [SSH tunnel](https://cs2680.com/computing/cluster.html#tunnel) to your server's node on the Docker bridge address instead, which the containers can reach. It is usually `172.17.0.1`; `docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'` prints it.

```bash
ssh -f -N -o ExitOnForwardFailure=yes -L 172.17.0.1:30000:<node>:30000 cs2680   # <node>: your server's node
export A3_API_UPSTREAM=http://172.17.0.1:30000          # an origin: no /v1
export CS2680_API_KEY=$(ssh cs2680 cat a4/serve.key)    # the same key as your server
export CS2680_MODEL_EXPERT=qwen3.6-35b-a3b-fp4 CS2680_MODEL_STANDARD=qwen3.6-35b-a3b-fp4 CS2680_MODEL_STARTER=qwen3.6-35b-a3b-fp4
curl -s -H "Authorization: Bearer $CS2680_API_KEY" $A3_API_UPSTREAM/v1/models   # should list qwen3.6-35b-a3b-fp4
bash evaluation_scripts/prepare_images.sh             # once
python3 evaluation_scripts/run_all.py --limit 0       # preflight
python3 evaluation_scripts/run_all.py
```

A passing preflight prints `egress: your server via ... -> 200, serving ['qwen3.6-35b-a3b-fp4']`, then `EGRESS OK`, then `preflight k: k ok` for every task from 0 to 24. `HTTP 502` means the proxy cannot reach `A3_API_UPSTREAM`: the tunnel is down, the address is wrong, or a firewall blocks the Docker bridge. `HTTP 401` means your server refused the key. When your server's job ends, stop its tunnel before you open one to the next job's node.

All three model ids name the one model your server serves, so an Assignment 3 agent needs no change to call it. Copy your Assignment 3 `src/` over this folder's `src/`.
