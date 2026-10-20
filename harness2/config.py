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

"""Deployment settings and dependency versions. Shell values override .env."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent if (PKG.parent / "pyproject.toml").is_file() else Path.cwd()
THIRD_PARTY = ROOT / "third_party"
METHOD_VERSION = 2

DEPENDENCIES = ROOT / "dependencies"


class ConfigError(RuntimeError):
    """A required deployment value is missing; the message carries the remediation."""


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from `path` into os.environ with setdefault (the shell wins).

    Accepts blank lines, `#` comments, an optional `export ` prefix, and single- or
    double-quoted values. A missing or unreadable file is not an error.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not all(c.isalnum() or c == "_" for c in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


load_dotenv()


def env(name: str, default=None):
    """Env override where a set-but-empty variable is the same as an unset one."""
    return os.environ.get(name) or default


def env_int(name: str, default: int) -> int:
    v = env(name)
    if v is None:
        return int(default)
    try:
        return int(v)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {v!r}") from e


def require(name: str, *, needed_by: str) -> str:
    """The value of `name`, or ConfigError with the remediation line when it is unset/empty."""
    v = env(name)
    if not v:
        raise ConfigError(
            f"{name} is required for {needed_by}; set it in .env or the shell (see .env.example)"
        )
    return str(v)


RUNS = Path(env("HARNESS2_RUNS_DIR", ROOT / "runs")).expanduser().resolve()
VERTEX_PROJECT = env("GOOGLE_CLOUD_PROJECT", env("HARNESS2_VERTEX_PROJECT", ""))
VERTEX_REGION = env("GOOGLE_CLOUD_LOCATION", "global")
if VERTEX_PROJECT:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", VERTEX_PROJECT)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", VERTEX_REGION)


TOOLS = ("opencode", "codex", "claude")
SUBSTRATE_OF_TOOL = {"opencode": "oc", "codex": "cx", "claude": "cc"}
CHANNEL_OF_TOOL = {"opencode": "opencode", "codex": "codex_cli", "claude": "claude_cli"}


def _role(name: str) -> tuple[str, str]:
    """`<tool>:<model>` from one role variable, e.g. HARNESS2_EXECUTOR=claude:claude-opus-5-5.

    Swapping two role lines swaps who executes and who improves; ("", "") when unset.
    """
    raw = env(name, "")
    if not raw:
        return "", ""
    tool, _, model = raw.partition(":")
    if tool not in TOOLS or not model:
        raise ConfigError(f"{name} must be <tool>:<model> with tool in {TOOLS}, got {raw!r}")
    return tool, model


EXECUTOR_TOOL, _EXECUTOR_MODEL = _role("HARNESS2_EXECUTOR")
IMPROVER_TOOL, _IMPROVER_MODEL = _role("HARNESS2_IMPROVER")
JUDGE_TOOL, _JUDGE_MODEL = _role("HARNESS2_JUDGE")
DEFAULT_SUBSTRATE = SUBSTRATE_OF_TOOL.get(EXECUTOR_TOOL, "oc")


def _for(tool: str, role_tool: str, role_model: str) -> str:
    return role_model if role_tool == tool else ""


SOLVER_MODEL_OC = env("HARNESS2_SOLVER_MODEL", _for("opencode", EXECUTOR_TOOL, _EXECUTOR_MODEL))
SOLVER_MODEL_CX = env(
    "HARNESS2_CODEX_MODEL",
    _for("codex", EXECUTOR_TOOL, _EXECUTOR_MODEL) or SOLVER_MODEL_OC.split("/", 1)[-1],
)
CLAUDE_MODEL = env("HARNESS2_CLAUDE_MODEL", _for("claude", EXECUTOR_TOOL, _EXECUTOR_MODEL))
CLAUDE_EFFORT = env("HARNESS2_CLAUDE_EFFORT", "high")
IMPROVER_MODEL = env("HARNESS2_IMPROVER_MODEL", _IMPROVER_MODEL or SOLVER_MODEL_OC)
JUDGE_MODEL = env("HARNESS2_JUDGE_MODEL", _JUDGE_MODEL)
# "openai": an OpenAI-compatible endpoint (Vertex by default), one call per rubric.
# "codex": `codex exec` on the judge's own ChatGPT login, all rubrics in one call (JobBench only).
JUDGE_BACKEND = env("HARNESS2_JUDGE_BACKEND", "codex" if JUDGE_TOOL == "codex" else "openai")
CODEX_JUDGE = JUDGE_BACKEND == "codex"

DEPENDENCY_PINS = {
    "job-bench-eval": "fa03a17f9ff662f136798a274e10011c3aa00fd0",
    "workbuddy-bench": "b516950be5b56eb3be406c2f76ee1c5111dcb57f",
    "harvey-labs": "b58a28ae9fde5ee4b810e53b4fd0a1915325a35d",
    "opencode": "23fb5e0516c99ac04a1aa46c193efda2e1b9bb24",
    "harbor": "b53b8134e1241686dca7759af188f987ecc48e8b",
    "harbor-datasets": "86723674f04e4209ac479d0fb75d9d9f44b4377e",
}
DEPENDENCY_ORIGINS = {
    "job-bench-eval": "https://github.com/Job-Bench/job-bench-eval.git",
    "workbuddy-bench": "https://github.com/Tencent/workbuddy-bench.git",
    "harvey-labs": "https://github.com/harveyai/harvey-labs.git",
    "opencode": "https://github.com/anomalyco/opencode.git",
    "harbor": "https://github.com/harbor-framework/harbor.git",
    "harbor-datasets": "https://github.com/laude-institute/harbor-datasets.git",
}


def jb_repo() -> Path:
    return DEPENDENCIES / "job-bench-eval"


def wb_repo() -> Path:
    return DEPENDENCIES / "workbuddy-bench"


def lab_repo() -> Path:
    return DEPENDENCIES / "harvey-labs"


def swe_tasks_dir() -> Path:
    return DEPENDENCIES / "harbor-datasets" / "datasets" / "swebench-verified"


HARBOR_BIN = DEPENDENCIES / "harbor" / ".venv" / "bin" / "harbor"
# A long-lived claude.ai token from `claude setup-token`: Claude Code inside the task
# containers cannot read the host profile's keychain login. Keep it in .env only.
CLAUDE_OAUTH_TOKEN = env("HARNESS2_CLAUDE_OAUTH_TOKEN", "")
# The Docker daemon for SWE-bench containers, e.g. a Colima profile of its own
# (unix:///Users/<you>/.colima/swebench/docker.sock); empty means the default context.
SWE_DOCKER_HOST = env("HARNESS2_SWE_DOCKER_HOST", "")


OPENCODE_DIR = DEPENDENCIES / "opencode"
BUN = DEPENDENCIES / "tools" / "node_modules" / ".bin" / "bun"
LAB_PYTHON = lab_repo() / ".venv" / "bin" / "python"
LAB_JUDGE_MODEL = (
    "vertex/" + JUDGE_MODEL if JUDGE_MODEL.startswith(("claude", "gemini")) else JUDGE_MODEL
)
LAB_DELIV_CAP = 60000
JB_JUDGE_PYTHON = jb_repo() / ".venv" / "bin" / "python"
JB_JUDGE_MODEL = (
    "google/" + JUDGE_MODEL
    if JUDGE_MODEL.startswith("gemini") and not env("HARNESS2_JUDGE_BASE_URL") and not CODEX_JUDGE
    else JUDGE_MODEL
)
_vertex_host = (
    "aiplatform.googleapis.com"
    if VERTEX_REGION == "global"
    else f"{VERTEX_REGION}-aiplatform.googleapis.com"
)
VERTEX_OPENAI_URL = (
    f"https://{_vertex_host}/v1/projects/{VERTEX_PROJECT}"
    f"/locations/{VERTEX_REGION}/endpoints/openapi"
)
JB_JUDGE_API_BASE = (
    "codex"
    if CODEX_JUDGE
    else env("HARNESS2_JUDGE_BASE_URL", VERTEX_OPENAI_URL if VERTEX_PROJECT else "")
)
JB_JUDGE_API_KEY = env("HARNESS2_JUDGE_API_KEY", "")
JB_JUDGE_REASONING_EFFORT = "low"
JB_REPORTING_JUDGE_DETAILS = ""
JB_OBS_TRUNC = 50000

# Newer ChatGPT models refuse older CLIs ("requires a newer version of Codex"). WorkBuddy's
# codex-harness ships a version file for the default only.
CODEX_VERSION = env("HARNESS2_CODEX_VERSION", "0.144.6")
CODEX_DIST = str(DEPENDENCIES / "codex" / "dist")
CODEX_BASE_URL = env("HARNESS2_CODEX_BASE_URL", "")
CODEX_CONTAINER_URL = env("HARNESS2_CODEX_CONTAINER_URL", CODEX_BASE_URL)
VERTEX_PROXY_KEY = env("HARNESS2_CODEX_API_KEY", "")
CODEX_HOME_DIR = DEPENDENCIES / "codex" / "home"
# "api": the Responses endpoint above. "chatgpt": a ChatGPT subscription login made once
# with `CODEX_HOME=<CODEX_HOME_DIR> codex login`; a session of its own, so refreshing it
# never invalidates the user's ~/.codex login.
CODEX_AUTH = env(
    "HARNESS2_CODEX_AUTH", "chatgpt" if "codex" in (EXECUTOR_TOOL, IMPROVER_TOOL) else "api"
)
CODEX_CHATGPT = CODEX_AUTH == "chatgpt"
# The judge's Codex is installed and logged in apart from the executor's: upgrading the
# executor's CLI must not change how runs are scored, and the judge never shares a
# CODEX_HOME with rollouts.
JUDGE_CODEX_VERSION = env("HARNESS2_JUDGE_CODEX_VERSION", CODEX_VERSION)
JUDGE_CODEX_ROOT = DEPENDENCIES / "codex-judge"
JUDGE_CODEX_HOME = JUDGE_CODEX_ROOT / "home"
JUDGE_CODEX_BIN = JUDGE_CODEX_ROOT / "dist" / "codex"

WB_SOLVER_MODEL = "harness2/solver"
WB_WEB_JUDGE = "harness2/judge"
WB_RENDER_PY = sys.executable
WB_OC_HARNESS_VER = "1.14.18"
WB_CX_HARNESS_VER = CODEX_VERSION
WB_MAX_JOBS = {"oc": 1, "cx": 1}
WB_MAX_TRIALS = {"oc": 2, "cx": 2}
WB_MODEL_CONNECTION = "local_proxy"

DEFAULT_PROPOSER_CHANNEL = env(
    "HARNESS2_IMPROVER_CHANNEL", CHANNEL_OF_TOOL.get(IMPROVER_TOOL, "opencode")
)
PROPOSER_MODEL_BY_CHANNEL = {
    "opencode": IMPROVER_MODEL,
    "claude_cli": IMPROVER_MODEL,
    "codex_cli": IMPROVER_MODEL,
}
PHASE_WIDTH = 2
TRANSPORT_ATTEMPTS = 2
PROPOSER_CONCURRENCY = 2

# How the claude_cli channel authenticates: "vertex", or "subscription" -- a claude.ai login
# made once with `CLAUDE_CONFIG_DIR=<CLAUDE_CONFIG_DIR> claude`. The separate profile keeps
# the user's own CLAUDE.md, hooks, skills and MCP servers out of the improver's sessions.
CLAUDE_AUTH = env("HARNESS2_CLAUDE_AUTH", "subscription" if IMPROVER_TOOL == "claude" else "vertex")
# The Claude Code profile for both roles: the executor always runs on its claude.ai login.
CLAUDE_CONFIG_DIR = DEPENDENCIES / "claude" / "home"


def needs_opencode(substrate: str) -> bool:
    """OpenCode is installed and checked only when it executes or improves."""
    return substrate == "oc" or DEFAULT_PROPOSER_CHANNEL == "opencode"


def validate_models(*, judge: bool = False, substrate: str = "oc") -> None:
    if substrate == "oc":
        require("HARNESS2_SOLVER_MODEL", needed_by="executor rollouts")
        if "/" not in SOLVER_MODEL_OC:
            raise ConfigError("The executor model must use OpenCode's provider/model syntax")
    elif substrate == "cx" and not SOLVER_MODEL_CX:
        require("HARNESS2_CODEX_MODEL", needed_by="Codex rollouts")
    elif substrate == "cc" and not CLAUDE_MODEL:
        require("HARNESS2_CLAUDE_MODEL", needed_by="Claude Code rollouts")
    if DEFAULT_PROPOSER_CHANNEL == "opencode" and "/" not in IMPROVER_MODEL:
        raise ConfigError("The improver model must use OpenCode's provider/model syntax")
    if DEFAULT_PROPOSER_CHANNEL in ("claude_cli", "codex_cli") and "/" in IMPROVER_MODEL:
        raise ConfigError(f"The {DEFAULT_PROPOSER_CHANNEL} improver takes a bare model id")
    if DEFAULT_PROPOSER_CHANNEL == "codex_cli" and not CODEX_CHATGPT:
        raise ConfigError("The codex_cli improver runs on a ChatGPT login: HARNESS2_CODEX_AUTH=chatgpt")
    vertex_improver = DEFAULT_PROPOSER_CHANNEL == "opencode" or CLAUDE_AUTH == "vertex"
    if SOLVER_MODEL_OC.startswith("google-vertex") or (
        vertex_improver and IMPROVER_MODEL.startswith("google-vertex")
    ):
        if not VERTEX_PROJECT:
            raise ConfigError("Set GOOGLE_CLOUD_PROJECT for Vertex AI")
    if judge and not JUDGE_MODEL:
        require("HARNESS2_JUDGE_MODEL", needed_by="benchmark judging")
