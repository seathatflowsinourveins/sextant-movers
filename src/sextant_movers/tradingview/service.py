"""Replay one segment into one validated display generation; no transport.

Acceptance: the reference, src/sextant/tradingview/prestage.py at
06562bec0898a7203f4eaf69086ced83d2477e6b:71,93-94,490-566,633-814,
excluding the view parameter and its branches (667,684-685,688-689,692,728-773).
Departures: ServiceError has our own docstring; no carried view, fetch exactly
 tuple(symbols) and replay all returned bars; required caller scope and paper_slot
reach the host unchanged; required sip_authority, mapping_source, reference_source
and asset_source replace input assertions; max_age and feed are required, clock
remains optional; imports use ranking.bars, ranking.references.Source, atomic,
ranking.baseline.Baseline/Segment and ranking.engine.ReferencePrices, without cast.
Required method_version, eligible_security_types and price_band_edges reach the
publisher unchanged; method_version and paper_slot also reach the bridge.
Required send uses the publisher's callable directly: no per-tick bus or trader id.

Keep ours: nautechsystems/nautilus_trader@v2.0.0rc8
 d53849b216e20f2c122ca01b4dd0da377638b186:
 docs/concepts/backtesting/apis-and-runs.md:22-47 replays streams to registered
components inside BacktestEngine with venues, not this host. Searches in
 docs/concepts/backtesting, python/nautilus_trader/{backtest,persistence,common}
and RELEASES.md found no directory-generation publication or standalone host replay.
Build on that pin's python/nautilus_trader/core/datetime.py:64 (unix_nanos_to_dt);
common/__init__.pyi:719 supplies the publisher's send shape on DataActor.
Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Doc/library/os.rst:2754-2760,3594 (atomic replace and symlink),
Doc/library/pathlib.rst:1021,1073,1583 (exists, is_symlink, unlink missing_ok),
Doc/library/dataclasses.rst:475 (replace), datetime.rst:965 (aware fromtimestamp).
This package's publisher, host, producer.Clocks, bridge, inputs reader and atomic
write are composed as shipped. Existing input_max_age_s and caller max_age supply ages.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nautilus_trader.core.datetime import unix_nanos_to_dt
from nautilus_trader.model import CustomData, DataType

from sextant_movers.atomic import write_atomic
from sextant_movers.lease import AccountScope, WriterLease
from sextant_movers.ranking.bars import MINUTE_NS, Bar, BarPoll
from sextant_movers.ranking.baseline import Baseline, Segment
from sextant_movers.ranking.engine import ReferencePrices
from sextant_movers.ranking.publisher import AssetState, RankingPublisher
from sextant_movers.ranking.references import Source
from sextant_movers.ranking.registry import Registry
from sextant_movers.sessions import ET, SECOND
from sextant_movers.tradingview.bridge import DisplayBounds, project_display_unit
from sextant_movers.tradingview.host import BarFidelityHost
from sextant_movers.tradingview.inputs import _ns, read_research_inputs
from sextant_movers.tradingview.producer import Clocks

SUPPORTED = frozenset({"PRE", "RTH", "POST"})


class ServiceError(ValueError):
    """A tick cannot publish a complete, validated display generation."""


def _completed(
    bars: Iterable[Bar], segment: Segment, completed_end_ns: int
) -> dict[int, list[Bar]]:
    by_minute: dict[int, list[Bar]] = {}
    for item in bars:
        if (
            item.start_ns < segment.start_ns
            or item.close_ns > segment.end_ns
            or item.close_ns > completed_end_ns
        ):
            continue
        by_minute.setdefault(item.close_ns, []).append(item)
    return by_minute


def _replay(
    *,
    lease: WriterLease,
    scope: AccountScope,
    paper_slot: int,
    send: Callable[[DataType, CustomData], None],
    method_version: str,
    eligible_security_types: frozenset[str],
    price_band_edges: tuple[int, int],
    segment: Segment,
    baseline: Baseline,
    symbols: Sequence[str],
    prior_closes: Mapping[str, Decimal],
    prior_close_ns: int,
    symbol_map: Mapping[str, str | None],
    registry: Registry,
    epoch: str,
    universe_source: str,
    feed: str,
    sip_authority: str,
    reference_source: str,
    asset_source: str,
    by_minute: Mapping[int, list[Bar]],
) -> tuple[RankingPublisher, BarFidelityHost, int]:
    """One fresh publisher and host fed every completed minute in clock order."""
    publisher = RankingPublisher(
        send=send,
        method_version=method_version,
        eligible_security_types=eligible_security_types,
        price_band_edges=price_band_edges,
        epoch=epoch,
        feed=feed,
        segment=segment,
        registry=registry,
        universe=tuple(symbols),
        universe_source=universe_source,
        baseline=baseline,
    )
    for name in symbols:
        publisher.set_symbol(
            name,
            references=ReferencePrices(prior_closes[name], prior_close_ns, reference_source),
            asset=AssetState(prior_close_ns, segment.end_ns, None, "common", asset_source),
        )
    host = BarFidelityHost(
        publisher,
        lease=lease,
        scope=scope,
        paper_slot=paper_slot,
        sip_authority=sip_authority,
        symbol_map=dict(symbol_map),
    )
    return publisher, host, _feed_minutes(host, symbols, by_minute, since_ns=None)


def _feed_minutes(
    host: BarFidelityHost,
    symbols: Sequence[str],
    by_minute: Mapping[int, list[Bar]],
    *,
    since_ns: int | None,
    before_ns: int | None = None,
) -> int:
    ingested = 0
    for close_ns in sorted(by_minute):
        if (since_ns is not None and close_ns < since_ns) or (
            before_ns is not None and close_ns >= before_ns
        ):
            continue
        ingested += host.ingest(
            BarPoll(bars=tuple(by_minute[close_ns]), requested=tuple(symbols)),
            market_ns=close_ns,
            received_ns=close_ns,
        )
    return ingested


def _drop_stale(publisher: RankingPublisher, publish_clock_ns: int, max_age_ns: int) -> None:
    for name, state in list(publisher.states.items()):
        if state.latest is None:
            continue
        event_ns = state.latest.observations.get("ts_event")
        price_ns = state.latest.observations.get("last_price_ts")
        if (
            type(event_ns) is not int
            or publish_clock_ns - event_ns > max_age_ns
            or (type(price_ns) is int and publish_clock_ns - price_ns > max_age_ns)
        ):
            publisher.states[name] = replace(state, latest=None)


def publish_tick(
    *,
    lease: WriterLease,
    scope: AccountScope,
    paper_slot: int,
    source: Source,
    send: Callable[[DataType, CustomData], None],
    method_version: str,
    eligible_security_types: frozenset[str],
    price_band_edges: tuple[int, int],
    segment: Segment,
    baseline: Baseline,
    symbols: Sequence[str],
    prior_closes: Mapping[str, Decimal],
    prior_close_ns: int,
    symbol_map: Mapping[str, str],
    registry: Registry,
    epoch: str,
    universe_source: str,
    now_ns: int,
    unit_dir: Path,
    j5_path: Path,
    bounds: DisplayBounds,
    sip_authority: str,
    mapping_source: str,
    reference_source: str,
    asset_source: str,
    max_age: int,
    feed: str,
    clock: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """Replay once, validate the three files, then replace one generation pointer.

    An optional caller clock is read after replay to stamp and validate publication.
    """
    if segment.name not in SUPPORTED:
        raise ServiceError(f"the producer does not write {segment.name} units")
    if not segment.start_ns <= now_ns < segment.end_ns:
        raise ServiceError("the publish clock is outside the supplied segment")
    start = datetime.fromtimestamp(segment.start_ns / SECOND, ET)
    completed_end_ns = now_ns - now_ns % MINUTE_NS
    if completed_end_ns <= segment.start_ns:
        raise ServiceError("no completed minute bars for this segment")
    end = datetime.fromtimestamp(completed_end_ns / SECOND, ET)
    fetch = tuple(symbols)
    bars = source.minute_bars(fetch, start=start, end=end)
    by_minute = _completed(bars, segment, completed_end_ns)
    clocks = sorted(by_minute)
    if not clocks:
        raise ServiceError("no completed minute bars for this segment")
    latest_close_ns = clocks[-1]

    publisher, host, ingested = _replay(
        lease=lease,
        scope=scope,
        paper_slot=paper_slot,
        send=send,
        method_version=method_version,
        eligible_security_types=eligible_security_types,
        price_band_edges=price_band_edges,
        segment=segment,
        baseline=baseline,
        symbols=symbols,
        prior_closes=prior_closes,
        prior_close_ns=prior_close_ns,
        symbol_map=symbol_map,
        registry=registry,
        epoch=epoch,
        universe_source=universe_source,
        feed=feed,
        sip_authority=sip_authority,
        reference_source=reference_source,
        asset_source=asset_source,
        by_minute=by_minute,
    )

    publication_ns = now_ns if clock is None else int(clock())
    if not segment.start_ns <= publication_ns < segment.end_ns:
        raise ServiceError(f"the {segment.name} segment closed before the tick could publish")
    if publication_ns < latest_close_ns:
        raise ServiceError(
            "the publication clock moved behind the replayed bars: "
            f"{publication_ns} < {latest_close_ns}"
        )
    publish_clock_ns = publication_ns
    max_age_ns = registry.seconds("input_max_age_s")
    _drop_stale(publisher, publish_clock_ns, max_age_ns)
    if not host.heartbeat(at=latest_close_ns, received_ns=publish_clock_ns):
        raise ServiceError("the bar-fidelity heartbeat was refused")
    j5 = host.publish(
        Path(j5_path),
        now=publish_clock_ns,
        mapping_clocks=Clocks(latest_close_ns, publish_clock_ns, mapping_source),
    )
    if j5.get("status") != "fresh":
        raise ServiceError(f"the J5 unit is not fresh: {j5.get('reason')}")
    projected = project_display_unit(
        j5, bounds=bounds, method_version=method_version, paper_slot=paper_slot
    )
    unit_dir = Path(unit_dir)
    if unit_dir.exists() and not unit_dir.is_symlink():
        raise ServiceError("the display unit is not a generation pointer; move it aside once")
    generations = unit_dir.parent / f"{unit_dir.name}.generations"
    generation = generations / j5["generation_sha256"]
    for filename, document in projected.documents().items():
        write_atomic(generation / filename, document)
    validated = read_research_inputs(
        generation,
        # Preserve the nanoseconds of the exact clock which stamped generated_at.
        now=unix_nanos_to_dt(publish_clock_ns),
        max_age=max_age,
    )
    pointer = unit_dir.parent / f".{unit_dir.name}.pointer"
    pointer.unlink(missing_ok=True)
    os.symlink(f"{generations.name}/{generation.name}", pointer, target_is_directory=True)
    os.replace(pointer, unit_dir)
    return {
        "schema": "sextant.tv-prestage-receipt/1",
        "segment": segment.name,
        "session_date": segment.session_date.isoformat(),
        "now_ns": now_ns,
        "publish_clock_ns": publish_clock_ns,
        "bars": sum(len(by_minute[clock]) for clock in clocks),
        "ingested": ingested,
        "expires_at_ns": _ns(validated.expires_at),
        "unit_dir": str(unit_dir),
        "generation_sha256": j5["generation_sha256"],
        "universe_source": universe_source,
    }
