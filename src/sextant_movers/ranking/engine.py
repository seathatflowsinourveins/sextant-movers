"""The score engine: one pure transition per trade, and the order of the rows it computes.

Score: move from the prior close times dollar volume over the time-matched baseline; gates and
acceleration sit beside it, and each unknown input is named in `unknown_reasons`, never filled.
Behaviour, value for value and refusal for refusal: the reference, `src/sextant/ranking/engine.py`
at `06562bec0898a7203f4eaf69086ced83d2477e6b`. Ticks: nautechsystems/nautilus_trader v2.0.0rc8
`d53849b216e20f2c122ca01b4dd0da377638b186` (`python/nautilus_trader/model/__init__.pyi:7035-7044`,
`:6365`). Feeds: alpacahq/alpaca-py v0.44.0 `cc4cb3b7ba50ae250e621983c2779047fb16bb28`
`alpaca/data/enums.py:69,72`, cited, never imported.

Departures: `Segment` comes from `ranking.baseline`; no sale-condition literal and no `conditions`
parameter, so a trade the reference skips by code is a call with both eligibility flags false.
Kept, each pinned by a test: a POST-start trade is quiet before the receipt checks; only a trade
at exactly `start_ns` covers the prefix; an older quote replaces the one held.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from nautilus_trader.model import QuoteTick, TradeTick

from sextant_movers.ranking.baseline import Baseline, Segment
from sextant_movers.ranking.registry import Registry
from sextant_movers.ranking.window import NS, TradeWindow

ZERO = Decimal(0)
GATES = ("mover_min_move", "move_1m_positive", "mover_min_trades_accel", "mover_min_dvol_ratio")
FEEDS = frozenset({"sip", "boats"})
_FUTURE = "regressed receipt or future reference/baseline"


@dataclass(frozen=True, slots=True)
class ReferencePrices:
    """The prior close and, optionally, the regular open with its event time."""

    prior_close: Decimal | None
    known_at: int
    source: str
    regular_open: Decimal | None = None
    open_ts: int | None = None

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("official split-adjusted completed-close source required")
        for price in (self.prior_close, self.regular_open):
            if price is not None and (
                type(price) is not Decimal or not price.is_finite() or price <= 0
            ):
                raise ValueError("reference price must be positive finite Decimal")
        if (self.regular_open is None) != (self.open_ts is None):
            raise ValueError("regular open requires its event timestamp")


@dataclass(frozen=True, slots=True)
class MoverRow:
    symbol: str
    known_at: int
    score: Decimal | None
    rank: int | None
    move: Decimal | None
    move_1m: Decimal | None
    move_5m: Decimal | None
    dvol_ratio: Decimal | None
    trades_accel: Decimal | None
    gates_passed: tuple[str, ...]
    observations: Mapping[str, Any]
    params_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", MappingProxyType(dict(self.observations)))


@dataclass(frozen=True, slots=True)
class MoverState:
    """One symbol's carry within one feed and segment; a transition returns a new one."""

    symbol: str | None = None
    segment_key: tuple[str, Segment] | None = None
    history: TradeWindow = field(default_factory=TradeWindow)
    volume_history: TradeWindow = field(default_factory=TradeWindow)
    segment_dollar_volume: Decimal = ZERO
    latest: MoverRow | None = None
    quote: QuoteTick | None = None
    known_at: int = 0
    segment_covered: bool = False
    params_hash: str | None = None


def qualifies(row: MoverRow) -> bool:
    return all(gate in row.gates_passed for gate in GATES)


def rank_movers(rows: Iterable[MoverRow]) -> tuple[MoverRow, ...]:
    """Every row with a score and an acceleration, by score, then acceleration, then symbol."""
    known = [row for row in rows if row.score is not None and row.trades_accel is not None]
    known.sort(key=lambda row: (-row.score, -row.trades_accel, row.symbol))  # type: ignore[operator]
    return tuple(replace(row, rank=i) for i, row in enumerate(known, 1))


def _check(
    state: MoverState,
    tick: TradeTick,
    symbol: str,
    key: tuple[str, Segment],
    baseline: Baseline,
    registry: Registry,
) -> None:
    feed, segment = key
    if baseline.feed != feed or feed not in FEEDS:
        raise ValueError("baseline feed mismatch")
    if state.segment_key is not None and state.segment_key != key:
        raise ValueError("source/session changed; create a fresh publisher")
    if state.symbol is not None and state.symbol != symbol:
        raise ValueError("one MoverState per symbol")
    if state.params_hash is not None and state.params_hash != registry.params_hash:
        raise ValueError("parameters changed; create fresh history")
    if not segment.start_ns <= tick.ts_event < segment.end_ns:
        raise ValueError("trade outside supplied session")


def _quote(held: QuoteTick | None, tick: TradeTick, now: int, start_ns: int) -> QuoteTick | None:
    if held is None:
        return None
    if held.instrument_id != tick.instrument_id:
        raise ValueError("quote symbol mismatch")
    if max(held.ts_event, held.ts_init) > now or held.ts_event > tick.ts_event:
        raise ValueError("future quote")
    return None if held.ts_event < start_ns else held


def _ratio(new: Decimal, old: TradeTick | None) -> Decimal | None:
    return new / old.price.as_decimal() - 1 if old is not None else None


def advance_mover(
    state: MoverState,
    tick: TradeTick,
    references: ReferencePrices,
    baseline: Baseline,
    *,
    segment: Segment,
    feed: str,
    registry: Registry,
    quote: QuoteTick | None = None,
    price_eligible: bool = True,
    volume_eligible: bool = True,
    known_at: int | None = None,
) -> tuple[MoverState, MoverRow | None]:
    """The new state and row after `tick`, or `(state, None)` for a trade that counts nowhere.

    Each history takes the trade by its own flag; a state attached late never covers the prefix.
    """
    symbol = str(tick.instrument_id.symbol)
    now = max(tick.ts_event, tick.ts_init, known_at or 0)
    key = (feed, segment)
    _check(state, tick, symbol, key, baseline, registry)
    if segment.name == "POST" and tick.ts_event == segment.start_ns:
        return state, None  # the close itself belongs to no extended segment
    if now < state.known_at or max(references.known_at, baseline.known_at) > now:
        raise ValueError(_FUTURE)
    if baseline.latest_session is not None and baseline.latest_session >= segment.session_date:
        raise ValueError("current/future baseline session")
    if not (price_eligible or volume_eligible):
        return state, None
    held = _quote(quote if quote is not None else state.quote, tick, now, segment.start_ns)
    recent_s = int(registry.trades_accel_window_s)
    baseline_s = int(registry.trades_accel_baseline_min) * 60
    longest = max(int(registry.move_1m_window_s), int(registry.move_5m_window_s))
    keep_s = max(longest, recent_s + baseline_s) + 1
    history = state.history.append(tick, keep_s) if price_eligible else state.history
    volumes = state.volume_history.append(tick, keep_s) if volume_eligible else state.volume_history
    covered = state.segment_covered or tick.ts_event == segment.start_ns
    dv = state.segment_dollar_volume
    if volume_eligible:
        dv += tick.price.as_decimal() * tick.size.as_decimal()

    ts = tick.ts_event
    last = history.price_at(ts)
    price = last.price.as_decimal() if last else None
    close = references.prior_close
    move = price / close - 1 if price is not None and close else None
    p1 = history.price_at(ts - registry.seconds("move_1m_window_s"))
    p5 = history.price_at(ts - registry.seconds("move_5m_window_s"))
    move_1m = _ratio(price, p1) if price is not None else None
    move_5m = _ratio(price, p5) if price is not None else None
    recent_start = ts - recent_s * NS
    prior_start = recent_start - baseline_s * NS
    trades = volumes.totals(recent_start, ts)[1]
    prior = volumes.totals(prior_start, recent_start)[1]
    prior_covered = volumes.price_at(prior_start) is not None
    accel = Decimal(trades) * baseline_s / (prior * recent_s) if prior and prior_covered else None
    normal = baseline.cumulative(symbol, segment.name, Decimal(ts - segment.start_ns) / NS)
    ratio = dv / normal.dollar_volume if covered and normal and normal.dollar_volume > 0 else None

    missing = {
        "reference_unknown": close is None,
        "price_unknown": price is None,
        "price_1m_unknown": p1 is None,
        "price_5m_unknown": p5 is None,
        "prior_window_uncovered": not prior_covered,
        "prior_window_zero_trades": prior_covered and not prior,
        "segment_prefix_unknown": not covered,
        "baseline_unknown": normal is None,
        "baseline_zero_dollar_volume": normal is not None and normal.dollar_volume == 0,
    }
    passed = (
        move is not None and move >= registry.mover_min_move,
        move_1m is not None and move_1m > 0,
        accel is not None and accel >= registry.mover_min_trades_accel,
        ratio is not None and ratio >= registry.mover_min_dvol_ratio,
    )
    opened = references.regular_open
    open_known = references.open_ts is not None and ts >= references.open_ts
    from_open = price / opened - 1 if price is not None and opened and open_known else None
    spread = held.ask_price.as_decimal() - held.bid_price.as_decimal() if held else None
    observations = {
        "price": price,
        "dollar_volume": dv if covered else None,
        "observed_dollar_volume_lower_bound": dv,
        "spread": spread,
        "move_from_open": from_open,
        "ts_event": ts,
        "ts_init": tick.ts_init,
        "last_price_ts": last.ts_event if last else None,
        "reference": close,
        "reference_source": references.source,
        "reference_known_at": references.known_at,
        "baseline_source": baseline.provenance,
        "baseline_known_at": baseline.known_at,
        "baseline_sessions": normal.sessions if normal else None,
        "baseline_dollar_volume": normal.dollar_volume if normal else None,
        "segment_total_covered": covered,
        "uncalibrated": True,
        "unknown_reasons": tuple(reason for reason, unknown in missing.items() if unknown),
    }
    score = move * ratio if move is not None and ratio is not None else None
    gates = tuple(gate for gate, ok in zip(GATES, passed, strict=True) if ok)
    params = registry.params_hash
    row = MoverRow(
        symbol, now, score, None, move, move_1m, move_5m, ratio, accel, gates, observations, params
    )
    new = MoverState(symbol, key, history, volumes, dv, row, held, now, covered, params)
    return new, row
