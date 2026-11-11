"""Size budgets, from the first ask: never bloat. Raising one takes a PR with a reason."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _lines(path: Path) -> int:
    return len(path.read_text().splitlines())


def test_agent_files_stay_short() -> None:
    assert _lines(ROOT / "AGENTS.md") + _lines(ROOT / "CLAUDE.md") < 120


def test_justfile_stays_short() -> None:
    assert _lines(ROOT / "justfile") < 105


def test_at_most_thirty_one_page_adrs() -> None:
    adrs = list((ROOT / "docs" / "adr").glob("*.md"))
    assert len(adrs) <= 30
    assert all(_lines(adr) <= 80 for adr in adrs)


def test_src_stays_under_budget() -> None:
    # An uncalibrated starting cap for src/sextant_movers/, the only package under src/: the
    # reference ranking and display code measure about 10,350 lines, plus helpers. Raise it only by
    # a PR that states a reason and its measurement. consolidation.yml reads the cap from the line
    # below, so keep its shape.
    assert sum(_lines(path) for path in (ROOT / "src").rglob("*.py")) < 12000


def test_the_src_cap_matches_its_registry_row() -> None:
    # Issue 4: the cap is a tunable value, so docs/manifest.md's Registry table holds its row.
    shape = r'\(ROOT / "src"\)\.rglob\("\*\.py"\)\) < ([0-9_]+)$'
    caps = re.findall(shape, Path(__file__).read_text(), flags=re.MULTILINE)
    manifest = (ROOT / "docs" / "manifest.md").read_text()
    assert "\n## Registry\n" in manifest, "docs/manifest.md has no Registry section"
    section = manifest.split("\n## Registry\n", 1)[1].split("\n## ", 1)[0]
    table = [
        [c.strip() for c in r.strip("|").split("|")] for r in section.split("\n") if r[:1] == "|"
    ]
    rows = [dict(zip(table[0], row, strict=True)) for row in table[2:]]
    row = [r for r in rows if r["Name"] == "`src_line_cap`"]
    assert len(caps) == 1 and len(row) == 1, (caps, row)
    assert int(row[0]["Value"]) == int(caps[0].replace("_", ""))
    assert row[0]["Status"] == "UNCALIBRATED"


def test_design_stays_short() -> None:
    assert _lines(ROOT / "docs" / "design.md") <= 220


def test_practices_stay_short() -> None:
    assert _lines(ROOT / "docs" / "handbook" / "practices.md") <= 80
