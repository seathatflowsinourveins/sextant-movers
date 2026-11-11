"""XNYS session segments on the event clock, on the pinned exchange_calendars.

The behaviour is the reference's (`sextant/paper/sessions.py` at
06562bec0898a7203f4eaf69086ced83d2477e6b): half-open segments, the actual XNYS close, early
closes, the 17:00 early-close post end, the 20:00 to 04:00 overnight belonging to the next trading
day, CLOSED on a non-session day. Every date is asked of the pinned calendar, never typed in.
Source: gerrymanoim/exchange_calendars@4.13.2 dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a.
"""

import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from exchange_calendars import get_calendar

from sextant_movers import sessions
from sextant_movers.sessions import ET, SECOND, Session, session_at, session_start_ns

ROOT = Path(__file__).parents[2]
YEAR = 2026
CALENDAR = get_calendar("XNYS", start=date(2024, 12, 1), end=date(2027, 1, 31), side="left")
EARLY = set(CALENDAR.early_closes.date)
CLOSED, NIGHT, PRE, RTH, POST = (Session[n] for n in ("CLOSED", "OVERNIGHT", "PRE", "RTH", "POST"))
ONE = timedelta(1)


def ns(day: date, at: time) -> int:
    return int(datetime.combine(day, at, tzinfo=ET).timestamp()) * SECOND


def pre(day: date) -> int:
    return ns(day, sessions.PRE_START)


def night(day: date) -> int:
    return ns(day, sessions.OVERNIGHT_START)


def post_end(day: date) -> int:
    return ns(day, sessions.EARLY_CLOSE_POST_END)


def days(year: int) -> list[date]:
    first = date(year, 1, 1)
    return [first + n * ONE for n in range((date(year + 1, 1, 1) - first).days)]


def is_session(day: date) -> bool:
    return bool(CALENDAR.is_session(day))


def opening(day: date) -> int:
    return int(CALENDAR.session_open(day).value)


def closing(day: date) -> int:
    return int(CALENDAR.session_close(day).value)


def find(test: Callable[[date], bool]) -> date:
    return next(d for d in days(YEAR) if test(d))


def dst_changes(year: int) -> list[date]:
    def offset(d: date) -> timedelta | None:
        return datetime.combine(d, time(12), tzinfo=ET).utcoffset()

    return [d for d in days(year) if offset(d) != offset(d - ONE)]


def full_day() -> date:
    return find(lambda d: all(is_session(d + n * ONE) for n in (-1, 0, 1)) and d not in EARLY)


def test_a_full_session_is_half_open_to_the_nanosecond() -> None:
    day = full_day()
    cases = [
        (pre(day) - 1, NIGHT, night(day - ONE)),
        (pre(day), PRE, pre(day)),
        (opening(day) - 1, PRE, pre(day)),
        (opening(day), RTH, opening(day)),
        (opening(day) + 1, RTH, opening(day)),
        (closing(day) - 1, RTH, opening(day)),
        (closing(day), POST, closing(day)),
        (closing(day) + 1, POST, closing(day)),
        (post_end(day), POST, closing(day)),
        (night(day) - 1, POST, closing(day)),
        (night(day), NIGHT, night(day)),
        (night(day) + 1, NIGHT, night(day)),
    ]
    assert [(session_at(t), session_start_ns(t)) for t, *_ in cases] == [c[1:] for c in cases]


def test_an_early_close_ends_post_at_the_house_bound() -> None:
    day, full = find(lambda d: d in EARLY), full_day()
    assert closing(day) - ns(day, time(0)) < closing(full) - ns(full, time(0))
    want = [RTH, POST, POST, CLOSED, CLOSED]
    moments = [closing(day) - 1, closing(day), post_end(day) - 1, post_end(day), night(day) - 1]
    assert [session_at(t) for t in moments] == want
    assert session_start_ns(closing(day)) == closing(day)
    assert session_at(night(day)) is (NIGHT if is_session(day + ONE) else CLOSED)


def test_a_holiday_and_the_evening_before_it_are_closed() -> None:
    day = find(lambda d: d.weekday() < 5 and not is_session(d) and is_session(d + ONE))
    assert {session_at(ns(day, time(h))) for h in range(20)} == {CLOSED}
    assert session_at(night(day - ONE)) is CLOSED
    assert session_at(night(day)) is NIGHT


def test_a_weekend_is_closed_until_the_overnight_before_the_next_session() -> None:
    friday = find(lambda d: d.weekday() == 4 and is_session(d) and d not in EARLY)
    sunday = friday + 2 * ONE
    assert [session_at(night(friday) - 1), session_at(night(friday))] == [POST, CLOSED]
    assert session_at(ns(friday + ONE, time(12))) is CLOSED
    assert session_at(night(sunday) - 1) is CLOSED
    assert session_at(night(sunday)) is (NIGHT if is_session(sunday + ONE) else CLOSED)


@pytest.mark.parametrize("change", dst_changes(YEAR), ids=lambda d: f"dst-{d.month:02}")
def test_both_dst_changes_keep_the_local_bounds(change: date) -> None:
    assert len(dst_changes(YEAR)) == 2
    for day in (d for d in (change + n * ONE for n in range(-3, 4)) if is_session(d)):
        assert opening(day) == ns(day, time(9, 30))
        assert [session_at(opening(day) - 1), session_at(opening(day))] == [PRE, RTH]
        assert session_start_ns(ns(day, time(1))) == night(day - ONE)


@pytest.mark.parametrize("value", [1.0, "1", True, None, -1, -SECOND])
def test_non_integer_and_negative_input_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="integer event nanoseconds"):
        session_at(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer event nanoseconds"):
        session_start_ns(value)  # type: ignore[arg-type]


def year_grid(year: int, step: timedelta) -> list[int]:
    """Every step across the year, plus every edge one nanosecond below, at and above."""
    first, last = ns(date(year, 1, 1), time(0)), ns(date(year + 1, 1, 1), time(0))
    grid = set(range(first, last, int(step.total_seconds()) * SECOND))
    for day in days(year):
        edges = [ns(day, time(0)), pre(day), night(day), post_end(day)]
        edges += [opening(day), closing(day)] if is_session(day) else []
        grid.update(edge + step for edge in edges for step in (-1, 0, 1))
    return sorted(grid)


def test_every_start_precedes_its_moment_and_opens_the_same_segment() -> None:
    """A CLOSED start is the local midnight, as the reference's line 72, so it can fall in the
    overnight before a closed evening; the reference's ranking reads CLOSED as no segment."""
    grid = year_grid(YEAR, timedelta(hours=1))
    for t in grid:
        start, segment = session_start_ns(t), session_at(t)
        assert start <= t, t
        if segment is CLOSED:
            assert start == ns(datetime.fromtimestamp(t // SECOND, tz=ET).date(), time(0)), t
        else:
            assert session_at(start) is segment, t


def test_the_house_bounds_equal_their_registry_rows() -> None:
    manifest = (ROOT / "docs" / "manifest.md").read_text()
    section = manifest.split("\n## Registry\n", 1)[1].split("\n## ", 1)[0]
    table = [
        [c.strip() for c in r.strip("|").split("|")] for r in section.split("\n") if r[:1] == "|"
    ]
    rows = {row[0]: dict(zip(table[0], row, strict=True)) for row in table[2:]}
    for name in ("PRE_START", "OVERNIGHT_START", "EARLY_CLOSE_POST_END"):
        row = rows[f"`{name}`"]
        assert row["Value"] == getattr(sessions, name).isoformat("minutes"), name
        assert row["Status"] == "UNCALIBRATED", name


CHILD = """
import hashlib, os, sys
# -I ignores PYTHONPATH, so the child puts the directory its env names on its path itself.
sys.path.insert(0, os.environ["PYTHONPATH"])
from sextant.paper.sessions import session_at, session_start_ns
digest = hashlib.sha256()
for line in sys.stdin:
    t = int(line)
    digest.update(f"{t} {session_at(t).value} {session_start_ns(t)}\\n".encode())
print(digest.hexdigest())
"""


@pytest.mark.local
def test_the_reference_and_this_module_agree_over_two_years() -> None:
    reference = os.environ.get("MOVERS_REFERENCE_SRC")
    if not reference:
        pytest.skip("MOVERS_REFERENCE_SRC names no reference export; the parity proof needs it")
    grid = sorted({t for year in (2025, 2026) for t in year_grid(year, timedelta(minutes=30))})
    child = subprocess.run(
        [sys.executable, "-I", "-c", CHILD],
        input="".join(f"{t}\n" for t in grid),
        env={"PYTHONPATH": reference},
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
    )
    digest = hashlib.sha256()
    for t in grid:
        digest.update(f"{t} {session_at(t).value} {session_start_ns(t)}\n".encode())
    want = child.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", want), child.stderr
    print(f"parity grid {len(grid)} points; digests equal: {digest.hexdigest() == want}")
    assert digest.hexdigest() == want
