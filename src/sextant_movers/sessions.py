"""XNYS session segments on the event clock: OVERNIGHT, PRE, RTH, POST, or CLOSED.

The exchange schedule only, never broker or instrument eligibility. Segments are half-open on
integer event nanoseconds. Each date fact (a session day, its open, its close, an early close)
comes from the pinned calendar, which has no pre, post or overnight segment, so those three bounds
are house values, each a `docs/manifest.md` Registry row. The 20:00 to 04:00 overnight belongs to
the next trading day.

Source: gerrymanoim/exchange_calendars@4.13.2 dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a:
`get_calendar` (calendar_utils.py:237), `XNYSExchangeCalendar` (exchange_calendar_xnys.py:76),
`early_closes`, `session_open`, `session_close`, `is_session` (exchange_calendar.py:990, 1006,
1012, 1263). Behaviour: the reference, `sextant/paper/sessions.py` at
06562bec0898a7203f4eaf69086ced83d2477e6b.
"""

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import cache
from zoneinfo import ZoneInfo

from exchange_calendars import ExchangeCalendar, get_calendar

ET = ZoneInfo("America/New_York")
SECOND = 1_000_000_000
PRE_START = time(4)
OVERNIGHT_START = time(20)
EARLY_CLOSE_POST_END = time(17)


class Session(StrEnum):
    OVERNIGHT = "overnight"
    PRE = "pre"
    RTH = "rth"
    POST = "post"
    CLOSED = "closed"


@cache
def _calendar(year: int) -> ExchangeCalendar:
    # A month past the year on each side, as the reference's `_calendar`, so the year's first and
    # last days lie inside the calendar's first and last sessions (it refuses a date outside them).
    return get_calendar("XNYS", start=date(year - 1, 12, 1), end=date(year + 1, 1, 31), side="left")


@cache
def _early_closes(year: int) -> frozenset[date]:
    return frozenset(_calendar(year).early_closes.date)


def _at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=ET)


def _segment(ts_ns: int) -> tuple[Session, datetime]:
    if type(ts_ns) is not int or ts_ns < 0:
        raise ValueError("integer event nanoseconds required")
    local = datetime.fromtimestamp(ts_ns // SECOND, tz=ET)
    at = local.time()
    day = local.date() + timedelta(days=1) if at >= OVERNIGHT_START else local.date()
    calendar = _calendar(day.year)
    if not calendar.is_session(day):
        return Session.CLOSED, _at(local.date(), time(0))
    if at >= OVERNIGHT_START or at < PRE_START:
        return Session.OVERNIGHT, _at(day - timedelta(days=1), OVERNIGHT_START)
    opening = calendar.session_open(day).tz_convert(ET).to_pydatetime()
    if local < opening:
        return Session.PRE, _at(day, PRE_START)
    close = calendar.session_close(day).tz_convert(ET).to_pydatetime()
    if local < close:
        return Session.RTH, opening
    if at >= EARLY_CLOSE_POST_END and day in _early_closes(day.year):
        return Session.CLOSED, _at(day, time(0))
    return Session.POST, close


def session_at(ts_ns: int) -> Session:
    """The segment holding ``ts_ns``; CLOSED on a day that is not a session, and from the
    early-close post end until the overnight."""
    return _segment(ts_ns)[0]


def session_start_ns(ts_ns: int) -> int:
    """The start of the segment holding ``ts_ns``, in whole seconds; a CLOSED moment starts at its
    local midnight, as in the reference."""
    return int(_segment(ts_ns)[1].timestamp()) * SECOND
