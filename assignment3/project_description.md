# CS2680 Assignment 3: Harness Competition

> ### ⚠️ THE LEADERBOARD DOES NOT ACCEPT LATE DAYS AND WILL CLOSE ON TUESDAY, OCTOBER 20, AT 11:59 P.M.
>
> If you have not submitted to the leaderboard by the time it closes, you get 0 points for the
> leaderboard. Late days apply only to the repo and the video.


## Part 0. The framework

### Two kinds of containers

The main agent runs in the **agent container**, built from the course's `dispatcher/agent.Dockerfile`,
with your `src/` mounted in. The main agent loop starts in this container at the very beginning, and
memory, subagents and any other features should live in it as well.
The `starter_code` folder is mounted read-only at `/madsOpt`, and your agent can write to only two
places in it: `.tasks/` (its working directory) and `madsOpt_logs/` (where logs go). Anything else it
needs to write (scratch files, caches) goes elsewhere inside the container, e.g. `/tmp`.

Every task runs in **its own task container**, built from that task's own image: the repository
checked out at the task's starting commit, with the toolchain and test dependencies the task
needs. This container holds the code your agent reads and modifies. Some Go tasks need modules that
the fix must add. Their task containers come with a pre-warmed module cache and have `GOPROXY=off`
set, so `go get <module>@<version>` and `go mod tidy` work offline. To see what is available, run
`ls $(go env GOMODCACHE)/cache/download/<module>/@v/`. `evaluation_scripts/prepare_images.sh` builds
the images so that tasks can be solved offline.

Your agent interacts with a facility dispatcher to start a task, generate a patch and evaluate it.
Neither container can reach the internet: the agent container's only way out is an HTTPS proxy to the
course API, and task containers have none.

### How your agent reaches a task

When your agent asks for a task, the dispatcher starts the task container, waits until it is ready,
and hands your agent a dict with the task spec and the details for reaching the container:

```
problem_statement, requirements, interface   # the issue and its specification
task                                         # the task index k
sandbox_url                                  # http://<container>:8000
workdir                                      # the repository path inside it, e.g. /app
```

A tiny HTTP server (`src/sandbox_server.py`) runs inside the task container with four routes:
`/health`, `/exec` (run a shell command in the repository and get stdout, stderr and the exit
code), `/read` (a file), `/write` (a file). Your agent talks to it with plain HTTP POSTs.
`src/sandbox.py` wraps them as `Sandbox(url, workdir)` with `exec`, `read_text` and
`write_text`; everything your agent does to a repository goes through those calls. You can add
routes for any features you need (a new tool, a subagent, ...). Do not touch the repo with local
`open()` or `subprocess`: those act on the agent container, which has no repo.



### Getting graded, and deciding what to do next

When your agent believes a task is solved, it produces a patch and asks the facility to grade it:

```python
dispatcher.extract_patch(k)        # `git diff` of the task container -> the task's patch folder
                                   # (or dispatcher.submit_patch(k, text) to write your own)
ev = dispatcher.evaluate(k)        # -> {"tests_failed": F, "tests_total": T, "attempt": a}
```

The facility copies the patch onto a fresh copy of the task image, applies the task's hidden tests, runs them, and sends the agent the total number of tests
and how many failed. Your agent then chooses between:

```python
dispatcher.continue_task(k)   # keep working: the task container and your edits stay exactly as
                              # they are, the submitted patch is deleted, and you may call
                              # evaluate(k) again when you have a new one
dispatcher.done(k, reason, iterations)
                              # the task is final. Its last evaluation is its result; a patch you
                              # submitted but never evaluated is graded now; no patch counts as a
                              # failure. The task container and the patch folder are removed.
```

### The rules the dispatcher enforces

- **At most 5 tasks live at once.** `next_task()` raises `DispatcherError` when 5 are open, and
  returns `None` when no tasks are left.
- **One attempt at a time per task.** `continue_task(k)`, `evaluate(k)` and `done(k)` are refused
  while a grading of k is still running.
- **Grading has a time limit.** A test suite that hangs counts as all of its tests failing.
- **The whole run has a time limit: 4 hours.** The clock starts when your agent opens its first
  task. When 4 hours have passed, the run stops: your agent's next call to the dispatcher raises
  `DispatcherShutdown`, every task still open is finished as if `done(k)` had been called at that
  moment (its last evaluation counts; a submitted but unevaluated patch is graded; no patch is a
  failure), and tasks never opened count as failures. Gradings already running still finish.


### Entry point

```python
class Agent:
    def __init__(self, dispatcher): ...
    def run(self): ...
```

The runner constructs `Agent(dispatcher)` and calls `run()`. After that, you decide the flow: how
many tasks to keep open, whether to work on them in parallel, when to evaluate, whether to spend
another attempt on a failing task or move on, and what to carry from one task to the next. The
starter `Agent` in `src/agentic_loop.py` only demonstrates these calls. It takes the tasks one at a
time and makes no attempt to fix them, so every task fails until you write your own.

Log your model calls and tool calls through `dispatcher.logger(k)`. The leaderboard reads your turn
counts from that trace.


## Part 1. Starter code

### What is in it
You can modify anything under `src/` and add features there. The rest (`dispatcher/`, `madsOpt.py`
and `evaluation_scripts/`) is the same as what the leaderboard uses.

```
starter_code/
├── src/                          YOURS: everything here may change (the leaderboard takes only src/)
│   ├── agentic_loop.py           class Agent (your entry point): a starter showing every dispatcher, sandbox, model and trace call
│   ├── config.py                 your harness settings (the course API settings are environment variables, see Step 2)
│   ├── sandbox.py                Sandbox(url, workdir): exec / read_text / write_text over HTTP
│   └── sandbox_server.py         the HTTP server inside every task container (run_all.py needs its GET /health)
├── dispatcher/                   course infrastructure
│   ├── dispatcher.py             Dispatcher, the client your Agent calls: next_task, extract_patch / submit_patch, evaluate, continue_task, done, logger
│   ├── egress_proxy.py           the HTTPS proxy: the agent container's only way out, to the course API
│   └── agent.Dockerfile          the agent container image (python + openai, built offline)
├── madsOpt.py                    course infrastructure: builds the client and calls Agent(...).run()
└── evaluation_scripts/
    ├── prepare_images.sh         one-time setup (needs internet)
    ├── run_all.py                runs your agent on all tasks and grades them
    ├── evaluate_one.sh           grades one patch on a pristine task container
    ├── make_predictions.py       collects the final patches into predictions.json
    ├── trace_logger.py           the JSONL trace your agent writes through dispatcher.logger(k)
    ├── agent_task_input.json     the tasks
    └── task_test.json            the tests each task is graded with
```

### Running it

**Step 0. Prerequisites.** Linux x86_64; Docker; `git`;
`python3` (3.10+) with `pip`; about 15 GB of free disk for the images.

```bash
# only if Docker is not installed:
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker            # or log out and back in
docker run hello-world   # check that it works

# only if python3 has no pip ("No module named pip"):
sudo apt update
sudo apt install python3-pip
```

**Step 1. Set up the images.**

```bash
cd starter_code
bash evaluation_scripts/prepare_images.sh
```

The script fetches the task data (SWE-bench Pro, at a fixed commit), installs a portable Python for
the task containers, downloads the openai wheels, pulls every task's image, builds the agent image,
and builds a pre-warmed image for each Go task that requires additional packages. A successful build
ends with `== all images present`.

**Step 2. Set the env variables and test it.**

```bash
export CS2680_API_KEY=...
export CS2680_MODEL_EXPERT=expert        # Expert tier
export CS2680_MODEL_STANDARD=standard    # Standard tier
export CS2680_MODEL_STARTER=starter      # Starter tier
python3 evaluation_scripts/run_all.py --limit 0
```

`run_all.py` passes every `CS2680_*` variable into the agent container, and `madsOpt.py` adds
`CS2680_BASE_URL`, the course API endpoint. Your agent reads them with `os.environ`. It may use any
of the three models in one run, and as many of them as it likes.

This command starts the agent container and the proxy, checks the network rules (course API
reachable, everything else blocked), starts and health-checks every task container, and then stops
without running your agent. Look for `EGRESS OK`, and for `preflight k: ... ok` for every task.

**Step 3. Run it.**

```bash
python3 evaluation_scripts/run_all.py 
```

Progress is written to `run_logs/sequence.log`. When the run ends, `starter_code` contains
`run_all_results.md` (e.g. `3/5 passed`), `pro_eval/` (the grader's output per task),
`model_patch_<k>.diff`, and `madsOpt_logs/<k>/run.jsonl` (your agent's traces). The starter
agent is only a skeleton and fixes nothing, so expect `0/5 passed` from it.
Before the next run, archive the previous run's outputs in another folder.


## Part 2. Leaderboard submission

The leaderboard grades your `src/` on the evaluation task set, using the same `dispatcher/`,
`madsOpt.py` and `evaluation_scripts/` as the starter code. It takes only your `src/` from what you
upload. The leaderboard is at https://leaderboard.cs2680.com/

**Step 1. Log in and change your password.** On the leaderboard page, log in with the Harvard email
address associated with your Canvas account. Your initial password is Cs2680-<your 8-digit student
ID>; these are the same initial login credentials as for your API account. After logging in, click
your name and select `Change Password` from the drop-down menu.

**Step 2. The leaderboard.** After you log in, the `Leaderboard` page shows the top 10 students,
each represented by their best submission, and below them your own best submission with its rank.
The leaderboard updates as soon as a grading finishes.

**Step 3. Submit.** On the Submit page, upload a zip of your `src/` folder and choose the largest
number of tasks this run may grade. The run takes the first *k* tasks of the evaluation set, or all
of them if you leave the field empty.

**Step 4. How a submission is measured.** Every submission is evaluated on three metrics:
- the number of tasks solved,
- the cost per solved task (what the course API account was charged during the run, divided by
  the number of tasks solved),
- the time per solved task (the end-to-end host-clock time, from the first task opened to the last
  task done, divided by the number of tasks solved).

**Step 5. Past submissions, cancelling, and your daily budget.** The Past submissions page lists
your ongoing and past submissions. You can have at most one submission queued or being graded at
a time, and that one has a **Cancel** button. Every student has **$30 per day** for leaderboard
grading, with a **$10 limit** per submission. If grading reaches either limit, it stops, and
whatever it completed up to that point is recorded as the submission's result.

- Cancelling a submission that is still **queued** removes it from the queue at no cost.
- Cancelling a submission that is **being graded** stops the run. The cost so far is deducted
  from your $30 for the day, and the submission is recorded with its three metrics as of the
  moment you cancelled.

**Step 6. Ranking: a skyline.** Each student is ranked by their best submission.

- Best submissions that solved **at least 7 tasks** are ranked in skyline layers over the three
  metrics. One submission dominates another if it is at least as good on all three metrics and
  better on at least one. Rank 1 is every submission that no other submission dominates; rank 2
  applies the same rule to the rest, and so on.
- Best submissions that solved fewer than 7 tasks rank below all of them, ordered by tasks solved
  alone.
- Equal ranks are ties, and the next rank counts everyone ahead of it: 1, 1, 1, 4, ...
- A new submission becomes your best only if it ranks strictly better than your current best. On a
  tie, the current best stays.

### More information about leaderboard submission

- **The leaderboard has 26 tasks**, numbered 1 to 26 (only the first *k* of them if you set a
  number of tasks on the Submit page).
- **A run is cut at 4 hours**, counted from the moment your agent opens its first task (see the
  rules in Part 0).
- **Crashed or timed-out runs still count**, with the tasks they had finished by then. The Past
  submissions page also shows each submission's per-task results, numbered 1 to 26: the time and
  the number of turns for each task, and a short reason for each unsolved one:
  - *not opened*: your agent never asked for the task, or the run's time ran out first;
  - *not finished*: the run crashed or timed out while the task was open;
  - *no patch*: the task ended with an empty patch;
  - *tests did not run*: the grading produced no test results;
  - *import error*: the tests could not be collected (e.g. an import or syntax error);
  - *broke existing tests*: a test that passed before your patch now fails;
  - *new tests failed*: a test your fix should make pass still fails (a task can show both of
    the last two).
- **The dollar cost is checked during grading.** Every 2 minutes the grader adds up what the run has
  spent so far. If that reaches what is left of your budget for the day, the grading stops and is
  saved as `Reached the budget`, with whatever it had done by then.


## Part 3. Submission and grading

### Submission

The submission has three parts: the **code** (your `src/` on the leaderboard), a **write-up**,
and a **video**.

#### Code

Zip your `src/` folder and upload `src.zip` on the leaderboard's Submit page (Part 2, Step 3):

```bash
cd starter_code
zip -r src.zip src
```

Also archive your Claude Code session files as described in
[What to submit with each assignment](https://cs2680.com/computing/claude-code.html#export) and
commit them to `assignment3/` in your repository. Check that your `.gitignore` does not exclude the
archive. As on every assignment, the session files carry no points of their own. They are the record
behind the claims in your write-up.

#### Write-up and video

Write down the lessons you learned from this assignment in a one-page PDF (one page is a hard
limit), and present them in a **3-minute** video. Some examples:

- what you learned from using AI to build your harness;
- a harness optimization you found useful, e.g. subagents, memory carried from one task to the next, the policy for
  continuing a failing task or giving up on it, or which model
  tier does what;
- a pitfall you ran into, and how you got past it;
- anything else you found worth noting.

The write-up must be entirely your own work, with no AI-generated text. Commit it to your repository
as `assignment3/writeup.pdf`. Your repository's `assignment3/` should then contain:

```
assignment3/
├── a3-sessions.tar.gz    your Claude Code session files
└── writeup.pdf           the write-up
```

In the video, present the same lessons with your face visible and in your own voice. When a lesson
is about your harness, open the code and move the cursor to the relevant part. Upload the video
to Canvas.

### Grading

| Part | Points | What is graded |
|---|---|---|
| Leaderboard | **80** | The rank of your best submission on the leaderboard (Part 2, Step 6), scored as below. |
| Write-up | **10** | The lessons you learned from the assignment, clearly explained in one page. |
| Video | **10** | A clear 3-minute presentation of the same lessons, showing the code where a lesson concerns your harness. |

**Leaderboard score.** If *r* is the rank of your best submission:

```
score = max(81 − r, 50)    if any of your submissions achieves the baseline
score = 81 − r             otherwise
```

The **baseline** solves 14 tasks in 4 hours within a limit of $10. If any of your submissions
achieves the baseline, you get credit for it, even if that submission is not your best.

For example, rank 1 scores 80; rank 12 scores 69 whether or not it beats the baseline; rank 40
scores 50 if it beats the baseline and 41 if it does not.
