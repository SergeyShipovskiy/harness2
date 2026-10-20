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

"""Benchmark dependency and runtime readiness checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import config
from .install import PATCHES, missing_runners
from .substrate.opencode import OpencodeSubstrate


def check(adapter, *, judge=False) -> list[str]:
    missing = missing_runners()
    if missing:
        return [missing]
    errors = []

    def probe(label, cmd, *, cwd=None, remedy="run harness2-setup for this benchmark"):
        try:
            r = subprocess.run(
                [str(c) for c in cmd], cwd=cwd, capture_output=True, text=True, timeout=45
            )
            if r.returncode:
                errors.append(f"{label} failed; {remedy}")
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{label}: {type(exc).__name__}; see README.md")
            return False

    opencode = config.needs_opencode(adapter.substrate.name)
    for validate in (
        lambda: config.validate_models(judge=judge, substrate=adapter.substrate.name),
        adapter.tasks,
        *((OpencodeSubstrate().preflight,) if opencode else ()),
        adapter.preflight,
    ):
        try:
            validate()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(str(exc))

    bench = adapter.spec.name.split("-", 1)[0]
    name = {"jb": "job-bench-eval", "lab": "harvey-labs", "wb": "workbuddy-bench"}[bench]
    repo = config.DEPENDENCIES / name
    for dependency in (name, "opencode") if opencode else (name,):
        target = config.DEPENDENCIES / dependency
        if (target / ".git").exists():
            try:
                head = subprocess.check_output(
                    ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
                ).strip()
                if head != config.DEPENDENCY_PINS[dependency]:
                    errors.append(f"{dependency}: checkout differs from the supported pin")
            except subprocess.CalledProcessError:
                errors.append(f"{dependency}: cannot read checkout revision")
        patch = config.THIRD_PARTY / "patches" / PATCHES[dependency]
        probe(
            f"{dependency} patch",
            ["git", "-C", target, "apply", "--reverse", "--check", patch],
        )
    if opencode and not (config.OPENCODE_DIR / "node_modules").is_dir():
        errors.append(f"OpenCode dependencies missing; run harness2-setup {bench}")

    if config.DEFAULT_PROPOSER_CHANNEL == "claude_cli" and config.CLAUDE_AUTH == "subscription":
        claude_login_errors(errors)
    if config.DEFAULT_PROPOSER_CHANNEL == "codex_cli":
        codex_login_errors(
            errors,
            Path(config.CODEX_DIST) / "codex",
            config.CODEX_HOME_DIR,
            "improver",
            f"harness2-setup {bench}",
        )
    vertex_judge = not (bench == "jb" and config.CODEX_JUDGE) and (
        bench == "wb" or config.JUDGE_MODEL.startswith(("claude", "gemini"))
    )
    if any(
        m.startswith("google-vertex") for m in (config.SOLVER_MODEL_OC, config.IMPROVER_MODEL)
    ) or (judge and vertex_judge):
        probe(
            "Google Application Default Credentials",
            ["gcloud", "auth", "application-default", "print-access-token"],
        )
    if bench == "lab":
        for binary in ("pandoc", "pdftoppm"):
            if not shutil.which(binary):
                errors.append(f"{binary} missing; install pandoc and poppler-utils (README.md)")
        probe(
            "LAB document and judge dependencies",
            [config.LAB_PYTHON, "-c", "import evaluation.scoring; import google.auth, anthropic"],
            cwd=repo,
        )
    elif bench == "jb" and judge:
        probe(
            "JobBench document and judge dependencies",
            [
                config.JB_JUDGE_PYTHON,
                "-c",
                "import pandas, openpyxl, docx, mammoth, pdfplumber, pptx, openai",
            ],
        )
        if config.CODEX_JUDGE:
            codex_login_errors(
                errors, config.JUDGE_CODEX_BIN, config.JUDGE_CODEX_HOME, "judge", "harness2-setup jb"
            )
        elif not config.JB_JUDGE_API_BASE:
            errors.append(
                "Set GOOGLE_CLOUD_PROJECT or HARNESS2_JUDGE_BASE_URL for JobBench judging"
            )
        elif (
            config.JUDGE_MODEL
            and not config.JB_JUDGE_API_KEY
            and not config.JUDGE_MODEL.startswith("gemini")
        ):
            errors.append(
                "A non-Gemini JobBench judge requires HARNESS2_JUDGE_BASE_URL and HARNESS2_JUDGE_API_KEY"
            )
    elif bench == "wb":
        probe("Docker daemon", ["docker", "info", "--format", "{{.ServerVersion}}"])
        if adapter.spec.name == "wb-office":
            probe(
                "WorkBuddy document rendering",
                [config.WB_RENDER_PY, "-c", "import openpyxl, pptx"],
                remedy="install 'harness2[render]' in the main environment",
            )
        probe(
            "WorkBuddy runtime",
            [
                repo / ".venv/bin/python",
                "-c",
                "import harbor; from workbuddy_bench.runner.harness_adapters import HARNESS_ADAPTERS; "
                "assert all(k in HARNESS_ADAPTERS for k in ('opencode', 'oc-harness', 'codex-harness'))",
            ],
            cwd=repo,
        )
        for role, expected in (
            ("solver", config.SOLVER_MODEL_OC.removeprefix("google-vertex/")),
            ("judge", config.JUDGE_MODEL),
        ):
            path = repo / "configs/models/harness2" / f"{role}.yaml"
            try:
                if json.loads(path.read_text())["model"]["name"] != "google/" + expected:
                    errors.append(f"WorkBuddy {role} configuration is stale; rerun setup")
            except (OSError, ValueError, KeyError, TypeError):
                errors.append(f"WorkBuddy {role} configuration missing; rerun setup")
    return errors


def claude_login_errors(errors: list[str]) -> None:
    """The improver's Claude Code profile holds a claude.ai login."""
    remedy = f"run once: CLAUDE_CONFIG_DIR={config.CLAUDE_CONFIG_DIR} claude, then /login"
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config.CLAUDE_CONFIG_DIR)}
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_VERTEX"):
        env.pop(k, None)
    try:
        r = subprocess.run(
            ["claude", "auth", "status"], env=env, capture_output=True, text=True, timeout=30
        )
        status = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"claude auth status failed ({type(exc).__name__}); {remedy}")
        return
    if not status.get("loggedIn"):
        errors.append(f"The improver's Claude profile has no claude.ai login; {remedy}")


def codex_login_errors(errors: list[str], binary: Path, home: Path, who: str, setup: str) -> None:
    """A Codex install exists at `binary` and its CODEX_HOME holds a ChatGPT login."""
    from .install import codex_login_command

    if not binary.is_file():
        errors.append(f"The {who}'s Codex is not installed at {binary}; run {setup}")
        return
    try:
        r = subprocess.run(
            [str(binary), "login", "status"],
            env={**os.environ, "CODEX_HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"{who} codex login status failed ({type(exc).__name__})")
        return
    if r.returncode or "ChatGPT" not in r.stdout + r.stderr:
        errors.append(
            f"The {who}'s Codex has no ChatGPT login; run: {codex_login_command(binary, home)}"
        )


def require_ready(adapter, *, judge=False):
    """Raise before a caller spends anything when this benchmark is not ready to run.

    `judge=True` also demands what grading will need: `HARNESS2_JUDGE_MODEL` set, and,
    where a benchmark grades in its own environment, that environment installed and
    pointed at an endpoint. Those are offline presence and import checks — they catch an
    unset or unbuilt judge, not an account that cannot serve the judge model, which is
    still first learned when the judge actually calls it.
    """
    errors = check(adapter, judge=judge)
    if errors:
        raise config.ConfigError(
            "Setup incomplete; nothing has run yet:\n  - " + "\n  - ".join(errors)
        )
