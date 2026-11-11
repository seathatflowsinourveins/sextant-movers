"""Proof: the session shelf is installed for a checkout, and session start checks it.

Source: code.claude.com/docs/en/plugins/install.md, "Choose an install scope" and
"Install from your shell"; code.claude.com/docs/en/plugins/loading.md, "Enabled in
project settings but not installed"; code.claude.com/docs/en/plugins/cli-reference.md,
"plugin install", "plugin list" (JSON output) and "plugin marketplace add". All read
2026-11-11. Claude Code 2.1.322 `claude plugin install --help` and
`claude plugin marketplace add --help`, read 2026-11-11.

A stub `claude` on a private PATH records each call; no real plugin command runs.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/plugin_shelf.py"
SETTINGS = json.loads((ROOT / ".claude/settings.json").read_text())
FIX = "python3 scripts/plugin_shelf.py --install </dev/null"
CHECK = "python3 scripts/plugin_shelf.py --check </dev/null"
STUB = """\
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": sys.stdin.read()}) + "\\n")
if argv[:3] == ["plugin", "list", "--json"]:
    print(open(os.environ["STUB_RECORDS"]).read())
fail = os.environ.get("STUB_FAIL")
raise SystemExit(1 if fail and argv[:2] == ["plugin", fail] else 0)
"""


def _enabled() -> list[str]:
    return [plugin for plugin, on in SETTINGS["enabledPlugins"].items() if on is True]


def _expected_source(source: dict) -> str:
    if source["source"] == "directory":
        return source["path"]
    assert source["source"] == "github"
    return source["repo"] + "#" + source["ref"]


def _run(
    tmp_path: Path, mode: str, records: list[dict] | None = None, fail: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(f"#!{sys.executable}\n{STUB}")
    stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.write_text("")
    (tmp_path / "records.json").write_text(json.dumps(records or []))
    env = {
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "STUB_RECORDS": str(tmp_path / "records.json"),
    }
    if fail:
        env["STUB_FAIL"] = fail
    result = subprocess.run(
        [sys.executable, str(SCRIPT), mode],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def _record(plugin: str, scope: str = "project", path: Path | str = ROOT) -> dict:
    record = {"id": plugin, "version": "1", "scope": scope, "enabled": True, "installPath": "x"}
    if scope in ("project", "local"):
        record["projectPath"] = str(path)
    return record


def test_install_adds_each_marketplace_and_installs_each_enabled_plugin(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    adds = [
        ["plugin", "marketplace", "add", _expected_source(entry["source"]), "--scope", "project"]
        for entry in SETTINGS["extraKnownMarketplaces"].values()
    ]
    installs = [["plugin", "install", plugin, "--scope", "project"] for plugin in _enabled()]
    assert [call["argv"] for call in calls] == adds + installs
    assert len(adds) == len(SETTINGS["extraKnownMarketplaces"]) == 4
    assert len(installs) == len(_enabled()) == 5
    # The directory marketplace's relative path resolves against the checkout, and no
    # call can wait on a prompt.
    assert {call["cwd"] for call in calls} == {str(ROOT)}
    assert {call["stdin"] for call in calls} == {""}


def test_install_repeats_the_same_calls_and_stops_on_a_failed_command(tmp_path: Path) -> None:
    first, once = _run(tmp_path, "--install")
    second, twice = _run(tmp_path, "--install")
    assert first.returncode == second.returncode == 0
    assert [call["argv"] for call in once] == [call["argv"] for call in twice]
    failed, calls = _run(tmp_path, "--install", fail="install")
    assert failed.returncode != 0
    assert calls[-1]["argv"][:2] == ["plugin", "install"]
    assert len([call for call in calls if call["argv"][:2] == ["plugin", "install"]]) == 1
    assert "plugin install" in failed.stderr


def test_check_fails_and_names_each_plugin_without_a_record_for_this_checkout(
    tmp_path: Path,
) -> None:
    elsewhere = [_record(plugin, "local", tmp_path / "other-checkout") for plugin in _enabled()]
    for records in ([], elsewhere):
        result, calls = _run(tmp_path, "--check", records)
        assert result.returncode == 1
        for plugin in _enabled():
            assert plugin in result.stderr
        assert FIX in result.stderr
        assert [call["argv"] for call in calls] == [["plugin", "list", "--json"]]
    some = [_record(plugin) for plugin in _enabled()[1:]]
    result, _ = _run(tmp_path, "--check", some)
    assert result.returncode == 1
    assert _enabled()[0] in result.stderr
    assert not any(plugin in result.stderr for plugin in _enabled()[1:])


def test_check_passes_when_every_enabled_plugin_has_a_record_here(tmp_path: Path) -> None:
    for records in (
        [_record(plugin) for plugin in _enabled()],
        [_record(plugin, "local") for plugin in _enabled()],
        [_record(plugin, "user") for plugin in _enabled()],
    ):
        result, _ = _run(tmp_path, "--check", records)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("ok:")


def test_check_without_a_claude_binary_says_so_and_fails(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": str(empty)},
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "claude is not on PATH" in result.stderr


def test_the_directory_marketplace_has_this_repository_s_own_name() -> None:
    names = set(SETTINGS["extraKnownMarketplaces"])
    ids = {plugin.split("@", 1)[1] for plugin in SETTINGS["enabledPlugins"]}
    assert "sextant-lsp" not in names | ids
    assert "movers-lsp" in names & ids
    # `marketplace add` registers a marketplace under its manifest's own name.
    for name, entry in SETTINGS["extraKnownMarketplaces"].items():
        if entry["source"]["source"] == "directory":
            manifest = ROOT / entry["source"]["path"] / ".claude-plugin/marketplace.json"
            assert json.loads(manifest.read_text())["name"] == name


def test_session_start_runs_the_check_and_ci_does_not() -> None:
    justfile = (ROOT / "justfile").read_text()
    session_start = justfile.split("session-start:", 1)[1].split("\n\n", 1)[0]
    assert CHECK in session_start
    ci_recipe = next(line for line in justfile.splitlines() if line.startswith("ci:"))
    assert "session-start" not in ci_recipe
    assert "session-start" not in (ROOT / ".github/workflows/ci.yml").read_text()
