"""The gate's dsh job loads its installer module in this tree, and the shelf lock it reads is true.

The gate's `dsh` job runs `.github/gate/parity-entry.py`, which loads the unchanged installer
module `.github/gate/upstream/parity.py` by path with importlib. That module reads
`parity.json` and then the lock it names (`.claude/shelf.lock.json`) at import, so a tree
without the lock fails before any key is read. The lock's format is the installer's own
(`LOCK["skills"]["files"]`, `LOCK["plugins"][key]` with `repo`, `ref` and `sha`); here it
records only what this repository's `.claude/settings.json` enables and what this tree vendors.
Offline: no dsh, pnpm, npm or network call.
"""

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).parents[2]
GATE = ROOT / ".github/gate"

# Every repository file the installer module reads at import, and every one parity-entry.py and
# the three helpers it calls (ensure_workspace, _native, _quality_missing) read on a fresh hosted
# profile: parity.py:24-25 (SPEC, LOCK), parity-entry.py:15-21 (the module, dsh-profile.json),
# parity.py:331 (ensure_workspace seeds a profile that has no pnpm-workspace.yaml, as the hosted
# job's fresh profile has none) and parity.py:_quality_missing (the quality bundle's package.json).
READ_BY_THE_GATE = (
    ".github/gate/upstream/parity.py",
    ".github/gate/upstream/parity.json",
    ".claude/shelf.lock.json",
    ".github/gate/dsh-profile.json",
    "deploy/wsl/dsh/pnpm-workspace.yaml",
    "config/dsh/quality/package.json",
)
# Repository directories parity-entry.py hands to native pnpm and dsh calls: the review shim's
# frozen install (parity-entry.py:40-50), the config/ bundles of dsh-profile.json other than the
# quality bundle (parity-entry.py:60-64) and the API-only bundle (parity-entry.py:75).
HANDED_TO_NATIVE_CALLS = (
    "config/dsh/review-cmd/package.json",
    "config/dsh/review-cmd/pnpm-lock.yaml",
    "config/dsh/package.json",
    "config/dsh/hard-lines/package.json",
    ".github/gate/bundles/api-only/package.json",
)


def _json(relative: str) -> Any:
    return json.loads((ROOT / relative).read_text())


def _load_parity() -> ModuleType:
    """parity-entry.py:15-20: importlib from the module's path, nothing else."""
    spec = importlib.util.spec_from_file_location("parity", GATE / "upstream/parity.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gate_module_loads_in_this_tree() -> None:
    parity = _load_parity()
    assert parity.SPEC["lock"] == ".claude/shelf.lock.json"
    assert parity.LOCK == _json(".claude/shelf.lock.json")


def test_every_repository_path_the_gate_entry_reads_exists() -> None:
    spec = _json(".github/gate/upstream/parity.json")
    assert spec["lock"] in READ_BY_THE_GATE
    assert f"{spec['runtimes']['dsh']['quality_bundle']}/package.json" in READ_BY_THE_GATE
    profile = _json(".github/gate/dsh-profile.json")
    local = [
        row["spec"]
        for row in profile["bundles"]
        if row["spec"].startswith(("config/", ".github/"))
        and row["package"] != "sextant-dsh-quality"
    ]
    assert sorted(local) == ["config/dsh", "config/dsh/hard-lines", "config/dsh/review-cmd"]
    for directory in local:
        assert f"{directory}/package.json" in HANDED_TO_NATIVE_CALLS
    missing = [p for p in (*READ_BY_THE_GATE, *HANDED_TO_NATIVE_CALLS) if not (ROOT / p).is_file()]
    assert not missing, f"the gate's dsh job reads files this tree lacks: {missing}"


def test_a_fresh_hosted_profile_gets_the_seed_the_module_accepts(tmp_path: Path) -> None:
    """The hosted job's profile holds only package.json when parity-entry.py runs."""
    parity = _load_parity()
    profile = tmp_path / "profiles/headless"
    assert parity.ensure_workspace(profile, ROOT) == "written"
    seeded = (profile / "pnpm-workspace.yaml").read_text()
    assert seeded == (ROOT / "deploy/wsl/dsh/pnpm-workspace.yaml").read_text()
    assert parity.ensure_workspace(profile, ROOT) == "unchanged"


def _session_shelf_rows() -> str:
    text = (ROOT / "docs/manifest.md").read_text()
    return text.split("\n## Session shelf\n", 1)[1].split("\n## ", 1)[0]


def test_the_lock_pins_what_settings_enables_at_the_manifest_commits() -> None:
    settings = _json(".claude/settings.json")
    lock = _json(".claude/shelf.lock.json")
    rows = _session_shelf_rows()
    github = {
        name: row["source"]
        for name, row in settings["extraKnownMarketplaces"].items()
        if row["source"]["source"] == "github"
    }
    enabled = {
        plugin
        for plugin, on in settings["enabledPlugins"].items()
        if on and plugin.split("@", 1)[1] in github
    }
    assert set(lock["plugins"]) == enabled
    assert len(enabled) == 3
    for plugin, pin in lock["plugins"].items():
        source = github[plugin.split("@", 1)[1]]
        assert {"repo": source["repo"], "ref": source["ref"]} == {
            "repo": pin["repo"],
            "ref": pin["ref"],
        }
        assert re.fullmatch(r"[0-9a-f]{40}", pin["sha"])
        assert f"{pin['repo']}@{pin['ref']} `{pin['sha']}`" in rows


def test_the_lock_records_the_vendored_lsp_files_and_nothing_this_tree_lacks() -> None:
    lock = _json(".claude/shelf.lock.json")
    assert set(lock) == {"readDate", "plugins", "skills", "lsp", "mcp"}
    lsp = lock["lsp"]
    plugins = ROOT / ".claude/lsp/plugins"
    actual = {
        str(path.relative_to(plugins)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in plugins.rglob("*")
        if path.is_file()
    }
    assert actual == lsp["files"]
    assert len(actual) == 6
    assert f"anthropics/claude-plugins-official@`{lsp['sha']}`" in _session_shelf_rows()
    # This tree vendors no skill folder and carries no project MCP file.
    assert lock["skills"] == {"files": {}}
    assert not (ROOT / ".claude/skills").exists()
    assert lock["mcp"] == {}
    assert not (ROOT / ".mcp.json").exists()
