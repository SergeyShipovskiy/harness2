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

"""Harbor's Claude Code agent with a Harness² harness installed at the repository root.

Harbor's own interpreter imports this module (`harbor trials start -a <module>:<class>`),
so it depends on Harbor and the standard library only.
"""

from __future__ import annotations

import shlex

from pydantic import Field

from harbor.agents.installed.claude_code import ClaudeCode, ClaudeCodeOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_STAGE = "/tmp/harness2-harness"
_STATE = "/tmp/harness2-state"

# Copy the harness into the repository without overwriting anything the repository owns
# (django has its own scripts/), and keep the copied paths out of git's view.
_INSTALL = """
set -eu
cd {workdir}
mkdir -p {state}
git rev-parse HEAD > {state}/base
cp .git/info/exclude {state}/exclude 2>/dev/null || : > {state}/exclude
: > {state}/installed
cd {stage}
find . -type f | sed 's|^\\./||' | sort | while IFS= read -r rel; do
  if [ -e "{workdir}/$rel" ]; then
    echo "$rel" >> {logs}/harness-skipped.txt
    continue
  fi
  mkdir -p "{workdir}/$(dirname "$rel")"
  cp "$rel" "{workdir}/$rel"
  echo "$rel" >> {state}/installed
  echo "/$rel" >> {workdir}/.git/info/exclude
done
cp {state}/installed {logs}/harness-installed.txt
"""

# Take the harness back out before the verifier runs, restore git's exclude file, and
# record the agent's change against the starting commit as the rollout's deliverable.
_FINISH = """
set -u
cd {workdir}
if [ -f {state}/installed ]; then
  while IFS= read -r rel; do
    rm -f "{workdir}/$rel"
    d=$(dirname "$rel")
    while [ "$d" != "." ] && rmdir "{workdir}/$d" 2>/dev/null; do d=$(dirname "$d"); done
  done < {state}/installed
  cp {state}/exclude .git/info/exclude
fi
base=$(cat {state}/base 2>/dev/null || git rev-parse HEAD)
git add -A -N . 2>/dev/null
git diff --binary "$base" > {logs}/agent.patch
git reset -q
"""


class HarnessClaudeCodeOptions(ClaudeCodeOptions):
    harness_dir: str | None = Field(
        default=None, description="Host directory holding the materialised harness."
    )
    workdir: str = Field(default="/testbed", description="Repository root in the task image.")


class HarnessClaudeCode(ClaudeCode):
    options_model = HarnessClaudeCodeOptions

    @staticmethod
    def name() -> str:
        return "harness2-claude-code"

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        opts = self.options
        fmt = {
            "workdir": shlex.quote(opts.workdir),
            "stage": _STAGE,
            "state": _STATE,
            "logs": shlex.quote(self.environment_logs_dir.as_posix()),
        }
        if opts.harness_dir:
            await environment.upload_dir(opts.harness_dir, _STAGE)
            await self.exec_as_root(environment, command=_INSTALL.format(**fmt))
        try:
            await super().run(instruction, environment, context)
        finally:
            await self.exec_as_root(environment, command=_FINISH.format(**fmt))
