#!/usr/bin/env python3
"""Launch a checked-in role definition, or check the four against the policy.

Source: code.claude.com/docs/en/sub-agents.md, "Define agents" and "Use the
agent"; code.claude.com/docs/en/cli-reference.md, `--agent`, `--name` and
`--autocompact`; code.claude.com/docs/en/model-config.md, `autoCompactWindow`;
code.claude.com/docs/en/settings-reference.md, `idleCompaction`. All read
2026-11-09. Carried from Sextant's scripts/role_launch.py (see ORIGINS.md).

Sextant's first-party probes on Claude Code 2.1.322 (2026-11-10) proved the
frontmatter model and `initialPrompt`; they did not observe a frontmatter hook
or a frontmatter effort taking effect. So the definitions carry no hook and no
effort, hooks stay in `.claude/settings.json`, effort comes from the project
`effortLevel`, and this launcher passes no `--model`, `--effort`, `--settings`
or `--advisor`. A `--model` here would hide a definition that drifted from the
policy.

Telemetry: code.claude.com/docs/en/monitoring-usage and
code.claude.com/docs/en/settings-reference#variables-claude-code-ignores-in-env,
read 2026-11-10. Claude Code ignores the OpenTelemetry exporter variables in a
repository's project settings, so every session starts through the one checked-in
process that sets them, `deploy/wsl/lane-runtimes/claude-launch.sh`.

A role's state file lives in the private folder the MOVERS_STATE_DIR variable
names; a definition names it through the variable, and `--check` says so when
the variable is unset.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compact_context import (  # noqa: E402
    ROLES,
    STATE_ENV,
    STATE_PATHS,
    UNSET,
    WORKER_TASK_DIR,
    state_dir,
)

POLICY = ROOT / "deploy/wsl/lane-runtimes/model-policy.json"
SETTINGS = ROOT / ".claude/settings.json"
WRAPPER = "deploy/wsl/lane-runtimes/claude-launch.sh"
AGENT_DIR = ROOT / ".claude" / "agents"
COORDINATION = ("lead", "dispatcher", "lander")
WINDOW = 200_000
KEYS = ("name", "description", "model", "initialPrompt")
POLICY_KEY = {
    "lead": "claude_lead",
    "dispatcher": "claude_roles",
    "lander": "claude_roles",
    "worker": "claude",
}
LAUNCH = {
    "lead": "python3 scripts/role_launch.py lead",
    "dispatcher": "python3 scripts/role_launch.py <dispatcher|lander>",
    "lander": "python3 scripts/role_launch.py <dispatcher|lander>",
    "worker": "python3 scripts/role_launch.py worker",
}
SESSION_NAME = {
    "lead": "lead",
    "dispatcher": "<role>",
    "lander": "<role>",
    "worker": "worker",
}
USAGE = "usage: role_launch.py {--check | --print ROLE | ROLE}"


def read_definition(path: Path) -> tuple[dict[str, str], str]:
    """Return a definition's frontmatter mapping and its body."""
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: no frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path}: unterminated frontmatter")
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"{path}: bad frontmatter line {line!r}")
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, text[end + 5 :]


def state_pointer(role: str) -> str:
    """Name the role's state path through the variable, never through its value."""
    if role == "worker":
        return "$" + STATE_ENV + "/" + WORKER_TASK_DIR.as_posix()
    return "$" + STATE_ENV + "/" + STATE_PATHS[role].as_posix()


def argv(role: str) -> list[str]:
    """Build the launch argv for one role; the definition supplies the model."""
    if role not in ROLES:
        raise KeyError(role)
    built = ["bash", WRAPPER, "--agent", role, "--name", role]
    if role in COORDINATION:
        built += ["--autocompact", str(WINDOW)]
    return built


def check(root: Path = ROOT) -> list[str]:
    """Return every drift between the definitions, the policy and the settings."""
    problems: list[str] = []
    policy = json.loads((root / "deploy/wsl/lane-runtimes/model-policy.json").read_text())
    settings = json.loads((root / ".claude/settings.json").read_text())
    effort = settings.get("effortLevel")
    if settings.get("model") != policy["claude"].get("model"):
        problems.append("settings model is not the policy's Claude workhorse model")
    if "CLAUDE_CODE_EFFORT_LEVEL" in settings.get("env", {}):
        problems.append("settings env pins CLAUDE_CODE_EFFORT_LEVEL over the project effortLevel")
    if policy["claude"].get("effort") != effort:
        problems.append("the project effortLevel is not the policy's Claude worker effort")

    for role in ROLES:
        entry = policy[POLICY_KEY[role]]
        path = root / ".claude" / "agents" / f"{role}.md"
        if not path.is_file():
            problems.append(f"{role}: missing definition {path.relative_to(root)}")
            continue
        try:
            meta, body = read_definition(path)
        except ValueError as error:
            problems.append(f"{role}: {error}")
            continue
        extra = sorted(set(meta) - set(KEYS))
        if extra:
            problems.append(f"{role}: unprobed frontmatter keys {extra}")
        missing = sorted(set(KEYS) - set(meta))
        if missing:
            problems.append(f"{role}: missing frontmatter keys {missing}")
        if meta.get("name") != role:
            problems.append(f"{role}: name is {meta.get('name')!r}")
        if meta.get("model") != entry.get("model"):
            problems.append(f"{role}: model is not the policy's {entry.get('model')}")
        if body.strip():
            problems.append(f"{role}: body is not empty")
        pointer = state_pointer(role)
        if pointer not in meta.get("initialPrompt", ""):
            problems.append(f"{role}: initialPrompt does not name {pointer}")
        if "READY" not in meta.get("initialPrompt", ""):
            problems.append(f"{role}: initialPrompt does not ask for READY")
        if entry.get("effort") != effort:
            problems.append(f"{role}: policy effort is not the project effortLevel")
        if entry.get("session_name") != SESSION_NAME[role]:
            problems.append(f"{role}: policy session_name is not {SESSION_NAME[role]!r}")
        if entry.get("launch") != LAUNCH[role]:
            problems.append(f"{role}: policy launch is not {LAUNCH[role]!r}")
        if entry.get("advisor") is not None:
            problems.append(f"{role}: policy records an advisor the probes did not verify")
        if role in COORDINATION:
            if entry.get("autocompact_window") != WINDOW:
                problems.append(f"{role}: policy window is not {WINDOW}")
            if entry.get("autocompact_window_calibration") != "UNCALIBRATED":
                problems.append(f"{role}: policy window is not marked UNCALIBRATED")
            if "model-config" not in entry.get("autocompact_window_source", ""):
                problems.append(f"{role}: policy window source does not name model-config")
        elif entry.get("autocompact_window") is not None:
            problems.append(f"{role}: policy gives a window to a role that keeps the default")
    return problems


def main(args: list[str]) -> int:
    if args == ["--check"]:
        problems = check(ROOT)
        if problems:
            for problem in problems:
                print(f"role_launch: {problem}", file=sys.stderr)
            return 1
        print(
            f"ok: {len(ROLES)} role definitions match model-policy.json and .claude/settings.json"
        )
        if state_dir() is None:
            print(UNSET)
        return 0
    if len(args) == 2 and args[0] == "--print" and args[1] in ROLES:
        print(shlex.join(argv(args[1])))
        return 0
    if len(args) == 1 and args[0] in ROLES:
        os.chdir(ROOT)
        built = argv(args[0])
        os.execvp(built[0], built)  # noqa: S606 - the launcher is the process
        return 1
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
