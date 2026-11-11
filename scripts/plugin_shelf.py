#!/usr/bin/env python3
"""Install the session shelf for this checkout, or check that it is installed.

`.claude/settings.json` declares the shelf: `extraKnownMarketplaces` names the
marketplaces and `enabledPlugins` the plugins. Declaring is not installing:
"Committing that entry turns the plugin on for your collaborators but doesn't
download it to their machines. Each collaborator also runs `claude plugin install
<name>@<marketplace> --scope project` once per clone"
(code.claude.com/docs/en/plugins/install.md, "Choose an install scope", read
2026-11-11). So `--install` calls the vendor's two commands, `claude plugin
marketplace add <source> --scope project` per marketplace and `claude plugin install
<id> --scope project` per enabled id, read from that file alone; both report a
repeat as success with exit 0 (code.claude.com/docs/en/plugins/cli-reference.md,
"plugin install" and "plugin marketplace add", read 2026-11-11; Claude Code 2.1.322
`--help`). `--check` reads `claude plugin list --json` and asks one question: does
every enabled id have an install record whose `projectPath` is this checkout, or
whose scope covers every path?
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOPE = "project"
EVERY_PATH = {"user", "managed"}
FIX = "python3 scripts/plugin_shelf.py --install </dev/null"
SOURCE_KEYS = {"github": {"repo", "ref"}, "git": {"url", "ref"}, "directory": {"path"}}
USAGE = "usage: plugin_shelf.py {--install | --check}"


def source_argument(source: dict) -> str:
    """Write a settings source object in the form `claude plugin marketplace add` takes."""
    kind = source.get("source")
    keys = SOURCE_KEYS.get(kind)
    if keys is None or not set(source) - {"source"} <= keys:
        raise ValueError(f"unsupported marketplace source {source!r}")
    if kind == "directory":
        return source["path"]
    base = source["repo"] if kind == "github" else source["url"]
    return base + "#" + source["ref"] if source.get("ref") else base


def commands(root: Path = ROOT) -> list[list[str]]:
    """Every vendor command the install runs, in order, from the settings file."""
    settings = json.loads((root / ".claude/settings.json").read_text())
    adds = [
        [
            "claude",
            "plugin",
            "marketplace",
            "add",
            source_argument(entry["source"]),
            "--scope",
            SCOPE,
        ]
        for entry in settings.get("extraKnownMarketplaces", {}).values()
    ]
    installs = [
        ["claude", "plugin", "install", plugin, "--scope", SCOPE]
        for plugin, on in settings.get("enabledPlugins", {}).items()
        if on is True
    ]
    return adds + installs


def missing(records: list[dict], root: Path = ROOT) -> list[str]:
    """Enabled ids with no install record for this checkout."""
    settings = json.loads((root / ".claude/settings.json").read_text())
    here = os.path.realpath(root)
    installed = {
        record.get("id")
        for record in records
        if record.get("scope") in EVERY_PATH
        or os.path.realpath(record.get("projectPath") or "/nonexistent") == here
    }
    return [
        plugin
        for plugin, on in settings.get("enabledPlugins", {}).items()
        if on is True and plugin not in installed
    ]


def install() -> int:
    for command in commands():
        print("plugin_shelf: " + " ".join(command), flush=True)
        done = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, check=False)
        if done.returncode != 0:
            print(
                f"plugin_shelf: `{' '.join(command[1:3])}` exited {done.returncode}: "
                + " ".join(command),
                file=sys.stderr,
            )
            return 1
    print(f"ok: {len(commands())} shelf commands ran for this checkout")
    return 0


def check() -> int:
    try:
        listed = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("plugin_shelf: `claude plugin list --json` ran past 60 seconds", file=sys.stderr)
        return 1
    if listed.returncode != 0:
        print(
            f"plugin_shelf: `claude plugin list --json` exited {listed.returncode}", file=sys.stderr
        )
        return 1
    absent = missing(json.loads(listed.stdout))
    if absent:
        print(
            "plugin_shelf: enabled in .claude/settings.json but not installed for this checkout: "
            + ", ".join(absent)
            + f"; fix: {FIX}",
            file=sys.stderr,
        )
        return 1
    print("ok: every plugin .claude/settings.json enables has an install record for this checkout")
    return 0


def main(args: list[str]) -> int:
    if args not in (["--install"], ["--check"]):
        print(USAGE, file=sys.stderr)
        return 2
    if shutil.which("claude") is None:
        print(
            "plugin_shelf: claude is not on PATH, so this checkout's plugin installs are unchecked",
            file=sys.stderr,
        )
        return 1
    return install() if args == ["--install"] else check()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
