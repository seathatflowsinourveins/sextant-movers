"""The ranking publisher: trade intake, full generation and top projection.

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
edges are required. `publish` builds the full generation and then its top projection, sending
each through the engine custom-data call. Only after both sends do the publication marks advance
and the heartbeat receive the full generation. Nothing here starts a stream, a
socket, a thread or a node, and nothing here places an order.

Source: the reference's `src/sextant/ranking/publisher.py:40-309` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`, rebuilt value for value, with the bus, the method
version, the security types and the band edges injected instead of fixed.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

from nautilus_trader.model import CustomData, DataType, TradeTick

from sextant_movers.ranking.baseline import BaselineTable, Segment
from sextant_movers.ranking.engine import (
    GATES,
    MoverState,
    ReferencePrices,
    advance_mover,
    qualifies,
    rank_movers,
)
from sextant_movers.ranking.generation import (
    TYPE_NAME,
    Coverage,
    CoverageState,
    GenerationId,
    GenerationStatus,
    RankingFeature,
    RankingGeneration,
    RankingReason,
    RankingRow,
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

    def publish(self, *, now: int) -> RankingGeneration | None:
        if self.published_at is not None:
            if now < self.published_at:
                raise ValueError("publication clock regressed")
            if now - self.published_at < self.registry.seconds("publish_min_interval_s"):
                return None
        ready = (
            self.coverage.state is CoverageState.READY
            and self.coverage.complete
            and self.coverage.expected_symbols == len(self.universe)
            and self.coverage.observed_symbols == len(self.universe)
            and self.coverage.missing_symbols
            == self.coverage.invalid_symbols
            == self.coverage.late_symbols
            == 0
            and self.segment.start_ns
            <= self.last_event
            <= self.input_cutoff
            <= now
            < self.segment.end_ns
            and now - self.last_event <= self.registry.seconds("input_max_age_s")
        )
        ranked = (
            rank_movers(state.latest for state in self.states.values() if state.latest is not None)
            if ready
            else ()
        )
        by_symbol = {row.symbol: row for row in ranked}
        source = json.dumps(
            {
                "feed": self.feed,
                "segment": self.segment.name,
                "start_ns": self.segment.start_ns,
                "end_ns": self.segment.end_ns,
                "session_source": self.segment.source,
                "universe_source": self.universe_source,
                "baseline_source": self.baseline.provenance,
                "params_hash": self.registry.params_hash,
                "universe_hash": self.universe_hash,
                "baseline_hash": self.baseline.identity,
            },
            sort_keys=True,
        )
        rows = []
        for name in self.universe:
            mover = by_symbol.get(name, self.states[name].latest) if ready else None
            asset = self.assets.get(name)
            asset_ok = asset is not None and asset.eligible(
                now, security_types=self.eligible_security_types
            )
            reasons = [RankingReason("SOURCE", source)]
            if not asset_ok:
                reasons.append(RankingReason("asset_or_halt_unknown"))
            if asset is not None:
                reasons.append(RankingReason("ASSET", json.dumps(asdict(asset), sort_keys=True)))
            if mover is not None:
                reasons.extend(
                    RankingReason(reason) for reason in mover.observations["unknown_reasons"]
                )
                reasons.extend(
                    RankingReason("gate_failed", gate)
                    for gate in GATES
                    if gate not in mover.gates_passed
                )
                reasons.append(
                    RankingReason("REFERENCE", str(mover.observations["reference_source"]))
                )
                price = mover.observations["price"]
                low, high = self.price_band_edges
                reasons.append(
                    RankingReason(
                        "LABELS",
                        json.dumps(
                            {
                                "price_band": None
                                if price is None
                                else f">={high}"
                                if price >= high
                                else f"{low}-{high}"
                                if price >= low
                                else f"<{low}",
                                "security_type": asset.security_type if asset else None,
                                "cap_bucket": None,
                            },
                            sort_keys=True,
                        ),
                    )
                )
                reasons.append(
                    RankingReason(
                        "CLOCKS",
                        json.dumps(
                            {
                                "event_ns": mover.observations["ts_event"],
                                "vendor_receipt_ns": mover.observations["ts_init"],
                                "local_receipt_ns": mover.known_at,
                                "generation_ns": now,
                                "publication_ns": now,
                                "entry_eligible_ns": None,
                                "submission_ns": None,
                            },
                            sort_keys=True,
                        ),
                    )
                )
            features = []
            if mover is not None:
                event = mover.observations["ts_event"]
                features = [
                    RankingFeature(key, getattr(mover, key), event, mover.known_at)
                    for key in ("move", "move_1m", "move_5m", "dvol_ratio", "trades_accel")
                ]
                features.extend(
                    RankingFeature(
                        key,
                        mover.observations[key],
                        mover.observations["last_price_ts"] if key == "price" else event,
                        mover.known_at,
                    )
                    for key in ("price", "dollar_volume", "spread", "move_from_open")
                )
            rows.append(
                RankingRow(
                    name,
                    score=mover.score if mover else None,
                    rank=mover.rank if mover else None,
                    features=tuple(features),
                    completed=ready,
                    partial=not ready,
                    focus_eligible=bool(mover and asset_ok and qualifies(mover)),
                    reasons=tuple(reasons),
                )
            )
        generation = RankingGeneration(
            generation_id=GenerationId(
                self.epoch, self.sequence, self.sequence - 1 if self.sequence else None
            ),
            session_date=self.segment.session_date,
            input_cutoff=self.input_cutoff,
            ts_event=self.last_event,
            ts_init=now,
            known_at=now,
            method_version=self.method_version,
            profile_version="registry/" + self.registry.params_hash,
            ranker_version=RANKER,
            coverage=self.coverage
            if ready
            else replace(self.coverage, state=CoverageState.DEGRADED, complete=False),
            status=GenerationStatus.PROVISIONAL,
            rows=tuple(rows),
        )
        data_type = ranking_data_type(self.feed)
        self.send(data_type, CustomData(data_type, generation))
        top = replace(
            generation,
            rows=tuple(row for row in generation.rows if row.rank is not None)[
                : int(self.registry.top_n)
            ],
            coverage=replace(
                generation.coverage, complete=False, reasons=(RankingReason("TOP_N_PROJECTION"),)
            ),
        )
        data_type = ranking_data_type(self.feed, "top")
        self.send(data_type, CustomData(data_type, top))
        self.published_at = now
        self.sequence += 1
        if self.heartbeat is not None:
            self.heartbeat.emit(generation)
        return generation
