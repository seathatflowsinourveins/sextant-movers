"""Polled minute bars into the publisher and one display document; no transport.

Callers discard a host after ingest raises and rebuild it by replay: a refused
bar remains remembered, so an identical retry is skipped as a repeat. A partial
poll (failed or unrequested symbols) needs a complete poll at that same market clock
to clear its gap.

Acceptance: the reference, src/sextant/tradingview/host.py:10-275 at
06562bec0898a7203f4eaf69086ced83d2477e6b, statement and refusal parity.
Departures: required caller scope and paper_slot pass unchanged to LiveProducer;
routing prefixes come from inputs.REFUSED_PREFIXES; imports use this package's
ranking.bars, lease, generation, publisher, inputs and producer modules.
No private validation text is carried. POST anchoring follows ranking/engine.py:160-161,175:
the start print is quiet and only an exact start print otherwise covers the prefix.

Build on nautechsystems/nautilus_trader@v2.0.0rc8
 d53849b216e20f2c122ca01b4dd0da377638b186:python/nautilus_trader/model/__init__.pyi:
7035 (TradeTick), 7023 (TradeId), 7657 (NO_AGGRESSOR), 6218 (Price.from_str),
6346 (Quantity.from_int), 2403 (InstrumentId.from_str), used as is.
Keep ours: docs/concepts/backtesting/bar-execution.md:1-57 ships four OHLC updates
for simulated matching; model/__init__.pyi:112 has no trade count;
persistence/__init__.pyi:825 takes record batches, not bars. Searches in model,
persistence, Rust wranglers, backtesting/data docs and RELEASES.md found no callable
bar-to-trade converter. The reference closes once and preserves the vendor count
(alpacahq/alpaca-py@v0.44.0 cc4cb3b7ba50ae250e621983c2779047fb16bb28:
alpaca/data/models/bars.py:21,33); ranking.window.append_count carries counted twins.
Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Doc/library/json.rst:356 (loads), Lib/pathlib/_local.py:539 and
Doc/library/pathlib.rst:1179 (UTF-8 read_text), Doc/library/dataclasses.rst:475
(replace), Doc/library/string.rst:553 (Decimal fixed-point format).
Exchange-prefixed map values: TradingView's watchlist import support page, read by
the lead 2026-11-11, cited in docs/manifest.md. keep_s derives existing registry windows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model import (
    AggressorSide,
    InstrumentId,
    Price,
    Quantity,
    TradeId,
    TradeTick,
)

from sextant_movers.lease import AccountScope, WriterLease
from sextant_movers.ranking.bars import Bar, BarPoll
from sextant_movers.ranking.generation import Coverage, CoverageState
from sextant_movers.ranking.publisher import RankingPublisher
from sextant_movers.tradingview.inputs import QUALIFIED, REFUSED_PREFIXES
from sextant_movers.tradingview.producer import Clocks, LiveProducer

VENUE = "SIM"
MASK_SOURCE = "sip minute bars carry no condition or aggressor mask"
RECEIPT_SOURCE = "bar fidelity SIP minute bars through the shared REST pacer"


def load_symbol_map(path: Path) -> dict[str, str | None]:
    """Read the operator's exact symbol to exchange-prefixed routing map."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("the symbol map must be readable JSON") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("the symbol map must be a non-empty JSON object")
    result: dict[str, str | None] = {}
    targets: set[str] = set()
    for symbol, mapping in data.items():
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol map keys must be tickers")
        if mapping is None:
            result[symbol] = None
            continue
        if not isinstance(mapping, str) or QUALIFIED.fullmatch(mapping) is None:
            raise ValueError("symbol map values must be exchange-prefixed mappings")
        venue = mapping.split(":", 1)[0]
        if venue in REFUSED_PREFIXES:
            raise ValueError("symbol map values need a venue exchange, not routing")
        if mapping in targets:
            raise ValueError("symbol map targets must be unique")
        targets.add(mapping)
        result[symbol] = mapping
    return result


class BarFidelityHost:
    """One segment's bar-fidelity publisher, fed by polled SIP minute bars."""

    def __init__(
        self,
        publisher: RankingPublisher,
        *,
        lease: WriterLease,
        scope: AccountScope,
        paper_slot: int,
        sip_authority: str,
        symbol_map: Mapping[str, str | None],
    ) -> None:
        if not isinstance(symbol_map, Mapping) or not symbol_map:
            raise ValueError("a verified symbol map is required")
        self.publisher = publisher
        self.lease = lease
        self.symbol_map = dict(symbol_map)
        self.producer = LiveProducer(
            publisher, lease, scope=scope, paper_slot=paper_slot, sip_authority=sip_authority
        )
        self._universe = frozenset(publisher.universe)
        self._anchored: set[str] = set()
        self.last_close: dict[str, int] = {}
        self._last_bar: dict[str, tuple[int, Decimal, int, int]] = {}
        self._gaps: dict[str, int] = {}
        registry = publisher.registry
        self.keep_s = (
            max(
                int(registry.move_1m_window_s),
                int(registry.move_5m_window_s),
                int(registry.trades_accel_window_s) + int(registry.trades_accel_baseline_min) * 60,
            )
            + 1
        )

    def ingest(self, poll: BarPoll, *, market_ns: int, received_ns: int) -> int:
        """Feed one polled batch and set the coverage it honestly observed."""
        if type(market_ns) is not int or type(received_ns) is not int:
            raise ValueError("market and receive clocks must be integers")
        if received_ns < market_ns:
            raise ValueError("the receive clock never precedes the market clock")
        requested = set(poll.requested)
        unknown = (requested | set(poll.failed)) - self._universe
        if unknown:
            raise ValueError(f"bar poll names symbols outside the universe: {sorted(unknown)}")
        missing = (self._universe - requested) | set(poll.failed)
        for symbol in missing:
            # Hold the earliest unverified minute until its bar or same-clock poll recovers it.
            self._gaps[symbol] = min(self._gaps.get(symbol, market_ns), market_ns)
        ingested = 0
        for item in sorted(poll.bars, key=lambda bar: (bar.close_ns, bar.symbol)):
            if self._feed(item, market_ns=market_ns, received_ns=received_ns):
                ingested += 1
        if not missing:
            # A complete poll of this minute verifies no trades for a symbol with no bar.
            for symbol in [name for name, gap in self._gaps.items() if gap == market_ns]:
                del self._gaps[symbol]
        unverified = missing | set(self._gaps)
        coverage = (
            Coverage(CoverageState.READY, True, len(self._universe), len(self._universe), 0, 0, 0)
            if not unverified
            else Coverage(
                CoverageState.DEGRADED,
                False,
                len(self._universe),
                len(self._universe) - len(unverified),
                len(unverified),
                0,
                0,
            )
        )
        self.publisher.set_coverage(coverage)
        return ingested

    def heartbeat(self, *, at: int, received_ns: int) -> bool:
        return self.producer.heartbeat(at=at, received_ns=received_ns)

    def publish(self, path: Path, *, now: int, mapping_clocks: Clocks | None = None) -> dict:
        clocks = mapping_clocks or Clocks(now, now, RECEIPT_SOURCE)
        return self.producer.publish_unit(
            path, now=now, symbol_map=self.symbol_map, mapping_clocks=clocks
        )

    def poll(
        self,
        poll: BarPoll,
        *,
        market_ns: int,
        received_ns: int,
        path: Path,
        mapping_clocks: Clocks | None = None,
    ) -> tuple[int, dict]:
        ingested = self.ingest(poll, market_ns=market_ns, received_ns=received_ns)
        if not self.heartbeat(at=market_ns, received_ns=received_ns):
            raise ValueError("the heartbeat was refused")
        # Publication must not precede the heartbeat receipt it uses.
        decision_ns = max(market_ns, received_ns)
        return ingested, self.publish(path, now=decision_ns, mapping_clocks=mapping_clocks)

    def _feed(self, item: Bar, *, market_ns: int, received_ns: int) -> bool:
        symbol = item.symbol
        if symbol not in self._universe:
            raise ValueError(f"bar outside the universe: {symbol}")
        if item.start_ns < self.publisher.segment.start_ns:
            raise ValueError(f"bar outside the segment: {symbol}")
        if item.close_ns >= self.publisher.segment.end_ns:
            # Boundary bars do not invalidate this segment's epoch.
            return False
        if item.close_ns > market_ns:
            raise ValueError(f"bar close is after the market clock: {symbol}")
        gap = self._gaps.get(symbol)
        if gap is not None:
            if item.close_ns != gap:
                return False
            del self._gaps[symbol]
        previous = self._last_bar.get(symbol)
        if previous is not None and item.close_ns <= previous[0]:
            if item.close_ns < previous[0]:
                return False
            if (item.close, item.volume, item.trades) == previous[1:]:
                return False
            self.producer.invalidate("a revised bar requires a new publisher epoch")
            raise ValueError(f"revised bar requires a new epoch: {symbol}")
        self.last_close[symbol] = item.close_ns
        self._last_bar[symbol] = (item.close_ns, item.close, item.volume, item.trades)
        if item.volume == 0:
            return False
        reference = self.publisher.references.get(symbol)
        if reference is None or reference.prior_close is None:
            raise ValueError("a reference close is required before the first bar")
        if symbol not in self._anchored:
            price = (
                item.open
                if item.start_ns == self.publisher.segment.start_ns
                else reference.prior_close
            )
            self._anchor(symbol, price, market_ns=market_ns, received_ns=received_ns)
            self._anchored.add(symbol)
        tick = TradeTick(
            self._instrument(symbol),
            Price.from_str(_text(item.close)),
            Quantity.from_int(item.volume),
            AggressorSide.NO_AGGRESSOR,
            TradeId(f"bar:{symbol}:{item.start_ns}"),
            item.close_ns,
            item.close_ns,
        )
        state = self.publisher.states[symbol]
        seeded = state.volume_history.append_count(tick, max(item.trades - 1, 0), self.keep_s)
        self.publisher.states[symbol] = replace(state, volume_history=seeded)
        self.producer.on_trade(
            tick,
            clocks=self._clocks(market_ns, received_ns),
            now=market_ns,
            price_eligible=True,
            volume_eligible=True,
            conditions=(),
            mask_source=MASK_SOURCE,
        )
        return True

    def _anchor(self, symbol: str, price: Decimal, *, market_ns: int, received_ns: int) -> None:
        segment = self.publisher.segment
        if segment.name == "POST":
            # ranking/engine.py:160-161 ignores the close at POST start; :175 needs coverage.
            self.publisher.states[symbol] = replace(
                self.publisher.states[symbol], segment_covered=True
            )
            at = segment.start_ns + 1
        else:
            at = segment.start_ns
        anchor = TradeTick(
            self._instrument(symbol),
            Price.from_str(_text(price)),
            Quantity.from_int(1),
            AggressorSide.NO_AGGRESSOR,
            TradeId(f"anchor:{symbol}"),
            at,
            at,
        )
        self.producer.on_trade(
            anchor,
            clocks=self._clocks(market_ns, received_ns),
            now=market_ns,
            price_eligible=True,
            volume_eligible=True,
            conditions=(),
            mask_source=MASK_SOURCE,
        )
        state = self.publisher.states[symbol]
        self.publisher.states[symbol] = replace(
            state, segment_dollar_volume=state.segment_dollar_volume - price
        )

    def _clocks(self, market_ns: int, received_ns: int) -> Clocks:
        return Clocks(market_ns, received_ns, RECEIPT_SOURCE)

    @staticmethod
    def _instrument(symbol: str) -> InstrumentId:
        return InstrumentId.from_str(f"{symbol}.{VENUE}")


def _text(value: Decimal) -> str:
    return format(value, "f")
