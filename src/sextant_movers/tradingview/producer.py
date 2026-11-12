"""One accepted generation becomes one display document; no transport or request.

Build on nautechsystems/nautilus_trader@v2.0.0rc8
 d53849b216e20f2c122ca01b4dd0da377638b186:python/nautilus_trader/model/__init__.pyi:
7035 (TradeTick, seven positional fields), 1673 (CustomData), 1688 (DataType), used as is.
Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Doc/library/json.rst:308,324,356 (Decimal and refusal of nonfinite constants),
Doc/library/dataclasses.rst:384 (asdict), used as is.
Feed name: alpacahq/alpaca-py@v0.44.0 cc4cb3b7ba50ae250e621983c2779047fb16bb28:
alpaca/data/enums.py:56 (DataFeed.SIP). Watchlist size: TradingView's support page quoted
in docs/landscape.md, read 2026-11-11; MAX_WATCHLIST_SYMBOLS is an UNCALIBRATED vendor bound.
Acceptance: the reference, src/sextant/tradingview/producer.py:50-263 at
06562bec0898a7203f4eaf69086ced83d2477e6b, value for value except caller scope, slot,
eligibility masks and reader identity inputs; shared feed, mapping prefixes and named bound.
Conditions are checked, never passed on: ranking/engine.py:11-12 has no conditions parameter;
price_eligible and volume_eligible are exactly the caller's, with no sale-condition literal.
ADR-0003 and the design's display path own the composition. Searches of the engine's model
stubs, crates and docs/concepts, and the MCP Python SDK, found no shipped ranked display unit.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from nautilus_trader.model import CustomData, TradeTick

from sextant_movers.atomic import write_atomic
from sextant_movers.lease import AccountScope, WriterLease
from sextant_movers.ranking.generation import Coverage, CoverageState
from sextant_movers.ranking.publisher import RankingPublisher, ranking_data_type
from sextant_movers.ranking.reader import GenerationReader
from sextant_movers.tradingview.inputs import QUALIFIED, REFUSED_PREFIXES

SIP_FEED = "sip"
MAX_WATCHLIST_SYMBOLS = 1000


@dataclass(frozen=True, slots=True)
class Clocks:
    MARKET_AVAILABLE_AT: int
    ACQUIRED_AT: int
    source: str

    def __post_init__(self) -> None:
        if (
            any(
                type(at) is not int or at < 0 for at in (self.MARKET_AVAILABLE_AT, self.ACQUIRED_AT)
            )
            or not self.source.strip()
        ):
            raise ValueError("explicit integer clocks and source required")


class LiveProducer:
    def __init__(
        self,
        publisher: RankingPublisher,
        lease: WriterLease,
        *,
        scope: AccountScope,
        paper_slot: int,
        sip_authority: str,
    ) -> None:
        if (
            lease.scope != scope
            or not lease.held
            or type(paper_slot) is not int
            or not sip_authority.strip()
            or publisher.feed != SIP_FEED
            or publisher.segment.name == "OVERNIGHT"
            or publisher.sequence != 0
            or publisher.registry.top_n > MAX_WATCHLIST_SYMBOLS
        ):
            raise ValueError("fresh slot-2 SIP publisher, entitlement and held lease required")
        self.publisher, self.lease, self.sip_authority = publisher, lease, sip_authority
        self.paper_slot = paper_slot
        self.reader = GenerationReader(
            publisher.registry,
            feed=SIP_FEED,
            segment=publisher.segment,
            universe_hash=publisher.universe_hash,
            baseline_hash=publisher.baseline.identity,
            method_version=publisher.method_version,
            eligible_security_types=publisher.eligible_security_types,
        )
        self.provenance: dict[str, dict] = {}
        self.snapshot: dict | None = None
        self.error: str | None = None
        self.last_market = self.heartbeat_deadline = 0

    def invalidate(self, reason: str) -> None:
        """Gaps, disconnects, revisions and invalid data require a recovered epoch."""
        self.error = reason
        self.publisher.set_coverage(Coverage(CoverageState.DEGRADED, False))
        self.reader.invalidate(reason)

    def _admit(self, clocks: Clocks, now: int) -> bool:
        if self.error or not self.lease.held:
            raise ValueError("publisher invalid or stream lease released")
        if type(now) is not int or not (
            self.publisher.segment.start_ns <= now < self.publisher.segment.end_ns
        ):
            raise ValueError("explicit clock inside supplied session required")
        return clocks.MARKET_AVAILABLE_AT <= now

    def on_trade(
        self,
        tick: TradeTick,
        *,
        clocks: Clocks,
        now: int,
        price_eligible: bool,
        volume_eligible: bool,
        conditions: tuple[str, ...],
        mask_source: str,
    ) -> bool:
        try:
            if not self._admit(clocks, now):
                return False
            if (
                not mask_source.strip()
                or type(price_eligible) is not bool
                or type(volume_eligible) is not bool
                or not isinstance(conditions, tuple)
                or any(not isinstance(c, str) or not c for c in conditions)
                or not tick.ts_event <= clocks.MARKET_AVAILABLE_AT
                or clocks.MARKET_AVAILABLE_AT < self.last_market
            ):
                raise ValueError("invalid market clocks or condition authority")
            view = TradeTick(
                tick.instrument_id,
                tick.price,
                tick.size,
                tick.aggressor_side,
                tick.trade_id,
                tick.ts_event,
                clocks.MARKET_AVAILABLE_AT,
            )
            self.publisher.on_trade(
                view,
                known_at=clocks.MARKET_AVAILABLE_AT,
                price_eligible=price_eligible,
                volume_eligible=volume_eligible,
            )
            self.last_market = clocks.MARKET_AVAILABLE_AT
            self.provenance[str(tick.instrument_id.symbol)] = {
                **asdict(clocks),
                "native_ts_init": tick.ts_init,
                "mask_source": mask_source,
            }
            return True
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.invalidate("invalid_trade; recover a new epoch")
            raise

    def on_snapshot(self, response_json: str, *, clocks: Clocks, now: int) -> bool:
        """Raw diagnostics only: aggregate/latest trades cannot seed tick history."""
        try:
            if not self._admit(clocks, now):
                return False
            payload = json.loads(response_json, parse_float=Decimal, parse_constant=_invalid_number)
            if (
                not isinstance(payload, dict)
                or not payload
                or not payload.keys() <= set(self.publisher.universe)
                or not all(isinstance(row, dict) for row in payload.values())
                or (
                    self.snapshot
                    and clocks.MARKET_AVAILABLE_AT < self.snapshot["MARKET_AVAILABLE_AT"]
                )
            ):
                raise ValueError("invalid or regressed snapshot")
            self.snapshot = {**asdict(clocks), "response_json": response_json}
            return True
        except (ValueError, KeyError, TypeError, ArithmeticError):
            self.invalidate("invalid_snapshot; recover a new epoch")
            raise

    def heartbeat(self, *, at: int, received_ns: int) -> bool:
        accepted = self.lease.held and self.reader.accept_heartbeat(at=at, received_ns=received_ns)
        self.heartbeat_deadline = (
            at + self.publisher.registry.seconds("heartbeat_max_age_s") if accepted else 0
        )
        return accepted

    def build_unit(
        self, *, now: int, symbol_map: Mapping[str, str | None], mapping_clocks: Clocks
    ) -> dict:
        common = {
            "schema": "sextant.j5-ranked/1",
            "display_only": True,
            "paper_slot": self.paper_slot,
        }
        try:
            if not self._admit(mapping_clocks, now):
                raise ValueError("mapping unavailable")
            record = self.publisher.publish(now=now)
            if record is not None:
                self.reader.accept_generation(
                    CustomData(ranking_data_type(SIP_FEED), record), now=now
                )
            record = self.reader.generation
            if not self.reader.fresh(now) or record is None:
                raise ValueError(self.reader.reason)
            expires = min(
                record.known_at + self.publisher.registry.seconds("generation_max_age_s"),
                record.ts_event + self.publisher.registry.seconds("input_max_age_s"),
                self.heartbeat_deadline,
                self.publisher.segment.end_ns,
            )
            if now >= expires or mapping_clocks.MARKET_AVAILABLE_AT > record.known_at:
                raise ValueError("expired generation or future mapping")
            selected = [r for r in record.rows if r.rank is not None][
                : int(self.publisher.registry.top_n)
            ]
            if any(row.partial or not row.completed for row in selected):
                raise ValueError("partial display row")
            if any(
                f.ts_event is None
                or f.value is None
                or not 0 <= now - f.ts_event <= self.publisher.registry.seconds("input_max_age_s")
                for row in selected
                for f in row.features
                if f.name in {"price", "move", "dvol_ratio", "trades_accel"}
            ):
                raise ValueError("projected symbol input stale")
            members = [symbol_map.get(row.symbol) for row in selected]
            if (
                not members
                or any(
                    not isinstance(member, str)
                    or not QUALIFIED.fullmatch(member)
                    or member.split(":", 1)[0] in REFUSED_PREFIXES
                    for member in members
                )
                or len(set(members)) != len(members)
            ):
                raise ValueError("empty ranking or missing/ambiguous exact mapping")
            return {
                **common,
                "status": "fresh",
                "generation_sha256": self.reader.generation_key,
                "params_hash": self.publisher.registry.params_hash,
                "generated_at_ns": record.known_at,
                "expires_at_ns": expires,
                "ranking_generation": record.to_dict(),
                "watchlists": {"NS Ranked": members},
                "provenance": {
                    "sip_authority": self.sip_authority,
                    "trades": self.provenance.copy(),
                    "snapshot": self.snapshot,
                    "mapping": asdict(mapping_clocks),
                },
            }
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            return {**common, "status": "unavailable", "watchlists": None, "reason": str(exc)}

    def publish_unit(self, path: Path, **inputs) -> dict:
        """One durable complete document replaces the carried multi-file assembler."""
        unit = self.build_unit(**inputs)
        write_atomic(path, unit)
        return unit


def _invalid_number(value: str) -> None:
    raise ValueError("nonfinite snapshot number")
