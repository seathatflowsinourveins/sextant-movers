"""Proof: the session shelf installs from the main checkout, and session start checks it.

Source: the origin repository's `deploy/wsl/shelf.sh` and `scripts/runtime_doctor.py`
(its plugin check) at 7252b0f5c7ccdb416356aaddd9b53fa9a33bd405;
code.claude.com/docs/en/plugins/loading.md, "Enabled in project settings but not
installed"; code.claude.com/docs/en/plugins/install.md, "Choose an install scope";
code.claude.com/docs/en/plugins/cli-reference.md, "plugin install", "plugin list",
"plugin marketplace add" and "plugin marketplace list"; git-scm.com/docs/git-worktree,
"list". All read 2026-11-11.

A stub `claude` on a private PATH records each call; no real plugin command runs. Each
test copies the script into a throwaway repository under `tmp_path` and makes its
linked worktree there with plain `git`. The real CLI run is the measurement in the PR.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
SETTINGS = json.loads((ROOT / ".claude/settings.json").read_text())
FIX = "python3 scripts/plugin_shelf.py --install </dev/null"
CHECK = "python3 scripts/plugin_shelf.py --check </dev/null"
COPIED = (
    "scripts/plugin_shelf.py",
    ".claude/settings.json",
    ".claude/lsp/.claude-plugin/marketplace.json",
    ".gitignore",
)
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


def _git(tmp_path: Path, *args: str, cwd: Path) -> str:
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, text=True, capture_output=True, check=True
    )
    return done.stdout


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout and its linked worktree, both under `tmp_path`."""
    main = tmp_path / "main"
    for name in COPIED:
        (main / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, main / name)
    _git(tmp_path, "init", "-q", "-b", "main", cwd=main)
    _git(tmp_path, "add", "-A", cwd=main)
    _git(tmp_path, "commit", "-q", "-m", "fixture", cwd=main)
    _git(tmp_path, "worktree", "add", "-q", "-b", "task", str(tmp_path / "task"), cwd=main)
    return main.resolve(), (tmp_path / "task").resolve()


def _enabled() -> list[str]:
    return [plugin for plugin, on in SETTINGS["enabledPlugins"].items() if on is True]


def _expected_source(source: dict) -> str:
    if source["source"] == "directory":
        return source["path"]
    assert source["source"] == "github"
    return source["repo"] + "#" + source["ref"]


def _ours(main: Path) -> list[dict]:
    """The registry as `claude plugin marketplace list --json` shows it after our adds."""
    rows = []
    for name, entry in SETTINGS["extraKnownMarketplaces"].items():
        source = entry["source"]
        if source["source"] == "directory":
            path = str(main / source["path"])
            rows.append({"name": name, "source": "directory", "path": path})
        else:
            rows.append({"name": name, "source": "github", "repo": source["repo"]})
    return rows


def _run(
    tmp_path: Path,
    checkout: Path,
    mode: str,
    records: str = "[]",
    markets: str = "[]",
    fail: str | None = None,
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


def _record(plugin: str, path: Path, project_enabled: bool = True, install: str = "") -> str:
    return json.dumps(
        {
            "id": plugin,
            "scope": "local",
            "enabled": True,
            "projectEnabled": project_enabled,
            "projectPath": str(path),
            "installPath": install or str(path),
        }
    )


def _records(main: Path, **kwargs: object) -> str:
    rows = [_record(plugin, main, **kwargs) for plugin in _enabled()]  # type: ignore[arg-type]
    # The CLI also lists a session-scope plugin that no settings file declares.
    rows.append(json.dumps({"id": "other@inline", "scope": "session", "enabled": True}))
    return "[" + ",".join(rows) + "]"


def _one_line(stderr: str) -> str:
    assert "Traceback" not in stderr
    lines = stderr.strip().splitlines()
    assert len(lines) == 1, stderr
    return lines[0]


def test_install_adds_at_user_scope_and_installs_at_local_scope(tmp_path: Path) -> None:
    main, _ = _repo(tmp_path)
    result, calls = _run(tmp_path, main, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    adds = [
        ["plugin", "marketplace", "add", _expected_source(entry["source"]), "--scope", "user"]
        for entry in SETTINGS["extraKnownMarketplaces"].values()
    ]
    installs = [["plugin", "install", plugin, "--scope", "local"] for plugin in _enabled()]
    listed = [["plugin", "marketplace", "list", "--json"]]
    assert [call["argv"] for call in calls] == listed + adds + installs
    assert len(adds) == 4 and len(installs) == 5
    assert {call["cwd"] for call in calls} == {str(main)}
    assert {call["stdin"] for call in calls} == {""}
    # Local scope writes the untracked `.claude/settings.local.json`, which this
    # repository's own .gitignore ignores (HOME here has no global ignore file).
    assert ".claude/settings.local.json" in result.stdout
    assert _git(tmp_path, "status", "--porcelain", cwd=main) == ""
    _git(tmp_path, "check-ignore", "-q", ".claude/settings.local.json", cwd=main)


def test_install_stops_before_any_add_when_git_does_not_ignore_the_local_file(
    tmp_path: Path,
) -> None:
    main, _ = _repo(tmp_path)
    (main / ".gitignore").write_text("__pycache__/\n")
    _git(tmp_path, "commit", "-q", "-am", "no ignore", cwd=main)
    result, calls = _run(tmp_path, main, "--install")
    assert result.returncode == 1
    assert ".claude/settings.local.json" in _one_line(result.stderr)
    assert [call["argv"] for call in calls] == [["plugin", "marketplace", "list", "--json"]]


def test_install_started_in_a_linked_worktree_runs_from_the_main_checkout(
    tmp_path: Path,
) -> None:
    main, task = _repo(tmp_path)
    result, calls = _run(tmp_path, task, "--install")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 10
    assert {call["cwd"] for call in calls} == {str(main)}
    assert _git(tmp_path, "status", "--porcelain", cwd=task) == ""


def test_install_repeats_and_stops_on_a_failed_command(tmp_path: Path) -> None:
    main, _ = _repo(tmp_path)
    first, once = _run(tmp_path, main, "--install")
    second, twice = _run(tmp_path, main, "--install", markets=json.dumps(_ours(main)))
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
    stale = [{"name": "sextant-lsp", "source": "directory", "path": str(main / ".claude/lsp")}]
    for checkout in (main, task):
        result, calls = _run(tmp_path, checkout, "--install", markets=json.dumps(stale))
        assert result.returncode == 1
        line = _one_line(result.stderr)
        assert "'sextant-lsp'" in line and "'movers-lsp'" in line
        assert "claude plugin marketplace remove sextant-lsp" in line
        # Nothing is added, installed, removed or re-pointed.
        assert [call["argv"] for call in calls] == [["plugin", "marketplace", "list", "--json"]]


def test_install_refuses_when_our_name_is_registered_from_another_path(tmp_path: Path) -> None:
    main, task = _repo(tmp_path)
    for path in (tmp_path / "another-repository/.claude/lsp", task / ".claude/lsp"):
        taken = [{"name": "movers-lsp", "source": "directory", "path": str(path)}]
        result, calls = _run(tmp_path, task, "--install", markets=json.dumps(taken))
        assert result.returncode == 1
        line = _one_line(result.stderr)
        assert str(path) in line and str(main / ".claude/lsp") in line
        assert "claude plugin marketplace remove movers-lsp" in line
        assert [call["argv"] for call in calls] == [["plugin", "marketplace", "list", "--json"]]


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
        _records(main, install=str(tmp_path / "gone")),
    ):
        for checkout in (main, task):
            result, _ = _run(tmp_path, checkout, "--check", records=records)
            assert result.returncode == 1
            line = _one_line(result.stderr)
            for plugin in _enabled():
                assert plugin in line
            assert FIX in line


def test_check_without_a_claude_binary_says_so_and_fails(tmp_path: Path) -> None:
    main, _ = _repo(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        [sys.executable, str(main / "scripts/plugin_shelf.py"), "--check"],
        cwd=main,
        env={"HOME": str(tmp_path), "PATH": str(empty)},
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "claude is not on PATH" in _one_line(result.stderr)


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
    """A recipe's header and every indented body line under it."""
    lines = justfile.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(name + ":"))
    body = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def test_session_start_runs_the_check_and_ci_does_not() -> None:
    justfile = (ROOT / "justfile").read_text()
    assert CHECK in _recipe(justfile, "session-start")
    assert "session-start" not in _recipe(justfile, "ci")
    assert "plugin_shelf" not in _recipe(justfile, "ci")
    # The recipe reader sees body lines, not only the header.
    assert "uv sync --frozen" in _recipe(justfile, "sync")
    assert "session-start" not in (ROOT / ".github/workflows/ci.yml").read_text()


def test_the_fixture_worktree_is_linked_to_the_fixture_main_checkout(tmp_path: Path) -> None:
    main, task = _repo(tmp_path)
    listed = _git(tmp_path, "worktree", "list", "--porcelain", cwd=task).splitlines()
    assert listed[0] == f"worktree {main}"
    assert f"worktree {task}" in listed
    assert os.path.realpath(task) != os.path.realpath(main)
