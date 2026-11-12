"""The two reads every pin proof makes: one dependency pin, and one `UPSTREAMS.md` row's tag.

Not a test module (pytest collects `test_*.py` only); the pin proofs import it as a sibling, which
pytest's default `prepend` import mode allows for this rootless folder. Each function takes the
file's text, so `test_pins.py` proves it on made-up text, and refuses any count other than one.
"""

import re
import tomllib


def pinned(pyproject: str, distribution: str) -> str:
    """The version `pyproject.toml`'s `[project]` dependencies pin `distribution` to, once."""
    project = tomllib.loads(pyproject)["project"]
    pins = [d for d in project["dependencies"] if d.startswith(distribution + "==")]
    assert len(pins) == 1, f"pyproject.toml pins {distribution} {len(pins)} times, not once"
    return pins[0].removeprefix(distribution + "==")


def upstream_tag(upstreams: str, name: str, repository: str) -> str:
    """The tag of the one `UPSTREAMS.md` row that names `name` and `repository`."""
    row = rf"^\| {re.escape(name)} \| {re.escape(repository)} \| (\S+) \| [0-9a-f]{{40}} \|"
    tags = re.findall(row, upstreams, re.MULTILINE)
    assert len(tags) == 1, f"UPSTREAMS.md has no single {name} row for {repository}"
    return tags[0]
