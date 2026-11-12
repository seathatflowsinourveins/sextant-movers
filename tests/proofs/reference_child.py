"""The shared reference-child call for the local parity proofs.

Build on python/cpython@v3.13.18 `0d4bd512fc429e3d4eacdd113c975e161f21644d`:
`Doc/library/subprocess.rst:38-93,128-158` (run, explicit env, captured stderr),
`Doc/using/cmdline.rst:308-316` (isolated interpreter). The timeout and stderr-tail
length retain the existing parity proofs' bounds. Every child disables bytecode writes.
pytest's default prepend import mode and imperative skip are used as documented at
docs.pytest.org/en/stable/explanation/pythonpath.html and how-to/skipping.html,
read 2026-11-11. Each proof still owns its child script, versions and comparison.
"""

import json
import os
import subprocess
from typing import Any

import pytest

NEEDS = "set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof"


def reference() -> tuple[str, str]:
    """Return the reference source and interpreter, or skip with the shared reason."""
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip(NEEDS)
    return src, python


def run(
    python: str,
    src: str,
    script: str,
    *args: str,
    stdin: str | None = None,
    timeout: int = 600,
) -> Any:
    """Run an isolated child and parse its PARITY answer, showing stderr on failure."""
    done = subprocess.run(
        [python, "-I", "-B", "-c", script, *args],
        input=stdin,
        env={"MOVERS_REFERENCE_SRC": src, "PATH": os.environ.get("PATH", os.defpath)},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert done.returncode == 0, f"reference child exited {done.returncode}\n{done.stderr[-2000:]}"
    line = next((line for line in done.stdout.splitlines() if line.startswith("PARITY ")), None)
    assert line is not None, f"reference child: no PARITY line\n{done.stdout[-2000:]}"
    return json.loads(line.removeprefix("PARITY "))
