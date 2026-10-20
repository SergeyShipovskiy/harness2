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

"""ClaudeSubstrate: Claude Code (`claude -p`) as a solver substrate.

Carries the c1..c8 surface (CLAUDE_SURFACE) in Claude Code's native layout, one install
mode (claude_root: CLAUDE.md, .claude/ and scripts/ copied to the workspace root, with
sub_agents/<name>/AGENT.md installed as .claude/agents/<name>.md), the `claude -p` flag
contract, and the stream-json trajectory renderer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping

from .. import config
from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from .base import Install, Launcher, RunRequest

CLAUDE_SURFACE = SurfaceSpec(
    name="claude",
    always_on=("CLAUDE.md",),
    always_on_max=8_000,
    always_on_cap_reason="CLAUDE.md is loaded whole into the context of EVERY session",
    skill_dir=".claude/skills",
    skill_name_rule="must_match",
    trigger_hint=(
        'Fix it by adding a trigger line naming `{name}` to CLAUDE.md, e.g. "When '
        "<the situation this skill handles>, use the {name} skill "
        '(.claude/skills/{name}/SKILL.md)."'
    ),
    autotrigger_file="CLAUDE.md",
    trigger_line="- When {desc}, use the {name} skill (.claude/skills/{name}/SKILL.md).\n",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=".claude/settings.json",
    hook_events=frozenset(
        {
            "PreToolUse",
            "PostToolUse",
            "Notification",
            "UserPromptSubmit",
            "Stop",
            "SubagentStop",
            "PreCompact",
            "SessionStart",
            "SessionEnd",
        }
    ),
    compile_skill_scripts=True,
    reject_empty_always_on=True,
    stubs=(
        (
            "scripts/README.md",
            "<!-- c7: deterministic helpers, run as `python3 scripts/<name>.py`. "
            "Add one by emitting scripts/<name>.py. -->\n",
        ),
        (
            "sub_agents/README.md",
            "<!-- c5: delegated Claude Code subagents, each run in its OWN context. Add one as "
            "sub_agents/<name>/AGENT.md with frontmatter `description:`; the runner installs it "
            "as .claude/agents/<name>.md. -->\n",
        ),
        (
            ".claude/README.md",
            "<!-- c8: Claude Code lifecycle hooks -- the only lever that acts INSIDE the tool "
            "loop. Add ONE file, .claude/settings.json, holding only a hooks object: "
            '{"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", '
            '"command": "..."}]}]}}. A PostToolUse command that prints '
            '{"decision":"block","reason":"..."} feeds the reason back to the model. -->\n',
        ),
        (
            ".claude/skills/README.md",
            "<!-- c6: reusable workflows. Add one as .claude/skills/<name>/SKILL.md with YAML "
            "frontmatter `name: <name>` (equal to the directory) and `description:`. Only "
            "name+description are advertised to the agent; the body is read on demand. -->\n",
        ),
    ),
)


CLAUDE_ROOT_ENTRIES = ("CLAUDE.md", ".claude", "scripts")


def _install_subagents(tree_dir: Path, staged: Path) -> None:
    """sub_agents/<name>/AGENT.md -> .claude/agents/<name>.md, with `name:` set to <name>."""
    for agent_md in sorted((tree_dir / "sub_agents").glob("*/AGENT.md")):
        name = agent_md.parent.name
        body = agent_md.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\n(.*?)\n---\n?", body, re.S)
        if m:
            front = re.sub(r"^name:.*\n?", "", m.group(1) + "\n", flags=re.M).rstrip("\n")
            body = f"---\nname: {name}\n{front}\n---\n{body[m.end():]}"
        dest = staged / ".claude" / "agents" / f"{name}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")


class ClaudeSubstrate:
    """Claude Code behind the one Substrate interface."""

    name = "cc"

    def __init__(self, launcher: Launcher | None = None):
        self._launcher = launcher

    def surface(self) -> SurfaceSpec:
        return CLAUDE_SURFACE

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        """Stage the claude_root entries for a copy to the workspace root."""
        if spec.install_mode != "claude_root":
            raise ValueError(
                f"install_mode {spec.install_mode!r} is not the Claude Code mode; this "
                f"substrate has exactly one: 'claude_root'"
            )
        tree_dir = Path(tree_dir)
        if not HT.is_tree(CLAUDE_SURFACE, tree_dir):
            raise ValueError(f"{tree_dir} is not a Claude Code harness tree (no CLAUDE.md)")
        staged = Path(tempfile.mkdtemp(prefix="adapt-cc-root-"))
        for entry in CLAUDE_ROOT_ENTRIES:
            src = tree_dir / entry
            if not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, staged / entry, ignore=shutil.ignore_patterns("README.md"))
            else:
                shutil.copy2(src, staged / entry)
        _install_subagents(tree_dir, staged)
        return Install(
            staging_dir=staged,
            fingerprint=RM.fingerprint(tree_dir),
            extras={
                "install_mode": "claude_root",
                **{k: v for k, v in ctx.items() if not k.startswith("file:")},
            },
        )

    def exec_argv(self, model: str, *, effort: str = "high") -> list[str]:
        """`claude -p` reading the prompt from stdin and streaming JSON events.

        Project settings stay loaded so the harness's .claude/settings.json hooks fire.
        """
        return [
            "claude",
            "-p",
            "--model",
            model,
            "--effort",
            effort,
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]

    def exec_env(self, base_env: Mapping[str, str]) -> dict[str, str]:
        """The executor's own Claude Code profile, never the user's ~/.claude."""
        env = {
            k: v
            for k, v in base_env.items()
            if k
            not in (
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
                "CLAUDE_CODE_USE_VERTEX",
                "CLAUDE_CODE_USE_BEDROCK",
                "ANTHROPIC_MODEL",
            )
        }
        env["CLAUDE_CONFIG_DIR"] = str(config.CLAUDE_CONFIG_DIR)
        return env

    def execute(
        self, task_ref: str, install: Install, run_ids: list[str], timeout: int
    ) -> list[Path]:
        if self._launcher is None:
            raise RuntimeError(
                "ClaudeSubstrate was constructed without a launcher body; the benchmark "
                "adapter must supply one"
            )
        return self._launcher(
            RunRequest(task_ref=task_ref, run_ids=tuple(run_ids), timeout=int(timeout)), install
        )

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000
    ) -> str:
        return render_events(
            Path(stream_path), obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def preflight(self) -> None:
        """The claude CLI is on PATH and the executor profile holds a claude.ai login."""
        if not config.CLAUDE_MODEL:
            config.require("HARNESS2_CLAUDE_MODEL", needed_by="Claude Code rollouts")
        if not shutil.which("claude"):
            raise RuntimeError("claude CLI not found on PATH; install Claude Code")
        login = f"CLAUDE_CONFIG_DIR={config.CLAUDE_CONFIG_DIR} claude auth login --claudeai"
        try:
            r = subprocess.run(
                ["claude", "auth", "status"],
                env=self.exec_env(os.environ),
                capture_output=True,
                text=True,
                timeout=30,
            )
            status = json.loads(r.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise RuntimeError(f"claude auth status failed ({type(exc).__name__})") from exc
        if not status.get("loggedIn"):
            raise config.ConfigError(f"The Claude Code profile has no login; run: {login}")


_TRUNC_FMT = "…(+{n} chars — render truncation)"


def _clip(s, n: int) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\x00", "")
    if n <= 0 or len(s) <= n:
        return s
    return s[:n] + _TRUNC_FMT.format(n=len(s) - n)


def _tool_input(name: str, payload) -> str:
    if isinstance(payload, dict):
        for key in ("command", "file_path", "pattern", "prompt", "url"):
            if isinstance(payload.get(key), str):
                rest = {k: v for k, v in payload.items() if k != key}
                tail = f" {json.dumps(rest, ensure_ascii=False)}" if rest else ""
                return f"{payload[key]}{tail}"
        return json.dumps(payload, ensure_ascii=False)
    return str(payload)


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text", "")))
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("(image)")
        return "\n".join(parts)
    return "" if content is None else str(content)


def render_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000) -> str:
    """Render a `claude -p --output-format stream-json --verbose` capture into the shared grammar."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace").replace("\x00", "")
    except OSError as e:
        return f"(failed to read trajectory: {e})"
    lines: list[str] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        et = ev.get("type")
        content = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
        if et == "assistant" and isinstance(content, list):
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and str(c.get("text", "")).strip():
                    lines.append(f"[ASSISTANT] {_clip(c['text'], obs_trunc)}")
                elif c.get("type") == "thinking" and str(c.get("thinking", "")).strip():
                    lines.append(f"[REASONING] {_clip(c['thinking'], obs_trunc)}")
                elif c.get("type") == "tool_use":
                    name = c.get("name") or "tool"
                    lines.append(
                        f"[ACTION] {name}: {_clip(_tool_input(name, c.get('input')), args_trunc)}"
                    )
        elif et == "user":
            if isinstance(content, str) and content.strip():
                lines.append(f"[USER] {_clip(content, obs_trunc)}")
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        tag = "OBSERVATION ERROR" if c.get("is_error") else "OBSERVATION"
                        lines.append(f"[{tag}] {_clip(_result_text(c.get('content')), obs_trunc)}")
                    elif isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                        lines.append(f"[USER] {_clip(c['text'], obs_trunc)}")
        elif et == "result":
            usage = ev.get("usage") or {}
            lines.append(
                "[HARNESS] result {s} | turns={t} | tokens in={i} cache_read={c} "
                "cache_write={w} out={o}".format(
                    s=ev.get("subtype"),
                    t=ev.get("num_turns"),
                    i=usage.get("input_tokens"),
                    c=usage.get("cache_read_input_tokens"),
                    w=usage.get("cache_creation_input_tokens"),
                    o=usage.get("output_tokens"),
                )
            )
            if ev.get("is_error"):
                lines.append(f"[ERROR] {_clip(ev.get('result') or ev.get('subtype'), 1000)}")
        elif et == "system" and ev.get("subtype") not in (None, "init"):
            lines.append(f"[HARNESS] {ev.get('subtype')}: {_clip(json.dumps(ev)[:500], 500)}")
    rendered = "\n".join(lines).strip()
    if not rendered:
        return "(no parseable events in trajectory)"
    if len(rendered) > max_chars:
        dropped = len(rendered) - max_chars
        rendered = rendered[:max_chars] + "\n" + _TRUNC_FMT.format(n=dropped)
    return rendered


__all__ = ["ClaudeSubstrate", "CLAUDE_SURFACE", "render_events"]
