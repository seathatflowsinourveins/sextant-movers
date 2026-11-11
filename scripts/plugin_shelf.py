#!/usr/bin/env python3
"""Install the session shelf from the main checkout, or check that it is installed.

Built on the origin repository's `deploy/wsl/shelf.sh` and `scripts/runtime_doctor.py`
at 7252b0f5c7ccdb416356aaddd9b53fa9a33bd405: marketplaces at scope user, plugins at
scope local, from the main checkout. Both modes read the plugin set from the main
checkout's .claude/settings.json, the only one `--install` acts on. Sources and
measurements: docs/manifest.md, "Shelf install step and its session-start check".
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
USER, LOCAL_SCOPE = ("--scope", "user"), ("--scope", "local")
SOURCE_KEYS = {"github": {"repo", "ref"}, "git": {"url", "ref"}, "directory": {"path"}}
USAGE = "usage: plugin_shelf.py {--install | --check}"
REMEDY = (
    "the CLI's remedy: to use this source instead, remove that marketplace first with "
    "`claude plugin marketplace remove {}` (it uninstalls its plugins), then run this again"
)


class Stop(Exception):
    """One line for stderr, then exit 1; `code` is the exit status of a command that ran."""

    def __init__(self, line: str, code: int | None = None) -> None:
        super().__init__(line)
        self.code = code


def run(command: list[str], cwd: Path, show: bool = False) -> str:
    """Run a command without a prompt; `show` lets the CLI's own lines through."""
    shown = " ".join(command)
    try:
        done = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=not show,
            text=True,
            timeout=None if show else 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Stop(f"`{shown}` ran past 60 seconds") from None
    except OSError as error:
        raise Stop(f"`{shown}` did not start: {error.strerror}") from None
    if done.returncode != 0:
        said = (done.stderr or "").strip().splitlines()[-1:]  # captured only when not `show`
        why = f"`{shown}` exited {done.returncode}" + "".join(": " + line for line in said)
        raise Stop(why, done.returncode)
    return done.stdout or ""


def rows(command: list[str], cwd: Path) -> list[dict]:
    try:
        answer = json.loads(run(command, cwd))
    except json.JSONDecodeError:
        answer = None
    if not isinstance(answer, list) or not all(isinstance(row, dict) for row in answer):
        raise Stop(f"`{' '.join(command)}` did not answer a JSON array of objects")
    return answer


def main_checkout() -> Path:
    first = run(["git", "worktree", "list", "--porcelain"], ROOT).partition("\n")[0]
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
        raise ValueError(f"unsupported marketplace source {source!r}")
    if kind == "directory":
        return source["path"]
    base = source["repo"] if kind == "github" else source["url"]
    return base + "#" + source["ref"] if source.get("ref") else base


def commands(declared: dict) -> list[list[str]]:
    """Every vendor command the install runs, in order."""
    marketplaces = declared.get("extraKnownMarketplaces", {}).values()
    adds = [
        ["claude", "plugin", "marketplace", "add", source_argument(entry["source"]), *USER]
        for entry in marketplaces
    ]
    return adds + [["claude", "plugin", "install", id, *LOCAL_SCOPE] for id in enabled(declared)]


def collision(declared: dict, registered: list[dict], main: Path) -> str | None:
    """A registry entry the adds would miss or re-point; it is never changed here."""
    for name, entry in declared.get("extraKnownMarketplaces", {}).items():
        if entry["source"].get("source") != "directory":
            continue
        ours = os.path.realpath(main / entry["source"]["path"])
        for row in registered:
            other, path = row.get("name"), row.get("path") or row.get("repo") or row.get("url")
            same = row.get("source") == "directory" and os.path.realpath(str(path)) == ours
            if same and other != name:
                why = f"{ours} is registered as marketplace '{other}', not '{name}'"
                return why + ", so the adds stop at 'already on disk'; " + REMEDY.format(other)
            if other == name and not same:
                why = f"marketplace '{name}' is registered from {path}, not from {ours}"
                return why + ", and an add would re-point it; " + REMEDY.format(name)
    return None


def install() -> int:
    main = main_checkout()
    declared = settings(main)
    refused = collision(
        declared, rows(["claude", "plugin", "marketplace", "list", "--json"], main), main
    )
    if refused:
        raise Stop(refused)
    try:
        run(["git", "check-ignore", "-q", LOCAL], main)
    except Stop as stop:
        # git-check-ignore(1): 1 is "None of the provided paths are ignored", 128 a fatal error.
        if stop.code != 1:
            raise
        raise Stop(f"git does not ignore {LOCAL} in {main}, which a local install writes") from None
    tracked = run(["git", "diff", "HEAD", "--binary"], main)
    for command in commands(declared):
        print("plugin_shelf: " + " ".join(command), flush=True)
        run(command, main, show=True)
    if run(["git", "diff", "HEAD", "--binary"], main) != tracked:
        raise Stop(f"a tracked file in {main} changed during the install; see `git diff`")
    print(f"ok: the shelf is installed from {main}; no tracked file changed; {LOCAL} is ignored")
    return 0


def check() -> int:
    main = main_checkout()
    here = {
        row.get("id")
        for row in rows(["claude", "plugin", "list", "--json"], ROOT)
        if os.path.realpath(str(row.get("projectPath"))) == str(main)
        and row.get("projectEnabled") is True
        and Path(str(row.get("installPath"))).is_dir()
    }
    absent = [plugin for plugin in enabled(settings(main)) if plugin not in here]
    if absent:
        raise Stop(
            f"enabled in the main checkout's .claude/settings.json but not installed for {main}: "
            + ", ".join(absent)
            + f"; fix: {FIX}"
        )
    print(f"ok: every plugin the main checkout's settings enable is installed for {main}")
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
