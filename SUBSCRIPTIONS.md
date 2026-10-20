# Running Harness² on subscriptions

This fork runs Harness² on a ChatGPT and a Claude subscription instead of API keys or a
Vertex AI project. Three roles take part in every experiment:

| Role | What it does | Tools it can be |
|---|---|---|
| Executor | Solves the task with the harness being refined | Claude Code (`cc`), Codex (`cx`) |
| Improver | Proposes harness edits, composes the refined harness, writes the lesson | Codex, Claude Code |
| Judge | Scores JobBench outputs after the run (SWE-bench needs none: tests decide) | Codex |

Each tool runs on its own login under `dependencies/`, never your `~/.codex` or `~/.claude`,
so your personal instructions, hooks and MCP servers stay out of the runs. Harness² never
installs anything into your own agent setup: refined harnesses are written under `runs/`,
and you copy what proves useful by hand.

Supported benchmarks: **SWE-bench Verified** (`swe`, executor Claude Code) and **JobBench**
(`jb`, executor Claude Code or Codex). LAB and WorkBuddy-Bench still need API endpoints.

## 1. Requirements

- macOS on Apple Silicon or Linux; Python 3.12 and [uv](https://docs.astral.sh/uv/);
  Node.js 20+ with npm; git.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) on PATH (`claude`), with a
  Claude subscription.
- A ChatGPT subscription that includes Codex. Setup installs its own pinned Codex CLI.
- SWE-bench only: a container runtime with the `docker` CLI and the `docker compose`
  plugin, able to run `linux/amd64` images (the task images are x86-only). On Apple
  Silicon this means Rosetta 2, see [section 5](#5-containers-for-swe-bench).

## 2. Install

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e '.[render]'
cp .env.example .env
```

## 3. Choose the roles

Put the role lines in `.env` as `<tool>:<model>`, with tool `claude`, `codex` or `opencode`:

```bash
HARNESS2_EXECUTOR=claude:claude-opus-5-5
HARNESS2_IMPROVER=codex:gpt-6-astra
HARNESS2_JUDGE=codex:gpt-6.1-sol
HARNESS2_CODEX_VERSION=0.160.0
HARNESS2_JUDGE_CODEX_VERSION=0.160.0
```

To swap who executes and who improves, swap the first two lines. `--substrate` defaults to
the executor's tool, so commands do not change. Newer ChatGPT models refuse older Codex
CLIs ("requires a newer version of Codex"); raise `HARNESS2_CODEX_VERSION` when that
happens. The judge's Codex is pinned separately so that upgrading the executor or improver
does not change how runs are scored.

## 4. Install a benchmark and log in

```bash
harness2-setup swe
harness2-setup jb
```

Setup installs what the configured roles need: the benchmark, a Codex CLI for a Codex
executor or improver, and for JobBench with a Codex judge a second Codex for the judge.
It prints the login commands still missing. Each login is a one-time browser sign-in:

Codex for the executor or improver (ChatGPT):

```bash
CODEX_HOME=dependencies/codex/home dependencies/codex/dist/codex login
```

Codex for the JobBench judge (ChatGPT):

```bash
CODEX_HOME=dependencies/codex-judge/home dependencies/codex-judge/dist/codex login
```

Claude Code on the host, for a JobBench executor or a Claude improver (claude.ai):

```bash
CLAUDE_CONFIG_DIR=dependencies/claude/home claude auth login --claudeai
```

SWE-bench runs Claude Code inside the task containers, which cannot use a browser login.
Create a long-lived subscription token from your normal Claude Code profile and put it in
`.env` (it is a credential: keep it out of git, chat and logs):

```bash
claude setup-token
```

```bash
HARNESS2_CLAUDE_OAUTH_TOKEN=<the printed token>
```

The token draws on the same subscription limits as an interactive session, and the
containers run the official Claude Code CLI unmodified; only the harness files
(`CLAUDE.md`, `.claude/skills`, `.claude/settings.json` hooks, `.claude/agents`) are added.

## 5. Containers for SWE-bench

Every rollout runs in the task's own x86 image (`swebench/sweb.eval.x86_64.*`) through
[Harbor](https://github.com/harbor-framework/harbor). A dedicated container VM keeps this
load apart from anything else running locally. With podman on Apple Silicon:

```bash
softwareupdate --install-rosetta --agree-to-license
```

Enable Rosetta for podman machines by adding `rosetta = true` under `[machine]` in
`~/.config/containers/containers.conf`, then create the machine:

```bash
CONTAINERS_MACHINE_PROVIDER=applehv podman machine init swebench --cpus 6 --memory 12288 --disk-size 150 --rootful
CONTAINERS_MACHINE_PROVIDER=applehv podman machine start swebench
```

Point Harness² at the machine's Docker-compatible socket in `.env` (`podman machine
inspect swebench` shows the socket path):

```bash
HARNESS2_SWE_DOCKER_HOST=unix:///<path>/podman/swebench-api.sock
```

Install the compose plugin if `docker compose version` fails (Homebrew:
`brew install docker-compose`, then link `/opt/homebrew/lib/docker/cli-plugins/docker-compose`
into `~/.docker/cli-plugins/`).

Rosetta, not qemu, must run the x86 images: under qemu the SWE-bench grader
(`uv run parser.py`) segfaults and every task scores 0. Check with
`podman machine ssh swebench "ls /proc/sys/fs/binfmt_misc/"`, which must list `rosetta`
and no `qemu-x86_64`.

Verify the whole chain without spending any subscription quota: Harbor's `oracle` agent
applies the reference fix, and the reward must be 1.0.

```bash
DOCKER_HOST=unix:///<path>/podman/swebench-api.sock dependencies/harbor/.venv/bin/harbor trials start -p dependencies/harbor-datasets/datasets/swebench-verified/psf__requests-1142 -a oracle --trial-name oracle-check --trials-dir runs/swe/smoke --delete
```

## 6. Check and run

`harness2-check` verifies every login, the token, Harbor, the container runtime and the
benchmark data without calling a model:

```bash
harness2-check --bench swe
```

Preview which tasks a run would take:

```bash
harness2 --bench swe --k 1 --mode parallel --only-domains psf__requests --max-tasks 1 --dry-run
```

Run one Propose-Probe-Compose step on one task:

```bash
harness2 --bench swe --k 1 --mode parallel --only-domains psf__requests --max-tasks 1
```

Score the adopted rollout, and the base rollouts for comparison, then report:

```bash
harness2-judge --bench swe --k 1 --mode parallel
harness2-judge --bench swe --k 1 --mode parallel --bases
harness2-report --bench swe --k 1 --mode parallel
```

JobBench uses the same commands with `--bench jb`; its domains are professions
(`--only-domains supply_chain_managers`).

SWE-bench domains are repositories. Lessons accumulate across tasks of the same repository
within one run, so `--max-tasks` above 1 is what lets the library build up. Task counts:
django 231, sympy 75, sphinx 44, matplotlib 34, scikit-learn 32, astropy 22, xarray 22,
pytest 19, pylint 10, requests 8, seaborn 2, flask 1. Each task's `task.toml` carries a
difficulty estimate (`<15 min fix` to `>4 hours`).

## 7. Where things land

| Path | Contents |
|---|---|
| `runs/<bench>/adapt/<namespace>/<task>/harnesses/` | Base, candidate and composed harness trees |
| `runs/<bench>/library/.../<domain>/` | Adopted harness and `EXPERIENCE.md` lesson per task |
| `runs/<bench>/adapt/<namespace>/judged.json`, `base_judged.json` | Scores of adopted and base rollouts |
| `runs/swe/trials/<label>/` | Harbor trial: `agent/claude-code.txt` trajectory, `agent/agent.patch` diff, verifier output |
| `runs/jb/dataset/<task>/model_output/<label>/` | JobBench deliverables |

The namespace encodes benchmark, executor, mode and `k` (`swe-cc-agg1`), so runs with
different roles or budgets never mix. A different model under the same namespace is
refused; use a new `HARNESS2_RUNS_DIR` for it.

## 8. Cost and time

One `k=1` step on one task is 4 executor rollouts and about 5 improver sessions; the
JobBench judge adds one call per scored output, SWE-bench none. Subscription usage grows
linearly with the number of tasks, so measure one task first: note the usage shown by
`/status` in Codex and `/usage` in Claude Code before and after.

Measured on an Apple Silicon Mac with the podman VM above: a SWE-bench task image takes
about 2 minutes to pull and build (reused by later rollouts of the same task) and the
repository's tests about 25 seconds. Agent time comes on top, capped at 50 minutes per
rollout. A JobBench Codex rollout took 10-15 minutes.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `requires a newer version of Codex` | Raise `HARNESS2_CODEX_VERSION` (or the judge's), rerun setup |
| `... has no ChatGPT login` / `no login` from `harness2-check` | Run the login command it prints |
| `Set HARNESS2_CLAUDE_OAUTH_TOKEN` | Run `claude setup-token`, put the token in `.env` |
| `docker: unknown command: docker compose` | Install the compose plugin (section 5) |
| Every SWE-bench reward is 0, `qemu: ... Segmentation fault` in `verifier/test-stdout.txt` | x86 images run under qemu; enable Rosetta (section 5) |
| `Judge model changed for this experiment` | Judging an old run with a new judge; use a new `HARNESS2_RUNS_DIR` |
