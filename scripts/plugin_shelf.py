#!/usr/bin/env python3
"""Install the session shelf from the main checkout, or check that it is installed.

`.claude/settings.json` declares the shelf: `extraKnownMarketplaces` names the
marketplaces and `enabledPlugins` the plugins. Declaring is not installing: "Claude
Code fetches a plugin with an external source only when one of these sources sets the
plugin to `true`: Your user settings; A `.claude/settings.local.json` that git doesn't
track; The `--settings` flag; Managed settings" (code.claude.com/docs/en/plugins/
loading.md, "Enabled in project settings but not installed", read 2026-11-11).

Built on the origin repository's `deploy/wsl/shelf.sh` at
7252b0f5c7ccdb416356aaddd9b53fa9a33bd405: `--install` adds each marketplace with
`claude plugin marketplace add <source> --scope user` and installs each enabled id with
`claude plugin install <id> --scope local`, always from the main checkout, the first
`worktree` line of `git worktree list --porcelain` (git-scm.com/docs/git-worktree,
"list", read 2026-11-11). Local scope writes only the untracked
`.claude/settings.local.json` (code.claude.com/docs/en/plugins/install.md, "Choose an
install scope", read 2026-11-11), which `.gitignore` ignores; no tracked file changes.
There is one marketplace registry per user, keyed by name (loading.md), so before any
add it reads `claude plugin marketplace list --json` and refuses a directory
marketplace of ours registered under another name, or our name registered from
another path: the CLI answers the first with "already on disk" and the later installs
with "not found in marketplace", and re-points the second ("now points at"), measured
2026-11-11 with Claude Code 2.1.322 in an isolated `CLAUDE_CONFIG_DIR`.

`--check` reads `claude plugin list --json` as the origin repository's
`scripts/runtime_doctor.py` does at the same commit: each enabled id needs a row whose
`projectPath` is the main checkout, whose `projectEnabled` is true and whose
`installPath` exists. A linked worktree lists the main checkout's local install with
those fields, measured the same day.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = "python3 scripts/plugin_shelf.py --install </dev/null"
LOCAL = ".claude/settings.local.json"
USER = ("--scope", "user")
SOURCE_KEYS = {"github": {"repo", "ref"}, "git": {"url", "ref"}, "directory": {"path"}}
USAGE = "usage: plugin_shelf.py {--install | --check}"
REMEDY = (
    "the CLI's remedy: to use this source instead, remove that marketplace first with "
    "`claude plugin marketplace remove {name}` (it uninstalls that marketplace's plugins), "
    "then run this again"
)


class Stop(Exception):
    """One line for stderr, then exit 1."""


def run(command: list[str], cwd: Path, timeout: int | None = 60, show: bool = False) -> str:
    """Run one command without a prompt and return its stdout, or stop in one line.

    With `show`, the CLI's own lines pass through to the terminal instead.
    """
    shown = " ".join(command)
    try:
        done = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=not show,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Stop(f"`{shown}` ran past {timeout} seconds") from None
    except OSError as error:
        raise Stop(f"`{shown}` did not start: {error.strerror}") from None
    if done.returncode != 0:
        raise Stop(f"`{shown}` exited {done.returncode}")
    return done.stdout or ""


def rows(command: list[str], cwd: Path) -> list[dict]:
    """A CLI answer that must be a JSON array of objects."""
    text = run(command, cwd)
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        answer = None
    if not isinstance(answer, list) or not all(isinstance(row, dict) for row in answer):
        raise Stop(f"`{' '.join(command)}` did not answer a JSON array of objects")
    return answer


def main_checkout(start: Path = ROOT) -> Path:
    """The main worktree, which `git worktree list --porcelain` lists first."""
    first = run(["git", "worktree", "list", "--porcelain"], start).partition("\n")[0]
    if not first.startswith("worktree "):
        raise Stop("`git worktree list --porcelain` did not start with a worktree line")
    return Path(first.removeprefix("worktree ")).resolve()


def settings(checkout: Path) -> dict:
    return json.loads((checkout / ".claude/settings.json").read_text())


def enabled(declared: dict) -> list[str]:
    return [plugin for plugin, on in declared.get("enabledPlugins", {}).items() if on is True]


def source_argument(source: dict) -> str:
    """Write a settings source object in the form `claude plugin marketplace add` takes."""
    kind = source.get("source")
    keys = SOURCE_KEYS.get(kind)
    if keys is None or not set(source) - {"source"} <= keys:
        raise Stop(f"unsupported marketplace source {source!r}")
    if kind == "directory":
        return source["path"]
    base = source["repo"] if kind == "github" else source["url"]
    return base + "#" + source["ref"] if source.get("ref") else base


def commands(declared: dict) -> list[list[str]]:
    """Every vendor command the install runs, in order."""
    adds = [
        ["claude", "plugin", "marketplace", "add", source_argument(entry["source"]), *USER]
        for entry in declared.get("extraKnownMarketplaces", {}).values()
    ]
    installs = [
        ["claude", "plugin", "install", plugin, "--scope", "local"] for plugin in enabled(declared)
    ]
    return adds + installs


def collision(declared: dict, registered: list[dict], main: Path) -> str | None:
    """A registry entry our adds would miss or re-point; never changed here."""
    for name, entry in declared.get("extraKnownMarketplaces", {}).items():
        if entry["source"].get("source") != "directory":
            continue
        ours = os.path.realpath(main / entry["source"]["path"])
        for row in registered:
            path = row.get("path") or row.get("repo") or row.get("url") or "?"
            same_path = row.get("source") == "directory" and os.path.realpath(path) == ours
            if same_path and row.get("name") != name:
                return (
                    f"{ours} is registered as marketplace '{row.get('name')}', so adding it as "
                    f"'{name}' stops at 'already on disk' and its installs fail; "
                    + REMEDY.format(name=row.get("name"))
                )
            if row.get("name") == name and not same_path:
                return (
                    f"marketplace '{name}' is registered from {path}, not from the main "
                    f"checkout's {ours}, and adding it would re-point it; "
                    + REMEDY.format(name=name)
                )
    return None


def install() -> int:
    main = main_checkout()
    declared = settings(main)
    listed = ["claude", "plugin", "marketplace", "list", "--json"]
    refused = collision(declared, rows(listed, main), main)
    if refused:
        raise Stop(refused)
    try:
        run(["git", "check-ignore", "-q", LOCAL], main)
    except Stop:
        raise Stop(f"git does not ignore {LOCAL} in {main}, which a local install writes") from None
    tracked = run(["git", "diff", "HEAD", "--binary"], main)
    steps = commands(declared)
    for command in steps:
        print("plugin_shelf: " + " ".join(command), flush=True)
        run(command, main, timeout=None, show=True)
    if run(["git", "diff", "HEAD", "--binary"], main) != tracked:
        raise Stop(f"a tracked file in {main} changed during the install; see `git diff`")
    print(
        f"ok: {len(steps)} shelf commands ran from the main checkout {main}; no tracked file "
        f"changed, and the local installs live in the untracked {LOCAL}, which git ignores"
    )
    return 0


def check() -> int:
    main = main_checkout()
    records = rows(["claude", "plugin", "list", "--json"], ROOT)
    here = {
        str(row.get("id"))
        for row in records
        if os.path.realpath(str(row.get("projectPath") or "/nonexistent")) == str(main)
        and row.get("projectEnabled") is True
        and Path(str(row.get("installPath") or "/nonexistent")).is_dir()
    }
    absent = [plugin for plugin in enabled(settings(ROOT)) if plugin not in here]
    if absent:
        raise Stop(
            f"enabled in .claude/settings.json but not installed for the main checkout {main}: "
            + ", ".join(absent)
            + f"; fix: {FIX}"
        )
    print(f"ok: every plugin .claude/settings.json enables is installed for {main}")
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
    try:
        return install() if args == ["--install"] else check()
    except Stop as stop:
        print(f"plugin_shelf: {stop}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
