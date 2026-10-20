# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SWE-bench Verified through Harbor, with Claude Code as the executor.

One rollout is one `harbor trials start` on a task from laude-institute/harbor-datasets:
Claude Code works in the task's container on /testbed with the harness at the repository
root, the harness is taken back out, and the repository's own tests decide the outcome.
The domain is the repository, so lessons accumulate per codebase.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .. import config
from ..core import runmark as RM
from ..core.spec import BenchPrompts, BenchSpec, resolve_channel_model
from ..substrate.base import Install, RunRequest
from ..substrate.claude import ClaudeSubstrate
from .jobbench import run_detached
from .tasks import discover, select

BASE_HARNESS = Path(__file__).resolve().parent / "swe_base_harness"
AGENT = "harness2.benchmarks.swe_harbor_agent:HarnessClaudeCode"
DEFAULT_TIMEOUT = 3000
# Image build, agent install and the repository's tests run outside the agent's budget.
_WALL_SLACK = 5400
_PATCH_CAP = 200_000

SWE_PROMPTS = BenchPrompts(
    work=(
        "Resolving a reported issue in a real open-source Python repository. The request is "
        "the issue text as a user filed it; the work product is a focused change to the "
        "repository's source that makes the described behavior correct. The surrounding "
        "code, its public interfaces and the repository's existing tests are the "
        "specification; behavior outside the issue must stay as it was."
    ),
    domain_word="repository",
    deliverable_word="repository change",
    inv_levers=(
        "- turn the issue text into a concrete, observable expectation before editing\n"
        "- reproduce the reported behavior with a minimal script or an existing test first\n"
        "- locate the responsible code by tracing callers, not by pattern-matching names\n"
        "- make the smallest idiomatic change and keep public names and error contracts\n"
        "- run the narrowest relevant existing tests after the edit, then a broader nearby set\n"
        "- review the final diff for debug output, stray files and unrelated edits"
    ),
    dom_levers=(
        "- encode how this repository is laid out and how its tests are run (runner, settings "
        "module, test paths, slow or flaky areas to avoid)\n"
        "- capture the repository's conventions for deprecations, error messages, version "
        "guards and backwards compatibility\n"
        "- record recurring mechanics of this codebase's internals that a fix must respect"
    ),
    yardstick=(
        "Did it pin down the expected behavior, reproduce the problem, find the responsible "
        "code, make a minimal idiomatic change, run the relevant existing tests and leave a "
        "clean diff?"
    ),
    selection_checks=(
        "Prefer the change that addresses the root cause the issue describes rather than a "
        "symptom, follows the repository's local style and conventions, preserves unrelated "
        "behavior and public interfaces, and contains no debug output, stray files or "
        "unrelated edits."
    ),
)


def make_spec(substrate: str) -> BenchSpec:
    if substrate != "cc":
        raise ValueError(f"SWE-bench is wired for the Claude Code executor (cc), got {substrate!r}")
    ch = resolve_channel_model(None, None, effort="high")
    return BenchSpec(
        name="swe",
        runs_root=config.RUNS / "swe",
        install_mode="claude_root",
        prompts=SWE_PROMPTS,
        channels={"proposer": ch, "selector": ch},
        domain_word="repository",
        leak_globs=("tests/**", "solution/**", "environment/**"),
        allow_subagents=True,
        proposer_deliverables="contents",
    )


def _docker_env() -> dict[str, str]:
    return {"DOCKER_HOST": config.SWE_DOCKER_HOST} if config.SWE_DOCKER_HOST else {}


class SweAdapter:
    """SWE-bench Verified behind the BenchmarkAdapter protocol."""

    def __init__(
        self,
        substrate: str = "cc",
        *,
        timeout: int | None = None,
        split_file: Path | None = None,
    ):
        self.spec = make_spec(substrate)
        self.substrate = ClaudeSubstrate(launcher=self._launch)
        self.timeout = int(timeout or DEFAULT_TIMEOUT)
        self.split_file = split_file
        self.trials = config.RUNS / "swe" / "trials"

    def tasks(self, split: str = "all") -> list[str]:
        return select(discover("swe"), split, self.split_file, benchmark="swe")

    def domain_of(self, task_id: str) -> str:
        return task_id.rsplit("-", 1)[0]

    def reference_domain_order(self) -> list[str]:
        return sorted(set(discover("swe").values()))

    def prompts_for(self, task_id: str) -> BenchPrompts:
        return SWE_PROMPTS

    def _task_dir(self, task_id: str) -> Path:
        if "/" in task_id or ".." in task_id:
            raise ValueError(f"malformed SWE-bench task id {task_id!r}")
        return config.swe_tasks_dir() / task_id

    def task_prompt(self, task_id: str) -> str:
        p = self._task_dir(task_id) / "instruction.md"
        if not p.is_file():
            raise FileNotFoundError(f"task instructions missing: {p}; run harness2-setup swe")
        return p.read_text(encoding="utf-8", errors="replace")

    def task_files(self, task_id: str) -> Path | None:
        """The repository lives in the task image, not on the host."""
        return None

    def execute(
        self, task_id: str, harness_dir: Path | None, run_id: str, *, timeout: int | None = None
    ) -> Path:
        label = run_id.replace("/", "__")
        trial = self.trials / label
        if RM.is_complete(trial, harness_dir=harness_dir) and self._agent_log(trial).is_file():
            return trial
        if trial.exists():
            print(f"  [swe] re-rolling {label}: {RM.why_not(trial, harness_dir=harness_dir)}")
            shutil.rmtree(trial)
        src = Path(harness_dir) if harness_dir is not None else self.base_harness()
        install = self.substrate.materialise(src, self.spec, {})
        try:
            self.substrate.execute(task_id, install, [run_id], int(timeout or self.timeout))
        finally:
            shutil.rmtree(install.staging_dir, ignore_errors=True)
        RM.mark_complete(trial, run_id=run_id, harness_dir=harness_dir)
        return trial

    def _launch(self, req: RunRequest, install: Install) -> list[Path]:
        if len(req.run_ids) != 1:
            raise ValueError(f"SWE-bench rolls one run per invocation; got {req.run_ids}")
        label = req.run_ids[0].replace("/", "__")
        self.trials.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(config.HARBOR_BIN),
            "trials",
            "start",
            "-p",
            str(self._task_dir(req.task_ref)),
            "-a",
            AGENT,
            "-m",
            f"anthropic/{config.CLAUDE_MODEL}",
            "--trial-name",
            label,
            "--trials-dir",
            str(self.trials),
            "--agent-timeout",
            str(req.timeout),
            "--agent-kwarg",
            f"harness_dir={install.staging_dir}",
            "--agent-kwarg",
            f"reasoning_effort={config.CLAUDE_EFFORT}",
            "--agent-kwarg",
            "disable_web_search=true",
            "--no-force-build",
            "--delete",
        ]
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
        }
        env.update(
            {
                "PYTHONPATH": str(config.ROOT),
                "CLAUDE_CODE_OAUTH_TOKEN": config.CLAUDE_OAUTH_TOKEN,
                "CLAUDE_FORCE_OAUTH": "1",
            }
        )
        env.update(_docker_env())
        log = self.trials / f"{label}.harbor.log"
        with log.open("w", encoding="utf-8") as out:
            try:
                run_detached(
                    cmd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    timeout=req.timeout + _WALL_SLACK,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(
                    f"harbor wall-timeout for {label}; left UNMARKED so the next attempt "
                    f"re-rolls (log: {log})"
                ) from None
        trial = self.trials / label
        if not self._agent_log(trial).is_file():
            raise RuntimeError(
                f"harbor produced no agent log for {label} (environment build, install or "
                f"auth failed); see {log} and {trial / 'result.json'}"
            )
        return [trial]

    @staticmethod
    def _agent_log(trial: Path) -> Path:
        return Path(trial) / "agent" / "claude-code.txt"

    @staticmethod
    def _patch(run_dir: Path) -> str:
        p = Path(run_dir) / "agent" / "agent.patch"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def delivered(self, run_dir: Path) -> bool:
        return bool(self._patch(run_dir).strip())

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        log = self._agent_log(run_dir)
        if not log.is_file():
            return f"(no trajectory at {log})"
        return self.substrate.read_trajectory(log, obs_trunc=obs_trunc, max_chars=8_000_000)

    def deliverable_text(self, run_dir: Path) -> str:
        patch = self._patch(run_dir)
        if len(patch) > _PATCH_CAP:
            patch = patch[:_PATCH_CAP] + f"\n... [diff truncated at {_PATCH_CAP} characters]"
        return patch

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        text = self.deliverable_text(run_dir)
        return {"agent.patch": text} if text.strip() else {}

    def base_harness(self) -> Path:
        return BASE_HARNESS

    def validate_tree(self, tree: dict) -> None:
        return None

    def preflight(self) -> None:
        """Everything a rollout needs, checked before one is paid for."""
        if not config.CLAUDE_MODEL:
            config.require("HARNESS2_CLAUDE_MODEL", needed_by="SWE-bench rollouts")
        if not config.CLAUDE_OAUTH_TOKEN:
            raise config.ConfigError(
                "Set HARNESS2_CLAUDE_OAUTH_TOKEN in .env to the token printed by "
                "`claude setup-token` (Claude Code in the task containers runs on it)"
            )
        if not config.HARBOR_BIN.is_file():
            raise RuntimeError(f"Harbor is not installed at {config.HARBOR_BIN}; run harness2-setup swe")
        if not config.swe_tasks_dir().is_dir():
            raise RuntimeError("SWE-bench Verified tasks are missing; run harness2-setup swe")
        harbor_py = config.HARBOR_BIN.with_name("python")
        r = subprocess.run(
            [str(harbor_py), "-c", f"import {AGENT.split(':')[0]}"],
            env={**os.environ, "PYTHONPATH": str(config.ROOT)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode:
            raise RuntimeError(f"Harbor cannot import {AGENT}: {r.stderr.strip()[-300:]}")
        docker = {**os.environ, **_docker_env()}
        d = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            env=docker,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if d.returncode:
            raise RuntimeError("Docker daemon is not reachable; start Docker (or the Colima profile)")
        c = subprocess.run(
            ["docker", "compose", "version"], env=docker, capture_output=True, text=True, timeout=60
        )
        if c.returncode:
            raise RuntimeError(
                "Harbor needs the `docker compose` plugin; on Homebrew: brew install "
                "docker-compose, then add its cli-plugins dir to ~/.docker/config.json"
            )

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        """The repository's tests, as Harbor's verifier recorded them; None when they never ran."""
        reward = None
        try:
            result = json.loads((Path(run_dir) / "result.json").read_text(encoding="utf-8"))
            reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        except (OSError, ValueError, AttributeError):
            pass
        if reward is None:
            try:
                reward = float((Path(run_dir) / "verifier" / "reward.txt").read_text().strip())
            except (OSError, ValueError):
                return None
        resolved = float(reward) >= 1.0
        return {"n_passed": int(resolved), "n_criteria": 1, "all_pass": resolved, "reward": reward}


__all__ = ["SweAdapter", "SWE_PROMPTS", "make_spec"]
