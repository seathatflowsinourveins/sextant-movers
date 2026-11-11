"""The atomic file write: a reader sees the old file or the new one whole, never a part.

The behaviour is the reference's `write_atomic` (`sextant/execution/state.py:13-29` at
06562bec0898a7203f4eaf69086ced83d2477e6b), on the standard library (see `atomic.py`).
"""

import json
import os
from pathlib import Path

import pytest

from sextant_movers.atomic import write_atomic

VALUE = {"b": [1, 2, 3], "a": {"nested": "text"}}


def test_the_file_holds_sorted_json_and_no_temporary_is_left(tmp_path: Path) -> None:
    path = tmp_path / "state" / "doc.json"
    write_atomic(path, VALUE)
    assert path.read_text() == json.dumps(VALUE, sort_keys=True)
    assert [p.name for p in path.parent.iterdir()] == ["doc.json"]


def test_an_existing_file_is_replaced_whole(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    path.write_text(json.dumps({"old": "x" * 4096}))
    write_atomic(path, {"new": 1})
    assert path.read_text() == '{"new": 1}'
    assert [p.name for p in tmp_path.iterdir()] == ["doc.json"]


def test_a_failing_dump_leaves_the_old_file_and_no_temporary(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    write_atomic(path, VALUE)
    with pytest.raises(TypeError):
        write_atomic(path, {"bad": object()})
    assert json.loads(path.read_text()) == VALUE
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doc.json"]


def test_a_crash_before_the_replace_leaves_the_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "doc.json"
    write_atomic(path, VALUE)

    def crash(src: str, dst: str) -> None:
        raise OSError("simulated crash between write and replace")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        write_atomic(path, {"new": 1})
    assert json.loads(path.read_text()) == VALUE
    assert sorted(p.name for p in tmp_path.iterdir()) == ["doc.json"]


def test_the_file_then_the_directory_are_synced_around_the_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "doc.json"
    events: list[tuple[str, object]] = []
    directories: set[int] = set()
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def spy_open(target: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(target, flags, *args, **kwargs)  # type: ignore[arg-type]
        if Path(str(target)) == tmp_path:
            directories.add(fd)
        return fd

    def spy_fsync(fd: int) -> None:
        events.append(("fsync-directory" if fd in directories else "fsync-file", fd))
        real_fsync(fd)

    def spy_replace(src: str, dst: str) -> None:
        events.append(("replace", Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)
    write_atomic(path, VALUE)
    assert [name for name, _ in events] == ["fsync-file", "replace", "fsync-directory"]
    assert events[1] == ("replace", "doc.json")
