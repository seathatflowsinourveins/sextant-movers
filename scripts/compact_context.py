#!/usr/bin/env python3
"""Print the session role's compaction context, or check the checked-in wiring.

Source: code.claude.com/docs/en/hooks-guide.md, "Re-inject context after
compaction"; code.claude.com/docs/en/hooks.md, SessionStart and PreCompact;
code.claude.com/docs/en/best-practices.md, "Keep cost and context down";
code.claude.com/docs/en/settings-reference.md, `idleCompaction`. All read
2026-11-09. Hooks "Common input fields", read 2026-11-11: `agent_id` marks a
subagent or in-process teammate; both hook modes stay silent when it is present.
Carried from Sextant's scripts/compact_context.py (see ORIGINS.md).

The state folder is private, so this public repository never names it: the
MOVERS_STATE_DIR variable does, and output shows the variable's name, never its
value. With the variable unset, each hook mode prints one line saying so.

The hook modes print only. They never read a state file's contents, never print
an environment variable's value, make no network call and write no repository
file. A hook failure prints the fixed reminder instead, and both hook modes
always exit 0: PreCompact exit 2 blocks a compaction, and compaction must never
be blocked by this script.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAP = 10_000
ROLES = ("lead", "dispatcher", "lander", "worker")
MODE_SESSION_START = "session-start"
MODE_PRE_COMPACT = "pre-compact"
MODE_CHECK = "--check"

FIXED_REMINDER = (
    "Reminder: read this session's state file before continuing; it is the "
    "context that survives compaction."
)

# The variable that names the private state folder; only its name is ever shown.
STATE_ENV = "MOVERS_STATE_DIR"
UNSET = STATE_ENV + " is unset, so no state file is named."
# Each coordination role has one fixed state file inside the folder.
STATE_PATHS = {
    "lead": Path("MOVERS-LEAD-STATE.md"),
    "dispatcher": Path("MOVERS-DISPATCHER-STATE.md"),
    "lander": Path("MOVERS-LANDER-STATE.md"),
}
WORKER_TASK_DIR = Path("tasks")

EXTRA_KEEP = {
    "lead": (
        "As lead: keep the owner's words quoted in this session, the open task "
        "with its dispatch id, and the decision waiting on the owner."
    ),
    "dispatcher": (
        "As dispatcher: keep each open task id and dispatch id, its worker's "
        "status, and the queue's next dispatch."
    ),
    "lander": (
        "As lander: keep the PR number and exact head SHA, the blocking check "
        "output, and the merge order."
    ),
    "worker": (
        "As worker: keep the task id, dispatch id, branch, HEAD SHA, acceptance "
        "checks and stop condition."
    ),
}


def state_dir() -> Path | None:
    """Return the state folder the variable names, or None when it is unset or empty."""
    value = os.environ.get(STATE_ENV, "")
    return Path(value).expanduser() if value else None


def _shown(relative: Path) -> str:
    return "$" + STATE_ENV + "/" + relative.as_posix()


def _stamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _pointer(role: str, state: Path) -> str:
    if role == "worker":
        return "no state file; task directory is " + _shown(WORKER_TASK_DIR) + "/<task>/"
    path = state / STATE_PATHS[role]
    if not path.is_file():
        return _shown(STATE_PATHS[role]) + " (missing)"
    return _shown(STATE_PATHS[role]) + " (" + _stamp(path) + ")"


def session_start(role: str | None, state: Path | None) -> str:
    if state is None:
        return UNSET
    if role is None:
        lines = [FIXED_REMINDER, "Role: unknown", "State files:"]
        lines.extend("- " + known + ": " + _pointer(known, state) for known in ROLES)
        return "\n".join(lines)
    return "\n".join([FIXED_REMINDER, "Role: " + role, "State file: " + _pointer(role, state)])


def _keep_all() -> str:
    lines = ["Keep, in addition to the shared list in CLAUDE.md:"]
    lines.extend("- " + known + ": " + EXTRA_KEEP[known] for known in ROLES)
    return "\n".join(lines)


def pre_compact(role: str | None, state: Path | None) -> str:
    if state is None:
        return UNSET
    if role is None:
        return _keep_all()
    return EXTRA_KEEP[role]


def _role_from(args: list[str], payload: dict) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--role" and index + 1 < len(args) and args[index + 1] in ROLES:
            return args[index + 1]
        if arg.startswith("--role=") and arg.split("=", 1)[1] in ROLES:
            return arg.split("=", 1)[1]
    agent = payload.get("agent_type")
    if isinstance(agent, str) and agent in ROLES:
        return agent
    return None


def _has_hook(hooks: dict, event: str, matcher: str | None, mode: str) -> bool:
    for entry in hooks.get(event, []):
        if matcher is not None and entry.get("matcher") != matcher:
            continue
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            if "scripts/compact_context.py" in command and mode in command:
                return True
    return False


def _compact_section(text: str) -> list[str]:
    marker = "# Compact instructions"
    if marker not in text:
        return []
    section = text.split(marker, 1)[1].split("\n# ", 1)[0]
    return [line for line in section.splitlines() if line.strip()]


def check(root: Path) -> list[str]:
    problems: list[str] = []
    try:
        section = _compact_section((root / "CLAUDE.md").read_text())
    except OSError as exc:
        problems.append("CLAUDE.md: unreadable (" + str(exc) + ")")
        section = []
    if not section:
        problems.append("CLAUDE.md: missing or empty '# Compact instructions' section")
    elif len(section) > 8:
        problems.append("CLAUDE.md: " + str(len(section)) + " compact lines, at most 8 allowed")

    hooks: dict = {}
    settings: dict = {}
    try:
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        hooks = settings.get("hooks", {})
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(".claude/settings.json: unreadable (" + str(exc) + ")")
    else:
        if settings.get("idleCompaction") is not True:
            problems.append(".claude/settings.json: idleCompaction is not set explicitly to true")
    if not _has_hook(hooks, "SessionStart", "compact", MODE_SESSION_START):
        problems.append(".claude/settings.json: missing the SessionStart compact hook")
    if not _has_hook(hooks, "PreCompact", None, MODE_PRE_COMPACT):
        problems.append(".claude/settings.json: missing the PreCompact hook")

    state = state_dir()
    for role in ROLES:
        for mode, text in (
            (MODE_SESSION_START, session_start(role, state)),
            (MODE_PRE_COMPACT, pre_compact(role, state)),
        ):
            if len(text) > CAP:
                problems.append(mode + " for " + role + ": over " + str(CAP) + " characters")
    return problems


def main(argv: list[str], stdin_text: str, state: Path | None) -> int:
    if not argv or argv[0] not in (MODE_SESSION_START, MODE_PRE_COMPACT, MODE_CHECK):
        print(
            "usage: compact_context.py {session-start|pre-compact|--check} [--role ROLE]",
            file=sys.stderr,
        )
        return 2
    if argv[0] == MODE_CHECK:
        problems = check(ROOT)
        if problems:
            print("\n".join(problems))
            return 1
        print(
            "ok: Compact instructions (at most 8 lines); SessionStart compact hook; "
            "PreCompact hook; idleCompaction explicit; output under "
            + str(CAP)
            + " characters for "
            + ", ".join(ROLES)
        )
        if state is None:
            print(UNSET)
        return 0
    try:
        payload = json.loads(stdin_text)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # A subagent must never inherit a session role's pointer, even via --role.
    if "agent_id" in payload:
        return 0
    role = _role_from(argv[1:], payload)
    try:
        if argv[0] == MODE_SESSION_START:
            text = session_start(role, state)
        else:
            text = pre_compact(role, state)
    except Exception:
        text = FIXED_REMINDER if argv[0] == MODE_SESSION_START else _keep_all()
    print(text)
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        code = main(sys.argv[1:], "" if sys.stdin.isatty() else sys.stdin.read(), state_dir())
    except Exception:
        if mode == MODE_CHECK:
            print("error: the compaction check could not run", file=sys.stderr)
            code = 1
        else:
            print(FIXED_REMINDER if mode != MODE_PRE_COMPACT else _keep_all())
            code = 0
    sys.exit(code)
