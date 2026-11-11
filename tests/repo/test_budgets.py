"""Size budgets, from the first ask: never bloat. Raising one takes a PR with a reason."""

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


def test_design_stays_short() -> None:
    assert _lines(ROOT / "docs" / "design.md") <= 220


def test_practices_stay_short() -> None:
    assert _lines(ROOT / "docs" / "handbook" / "practices.md") <= 80
