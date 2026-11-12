"""Compact cumulative session curves from minute bars, with a median after interpolation.

Sources: python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d,
`Lib/statistics.py:621-642` (median, used as is; even counts average the middle two),
`Doc/library/statistics.rst:359-365`; `Doc/library/array.rst:17-52,235-240` (signed
'q' and 'i', minimum eight and two bytes, machine-value tobytes, used as is).
Out-of-range assignment raises OverflowError (`Modules/arraymodule.c:407-412,533-538`).
The time-of-day curve remains ours by ADR-0002: docs/landscape.md's volume-baseline row
and docs/manifest.md's baseline-builder row record the papers and upstream search.
Value-for-value acceptance: the reference at 06562bec0898a7203f4eaf69086ced83d2477e6b,
`src/sextant/tradingview/baseline.py:25-212` (scale, session clock, class and builder),
`src/sextant/tradingview/prestage.py:208-282` (one session at a time, same curves/digest).

Departures: SessionCurves is count-neutral; one build_curves accepts an iterable of
sessions in strictly ascending date order, finishing each before requesting the next.
The caller supplies a non-empty string feed, omitted from identity as in the reference.
Bar, minute/second units, Segment, BaselineValue and the New York zone come from this
package. No session count or authority default is supplied. No request is made, no
stream starts and no order is placed.
"""

import json
from array import array
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from statistics import median

from sextant_movers.ranking.bars import MINUTE_NS, Bar
from sextant_movers.ranking.baseline import NS, BaselineValue, Segment
from sextant_movers.sessions import ET

SCALE = 10_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _session_start(day: date, segment: Segment) -> int:
    """The current segment's New York clock time, on the prior session's date."""
    local = (_EPOCH + timedelta(microseconds=segment.start_ns // 1000)).astimezone(ET)
    value = datetime.combine(day, local.timetz())
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * NS + delta.microseconds * 1000


class SessionCurves:
    """Flat per-session curves; interpolate within each, then take their median."""

    def __init__(
        self,
        *,
        segment: str,
        end_s: int,
        sessions: tuple[date, ...],
        symbols: tuple[str, ...],
        known_at: int,
        provenance: str,
        dv: tuple[array, ...],
        counts: tuple[array, ...],
        data_digest: str,
        feed: str,
    ) -> None:
        if not provenance:
            raise ValueError("A baseline needs provenance")
        if not segment:
            raise ValueError("A baseline names its segment")
        if type(end_s) is not int or end_s <= 0 or end_s % 60:
            raise ValueError("A baseline ends on a whole minute")
        if not sessions or len(set(sessions)) != len(sessions):
            raise ValueError("A baseline stores distinct prior sessions")
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("A baseline stores distinct symbols")
        points = end_s // 60 + 1
        if len(dv) != len(sessions) or len(counts) != len(sessions):
            raise ValueError("A baseline stores one curve per session")
        if any(len(curve) != len(symbols) * points for curve in (*dv, *counts)):
            raise ValueError("A baseline curve covers every symbol and point")
        if not isinstance(feed, str) or not feed:
            raise ValueError("A baseline needs a non-empty string feed")
        self.segment = segment
        self.end_s = end_s
        self.points = points
        self.sessions = sessions
        self.symbols = symbols
        self.known_at = known_at
        self.provenance = provenance
        self.dv = dv
        self.counts = counts
        self.feed = feed
        self.latest_session = max(sessions)
        self._index = {symbol: index for index, symbol in enumerate(symbols)}
        self.identity = sha256(
            json.dumps(
                {
                    "segment": segment,
                    "end_s": end_s,
                    "sessions": [session.isoformat() for session in sessions],
                    "symbols": list(symbols),
                    "known_at": known_at,
                    "provenance": provenance,
                    "data": data_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def cumulative(
        self, symbol: str, segment: str, offset_s: int | Decimal
    ) -> BaselineValue | None:
        if segment != self.segment:
            return None
        row = self._index.get(symbol)
        if row is None:
            return None
        offset = Decimal(offset_s)
        if offset < 0 or offset > self.end_s:
            return None
        lower = int(offset // 60)
        fraction = (offset - lower * 60) / 60
        upper = min(lower + 1, self.points - 1)
        base = row * self.points
        dollars: list[Decimal] = []
        trades: list[Decimal] = []
        for session in range(len(self.sessions)):
            low = Decimal(self.dv[session][base + lower])
            high = Decimal(self.dv[session][base + upper])
            dollars.append((low + (high - low) * fraction) / SCALE)
            low = Decimal(self.counts[session][base + lower])
            high = Decimal(self.counts[session][base + upper])
            trades.append(low + (high - low) * fraction)
        return BaselineValue(
            dollar_volume=median(dollars),
            trade_count=median(trades),
            sessions=len(self.sessions),
            known_at=self.known_at,
            provenance=self.provenance,
        )


def build_curves(
    sessions: Iterable[tuple[date, Iterable[Bar]]],
    *,
    symbols: Sequence[str],
    segment: Segment,
    known_at: int,
    provenance: str,
    feed: str,
) -> SessionCurves:
    """Finish each session's arrays before pulling the next session from the caller."""
    if not provenance:
        raise ValueError("A baseline needs provenance")
    if type(known_at) is not int or known_at < 0:
        raise ValueError("A baseline needs a non-negative known-at clock")
    if not isinstance(feed, str) or not feed:
        raise ValueError("A baseline needs a non-empty string feed")
    names = tuple(sorted({symbol for symbol in symbols}))
    if not names or len(set(symbols)) != len(symbols):
        raise ValueError("A baseline needs distinct symbols")
    index = {symbol: position for position, symbol in enumerate(names)}
    span = segment.end_ns - segment.start_ns
    if span <= 0 or span % MINUTE_NS:
        raise ValueError("A baseline segment spans whole minutes")
    points = span // MINUTE_NS + 1
    if segment.name not in {"PRE", "RTH", "POST"}:
        raise ValueError("A baseline covers a supplied PRE, RTH or POST segment")
    days: list[date] = []
    dv_curves: list[array] = []
    count_curves: list[array] = []
    digest = sha256()
    for day, bars in sessions:
        if days and day <= days[-1]:
            raise ValueError("Baseline session dates must be strictly ascending")
        days.append(day)
        session_start = _session_start(day, segment)
        dv = array("q", [0]) * (len(names) * points)
        counts = array("i", [0]) * (len(names) * points)
        deltas: dict[int, list[int]] = {}
        trade_deltas: dict[int, list[int]] = {}
        for bar in bars:
            row = index.get(bar.symbol)
            if row is None:
                raise ValueError(f"Bar outside the baseline universe: {bar.symbol}")
            offset = (bar.start_ns - session_start) // MINUTE_NS
            if offset < 0 or offset >= points - 1:
                continue
            delta = deltas.setdefault(row, [0] * points)
            counts_delta = trade_deltas.setdefault(row, [0] * points)
            delta[offset] += int(bar.close * bar.volume * SCALE)
            counts_delta[offset] += bar.trades
        for row, delta in deltas.items():
            running = 0
            running_trades = 0
            base = row * points
            counts_delta = trade_deltas[row]
            for point in range(1, points):
                running += delta[point - 1]
                running_trades += counts_delta[point - 1]
                dv[base + point] = running
                counts[base + point] = running_trades
        dv_curves.append(dv)
        count_curves.append(counts)
        digest.update(day.isoformat().encode())
        digest.update(dv.tobytes())
        digest.update(counts.tobytes())
    if not days:
        raise ValueError("A baseline needs distinct prior sessions")
    return SessionCurves(
        segment=segment.name,
        end_s=span // NS,
        sessions=tuple(days),
        symbols=names,
        known_at=known_at,
        provenance=provenance,
        dv=tuple(dv_curves),
        counts=tuple(count_curves),
        data_digest=digest.hexdigest(),
        feed=feed,
    )
