"""The ranking data type, the universe digest, the asset state and the publisher's trade intake.

`ranking_data_type(feed, view)` is the engine's `DataType` (nautechsystems/nautilus_trader
v2.0.0rc8 `d53849b216e20f2c122ca01b4dd0da377638b186`,
`python/nautilus_trader/model/__init__.pyi:1688`) under the registered type name,
`generation.TYPE_NAME`, with the metadata `feed`, `view` and `ranker`. The engine builds the topic
from those pairs sorted by key (`crates/model/src/data/data_type.rs:154-163,346-361`) and compares
and hashes data types by topic (`:584-608`). Feed and view are not checked here: they cannot take
another's topic, because the engine escapes a value shaped like a pair or like JSON
(`value_to_topic_string`, `:41-86`). `universe_digest` is the SHA-256 of the compact JSON of the
sorted symbols: a repeat counts, and nothing is checked.

`AssetState.eligible` holds when the source is set, `0 <= known_at <= now < expires_at`, the halt
flag is exactly False and the security type is in the supplied set. `RankingPublisher` takes the
engine's `TradeTick` (`model/__init__.pyi:7035`) through `on_trade`, folds it with
`engine.advance_mover` and degrades its coverage on a refused trade, re-raised: the engine's own
errors, a name outside the universe or without references, and a fact the engine does not take. It
sends through the injected `send(DataType, CustomData)`, the shape of `DataActor.publish_data`
(`common/__init__.pyi:719`); the method version, the eligible security types and the price band
edges are required and stored, used by `publish`, the next part. Nothing here starts a stream, a
socket, a thread or a node, and nothing here places an order.

Source: the reference's `src/sextant/ranking/publisher.py:40-134` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`, rebuilt value for value, with the bus, the method
version, the security types and the band edges injected instead of fixed.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nautilus_trader.model import CustomData, DataType, TradeTick

from sextant_movers.ranking.baseline import BaselineTable, Segment
from sextant_movers.ranking.engine import MoverState, ReferencePrices, advance_mover
from sextant_movers.ranking.generation import (
    TYPE_NAME,
    Coverage,
    CoverageState,
    register_ranking_generation,
)
from sextant_movers.ranking.heartbeat import HeartbeatEmitter
from sextant_movers.ranking.registry import Registry

RANKER = "sextant/fixture-ranking/1"  # the reference's publisher.py:40, word for word


def universe_digest(symbols: tuple[str, ...]) -> str:
    text = json.dumps(sorted(symbols), separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def ranking_data_type(feed: str, view: str = "full") -> DataType:
    return DataType(TYPE_NAME, metadata={"feed": feed, "view": view, "ranker": RANKER})


@dataclass(frozen=True, slots=True)
class AssetState:
    known_at: int
    expires_at: int
    halted: bool | None
    security_type: str
    source: str

    def eligible(self, now: int, *, security_types: frozenset[str]) -> bool:
        return bool(
            self.source
            and 0 <= self.known_at <= now < self.expires_at
            and self.halted is False
            and self.security_type in security_types
        )


def _edges(value: object) -> bool:
    if not isinstance(value, tuple) or len(value) != 2:
        return False
    low, high = value
    return type(low) is int and type(high) is int and 0 < low < high


class RankingPublisher:
    def __init__(
        self,
        *,
        send: Callable[[DataType, CustomData], None],
        epoch: str,
        feed: str,
        segment: Segment,
        registry: Registry,
        universe: tuple[str, ...],
        universe_source: str,
        baseline: BaselineTable,
        method_version: str,
        eligible_security_types: frozenset[str],
        price_band_edges: tuple[int, int],
        heartbeat: HeartbeatEmitter | None = None,
    ) -> None:
        if not epoch or not universe_source or not universe or len(set(universe)) != len(universe):
            raise ValueError("explicit epoch, unique universe and source required")
        if baseline.feed != feed or (feed == "boats") != (segment.name == "OVERNIGHT"):
            raise ValueError("feed/segment/baseline mismatch")
        if not callable(send):
            raise ValueError("send callable required")
        if not isinstance(method_version, str) or not method_version:
            raise ValueError("explicit method version required")
        if not isinstance(eligible_security_types, frozenset) or not eligible_security_types:
            raise ValueError("eligible security types required")
        if not _edges(price_band_edges):
            raise ValueError("price band edges required")
        register_ranking_generation()
        self.send, self.method_version = send, method_version
        self.eligible_security_types, self.price_band_edges = (
            eligible_security_types,
            price_band_edges,
        )
        self.epoch, self.feed, self.segment, self.registry = epoch, feed, segment, registry
        self.universe, self.universe_source = tuple(sorted(universe)), universe_source
        self.baseline, self.heartbeat = baseline, heartbeat
        self.universe_hash = universe_digest(self.universe)
        self.states = {name: MoverState() for name in universe}
        self.references: dict[str, ReferencePrices] = {}
        self.assets: dict[str, AssetState | None] = {}
        self.coverage = Coverage(CoverageState.UNKNOWN, False)
        self.published_at: int | None = None
        self.sequence = 0
        self.last_event = segment.start_ns
        self.input_cutoff = segment.start_ns

    def set_symbol(
        self, name: str, *, references: ReferencePrices, asset: AssetState | None
    ) -> None:
        if name not in self.states:
            raise ValueError("symbol outside supplied universe")
        self.references[name], self.assets[name] = references, asset

    def set_coverage(self, coverage: Coverage) -> None:
        self.coverage = coverage

    def on_trade(self, tick: TradeTick, **facts: Any) -> None:
        name = str(tick.instrument_id.symbol)
        try:
            state, row = advance_mover(
                self.states[name],
                tick,
                self.references[name],
                self.baseline,
                segment=self.segment,
                feed=self.feed,
                registry=self.registry,
                **facts,
            )
            self.states[name] = state
            if row is not None:
                self.last_event = max(self.last_event, tick.ts_event)
                self.input_cutoff = max(self.input_cutoff, row.known_at)
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.coverage = Coverage(CoverageState.DEGRADED, False)
            raise
