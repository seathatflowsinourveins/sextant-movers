"""Proof: compaction keeps the session's own context without a stale summary.

Source: code.claude.com/docs/en/hooks-guide.md, "Re-inject context after
compaction"; code.claude.com/docs/en/hooks.md, SessionStart and PreCompact;
code.claude.com/docs/en/context-window.md, "What survives compaction";
code.claude.com/docs/en/best-practices.md, "Keep cost and context down";
code.claude.com/docs/en/settings-reference.md, `idleCompaction`. All read
2026-11-09. Carried from Sextant's tests/proofs/test_compact_context.py (see
ORIGINS.md); the state folder is named by the MOVERS_STATE_DIR variable, never
by a path in this public repository.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "compact_context.py"
CAP = 10_000
TS = 1_700_000_000
SENTINEL = "STATE-CONTENT-MUST-NOT-BE-PRINTED"
VALUE_SENTINEL = "STATE-DIR-VALUE-MUST-NOT-BE-PRINTED"
STATE_ENV = "MOVERS_STATE_DIR"
UNSET = STATE_ENV + " is unset"

ROLES = ("lead", "dispatcher", "lander", "worker")
ROLE_FILES = {
    "lead": "MOVERS-LEAD-STATE.md",
    "dispatcher": "MOVERS-DISPATCHER-STATE.md",
    "lander": "MOVERS-LANDER-STATE.md",
}


def _state(tmp_path: Path) -> Path:
    state = tmp_path / VALUE_SENTINEL
    state.mkdir(parents=True, exist_ok=True)
    for name in ROLE_FILES.values():
        path = state / name
        path.write_text(SENTINEL + "\n")
        os.utime(path, (TS, TS))
    return state


def _run(
    *args: str, state: Path | None, tmp_path: Path, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop(STATE_ENV, None)
    if state is not None:
        env[STATE_ENV] = str(state)
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _pointer(role: str) -> str:
    return "$" + STATE_ENV + "/" + ROLE_FILES[role]


def _mtime() -> str:
    return datetime.fromtimestamp(TS).astimezone().strftime("%Y-%m-%d %H:%M")


_MISSING = object()


def _hook_command(mode: str) -> str:
    return 'python3 -I "${CLAUDE_PROJECT_DIR}/scripts/compact_context.py" ' + mode + " || true"


def _check_module():
    spec = importlib.util.spec_from_file_location("compact_context", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_root(tmp_path: Path, *, idle: object = _MISSING) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / "CLAUDE.md").write_text(
        "# Compact instructions\n\n" + "".join("- line " + str(n) + "\n" for n in range(8))
    )
    settings: dict[str, object] = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "compact",
                    "hooks": [{"type": "command", "command": _hook_command("session-start")}],
                }
            ],
            "PreCompact": [
                {"hooks": [{"type": "command", "command": _hook_command("pre-compact")}]}
            ],
        }
    }
    if idle is not _MISSING:
        settings["idleCompaction"] = idle
    (root / ".claude" / "settings.json").write_text(json.dumps(settings))
    return root


def test_claude_md_keeps_the_shared_compact_instructions_to_eight_lines() -> None:
    text = (ROOT / "CLAUDE.md").read_text()
    marker = "# Compact instructions"
    assert marker in text
    section = text.split(marker, 1)[1].split("\n# ", 1)[0]
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    assert 0 < len(lines) <= 8, lines
    joined = " ".join(lines).lower()
    for phrase in (
        "owner's words",
        "role",
        "state-file",
        "peer",
        "open tasks",
        "waits on the owner",
        "sha-256",
        "tool output",
        "fetched pages",
        "finished work",
    ):
        assert phrase in joined, phrase


def test_settings_declare_the_two_documented_compaction_hooks() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    hooks = settings["hooks"]
    assert set(hooks) == {"SessionStart", "PreCompact"}

    (start,) = hooks["SessionStart"]
    assert start["matcher"] == "compact"
    (start_hook,) = start["hooks"]
    assert start_hook["type"] == "command"
    assert "scripts/compact_context.py" in start_hook["command"]
    assert "session-start" in start_hook["command"]
    assert start_hook["command"].rstrip().endswith("|| true")

    (pre,) = hooks["PreCompact"]
    assert "matcher" not in pre
    (pre_hook,) = pre["hooks"]
    assert pre_hook["type"] == "command"
    assert "scripts/compact_context.py" in pre_hook["command"]
    assert "pre-compact" in pre_hook["command"]
    assert pre_hook["command"].rstrip().endswith("|| true")


def test_each_role_gets_a_pointer_and_its_modification_time(tmp_path: Path) -> None:
    state = _state(tmp_path)
    for role in ("lead", "dispatcher", "lander"):
        out = _run("session-start", "--role", role, state=state, tmp_path=tmp_path)
        assert out.returncode == 0, out.stderr
        assert out.stdout.count("Reminder:") == 1
        assert role in out.stdout
        assert _pointer(role) in out.stdout
        assert _mtime() in out.stdout
        assert SENTINEL not in out.stdout


def test_a_missing_state_file_is_named_missing(tmp_path: Path) -> None:
    state = tmp_path / "empty-state"
    state.mkdir()
    out = _run("session-start", "--role", "lead", state=state, tmp_path=tmp_path)
    assert out.returncode == 0, out.stderr
    assert _pointer("lead") + " (missing)" in out.stdout


def test_worker_names_no_state_file(tmp_path: Path) -> None:
    out = _run("session-start", "--role", "worker", state=_state(tmp_path), tmp_path=tmp_path)
    assert out.returncode == 0, out.stderr
    assert "no state file" in out.stdout.lower()
    assert "MOVERS-LEAD-STATE.md" not in out.stdout


def test_hook_input_agent_type_selects_the_role(tmp_path: Path) -> None:
    state = _state(tmp_path)
    stdin = json.dumps(
        {"hook_event_name": "SessionStart", "source": "compact", "agent_type": "lander"}
    )
    out = _run("session-start", state=state, tmp_path=tmp_path, stdin=stdin)
    assert out.returncode == 0, out.stderr
    assert _pointer("lander") in out.stdout
    assert "MOVERS-LEAD-STATE.md" not in out.stdout


def test_unknown_role_lists_every_known_role(tmp_path: Path) -> None:
    state = _state(tmp_path)
    out = _run("session-start", state=state, tmp_path=tmp_path, stdin="{}")
    assert out.returncode == 0, out.stderr
    for role in ("lead", "dispatcher", "lander"):
        assert _pointer(role) in out.stdout
    assert "worker" in out.stdout
    assert len(out.stdout) < CAP


def test_pre_compact_prints_the_roles_extra_keep_lines_under_the_cap(tmp_path: Path) -> None:
    state = _state(tmp_path)
    for role in ROLES:
        out = _run("pre-compact", "--role", role, state=state, tmp_path=tmp_path)
        assert out.returncode == 0, out.stderr
        assert 0 < len(out.stdout) < CAP
        assert role in out.stdout.lower()
    out = _run("pre-compact", state=state, tmp_path=tmp_path, stdin="{}")
    assert out.returncode == 0, out.stderr
    assert 0 < len(out.stdout) < CAP
    for role in ROLES:
        assert role in out.stdout.lower()


def test_hook_output_never_carries_state_file_contents_or_the_folder_value(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    outputs = [_run("session-start", state=state, tmp_path=tmp_path, stdin="{}").stdout]
    outputs += [
        _run("session-start", "--role", role, state=state, tmp_path=tmp_path).stdout
        for role in ROLES
    ]
    outputs += [
        _run("pre-compact", "--role", role, state=state, tmp_path=tmp_path).stdout for role in ROLES
    ]
    assert all(text.strip() for text in outputs)
    assert all(SENTINEL not in text for text in outputs)
    assert all(VALUE_SENTINEL not in text for text in outputs)
    assert all(str(tmp_path) not in text for text in outputs)


def test_an_unset_state_folder_makes_each_hook_print_one_line_and_exit_zero(
    tmp_path: Path,
) -> None:
    for args in (
        ("session-start",),
        ("session-start", "--role", "lead"),
        ("pre-compact",),
        ("pre-compact", "--role", "worker"),
    ):
        for stdin in ("", "{}", json.dumps({"agent_type": "lead"})):
            out = _run(*args, state=None, tmp_path=tmp_path, stdin=stdin)
            assert out.returncode == 0, out.stderr
            lines = out.stdout.splitlines()
            assert len(lines) == 1, out.stdout
            assert UNSET in lines[0]
            assert "Traceback" not in out.stderr


def test_an_empty_state_folder_value_counts_as_unset(tmp_path: Path) -> None:
    env = dict(os.environ, HOME=str(tmp_path), **{STATE_ENV: ""})
    out = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "session-start", "--role", "lead"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert len(out.stdout.splitlines()) == 1, out.stdout
    assert UNSET in out.stdout


def test_hook_modes_exit_zero_on_bad_input_and_an_empty_state_folder(tmp_path: Path) -> None:
    state = tmp_path / "empty-state"
    state.mkdir()
    for mode in ("session-start", "pre-compact"):
        out = _run(mode, state=state, tmp_path=tmp_path, stdin="not json")
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip()
        assert "Traceback" not in out.stderr


def test_script_reads_one_named_variable_and_makes_no_network_or_subprocess_call() -> None:
    source = SCRIPT.read_text()
    for forbidden in (
        "import subprocess",
        "import socket",
        "import urllib",
        "import requests",
        "os.system",
        "os.getenv",
        "os.environ[",
    ):
        assert forbidden not in source, forbidden
    assert "git " not in source
    assert 'STATE_ENV = "MOVERS_STATE_DIR"' in source
    assert source.count("os.environ") == 1
    assert "os.environ.get(STATE_ENV" in source


def test_check_passes_and_names_its_checks(tmp_path: Path) -> None:
    out = _run("--check", state=_state(tmp_path), tmp_path=tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Compact instructions" in out.stdout
    assert "SessionStart" in out.stdout
    assert "PreCompact" in out.stdout
    assert "idleCompaction" in out.stdout


def test_check_fails_without_an_explicit_idle_compaction_key(tmp_path: Path) -> None:
    module = _check_module()
    problems = module.check(_fake_root(tmp_path))
    assert any("idleCompaction" in problem for problem in problems), problems


def test_check_passes_on_a_complete_synthetic_tree(tmp_path: Path) -> None:
    module = _check_module()
    assert module.check(_fake_root(tmp_path, idle=True)) == []


def test_claude_md_instructs_clear_between_unrelated_tasks() -> None:
    text = (ROOT / "CLAUDE.md").read_text()
    assert "`/clear`" in text
    assert "unrelated tasks" in text


def test_settings_make_idle_compaction_explicit() -> None:
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    assert settings["idleCompaction"] is True


def test_just_session_start_runs_the_compact_check() -> None:
    lines = (ROOT / "justfile").read_text().splitlines()
    start = lines.index("session-start:")
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith((" ", "\t")):
            recipe.append(line.strip())
        elif line.strip():
            break
    assert any("scripts/compact_context.py --check" in line for line in recipe), recipe


def test_manifest_records_the_decision() -> None:
    manifest = (ROOT / "docs/manifest.md").read_text()
    assert "| Compaction is native |" in manifest
    assert "2026-11-09" in manifest
