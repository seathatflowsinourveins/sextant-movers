"""Proof: the session shelf installs from the main checkout, and session start checks it.

Sources: docs/manifest.md, "Shelf install step and its session-start check". A stub
`claude` records each call in a repository and linked worktree made under `tmp_path`;
it proves only our argument list, and the real run is in the PR.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SETTINGS = json.loads((ROOT / ".claude/settings.json").read_text())
FIX = "python3 scripts/plugin_shelf.py --install </dev/null"
CHECK = "python3 scripts/plugin_shelf.py --check </dev/null"
LISTED = [["plugin", "marketplace", "list", "--json"]]
STUB = """\
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "stdin": sys.stdin.read()}) + "\\n")
if argv[:3] == ["plugin", "list", "--json"]:
    print(open(os.environ["STUB_RECORDS"]).read())
if argv[:4] == ["plugin", "marketplace", "list", "--json"]:
    print(open(os.environ["STUB_MARKETS"]).read())
fail = os.environ.get("STUB_FAIL")
raise SystemExit(1 if fail and argv[:2] == ["plugin", fail] else 0)
"""


def _git(tmp_path: Path, cwd: Path, *args: str) -> str:
    who = {
        f"GIT_{role}_{key}": "t" for role in ("AUTHOR", "COMMITTER") for key in ("NAME", "EMAIL")
    }
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", **who}
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, text=True, capture_output=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout and its linked worktree, both under `tmp_path`."""
    main, task = tmp_path / "main", tmp_path / "task"
    for name in ("scripts/plugin_shelf.py", ".claude/settings.json", ".gitignore"):
        (main / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, main / name)
    _git(tmp_path, main, "init", "-q", "-b", "main")
    _git(tmp_path, main, "add", "-A")
    _git(tmp_path, main, "commit", "-q", "-m", "fixture")
    _git(tmp_path, main, "worktree", "add", "-q", "-b", "task", str(task))
    listed = _git(tmp_path, task, "worktree", "list", "--porcelain").splitlines()
    assert listed[0] == f"worktree {main.resolve()}" and f"worktree {task.resolve()}" in listed
    return main.resolve(), task.resolve()


def _enabled() -> list[str]:
    return [plugin for plugin, on in SETTINGS["enabledPlugins"].items() if on is True]


def _expected_source(source: dict) -> str:
    if source["source"] == "directory":
        return source["path"]
    assert source["source"] == "github"
    return source["repo"] + "#" + source["ref"]


def _run(
    tmp_path: Path,
    checkout: Path,
    mode: str,
    records: str = "[]",
    markets: str = "[]",
    fail: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[dict]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "claude"
    stub.write_text(f"#!{sys.executable}\n{STUB}")
    stub.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.write_text("")
    (tmp_path / "records.json").write_text(records)
    (tmp_path / "markets.json").write_text(markets)
    env = {
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "STUB_LOG": str(log),
        "STUB_RECORDS": str(tmp_path / "records.json"),
        "STUB_MARKETS": str(tmp_path / "markets.json"),
    }
    if fail:
        env["STUB_FAIL"] = fail
    result = subprocess.run(
        [sys.executable, str(checkout / "scripts/plugin_shelf.py"), mode],
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, calls


def _records(path: Path, project_enabled: bool = True, install: Path | None = None) -> str:
    row = {"scope": "local", "enabled": True, "projectEnabled": project_enabled}
    row |= {"projectPath": str(path), "installPath": str(install or path)}
    rows = [{"id": plugin, **row} for plugin in _enabled()]
    # The CLI also lists a session-scope plugin that no settings file declares.
    return json.dumps([*rows, {"id": "other@inline", "scope": "session", "enabled": True}])


def _one_line(stderr: str) -> str:
    assert "Traceback" not in stderr and len(stderr.strip().splitlines()) == 1, stderr
    return stderr.strip()


def _lsp(path: Path, name: str = "movers-lsp") -> str:
    return json.dumps([{"name": name, "source": "directory", "path": str(path)}])


def test_install_adds_at_user_scope_and_installs_at_local_scope(tmp_path: Path) -> None:
    main, _ = _repo(tmp_path)
    result, calls = _run(tmp_path, main, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    adds = [
        ["plugin", "marketplace", "add", _expected_source(entry["source"]), "--scope", "user"]
        for entry in SETTINGS["extraKnownMarketplaces"].values()
    ]
    installs = [["plugin", "install", plugin, "--scope", "local"] for plugin in _enabled()]
    assert [call["argv"] for call in calls] == LISTED + adds + installs
    assert len(adds) == 4 and len(installs) == 5
    assert {call["cwd"] for call in calls} == {str(main)}
    assert {call["stdin"] for call in calls} == {""}
    # Local scope writes the untracked settings.local.json, which this repository's
    # own .gitignore ignores (HOME here has no global ignore file).
    assert ".claude/settings.local.json is ignored" in result.stdout
    assert _git(tmp_path, main, "status", "--porcelain") == ""
    _git(tmp_path, main, "check-ignore", "-q", ".claude/settings.local.json")


def test_install_stops_before_any_add_when_git_does_not_ignore_the_local_file(
    tmp_path: Path,
) -> None:
    main, _ = _repo(tmp_path)
    (main / ".gitignore").write_text("__pycache__/\n")
    _git(tmp_path, main, "commit", "-q", "-am", "no ignore")
    result, calls = _run(tmp_path, main, "--install")
    assert result.returncode == 1
    assert ".claude/settings.local.json" in _one_line(result.stderr)
    assert [call["argv"] for call in calls] == LISTED


def test_install_started_in_a_linked_worktree_runs_from_the_main_checkout(
    tmp_path: Path,
) -> None:
    main, task = _repo(tmp_path)
    result, calls = _run(tmp_path, task, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 10
    assert {call["cwd"] for call in calls} == {str(main)}


def test_install_repeats_and_stops_on_a_failed_command(tmp_path: Path) -> None:
    main, _ = _repo(tmp_path)
    first, once = _run(tmp_path, main, "--install")
    second, twice = _run(tmp_path, main, "--install", markets=_lsp(main / ".claude/lsp"))
    assert first.returncode == second.returncode == 0, second.stderr
    assert [call["argv"] for call in once] == [call["argv"] for call in twice]
    failed, calls = _run(tmp_path, main, "--install", fail="install")
    assert failed.returncode == 1
    assert calls[-1]["argv"][:2] == ["plugin", "install"]
    assert len([call for call in calls if call["argv"][:2] == ["plugin", "install"]]) == 1
    assert "claude plugin install" in _one_line(failed.stderr)


def test_install_refuses_when_our_directory_is_registered_under_another_name(
    tmp_path: Path,
) -> None:
    main, task = _repo(tmp_path)
    for checkout in (main, task):
        markets = _lsp(main / ".claude/lsp", "sextant-lsp")
        result, calls = _run(tmp_path, checkout, "--install", markets=markets)
        assert result.returncode == 1
        line = _one_line(result.stderr)
        assert "'sextant-lsp'" in line and "'movers-lsp'" in line
        assert "claude plugin marketplace remove sextant-lsp" in line
        # Nothing is added, installed, removed or re-pointed.
        assert [call["argv"] for call in calls] == LISTED


def test_install_refuses_when_our_name_is_registered_from_another_path(tmp_path: Path) -> None:
    main, task = _repo(tmp_path)
    for path in (tmp_path / "another-repository/.claude/lsp", task / ".claude/lsp"):
        result, calls = _run(tmp_path, task, "--install", markets=_lsp(path))
        assert result.returncode == 1
        line = _one_line(result.stderr)
        assert str(path) in line and str(main / ".claude/lsp") in line
        assert "claude plugin marketplace remove movers-lsp" in line
        assert [call["argv"] for call in calls] == LISTED


def test_an_answer_that_is_not_json_fails_in_one_line_naming_the_command(
    tmp_path: Path,
) -> None:
    main, _ = _repo(tmp_path)
    for text in ("Welcome to Claude Code\n[]", "{}", '["row"]'):
        result, _ = _run(tmp_path, main, "--check", records=text)
        assert result.returncode == 1
        assert "`claude plugin list --json`" in _one_line(result.stderr)
        result, calls = _run(tmp_path, main, "--install", markets=text)
        assert result.returncode == 1
        assert "`claude plugin marketplace list --json`" in _one_line(result.stderr)
        assert len(calls) == 1


def test_check_passes_in_the_main_checkout_and_in_a_linked_worktree(tmp_path: Path) -> None:
    main, task = _repo(tmp_path)
    for checkout in (main, task):
        result, calls = _run(tmp_path, checkout, "--check", records=_records(main))
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("ok:")
        assert [call["argv"] for call in calls] == [["plugin", "list", "--json"]]


def test_check_fails_naming_each_plugin_without_a_main_checkout_record(tmp_path: Path) -> None:
    main, task = _repo(tmp_path)
    for records in (
        "[]",
        _records(task),
        _records(tmp_path / "another-repository"),
        _records(main, project_enabled=False),
        _records(main, install=tmp_path / "gone"),
    ):
        for checkout in (main, task):
            result, _ = _run(tmp_path, checkout, "--check", records=records)
            assert result.returncode == 1
            line = _one_line(result.stderr)
            assert all(plugin in line for plugin in _enabled()) and FIX in line


def test_check_without_a_claude_binary_says_so_and_fails(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/plugin_shelf.py"), "--check"],
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


def _recipe(justfile: str, name: str) -> str:
    """A recipe's header line and every indented body line under it."""
    lines = justfile.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(name + ":"))
    end = next((i for i in range(start + 1, len(lines)) if lines[i][:1].strip()), len(lines))
    return "\n".join(lines[start:end])


def test_session_start_runs_the_check_and_ci_does_not() -> None:
    justfile = (ROOT / "justfile").read_text()
    assert CHECK in _recipe(justfile, "session-start")
    assert "session-start" not in _recipe(justfile, "ci")
    assert "plugin_shelf" not in _recipe(justfile, "ci")
    # The reader sees body lines, not only the header.
    assert "uv sync --frozen" in _recipe(justfile, "sync")
    assert "session-start" not in (ROOT / ".github/workflows/ci.yml").read_text()
