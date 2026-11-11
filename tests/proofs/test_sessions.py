"""XNYS session segments on the event clock, on the pinned exchange_calendars.

The behaviour to keep is the reference's (`sextant/paper/sessions.py` at
06562bec0898a7203f4eaf69086ced83d2477e6b, and its tests in `tests/proofs/test_paper_config.py`):
half-open segments, the actual XNYS open and close, early closes, the 17:00 end of an early-close
post session, the 20:00 to 04:00 overnight belonging to the next trading day, CLOSED on a day that
is not a session. Every date below is asked of the pinned calendar here, never typed in.

Source: gerrymanoim/exchange_calendars@4.13.2 dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a:
`get_calendar` (exchange_calendars/calendar_utils.py:237), `XNYSExchangeCalendar`
(exchange_calendars/exchange_calendar_xnys.py:76), `early_closes`, `session_open`,
`session_close`, `is_session` (exchange_calendars/exchange_calendar.py:990,1006,1012,1263).
"""

import hashlib
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest
from exchange_calendars import get_calendar
from sextant_movers.sessions import ET, SECOND, Session, session_at, session_start_ns

from sextant_movers import sessions

ROOT = Path(__file__).parents[2]
YEAR = 2026
PARITY_YEARS = (2025, 2026)
CALENDAR = get_calendar("XNYS", start=date(2024, 12, 1), end=date(2027, 1, 31), side="left")


def ns(day: date, at: time) -> int:
    return int(datetime.combine(day, at, tzinfo=ET).timestamp()) * SECOND


def days(year: int) -> list[date]:
    return [
        date(year, 1, 1) + timedelta(n)
        for n in range((date(year + 1, 1, 1) - date(year, 1, 1)).days)
    ]


def is_session(day: date) -> bool:
    return bool(CALENDAR.is_session(day))


def opening(day: date) -> int:
    return int(CALENDAR.session_open(day).value)


def closing(day: date) -> int:
    return int(CALENDAR.session_close(day).value)


def early(day: date) -> bool:
    return day in set(CALENDAR.early_closes.date)


def normal_day() -> date:
    """A full session between two sessions, so its overnight and its next overnight both run."""
    return next(
        d
        for d in days(YEAR)
        if is_session(d)
        and not early(d)
        and is_session(d - timedelta(1))
        and is_session(d + timedelta(1))
    )


def holiday() -> date:
    """A weekday that is not a session and is followed by a session."""
    return next(
        d
        for d in days(YEAR)
        if d.weekday() < 5 and not is_session(d) and is_session(d + timedelta(1))
    )


def early_close_day() -> date:
    return next(d for d in days(YEAR) if is_session(d) and early(d))


def dst_changes(year: int) -> list[date]:
    def offset(d: date) -> timedelta | None:
        return datetime.combine(d, time(12), tzinfo=ET).utcoffset()

    return [d for d in days(year) if offset(d) != offset(d - timedelta(1))]


def boundaries(day: date) -> list[int]:
    """Every segment edge on one ET day, from the calendar and the three house bounds."""
    edges = [ns(day, time(0)), ns(day, sessions.PRE_START), ns(day, sessions.OVERNIGHT_START)]
    edges.append(ns(day, sessions.EARLY_CLOSE_POST_END))
    if is_session(day):
        edges += [opening(day), closing(day)]
    return [edge + step for edge in edges for step in (-1, 0, 1)]


def test_the_calendar_supplies_the_days_these_cases_use() -> None:
    day, off, short = normal_day(), holiday(), early_close_day()
    assert closing(short) < closing(day)
    assert not is_session(off) and off.weekday() < 5
    assert len(dst_changes(YEAR)) == 2


def test_a_normal_day_is_half_open_to_the_nanosecond() -> None:
    day = normal_day()
    cases = [
        (ns(day, sessions.PRE_START) - 1, Session.OVERNIGHT),
        (ns(day, sessions.PRE_START), Session.PRE),
        (opening(day) - 1, Session.PRE),
        (opening(day), Session.RTH),
        (opening(day) + 1, Session.RTH),
        (closing(day) - 1, Session.RTH),
        (closing(day), Session.POST),
        (closing(day) + 1, Session.POST),
        (ns(day, sessions.EARLY_CLOSE_POST_END), Session.POST),
        (ns(day, sessions.OVERNIGHT_START) - 1, Session.POST),
        (ns(day, sessions.OVERNIGHT_START), Session.OVERNIGHT),
        (ns(day, sessions.OVERNIGHT_START) + 1, Session.OVERNIGHT),
    ]
    assert [session_at(t) for t, _ in cases] == [want for _, want in cases]


def test_each_segment_starts_where_the_calendar_and_the_bounds_say() -> None:
    day = normal_day()
    before = day - timedelta(1)
    assert session_start_ns(ns(day, time(3))) == ns(before, sessions.OVERNIGHT_START)
    assert session_start_ns(ns(day, sessions.OVERNIGHT_START) + 1) == ns(
        day, sessions.OVERNIGHT_START
    )
    assert session_start_ns(opening(day) - 1) == ns(day, sessions.PRE_START)
    assert session_start_ns(opening(day)) == opening(day)
    assert session_start_ns(closing(day) - 1) == opening(day)
    assert session_start_ns(closing(day)) == closing(day)
    assert session_start_ns(ns(day, sessions.OVERNIGHT_START) - 1) == closing(day)


def test_an_early_close_ends_post_at_the_house_bound() -> None:
    day = early_close_day()
    assert session_at(closing(day) - 1) is Session.RTH
    assert session_at(closing(day)) is Session.POST
    assert session_start_ns(closing(day)) == closing(day)
    assert session_at(ns(day, sessions.EARLY_CLOSE_POST_END) - 1) is Session.POST
    assert session_at(ns(day, sessions.EARLY_CLOSE_POST_END)) is Session.CLOSED
    assert session_at(ns(day, sessions.OVERNIGHT_START) - 1) is Session.CLOSED
    after = Session.OVERNIGHT if is_session(day + timedelta(1)) else Session.CLOSED
    assert session_at(ns(day, sessions.OVERNIGHT_START)) is after


def test_a_holiday_and_its_overnight_are_closed() -> None:
    day = holiday()
    assert {session_at(ns(day, time(h))) for h in range(20)} == {Session.CLOSED}
    assert session_at(ns(day - timedelta(1), sessions.OVERNIGHT_START)) is Session.CLOSED
    assert session_at(ns(day, sessions.OVERNIGHT_START)) is Session.OVERNIGHT


def test_a_weekend_is_closed_until_the_overnight_before_the_next_session() -> None:
    friday = next(d for d in days(YEAR) if d.weekday() == 4 and is_session(d) and not early(d))
    sunday = friday + timedelta(2)
    assert session_at(ns(friday, sessions.OVERNIGHT_START) - 1) is Session.POST
    assert session_at(ns(friday, sessions.OVERNIGHT_START)) is Session.CLOSED
    assert session_at(ns(friday + timedelta(1), time(12))) is Session.CLOSED
    assert session_at(ns(sunday, sessions.OVERNIGHT_START) - 1) is Session.CLOSED
    want = Session.OVERNIGHT if is_session(sunday + timedelta(1)) else Session.CLOSED
    assert session_at(ns(sunday, sessions.OVERNIGHT_START)) is want


@pytest.mark.parametrize("change", dst_changes(YEAR), ids=lambda d: f"dst-{d.month:02}")
def test_both_dst_changes_keep_the_local_bounds(change: date) -> None:
    week = [change + timedelta(n) for n in range(-3, 4)]
    for day in (d for d in week if is_session(d)):
        assert opening(day) == ns(day, time(9, 30))
        assert session_at(opening(day) - 1) is Session.PRE
        assert session_at(opening(day)) is Session.RTH
        assert session_start_ns(ns(day, time(3))) == ns(
            day - timedelta(1), sessions.OVERNIGHT_START
        )
    sunday = next(d for d in week if d.weekday() == 6)
    if is_session(sunday + timedelta(1)):
        assert session_at(ns(sunday, sessions.OVERNIGHT_START)) is Session.OVERNIGHT
        assert session_start_ns(ns(sunday + timedelta(1), time(1))) == ns(
            sunday, sessions.OVERNIGHT_START
        )


@pytest.mark.parametrize("value", [1.0, "1", True, None, -1, -SECOND])
def test_non_integer_and_negative_input_is_refused(value: object) -> None:
    with pytest.raises(ValueError, match="integer event nanoseconds"):
        session_at(value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="integer event nanoseconds"):
        session_start_ns(value)  # type: ignore[arg-type]


def year_grid(year: int, step: timedelta) -> list[int]:
    first = ns(date(year, 1, 1), time(0))
    last = ns(date(year + 1, 1, 1), time(0))
    grid = set(range(first, last, int(step.total_seconds()) * SECOND))
    for day in days(year):
        grid.update(boundaries(day))
    return sorted(grid)


def test_every_start_precedes_its_moment_and_opens_the_same_segment() -> None:
    """Over a year: the start is at or before the moment, and outside CLOSED it opens the same
    segment. A CLOSED start is the local midnight, as in the reference (its line 72), which can
    fall in the overnight segment before a closed evening; the reference's ranking reads CLOSED
    as no segment (`segment_start`, `sextant/ranking/baseline.py:350-366`)."""
    grid = year_grid(YEAR, timedelta(hours=1))
    for t in grid:
        start, segment = session_start_ns(t), session_at(t)
        assert start <= t, t
        if segment is Session.CLOSED:
            local = datetime.fromtimestamp(t // SECOND, tz=ET)
            assert start == ns(local.date(), time(0)), t
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
# -I ignores PYTHONPATH, so the child puts the directory its env names on its own path.
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
    grid = sorted({t for year in PARITY_YEARS for t in year_grid(year, timedelta(minutes=30))})
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
