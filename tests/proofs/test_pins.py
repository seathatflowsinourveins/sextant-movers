"""The pin helper reads one dependency pin and one `UPSTREAMS.md` row, and refuses any other count.

`pins.py` is the one owner of the two reads the pin proofs make (`test_engine_pin.py`,
`test_alpaca_pin.py`). This proof feeds it made-up text, never this repository's files: a
distribution pinned twice, one not pinned, a row missing, a row twice, and one accepted case.
pytest's default `prepend` import mode puts this rootless folder on `sys.path`, so the sibling
module imports by name (docs.pytest.org, "pytest import mechanisms and sys.path/PYTHONPATH").
"""

import re

import pytest
from pins import pinned, upstream_tag

PYPROJECT = """
[project]
name = "made-up"
dependencies = ["made-up-engine==1.2.3", "made-up-engine-extra==9.9.9", "other==0.1"]
"""

TWICE = """
[project]
dependencies = ["made-up-engine==1.2.3", "made-up-engine==1.2.4"]
"""

UPSTREAMS = """
| Name | Repository | Tag | Commit | Use |
|---|---|---|---|---|
| made_up | example-org/made_up | v1.2.3 | 0123456789abcdef0123456789abcdef01234567 | engine |
| other | example-org/other | v0.1 | 89abcdef0123456789abcdef0123456789abcdef | other |
"""


def test_one_pin_and_one_row_are_read() -> None:
    assert pinned(PYPROJECT, "made-up-engine") == "1.2.3"
    assert upstream_tag(UPSTREAMS, "made_up", "example-org/made_up") == "v1.2.3"


def test_a_distribution_pinned_twice_is_refused() -> None:
    with pytest.raises(
        AssertionError, match=re.escape("pyproject.toml pins made-up-engine 2 times, not once")
    ):
        pinned(TWICE, "made-up-engine")


def test_a_distribution_not_pinned_is_refused() -> None:
    with pytest.raises(
        AssertionError, match=re.escape("pyproject.toml pins absent 0 times, not once")
    ):
        pinned(PYPROJECT, "absent")


def test_a_missing_row_is_refused() -> None:
    with pytest.raises(AssertionError, match=re.escape("UPSTREAMS.md has no single absent row")):
        upstream_tag(UPSTREAMS, "absent", "example-org/absent")


def test_a_row_of_another_repository_is_not_read() -> None:
    with pytest.raises(AssertionError, match=re.escape("UPSTREAMS.md has no single made_up row")):
        upstream_tag(UPSTREAMS, "made_up", "example-org/elsewhere")


def test_a_row_twice_is_refused() -> None:
    lines = UPSTREAMS.strip().splitlines()
    twice = "\n".join([*lines, lines[2]]) + "\n"
    with pytest.raises(AssertionError, match=re.escape("UPSTREAMS.md has no single made_up row")):
        upstream_tag(twice, "made_up", "example-org/made_up")
