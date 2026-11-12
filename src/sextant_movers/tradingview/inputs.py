"""Read one three-file display unit; display only, no host reached or file written.

Build on nautechsystems/nautilus_trader@v2.0.0rc8
 d53849b216e20f2c122ca01b4dd0da377638b186:python/nautilus_trader/core/datetime.py:81,122
(public Timestamp bridge; private full-range datetime bridge, public overflows outside ns range).
Build on gerrymanoim/exchange_calendars@4.13.2
 dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a:exchange_calendars/calendar_utils.py:237,
exchange_calendars/exchange_calendar.py:1263 (bounded XNYS calendar, is_session).
Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Doc/library/datetime.rst:183,208,1062; Doc/library/json.rst:308; Doc/library/re.rst:670,952.
Wire grammar: RFC 3339 section 5.6, read 2026-11-11; reference extensions retained.
Acceptance: the reference, src/sextant/tradingview/inputs.py:1-252 at
06562bec0898a7203f4eaf69086ced83d2477e6b. Caller supplies max_age; unused slot omitted;
ET is shared. sessions.py exports no public date-only calendar query, so retain this calendar.
The four acceptance bounds are UNCALIBRATED Registry rows, never measured success.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any

from exchange_calendars import get_calendar
from nautilus_trader.core.datetime import _datetime_to_unix_nanos, dt_to_unix_nanos
from pandas import Timestamp

from sextant_movers.sessions import ET

NAMES = ("NS movers", "NS focus")
QUALIFIED = re.compile(r"[A-Z0-9_]+:[A-Z0-9_.!\-]+\Z")
TICKER = re.compile(r"[A-Z0-9_.!\-]+\Z")
RFC3339 = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?"
    r"(Z|[+-](?:[01]\d|2[0-3]):?[0-5]\d)"
)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MAX_MEMBERS = 100
MAX_UNMAPPED = 5
MAX_SNAPSHOT_LAG_S = 600
MAX_AGE_RANGE = (1, 3600)
REFUSED_PREFIXES = frozenset({"SMART", "IB", "IBKR", "ALPACA"})


class InputError(ValueError):
    """Unavailable input never means an empty desired list."""


@dataclass(frozen=True)
class ResearchInputs:
    memberships: dict[str, frozenset[str]]
    expires_at: datetime
    unmapped: frozenset[str]
    generation: dict[str, str]


def timestamp(value: Any) -> datetime:
    # Calendar view only; comparisons retain integer nanoseconds.
    return EPOCH + timedelta(microseconds=_ns(value) // 1000)


def _ns(value: Any) -> int:
    """Wire validation plus Nautilus rc8's pinned, full-range integer bridge."""
    try:
        if isinstance(value, str) and (match := RFC3339.fullmatch(value)):
            whole, decimal, zone = match.groups()
            fraction = int((decimal or "0").ljust(9, "0"))
            result = datetime.fromisoformat(whole + zone)
        elif isinstance(value, Timestamp):
            if value.tzinfo is not None:
                return dt_to_unix_nanos(value)
            raise ValueError
        elif isinstance(value, datetime):
            result = value
            fraction = result.microsecond * 1000
            result = result.replace(microsecond=0)
        else:
            raise ValueError
        if result.utcoffset() is not None:
            result = result.astimezone(UTC).replace(microsecond=0)
            return _datetime_to_unix_nanos(result) + fraction
    except (TypeError, ValueError):
        pass
    raise InputError("Expected an explicit timezone-aware timestamp")


@cache
def _calendar(year: int):
    try:
        return get_calendar("XNYS", start=date(year - 1, 12, 1), end=date(year + 1, 1, 31))
    except ValueError:
        raise InputError("No supported XNYS trading date for this input") from None


def _session(at: datetime) -> tuple[str, bool]:
    local = at.astimezone(ET)
    overnight = local.time() >= time(20) or local.time() < time(4)
    day = local.date() + timedelta(days=1) if local.time() >= time(20) else local.date()
    if not _calendar(day.year).is_session(day):
        raise InputError("No supported XNYS trading date for this input")
    return day.isoformat(), overnight


def _read(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(), parse_float=Decimal)
        if isinstance(doc, dict):
            return doc
    except (OSError, ValueError):
        pass
    raise InputError("Missing or invalid research unit; lists NOT synced")


def _envelope(doc: dict[str, Any], schema: str, now: datetime, max_age: int):
    if (
        doc.get("schema") != schema
        or doc.get("status") != "fresh"
        or doc.get("snapshot_status") != "observed"
    ):
        raise InputError("Research generation is unavailable; lists NOT synced")
    snapshot, as_of, generated = (
        timestamp(doc.get(key)) for key in ("as_of_snapshot", "as_of", "generated_at")
    )
    clocks = tuple(_ns(doc.get(key)) for key in ("as_of_snapshot", "as_of", "generated_at"))
    if not clocks[0] <= clocks[1] <= clocks[2] <= _ns(now):
        raise InputError("Inconsistent or future research clocks")
    if (
        _ns(now) - clocks[0] >= max_age * 1_000_000_000
        or clocks[1] - clocks[0] > MAX_SNAPSHOT_LAG_S * 1_000_000_000
    ):
        raise InputError("Research generation expired; lists NOT synced")
    session, overnight = _session(now)
    if doc.get("session_date") != session or any(
        _session(at)[0] != session for at in (snapshot, as_of, generated)
    ):
        raise InputError("Research belongs to a different Eastern trading session")
    if overnight:
        expected = {
            "session": "overnight",
            "session_date": session,
            "intraday_feed": "boats",
            "daily_history_feed": "sip",
            "daily_history_scope": "prior completed XNYS sessions; not BOATS intraday evidence",
        }
        if doc.get("ranking_context") != expected or not isinstance(
            doc.get("research_inputs"), dict
        ):
            raise InputError("Overnight input requires explicit BOATS provenance")
    return clocks


def _members(rows: Any, identities: dict[str, Any], reverse: dict[str, str], unmapped: set[str]):
    if not isinstance(rows, list):
        raise InputError("Research members must be an array")
    result: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise InputError("Invalid research member")
        symbol, qualified = row.get("symbol"), row.get("tradingview_symbol")
        if not isinstance(symbol, str) or not TICKER.fullmatch(symbol) or symbol in seen:
            raise InputError("Research tickers must be explicit and unique")
        seen.add(symbol)
        if symbol in identities and identities[symbol] != qualified:
            raise InputError("Cross-list TradingView mappings disagree")
        identities[symbol] = qualified
        if qualified is None:
            unmapped.add(symbol)
            continue
        if (
            not isinstance(qualified, str)
            or not QUALIFIED.fullmatch(qualified)
            or qualified.split(":", 1)[0] in REFUSED_PREFIXES
        ):
            raise InputError("Research needs exact TradingView exchange mappings")
        if qualified in result or (qualified in reverse and reverse[qualified] != symbol):
            raise InputError("Research mappings alias different tickers")
        reverse[qualified] = symbol
        result.add(qualified)
    return frozenset(result)


def read_research_inputs(unit: Path, *, now: datetime, max_age: int) -> ResearchInputs:
    """Resolve latest once and bind all three documents before OAuth or any write."""
    if (
        now.utcoffset() is None
        or type(max_age) is not int
        or not MAX_AGE_RANGE[0] <= max_age <= MAX_AGE_RANGE[1]
    ):
        raise InputError("Need an aware clock and max age 1..3600 seconds")
    unit = unit.resolve()
    top, focus, display = (
        _read(unit / name) for name in ("top100.json", "focus.json", "display-lists.json")
    )
    clocks = _envelope(top, "morning-pack.candidates/1", now, max_age)
    for doc, schema in ((focus, "top100.focus/1"), (display, "ns-display-lists/1")):
        if _envelope(doc, schema, now, max_age) != clocks or any(
            doc.get(key) != top.get(key)
            for key in ("generation", "ranking_context", "research_inputs")
        ):
            raise InputError("Research files must share one exact producer generation")
    generation = display.get("generation")
    if (
        not isinstance(generation, dict)
        or set(generation) != {"as_of", "sha256"}
        or not isinstance(generation["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", generation["sha256"])
        or _ns(generation["as_of"]) != clocks[0]
    ):
        raise InputError("Invalid snapshot generation identity")
    try:
        snapshot = display["sources"]["snapshot"]
        bounds = display["bounds"]
        values = [Decimal(str(bounds[key])) for key in ("gap_bound", "rvol_bound")]
        valid = (
            all(value.is_finite() and value > 0 for value in values)
            and type(bounds["max_age_s"]) is int
            and bounds["max_age_s"] > 0
            and all(snapshot.get(key) == value for key, value in generation.items())
            and display["armed"] in {"observed", "UNVERIFIED"}
            and type(display["excluded_unknown"]) is int
            and display["excluded_unknown"] >= 0
        )
        for name in ("ns_movers", "ns_focus"):
            rows = display[name]
            valid = valid and isinstance(rows, list) and type(display["counts"][name]) is int
            valid = valid and display["counts"][name] == len(rows)
            valid = valid and all(
                isinstance(row.get("labels"), list)
                and row["labels"]
                and all(isinstance(label, str) and label for label in row["labels"])
                for row in rows
            )
        if not valid or len(display["ns_movers"]) > MAX_MEMBERS:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        raise InputError("Invalid producer display contract") from None
    effective = min(max_age, bounds["max_age_s"])
    if _ns(now) - clocks[0] >= effective * 1_000_000_000:
        raise InputError("Research generation expired; lists NOT synced")
    parent_unmapped: set[str] = set()
    parent = top.get("candidates")
    if (
        not isinstance(parent, list)
        or len(parent) > MAX_MEMBERS
        or not _members(parent, {}, {}, parent_unmapped)
        or len(parent_unmapped) > MAX_UNMAPPED
    ):
        raise InputError("Parent ranking empty or more than five unmapped names")
    identities: dict[str, Any] = {}
    reverse: dict[str, str] = {}
    unmapped: set[str] = set()
    memberships = {
        name: _members(display[key], identities, reverse, unmapped)
        for name, key in zip(NAMES, ("ns_movers", "ns_focus"), strict=True)
    }
    if not memberships["NS movers"] or len(unmapped) > MAX_UNMAPPED:
        raise InputError("Display movers empty or more than five unmapped names")
    return ResearchInputs(
        memberships,
        EPOCH + timedelta(microseconds=clocks[0] // 1000, seconds=effective),
        frozenset(unmapped),
        generation,
    )
