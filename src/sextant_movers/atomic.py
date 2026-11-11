"""The atomic file write: a reader sees the old file or the new one whole, never a part.

The value is written as sorted JSON to a temporary file in the target's own directory, flushed
and synced, renamed over the target, and the directory is synced so the rename survives a crash.
On any failure the temporary file is removed and the target keeps its old content. The file has
`mkstemp`'s mode, readable and writable only by its owner.

Source: the CPython 3.13 standard library, read 2026-11-11:
https://docs.python.org/3.13/library/tempfile.html (`mkstemp`: no race in the file's creation; the
caller closes and deletes it) and https://docs.python.org/3.13/library/os.html (`os.replace`: an
atomic rename, which may fail across filesystems, hence the same directory; `os.fsync` after a
flush). Behaviour: the reference, `sextant/execution/state.py:13-29` at
06562bec0898a7203f4eaf69086ced83d2477e6b.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
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
