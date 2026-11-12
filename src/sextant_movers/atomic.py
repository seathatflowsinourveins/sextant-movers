"""The atomic file write: a reader sees the old file or the new one whole, never a part.

The value is written as sorted JSON, or text as UTF-8 without newline translation, through one
shared core to a temporary file in the target's own directory, flushed
and synced, renamed over the target, and the directory is synced so the rename survives a crash.
On a failure before the rename the temporary file is removed and the target keeps its old content.
A failure after the rename (opening or syncing the directory) raises while the target already
holds the new content whole; what is then unknown is only whether the rename survives a crash.
The file has `mkstemp`'s mode, readable and writable only by its owner.

Source: python/cpython@v3.13.18 `0d4bd512fc429e3d4eacdd113c975e161f21644d`,
`Lib/tempfile.py:372`, `Doc/library/tempfile.rst:230`, `Doc/library/os.rst:1108,2754`,
read 2026-11-11:
https://docs.python.org/3.13/library/tempfile.html (`mkstemp`: no race in the file's creation; the
caller closes and deletes it) and https://docs.python.org/3.13/library/os.html (`os.replace`: an
atomic rename, which may fail across filesystems, hence the same directory; `os.fsync` after a
flush). Behaviour: the reference, `sextant/execution/state.py:13-29` at
06562bec0898a7203f4eaf69086ced83d2477e6b.
"""

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO


def write_atomic(path: Path, value: Any) -> None:
    """Write sorted JSON, retaining the JSON writer's encoding and newline behaviour."""
    _write_atomic(path, lambda stream: json.dump(value, stream, sort_keys=True))


def write_atomic_text(path: Path, text: str) -> None:
    """Write the exact text as UTF-8, through the same atomic write as JSON."""
    _write_atomic(path, lambda stream: stream.write(text), encoding="utf-8", newline="")


def _write_atomic(
    path: Path,
    write: Callable[[TextIO], object],
    *,
    encoding: str | None = None,
    newline: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
