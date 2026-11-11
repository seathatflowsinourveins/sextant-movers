"""Proof: a role is a checked-in launch definition.

Source: code.claude.com/docs/en/sub-agents.md, "Define agents" and "Use the
agent"; code.claude.com/docs/en/cli-reference.md, `--agent`, `--name` and
`--autocompact`; code.claude.com/docs/en/model-config.md, `autoCompactWindow`;
code.claude.com/docs/en/settings-reference.md, `idleCompaction`. All read
2026-11-09. Carried from Sextant's tests/proofs/test_role_launch_defs.py (see
ORIGINS.md); the state folder is named by the MOVERS_STATE_DIR variable, never
by a path in this public repository.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/role_launch.py"
WRAPPER = "deploy/wsl/lane-runtimes/claude-launch.sh"
POLICY = ROOT / "deploy/wsl/lane-runtimes/model-policy.json"
AGENTS = ROOT / ".claude/agents"
ROLES = ("lead", "dispatcher", "lander", "worker")
COORDINATION = ("lead", "dispatcher", "lander")
POLICY_KEY = {
    "lead": "claude_lead",
    "dispatcher": "claude_roles",
    "lander": "claude_roles",
    "worker": "claude",
}
STATE_ENV = "MOVERS_STATE_DIR"
STATE_POINTERS = {
    "lead": "$MOVERS_STATE_DIR/MOVERS-LEAD-STATE.md",
    "dispatcher": "$MOVERS_STATE_DIR/MOVERS-DISPATCHER-STATE.md",
    "lander": "$MOVERS_STATE_DIR/MOVERS-LANDER-STATE.md",
    "worker": "$MOVERS_STATE_DIR/tasks",
}
KEYS = {"name", "description", "model", "initialPrompt"}


def _module():
    spec = importlib.util.spec_from_file_location("role_launch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy() -> dict:
    return json.loads(POLICY.read_text())


def _minimal_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / "deploy/wsl/lane-runtimes").mkdir(parents=True)
    shutil.copy(POLICY, root / "deploy/wsl/lane-runtimes/model-policy.json")
    shutil.copy(ROOT / ".claude/settings.json", root / ".claude/settings.json")
    return root


def test_every_role_definition_matches_the_model_policy() -> None:
    assert _module().check(ROOT) == []


def test_each_definition_has_the_probed_keys_an_empty_body_and_its_model() -> None:
    module = _module()
    policy = _policy()
    for role in ROLES:
        meta, body = module.read_definition(AGENTS / f"{role}.md")
        assert set(meta) == KEYS
        assert body.strip() == ""
        assert meta["name"] == role
        assert meta["description"]
        assert meta["model"] == policy[POLICY_KEY[role]]["model"]
        assert STATE_POINTERS[role] in meta["initialPrompt"]


def test_the_launcher_uses_the_agent_definition_and_names_the_session() -> None:
    module = _module()
    assert module.argv("lead") == [
        "bash",
        WRAPPER,
        "--agent",
        "lead",
        "--name",
        "lead",
        "--autocompact",
        "200000",
    ]
    assert module.argv("worker") == [
        "bash",
        WRAPPER,
        "--agent",
        "worker",
        "--name",
        "worker",
    ]
    for role in ROLES:
        built = module.argv(role)
        assert "--model" not in built
        assert "--effort" not in built
        assert "--settings" not in built
        assert "--advisor" not in built


def test_the_launcher_command_goes_through_the_checked_in_wrapper(tmp_path: Path) -> None:
    """D-37 item 4: the printed command and the started process both cross the wrapper.

    Source: code.claude.com/docs/en/monitoring-usage and
    code.claude.com/docs/en/settings-reference#variables-claude-code-ignores-in-env,
    read 2026-11-10.
    """
    printed = subprocess.run(
        [sys.executable, str(SCRIPT), "--print", "worker"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert printed.returncode == 0, printed.stderr
    assert shlex.split(printed.stdout) == [
        "bash",
        WRAPPER,
        "--agent",
        "worker",
        "--name",
        "worker",
    ]
    assert (ROOT / WRAPPER).is_file()
    # A stub on PATH stands in for native Claude, so the hop through the wrapper is
    # observable in the child's environment; no Claude session starts.
    stub = tmp_path / "bin/claude"
    stub.parent.mkdir()
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], "
        "'endpoint': os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT'), "
        "'temporality': os.environ.get('OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE'), "
        "'attributes': os.environ.get('OTEL_RESOURCE_ATTRIBUTES')}))\n"
    )
    stub.chmod(0o755)
    launched = subprocess.run(
        [sys.executable, str(SCRIPT), "worker"],
        cwd=ROOT,
        env={
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "PATH": f"{tmp_path / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin",
        },
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    assert launched.returncode == 0, launched.stderr
    payload = json.loads(launched.stdout)
    assert payload["argv"] == ["--agent", "worker", "--name", "worker"]
    assert payload["endpoint"] == "http://127.0.0.1:4317"
    assert payload["temporality"] == "cumulative"
    assert payload["attributes"] == "sextant.runtime=claude-code"


def test_the_three_coordination_roles_take_the_window_and_workers_keep_the_default() -> None:
    module = _module()
    for role in COORDINATION:
        assert module.argv(role)[-2:] == ["--autocompact", "200000"]
    assert "--autocompact" not in module.argv("worker")


def test_the_policy_records_every_launch_value_the_definitions_use() -> None:
    policy = _policy()
    lead, roles, worker = policy["claude_lead"], policy["claude_roles"], policy["claude"]
    assert (lead["launch"], lead["session_name"]) == (
        "python3 scripts/role_launch.py lead",
        "lead",
    )
    assert (lead["autocompact_window"], lead["autocompact_window_calibration"]) == (
        200000,
        "UNCALIBRATED",
    )
    assert lead["advisor"] is None and "model-config" in lead["autocompact_window_source"]
    assert (
        roles["launch"],
        roles["session_name"],
    ) == ("python3 scripts/role_launch.py <dispatcher|lander>", "<role>")
    assert (roles["autocompact_window"], roles["autocompact_window_calibration"]) == (
        200000,
        "UNCALIBRATED",
    )
    assert roles["advisor"] is None
    assert (worker["launch"], worker["session_name"]) == (
        "python3 scripts/role_launch.py worker",
        "worker",
    )
    assert worker["autocompact_window"] is None and worker["advisor"] is None


def test_the_session_start_check_is_wired() -> None:
    justfile = (ROOT / "justfile").read_text()
    session_start = justfile.split("session-start:", 1)[1].split("\n\n", 1)[0]
    assert "python3 scripts/role_launch.py --check" in session_start


def test_check_names_a_missing_definition(tmp_path: Path) -> None:
    problems = _module().check(_minimal_root(tmp_path))
    assert len([problem for problem in problems if "missing" in problem]) == len(ROLES)


def test_check_rejects_a_frontmatter_key_the_probes_did_not_prove(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path)
    shutil.copytree(AGENTS, root / ".claude/agents")
    path = root / ".claude/agents/lead.md"
    path.write_text(path.read_text().replace("model:", "effort: high\nmodel:", 1))
    problems = _module().check(root)
    assert any("lead" in problem and "effort" in problem for problem in problems)


def _check_run(state: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop(STATE_ENV, None)
    if state is not None:
        env[STATE_ENV] = state
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_the_check_mode_prints_ok_and_exits_zero(tmp_path: Path) -> None:
    result = _check_run(str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("ok:")
    assert STATE_ENV not in result.stdout
    assert str(tmp_path) not in result.stdout + result.stderr


def test_the_check_mode_says_when_the_state_folder_variable_is_unset() -> None:
    for state in (None, ""):
        result = _check_run(state)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("ok:")
        unset = [line for line in result.stdout.splitlines() if STATE_ENV in line]
        assert len(unset) == 1, result.stdout
        assert STATE_ENV + " is unset" in unset[0]


def test_the_state_pointers_name_the_variable_never_its_value() -> None:
    module = _module()
    for role in ROLES:
        assert module.state_pointer(role) == STATE_POINTERS[role]


def test_an_unknown_role_is_a_usage_error() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "no-such-role"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage" in (result.stdout + result.stderr).lower()
