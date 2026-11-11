"""docs/landscape.md covers the package and keeps its own promises.

Every top-level package or module under src/sextant_movers/ is named in some row's Package cell,
so coverage cannot drift (docs/design.md, "Landscape coverage"; ADR-0002 item 5). Each row of the
Capabilities and runner-up tables holds one of the landscape's own four states; an adopted or
candidate row has a 40-hex pin or says "not pinned yet:" and why; a no-source row says why; a
rejected row covers no package. The package imports neither Sextant nor a trading client.

Source: this repository's pattern of a Markdown table a test reads
(tests/proofs/test_manifest_union.py). The tables and the states are read from the file; the test
keeps no second list.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
LANDSCAPE = ROOT / "docs" / "landscape.md"
PACKAGE = ROOT / "src" / "sextant_movers"
SHA = re.compile(r"\b[0-9a-f]{40}\b")
PATH = re.compile(r"`([^`]+)`")
# Sextant itself (the dependency runs one way) and alpaca-py's trading client, which places orders:
# alpacahq/alpaca-py@v0.44.0 `cc4cb3b7ba50ae250e621983c2779047fb16bb28`, `alpaca/trading/client.py`.
FORBIDDEN = ("sextant", "alpaca.trading")
Row = dict[str, str]


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _tables(text: str) -> dict[str, list[Row]]:
    """Each `## ` section's table, as rows keyed by its header cells."""
    found: dict[str, list[Row]] = {}
    for section in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        title, _, body = section.partition("\n")
        lines = [line for line in body.splitlines() if line.startswith("|")]
        if len(lines) > 2:
            header = _cells(lines[0])
            found[title.strip()] = [dict(zip(header, _cells(x), strict=True)) for x in lines[2:]]
    return found


def _states(text: str) -> list[str]:
    return re.findall(r"^- \*\*([a-z ]+)\*\*:", text, flags=re.MULTILINE)


def _state(cell: str, states: list[str]) -> str | None:
    found = re.search(r"\b(" + "|".join(map(re.escape, states)) + r")\b", cell)
    return found.group(1) if found else None


def _sources(package: Path) -> set[str]:
    """Top-level packages as `name/` and modules as `name.py`; `__init__.py` is the package."""
    found: set[str] = set()
    for child in package.iterdir():
        if child.is_dir() and any(child.rglob("*.py")):
            found.add(f"{child.name}/")
        elif child.suffix == ".py" and child.name != "__init__.py":
            found.add(child.name)
    return found


def uncovered(package: Path, text: str) -> list[str]:
    named = {path for row in _tables(text)["Capabilities"] for path in PATH.findall(row["Package"])}
    return sorted(_sources(package) - named)


def _rows(text: str) -> list[tuple[str, Row]]:
    """(state cell, row) for the Capabilities and the runner-up tables."""
    tables = _tables(text)
    capabilities = [(row["Chosen and state"], row) for row in tables["Capabilities"]]
    others = [(row["State"], row) for row in tables["Runner-ups and other candidates"]]
    return capabilities + others


def test_the_landscape_defines_four_states() -> None:
    assert len(_states(LANDSCAPE.read_text())) == 4


def test_every_source_package_or_module_has_a_landscape_row() -> None:
    missing = uncovered(PACKAGE, LANDSCAPE.read_text())
    assert not missing, f"docs/landscape.md has no row whose Package cell names: {missing}"


def test_an_uncovered_package_fails_and_a_row_covers_it(tmp_path: Path) -> None:
    for name in ("__init__.py", "contract.py", "scores/__init__.py", "__pycache__/x.pyc"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("")
    text = (
        "## Capabilities\n\n| Capability | Package | Chosen and state |\n|---|---|---|\n"
        "| The contract | `contract.py` | x (adopted) |\n"
    )
    assert uncovered(tmp_path, text) == ["scores/"]
    assert uncovered(tmp_path, text + "| Scores | `scores/` | y (adopted) |\n") == []


def test_every_row_has_one_of_the_four_states() -> None:
    text = LANDSCAPE.read_text()
    stateless = [row for cell, row in _rows(text) if _state(cell, _states(text)) is None]
    assert not stateless, f"rows without a defined state: {stateless}"


def test_an_adopted_or_candidate_row_is_pinned_or_says_why_not() -> None:
    text = LANDSCAPE.read_text()
    unpinned = [
        row
        for cell, row in _rows(text)
        if _state(cell, _states(text)) in ("adopted", "candidate")
        and not SHA.search(row.get("Pin", row.get("Tag and commit", "")))
        and not re.match(r"not pinned yet: \S", row.get("Pin", ""))
    ]
    assert not unpinned, f"adopted or candidate rows with no pin and no reason: {unpinned}"


def test_a_no_source_row_says_why() -> None:
    text = LANDSCAPE.read_text()
    silent = [
        row
        for cell, row in _rows(text)
        if _state(cell, _states(text)) == "no source"
        and not re.search(r"no source[^:]*: \S", cell)
        and row.get("Runner-up or note", "none") == "none"
    ]
    assert not silent, f"no-source rows that do not say why: {silent}"


def test_a_rejected_row_covers_no_package() -> None:
    text = LANDSCAPE.read_text()
    rows = _tables(text)["Capabilities"]
    covering = [
        row["Capability"]
        for row in rows
        if _state(row["Chosen and state"], _states(text)) == "rejected"
        and PATH.search(row["Package"])
    ]
    assert not covering, f"rejected rows that name a package path: {covering}"


def test_the_package_imports_neither_sextant_nor_a_trading_client() -> None:
    found: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            else:
                continue
            for name in names:
                if any(name == f or name.startswith(f"{f}.") for f in FORBIDDEN):
                    found.append(f"{path.relative_to(ROOT)}: {name}")
    assert not found, f"forbidden imports: {found}"
