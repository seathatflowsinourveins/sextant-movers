"""The XNYS segment of a moment, and the time-of-day volume baseline table.

The table is a non-parametric stand-in for the seasonal term of the model of Chen, Feng and
Palomar (convexfi/intradayModel@v0.0.1 c1849c8e9332fbeb4c4d9609e5eb8533ba2088e6, `R/fit.R:3-18`:
log volume per bin as a daily level plus an intraday dynamic term in a Kalman state, plus a per-bin
seasonal term, fitted by EM): a median cumulative dollar-volume curve by time of day. It has no
daily-level and no dynamic term. The median across sessions, the cumulative form and dollar volume
are the reference's own choices and come from no paper. Each session's curve is linear between its
minute points; a window is taken within each session, then the median across sessions (a
difference of medians is not the median of differences). Outside coverage the answer is None,
never zero, and never an extrapolation.

Sources: gerrymanoim/exchange_calendars@4.13.2 dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a
(`get_calendar` calendar_utils.py:237; `session_open`, `session_close`, `date_to_session`
exchange_calendar.py:1006, 1012, 1281); nautechsystems/nautilus_trader@v2.0.0rc8
d53849b216e20f2c122ca01b4dd0da377638b186 (`dt_to_unix_nanos`,
python/nautilus_trader/core/datetime.py:81-104). Behaviour, value for value and refusal for
refusal: the reference, `src/sextant/ranking/baseline.py` at
06562bec0898a7203f4eaf69086ced83d2477e6b, and its `Segment` (`src/sextant/ranking/engine.py:24-36`).
Departure: `Segment` is defined here, so the engine imports it from this module; the reference's
baseline imports it back from its engine through a delayed import, a cycle.
"""

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import cache
from itertools import pairwise
from operator import attrgetter
from statistics import median
from types import MappingProxyType

from exchange_calendars import ExchangeCalendar, get_calendar
from nautilus_trader.core.datetime import dt_to_unix_nanos

from sextant_movers.sessions import ET, Session, session_at

ZERO = Decimal(0)
NS = 1_000_000_000
MINUTE_NS = 60 * NS
_HOUR_NS = 3_600 * NS
_BASELINE_KEYS = frozenset({"samples", "provenance", "known_at", "feed"})
_SAMPLE_KEYS = frozenset(
    {"symbol", "segment", "session", "offset_s", "dollar_volume", "trade_count"}
)
_CURVE = attrgetter("symbol", "segment", "session")


@dataclass(frozen=True, slots=True)
class Segment:
    name: str
    session_date: date
    start_ns: int
    end_ns: int
    source: str

    def __post_init__(self) -> None:
        if self.name not in {"PRE", "RTH", "POST", "OVERNIGHT"}:
            raise ValueError("unsupported segment")
        if not self.source or not 0 <= self.start_ns < self.end_ns:
            raise ValueError("explicit sourced session boundaries required")


@dataclass(frozen=True, slots=True)
class BaselineSample:
    """One point of one session's cumulative curve, ``offset_s`` seconds after its start."""

    symbol: str
    segment: str
    session: str
    offset_s: int
    dollar_volume: Decimal
    trade_count: Decimal

    def __post_init__(self) -> None:
        if self.offset_s < 0:
            raise ValueError("negative baseline offset")
        for value in (self.dollar_volume, self.trade_count):
            if not isinstance(value, Decimal):
                raise TypeError("baseline values must be Decimal")
            if not value.is_finite() or value < 0:
                raise ValueError("invalid baseline value")


@dataclass(frozen=True, slots=True)
class BaselineValue:
    dollar_volume: Decimal
    trade_count: Decimal
    sessions: int
    known_at: int
    provenance: str


Curve = tuple[BaselineSample, ...]


@dataclass(frozen=True, slots=True)
class BaselineTable:
    samples: tuple[BaselineSample, ...]
    provenance: str
    known_at: int
    feed: str
    latest_session: date | None = field(init=False)
    _identity: str = field(init=False, repr=False)
    _index: Mapping[tuple[str, str], tuple[Curve, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not self.provenance or self.feed not in {"sip", "boats"}:
            raise ValueError("baseline source and feed required")
        ordered = tuple(sorted(self.samples, key=lambda s: (*_CURVE(s), s.offset_s)))
        for before, after in pairwise(ordered):
            if _CURVE(before) != _CURVE(after):
                continue
            if after.offset_s == before.offset_s:
                raise ValueError("duplicate baseline offset")
            if after.dollar_volume < before.dollar_volume or after.trade_count < before.trade_count:
                raise ValueError("cumulative baseline decreases")
        object.__setattr__(self, "samples", ordered)
        latest = max((date.fromisoformat(s.session) for s in ordered), default=None)
        object.__setattr__(self, "latest_session", latest)
        payload = _payload(self, [asdict(s) for s in ordered])
        text = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "_identity", hashlib.sha256(text.encode()).hexdigest())
        grouped: dict[tuple[str, str], dict[str, list[BaselineSample]]] = defaultdict(dict)
        for s in ordered:
            grouped[s.symbol, s.segment].setdefault(s.session, []).append(s)
        index = {key: tuple(map(tuple, days.values())) for key, days in grouped.items()}
        object.__setattr__(self, "_index", MappingProxyType(index))

    @property
    def identity(self) -> str:
        """SHA-256 of the sorted samples and the three fields, as sorted compact JSON."""
        return self._identity

    def _curves(self, symbol: str, segment: str) -> tuple[Curve, ...]:
        return self._index.get((symbol, segment), ())

    @staticmethod
    def _at(curve: Curve, offset: Decimal) -> tuple[Decimal, Decimal] | None:
        if offset < curve[0].offset_s or offset > curve[-1].offset_s:
            return None
        i = bisect_left(curve, offset, key=lambda s: s.offset_s)
        upper = curve[i]
        if upper.offset_s == offset:
            return upper.dollar_volume, upper.trade_count
        lower = curve[i - 1]
        fraction = (offset - lower.offset_s) / (upper.offset_s - lower.offset_s)
        return (
            lower.dollar_volume + fraction * (upper.dollar_volume - lower.dollar_volume),
            lower.trade_count + fraction * (upper.trade_count - lower.trade_count),
        )

    def cumulative(
        self, symbol: str, segment: str, offset_s: int | Decimal
    ) -> BaselineValue | None:
        """The median across sessions of the curve at ``offset_s``; None without coverage."""
        found = (self._at(curve, Decimal(offset_s)) for curve in self._curves(symbol, segment))
        return self._median([value for value in found if value is not None])

    def window(
        self, symbol: str, segment: str, offset_s: int | Decimal, window_s: int
    ) -> BaselineValue | None:
        """The median across sessions of each session's ``[offset_s - window_s, offset_s]``."""
        end = Decimal(offset_s)
        start = end - window_s
        values = []
        for curve in self._curves(symbol, segment):
            a, b = self._at(curve, start), self._at(curve, end)
            if a is not None and b is not None:
                values.append((b[0] - a[0], b[1] - a[1]))
        return self._median(values)

    def _median(self, values: list[tuple[Decimal, Decimal]]) -> BaselineValue | None:
        if not values:
            return None
        return BaselineValue(
            Decimal(median(v[0] for v in values)),
            Decimal(median(v[1] for v in values)),
            len(values),
            self.known_at,
            self.provenance,
        )

    def covers(self, symbol: str, segment: str) -> bool:
        """Whether any session's curve exists for ``symbol`` in ``segment``."""
        return bool(self._curves(symbol, segment))

    def sessions(self, symbol: str, segment: str) -> tuple[str, ...]:
        """The session dates of ``symbol`` in ``segment``, ascending."""
        return tuple(curve[0].session for curve in self._curves(symbol, segment))


def _payload(table: BaselineTable, samples: list[dict[str, object]]) -> dict[str, object]:
    return {
        "samples": samples,
        "provenance": table.provenance,
        "known_at": table.known_at,
        "feed": table.feed,
    }


def dump_baseline(table: BaselineTable) -> str:
    """The table as sorted compact JSON, Decimals as strings; `load_baseline` reads it back."""
    samples = [
        {**asdict(s), "dollar_volume": str(s.dollar_volume), "trade_count": str(s.trade_count)}
        for s in table.samples
    ]
    return json.dumps(_payload(table, samples), sort_keys=True, separators=(",", ":"))


def _json_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"baseline JSON {name} must be a decimal string")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"baseline JSON {name} must be a decimal string") from exc


def _json_sample(entry: object) -> BaselineSample:
    if not isinstance(entry, Mapping) or set(entry) != _SAMPLE_KEYS:
        raise ValueError(f"baseline JSON samples must have {sorted(_SAMPLE_KEYS)}")
    symbol, segment, session = entry["symbol"], entry["segment"], entry["session"]
    if not all(isinstance(value, str) for value in (symbol, segment, session)):
        raise ValueError("baseline JSON symbol, segment and session must be strings")
    offset_s = entry["offset_s"]
    if type(offset_s) is not int:
        raise ValueError("baseline JSON offset must be an integer")
    dollar_volume = _json_decimal(entry["dollar_volume"], "dollar_volume")
    trade_count = _json_decimal(entry["trade_count"], "trade_count")
    return BaselineSample(symbol, segment, session, offset_s, dollar_volume, trade_count)


def load_baseline(text: str) -> BaselineTable:
    """Read what `dump_baseline` writes; any other shape is refused."""
    payload = json.loads(text)
    if not isinstance(payload, Mapping) or set(payload) != _BASELINE_KEYS:
        raise ValueError(f"baseline JSON must have {sorted(_BASELINE_KEYS)}")
    samples, provenance = payload["samples"], payload["provenance"]
    known_at, feed = payload["known_at"], payload["feed"]
    if not isinstance(samples, list):
        raise ValueError("baseline JSON samples must be a list")
    if not isinstance(provenance, str):
        raise ValueError("baseline JSON provenance must be a string")
    if type(known_at) is not int:
        raise ValueError("baseline JSON known_at must be an integer")
    if not isinstance(feed, str):
        raise ValueError("baseline JSON feed must be a string")
    return BaselineTable(tuple(map(_json_sample, samples)), provenance, known_at, feed)


def _nanos(value: datetime) -> int:
    return dt_to_unix_nanos(value)


@cache
def _calendar(year: int) -> ExchangeCalendar:
    # The same span as `sextant_movers.sessions`, so labels and closes agree.
    return get_calendar("XNYS", start=date(year - 1, 12, 1), end=date(year + 1, 1, 31), side="left")


def _open_ns(day: date) -> int:
    return int(_calendar(day.year).session_open(day).value)


def _close_ns(day: date) -> int:
    return int(_calendar(day.year).session_close(day).value)


def segment_start(ts_ns: int) -> tuple[Segment | None, date, int]:
    """The XNYS segment holding ``ts_ns``, its session date and its start.

    A closed moment (a day that is not a session, or the 17:00 to 20:00 gap after an early close)
    is ``(None, its local date, ts_ns)``: `Segment` has no closed member. The overnight from 20:00
    belongs to the next session. POST ends four hours after the close and OVERNIGHT eight hours
    after its start, as in the reference.
    """
    segment = session_at(ts_ns)
    local = datetime.fromtimestamp(ts_ns // NS, tz=ET)
    if segment is Session.CLOSED:
        return None, local.date(), ts_ns
    session_date = (local + timedelta(hours=4)).date()
    if segment is Session.RTH:
        start_ns, end_ns = _open_ns(session_date), _close_ns(session_date)
    elif segment is Session.POST:
        start_ns = _close_ns(session_date)
        end_ns = start_ns + 4 * _HOUR_NS
    else:
        day = local.date()
        if segment is Session.OVERNIGHT and local.hour < 4:
            day -= timedelta(days=1)
        hour = 4 if segment is Session.PRE else 20
        start_ns = _nanos(datetime.combine(day, time(hour), tzinfo=ET))
        end_ns = _open_ns(session_date) if segment is Session.PRE else start_ns + 8 * _HOUR_NS
    return Segment(segment.name, session_date, start_ns, end_ns, "xnys"), session_date, start_ns


def closed_until(ts_ns: int) -> int:
    """20:00 ET on the day before the next XNYS session after the trading day of ``ts_ns``.

    For a closed moment that is the end of its closed interval, where the next overnight starts.
    """
    local = datetime.fromtimestamp(ts_ns // NS, tz=ET)
    following = (local + timedelta(hours=4)).date() + timedelta(days=1)
    session = _calendar(following.year).date_to_session(following, direction="next").date()
    return _nanos(datetime.combine(session - timedelta(days=1), time(20), tzinfo=ET))
