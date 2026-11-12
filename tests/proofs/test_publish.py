"""Part 5e-2: publication and both engine views, with synthetic inputs only.

Build on the reference's `src/sextant/ranking/publisher.py:136-309` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`.
"""

import json
import subprocess
import sys
from dataclasses import asdict, replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import reference_child as child
from test_publisher import ASSET, NAMES, NS, REFS, START, Send, ready, registry, tick

from sextant_movers.ranking import engine, publisher
from sextant_movers.ranking.generation import (
    Coverage,
    CoverageState,
    GenerationId,
    GenerationStatus,
    RankingGeneration,
    RankingReason,
)
from sextant_movers.ranking.publisher import RANKER, RankingPublisher, ranking_data_type

NOW = START + 360 * NS + 100
COVER = Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0, (RankingReason("ZZ_COVER"),))


def filled(**kw: Any) -> RankingPublisher:
    pub = ready(registry=registry(), **kw)
    for name in NAMES:
        for second, price in ((0, "2.00"), (60, "2.10"), (300, "2.40"), (360, "2.60")):
            pub.on_trade(tick(second, price, name=name))
    pub.set_coverage(COVER)
    return pub


def full(pub: RankingPublisher, now: int = NOW) -> RankingGeneration:
    gen = pub.publish(now=now)
    assert gen is not None
    return gen


def changed(pub: RankingPublisher, name: str = "ZZPBA", **kw: Any) -> None:
    state = pub.states[name]
    assert state.latest is not None
    pub.states[name] = replace(state, latest=replace(state.latest, **kw))


def unready(pub: RankingPublisher, now: int = NOW) -> RankingGeneration:
    gen = full(pub, now)
    assert gen.coverage == replace(pub.coverage, state=CoverageState.DEGRADED, complete=False)
    assert all(
        r.score is r.rank is None
        and not r.features
        and not r.completed
        and r.partial
        and not r.focus_eligible
        for r in gen.rows
    )
    return gen


def test_gate_names_are_the_reference_order_as_test_input_only() -> None:
    assert engine.GATES == (
        "mover_min_move",
        "move_1m_positive",
        "mover_min_trades_accel",
        "mover_min_dvol_ratio",
    )


@pytest.mark.parametrize("state", [s for s in CoverageState if s is not CoverageState.READY])
def test_ready_requires_the_ready_state(state: CoverageState) -> None:
    pub = filled()
    pub.set_coverage(replace(COVER, state=state))
    unready(pub)


def test_ready_requires_complete() -> None:
    pub = filled()
    pub.set_coverage(replace(COVER, complete=False))
    unready(pub)


@pytest.mark.parametrize("count", [None, 2, 4])
def test_ready_requires_matching_expected_count(count: int | None) -> None:
    pub = filled()
    pub.set_coverage(replace(COVER, expected_symbols=count))
    unready(pub)


@pytest.mark.parametrize("count", [None, 2, 4])
def test_ready_requires_matching_observed_count(count: int | None) -> None:
    pub = filled()
    pub.set_coverage(replace(COVER, observed_symbols=count))
    unready(pub)


@pytest.mark.parametrize("count", [None, 1])
def test_ready_requires_zero_missing(count: int | None) -> None:
    pub = filled()
    pub.set_coverage(replace(COVER, missing_symbols=count))
    unready(pub)


@pytest.mark.parametrize("count", [None, 1])
def test_ready_requires_zero_invalid(count: int | None) -> None:
    pub = filled()
    pub.set_coverage(replace(COVER, invalid_symbols=count))
    unready(pub)


@pytest.mark.parametrize("count", [None, 1])
def test_ready_requires_zero_late(count: int | None) -> None:
    pub = filled()
    pub.set_coverage(replace(COVER, late_symbols=count))
    unready(pub)


def test_ready_requires_event_at_or_after_segment_start() -> None:
    pub = filled()
    pub.last_event = START - 1
    unready(pub, START)


def test_ready_requires_event_at_or_before_input_cutoff() -> None:
    pub = filled()
    pub.input_cutoff = pub.last_event - 1
    unready(pub)


def test_ready_requires_cutoff_before_now_constructor_keeps_incoherence() -> None:
    pub = filled()
    pub.input_cutoff = NOW + 1
    gen = unready(pub)
    assert gen.input_cutoff > gen.known_at
    assert (pub.sequence, pub.published_at) == (1, NOW)


def test_ready_requires_now_before_segment_end() -> None:
    pub = filled()
    pub.last_event = pub.input_cutoff = pub.segment.end_ns
    unready(pub, pub.segment.end_ns)


def test_ready_accepts_all_equal_time_links_at_segment_start() -> None:
    pub = ready()
    pub.set_coverage(COVER)
    assert full(pub, START).coverage is COVER


def test_ready_accepts_input_age_at_the_edge() -> None:
    pub = filled()
    assert full(pub, pub.last_event + pub.registry.seconds("input_max_age_s")).coverage is COVER


def test_ready_refuses_input_age_one_past_the_edge() -> None:
    pub = filled()
    unready(pub, pub.last_event + pub.registry.seconds("input_max_age_s") + 1)


def test_ranking_is_called_only_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    original = engine.rank_movers

    def rank(rows: Any) -> Any:
        rows = tuple(rows)
        called.append(rows)
        return original(rows)

    monkeypatch.setattr(publisher, "rank_movers", rank)
    pub = filled()
    full(pub)
    assert called == [tuple(s.latest for s in pub.states.values())]
    pub.set_coverage(replace(COVER, complete=False))
    unready(pub, NOW + NS)
    assert len(called) == 1


@pytest.mark.parametrize("failed", [None, *engine.GATES])
@pytest.mark.parametrize("asset_kind", ["unknown", "ineligible", "eligible"])
def test_row_reasons_and_each_focus_link(failed: str | None, asset_kind: str) -> None:
    pub = filled()
    state = pub.states["ZZPBA"]
    mover = state.latest
    assert mover is not None
    obs = dict(mover.observations) | {"unknown_reasons": ("zz_unknown_one", "zz_unknown_two")}
    passed = tuple(g for g in engine.GATES if g != failed)
    changed(pub, gates_passed=passed, observations=obs)
    asset = None if asset_kind == "unknown" else replace(ASSET, halted=asset_kind == "ineligible")
    pub.set_symbol("ZZPBA", references=REFS, asset=asset)
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    codes = ["SOURCE"] + (["asset_or_halt_unknown"] if asset_kind != "eligible" else [])
    codes += (["ASSET"] if asset else []) + ["zz_unknown_one", "zz_unknown_two"]
    codes += (["gate_failed"] if failed else []) + ["REFERENCE", "LABELS", "CLOCKS"]
    assert [r.code for r in row.reasons] == codes
    reasons = {r.code: r for r in row.reasons}
    source = {
        "feed": pub.feed,
        "segment": pub.segment.name,
        "start_ns": START,
        "end_ns": pub.segment.end_ns,
        "session_source": pub.segment.source,
        "universe_source": pub.universe_source,
        "baseline_source": pub.baseline.provenance,
        "params_hash": pub.registry.params_hash,
        "universe_hash": pub.universe_hash,
        "baseline_hash": pub.baseline.identity,
    }
    assert json.loads(reasons["SOURCE"].detail or "null") == source
    if asset:
        assert json.loads(reasons["ASSET"].detail or "null") == asdict(asset)
    assert reasons["REFERENCE"].detail == obs["reference_source"]
    assert json.loads(reasons["LABELS"].detail or "null") == {
        "price_band": "1-5",
        "security_type": asset.security_type if asset else None,
        "cap_bucket": None,
    }
    assert json.loads(reasons["CLOCKS"].detail or "null") == {
        "event_ns": obs["ts_event"],
        "vendor_receipt_ns": obs["ts_init"],
        "local_receipt_ns": mover.known_at,
        "generation_ns": NOW,
        "publication_ns": NOW,
        "entry_eligible_ns": None,
        "submission_ns": None,
    }
    if failed:
        assert reasons["gate_failed"].detail == failed
    assert row.focus_eligible is (asset_kind == "eligible" and failed is None)
    assert row.rank is not None and row.score == mover.score and row.completed and not row.partial
    assert all(r.children == () for r in row.reasons)


def test_all_failed_gates_keep_the_engine_order() -> None:
    pub = filled()
    changed(pub, gates_passed=())
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    assert tuple(r.detail for r in row.reasons if r.code == "gate_failed") == engine.GATES


def test_features_values_times_and_last_price_time() -> None:
    pub = filled()
    mover = pub.states["ZZPBA"].latest
    assert mover is not None
    obs = dict(mover.observations) | {"last_price_ts": START + NS}
    changed(pub, observations=obs)
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    keys = ("move", "move_1m", "move_5m", "dvol_ratio", "trades_accel")
    values = {k: getattr(mover, k) for k in keys}
    values |= {k: obs[k] for k in ("price", "dollar_volume", "spread", "move_from_open")}
    assert [f.name for f in row.features] == sorted(values)
    for f in row.features:
        assert (f.value, f.ts_event, f.known_at, f.partial, f.completed) == (
            values[f.name],
            obs["last_price_ts"] if f.name == "price" else obs["ts_event"],
            mover.known_at,
            False,
            True,
        )


@pytest.mark.parametrize("edges", [(1, 5), (3, 9)])
@pytest.mark.parametrize("which", ["below", "low", "high", "unknown"])
def test_price_band_labels_follow_both_supplied_edges(edges: tuple[int, int], which: str) -> None:
    pub = filled(price_band_edges=edges)
    mover = pub.states["ZZPBA"].latest
    assert mover is not None
    low, high = edges
    prices = {"below": D(low) - D("0.01"), "low": D(low), "high": D(high), "unknown": None}
    changed(pub, observations=dict(mover.observations) | {"price": prices[which]})
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    labels = json.loads(next(r.detail for r in row.reasons if r.code == "LABELS") or "null")
    expected = {"below": f"<{low}", "low": f"{low}-{high}", "high": f">={high}", "unknown": None}
    assert labels["price_band"] == expected[which]


def test_kept_never_traded_name_has_no_score_or_focus() -> None:
    pub = filled()
    pub.states["ZZPBA"] = engine.MoverState()
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    assert row.score is row.rank is None and row.features == ()
    assert row.completed and not row.partial and not row.focus_eligible
    assert [r.code for r in row.reasons] == ["SOURCE", "ASSET"]


def test_three_identities_versions_times_and_status() -> None:
    pub = filled(method_version="zz-caller-method")
    for sequence in range(3):
        now = NOW + sequence * NS
        gen = full(pub, now)
        assert gen.generation_id == GenerationId(
            "zz-epoch", sequence, sequence - 1 if sequence else None
        )
        assert gen.session_date == pub.segment.session_date
        assert (gen.ts_event, gen.input_cutoff) == (pub.last_event, pub.input_cutoff)
        assert (gen.known_at, gen.ts_init) == (now, now)
        assert gen.method_version == "zz-caller-method"
        assert gen.profile_version == "registry/" + pub.registry.params_hash
        assert gen.ranker_version == RANKER
        assert gen.status is GenerationStatus.PROVISIONAL
        assert (pub.published_at, pub.sequence) == (now, sequence + 1)


def test_kept_top_projects_constructor_rank_order() -> None:
    send, pub = Send(), filled()
    pub.send = send
    for name, score in zip(sorted(NAMES), (D(1), D(3), D(2)), strict=True):
        changed(pub, name, score=score)
    gen = full(pub)
    assert [r.symbol for r in gen.rows] == ["ZZPBB", "ZZPBC", "ZZPBA"]
    assert [r.rank for r in gen.rows] == [1, 2, 3]
    assert len(send.calls) == 2
    for (dt, envelope), view in zip(send.calls, ("full", "top"), strict=True):
        assert dt == envelope.data_type == ranking_data_type(pub.feed, view)
        assert dt.metadata == {"feed": pub.feed, "view": view, "ranker": RANKER}
    assert send.calls[0][1].data is gen
    top = send.calls[1][1].data
    assert top == replace(
        gen,
        rows=gen.rows[:2],
        coverage=replace(COVER, complete=False, reasons=(RankingReason("TOP_N_PROJECTION"),)),
    )
    assert top.coverage.state is CoverageState.READY and top.generation_id == gen.generation_id


def test_kept_not_ready_top_is_empty_with_degraded_counts() -> None:
    pub, send = filled(), Send()
    pub.send = send
    pub.set_coverage(replace(COVER, missing_symbols=1))
    unready(pub)
    top = send.calls[1][1].data
    assert top.rows == ()
    assert top.coverage == replace(
        pub.coverage,
        state=CoverageState.DEGRADED,
        complete=False,
        reasons=(RankingReason("TOP_N_PROJECTION"),),
    )


def test_kept_equal_and_early_clocks_send_nothing() -> None:
    pub, send = filled(), Send()
    pub.send = send
    first = full(pub)
    for at in (NOW, NOW + NS - 1):
        assert pub.publish(now=at) is None and len(send.calls) == 2
        assert (pub.published_at, pub.sequence) == (NOW, 1)
    with pytest.raises(ValueError, match=r"^publication clock regressed$"):
        pub.publish(now=NOW - 1)
    assert len(send.calls) == 2
    assert full(pub, NOW + NS).generation_id.previous_sequence == first.generation_id.sequence


class Beat:
    def __init__(self, events: list[Any], result: bool | Exception = True) -> None:
        self.events, self.result = events, result

    def emit(self, gen: RankingGeneration) -> bool:
        self.events.append(("beat", gen))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_heartbeat_once_after_both_sends_and_marks() -> None:
    events: list[Any] = []

    class ObservedBeat(Beat):
        def emit(self, gen: RankingGeneration) -> bool:
            assert (pub.published_at, pub.sequence) == (NOW, 1)
            return super().emit(gen)

    pub = filled(heartbeat=ObservedBeat(events))

    def send(dt: Any, data: Any) -> None:
        assert pub.published_at is None and pub.sequence == 0
        events.append((dt.metadata["view"], data.data))

    pub.send = send
    gen = full(pub)
    assert [event[0] for event in events] == ["full", "top", "beat"]
    assert events[0][1] is events[2][1] is gen
    assert pub.publish(now=NOW) is None and len(events) == 3


@pytest.mark.parametrize("fail_on", [1, 2])
def test_kept_send_failure_preserves_marks_and_heartbeat(
    fail_on: int,
) -> None:
    events: list[Any] = []
    pub = filled(heartbeat=Beat(events))
    # Start after a successful publication, to prove preservation of noninitial marks too.
    full(pub)
    events.clear()
    before = pub.published_at, pub.sequence

    def send(dt: Any, data: Any) -> None:
        events.append(dt.metadata["view"])
        if len(events) == fail_on:
            raise RuntimeError("zz-send")

    pub.send = send
    with pytest.raises(RuntimeError, match=r"^zz-send$"):
        pub.publish(now=NOW + NS)
    assert (pub.published_at, pub.sequence) == before
    assert events == ["full", "top"][:fail_on]


@pytest.mark.parametrize("result", [False, RuntimeError("zz-heartbeat")])
def test_heartbeat_false_or_raise_happens_after_state_advances(result: bool | Exception) -> None:
    events: list[Any] = []
    pub, send = filled(heartbeat=Beat(events, result)), Send()
    pub.send = send
    if isinstance(result, Exception):
        with pytest.raises(RuntimeError, match=r"^zz-heartbeat$"):
            pub.publish(now=NOW)
    else:
        full(pub)
    assert (pub.published_at, pub.sequence) == (NOW, 1)
    assert len(send.calls) == 2 and len(events) == 1


@pytest.mark.parametrize("now", [True, float(NOW), -1, 2**63])
def test_bad_clock_fails_before_send_or_marks(now: Any) -> None:
    events: list[Any] = []
    pub, send = filled(heartbeat=Beat(events)), Send()
    pub.send = send
    with pytest.raises(ValueError, match="ts_init must be an int64 >= 0"):
        pub.publish(now=now)
    assert (pub.published_at, pub.sequence) == (None, 0)
    assert not send.calls and not events


@pytest.mark.parametrize("top_n", [0, -1])
def test_registry_refuses_nonpositive_top_n(top_n: int) -> None:
    with pytest.raises(ValueError, match="top_n: non-positive value"):
        registry(top_n=top_n)


def test_unserializable_asset_preserves_state() -> None:
    events: list[Any] = []
    pub, send = filled(heartbeat=Beat(events)), Send()
    pub.send = send
    pub.assets["ZZPBA"] = replace(ASSET, source=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="is not JSON serializable"):
        pub.publish(now=NOW)
    assert (pub.published_at, pub.sequence) == (None, 0)
    assert not send.calls and not events


def test_kept_ready_ignores_per_symbol_age() -> None:
    pub = filled()
    mover = pub.states["ZZPBA"].latest
    assert mover is not None
    obs = dict(mover.observations) | {"ts_event": START, "ts_init": START, "last_price_ts": START}
    changed(pub, known_at=START, observations=obs)
    gen = full(pub)
    row = next(r for r in gen.rows if r.symbol == "ZZPBA")
    assert gen.coverage is COVER and row.rank is not None
    assert all(f.known_at == START for f in row.features)
    assert NOW - START > pub.registry.seconds("input_max_age_s")


BUS = """
import json, sys
sys.path[:0] = sys.argv[1:3]
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import DataActor
from test_publish import filled, NOW
from sextant_movers.ranking.publisher import ranking_data_type
full, top = ranking_data_type("sip"), ranking_data_type("sip", "top")
seen, sent = {"full": [], "top": []}, []

class Full(DataActor):
    def on_start(self):
        self.subscribe_data(full)
    def on_data(self, envelope):
        seen["full"].append(envelope.data)

class Top(DataActor):
    def on_start(self):
        self.subscribe_data(top)
    def on_data(self, envelope):
        seen["top"].append(envelope.data)

class Publisher(DataActor):
    def on_start(self):
        publisher = filled(send=self.publish_data)
        sent.append(publisher.publish(now=NOW))

engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True, run_analysis=False))
try:
    for actor in (Full(), Top(), Publisher()):
        engine.add_actor(actor)
    engine.run()
    assert len(seen["full"]) == len(seen["top"]) == 1
    gen, projection = seen["full"][0], seen["top"][0]
    assert gen is sent[0] and projection is not gen
    assert projection.generation_id == gen.generation_id
    assert projection.rows == gen.rows[:2] and not projection.coverage.complete
    assert gen.coverage.complete and len(gen.rows) == 3
finally:
    engine.dispose()
print("BUS two views, one gen each, full identity and top projection")
"""


def test_both_views_on_the_isolated_engine_bus() -> None:
    here = Path(__file__).resolve()
    done = subprocess.run(
        [sys.executable, "-I", "-c", BUS, str(here.parents[2] / "src"), str(here.parent)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "BUS two views, one gen each, full identity and top projection" in done.stdout


def publish_answers(m: dict[str, Any], nt: Any) -> dict[str, Any]:
    """Seeded complete publications and both send transcripts; also executed by the child."""
    import random
    from dataclasses import replace
    from datetime import date
    from decimal import Decimal as D

    ns, day = 1_000_000_000, date(2030, 11, 29)
    start = (1_922_140_800 + 9 * 3600) * ns
    cases = (
        "ready",
        "early",
        "regressed",
        "ready",
        "state",
        "complete",
        "expected",
        "observed",
        "missing",
        "invalid",
        "late",
        "ready",
        "age",
        "ready",
        "start",
        "event_cutoff",
        "cutoff_now",
        "end",
    )
    runs = []
    for run in range(48):
        rng = random.Random(20261113 + run)  # noqa: S311 - deterministic synthetic input
        pool = ("ZZVPA", "ZZVPB", "ZZVPC")
        names = tuple(rng.sample(pool, rng.randint(1, 3)))
        feed = "sip" if run % 2 else "boats"
        segment = m["Segment"](
            "PRE" if feed == "sip" else "OVERNIGHT", day, start, start + 19_800 * ns, "zz-clock"
        )
        values = {
            "mover_min_move": "0.05",
            "mover_min_trades_accel": "2",
            "mover_min_dvol_ratio": "0.5",
            "move_1m_window_s": "60",
            "move_5m_window_s": "300",
            "trades_accel_window_s": "60",
            "trades_accel_baseline_min": "5",
            "publish_min_interval_s": "1",
            "input_max_age_s": "2",
            "top_n": str(run % 3 + 1),
        }
        registry = m["Registry"](
            tuple(
                m["RegistryRow"](n, u, k(values.get(n, "2")), "zz-source", "UNCALIBRATED", day)
                for n, k, u in m["rows"]
            )
        )
        curve = ((0, D(0), D(0)), (600, D(rng.randint(200, 900)), D(100)))
        samples = tuple(
            m["BaselineSample"](n, segment.name, "2030-11-27", off, volume, count)
            for n in names
            for off, volume, count in curve
        )
        baseline = m["BaselineTable"](samples, "zz-curve", start - ns, feed)
        sent: list[Any] = []

        def record(data_type: Any, envelope: Any, sent: list[Any] = sent) -> None:
            topic = data_type if isinstance(data_type, str) else data_type.topic
            sent.append([topic, envelope.data.to_json()])

        pub = m["make"](
            send=record,
            epoch=f"zz-publish-{run}",
            feed=feed,
            segment=segment,
            registry=registry,
            universe=names,
            universe_source="zz-universe",
            baseline=baseline,
        )
        refs = m["ReferencePrices"](D(2), start - ns, "zz-close")
        for name in names:
            asset = m["AssetState"](start - ns, segment.end_ns, False, "common", "zz-assets")
            pub.set_symbol(name, references=refs, asset=asset)
            for sec, price in ((0, "2.00"), (60, "2.10"), (300, "2.40"), (360, "2.60")):
                trade = nt.TradeTick(
                    nt.InstrumentId.from_str(name + ".SIM"),
                    nt.Price.from_str(price),
                    nt.Quantity.from_int(rng.randint(1, 20)),
                    nt.AggressorSide.BUY,
                    nt.TradeId("zz-warm"),
                    start + sec * ns,
                    start + sec * ns,
                )
                pub.on_trade(trade)
        now, steps = start + 360 * ns + 100, []
        for index, case in enumerate(cases):
            coverage = m["Coverage"](
                m["CoverageState"].READY, True, len(names), len(names), 0, 0, 0
            )
            if case in ("early", "regressed"):
                at = pub.published_at + (rng.choice((0, ns - 1)) if case == "early" else -1)
            else:
                if index:
                    now += 2 * ns
                    for name in names:
                        asset = rng.choice(
                            (
                                None,
                                m["AssetState"](
                                    start,
                                    segment.end_ns,
                                    rng.choice((False, False, None)),
                                    rng.choice(("common", "adr", "etf")),
                                    "zz-assets",
                                ),
                            )
                        )
                        pub.set_symbol(name, references=refs, asset=asset)
                        trade = nt.TradeTick(
                            nt.InstrumentId.from_str(name + ".SIM"),
                            nt.Price.from_str(rng.choice(("0.70", "2.70", "5.20"))),
                            nt.Quantity.from_int(rng.randint(1, 20)),
                            nt.AggressorSide.BUY,
                            nt.TradeId(f"zz-{index}"),
                            now - 100,
                            now - 90,
                        )
                        pub.on_trade(trade, known_at=now - 80)
                pub.last_event, pub.input_cutoff = now - 100, now - 80
                at = now
                if case == "state":
                    states = [s for s in m["CoverageState"] if s is not m["CoverageState"].READY]
                    coverage = replace(coverage, state=states[run % len(states)])
                elif case == "complete":
                    coverage = replace(coverage, complete=False)
                elif case in ("expected", "observed"):
                    coverage = replace(coverage, **{case + "_symbols": rng.choice((None, 0, 4))})
                elif case in ("missing", "invalid", "late"):
                    coverage = replace(coverage, **{case + "_symbols": rng.choice((None, 1))})
                elif case == "age":
                    pub.last_event = at - registry.seconds("input_max_age_s") - 1
                elif case == "start":
                    pub.last_event = start - 1
                elif case == "event_cutoff":
                    pub.input_cutoff = pub.last_event - 1
                elif case == "cutoff_now":
                    pub.input_cutoff = at + 1
                elif case == "end":
                    at = segment.end_ns
                    pub.last_event = pub.input_cutoff = at
                pub.set_coverage(coverage)
            before = len(sent)
            try:
                generation = pub.publish(now=at)
                outcome = None if generation is None else ["generation", generation.to_json()]
            except Exception as error:
                outcome = ["raised", type(error).__name__, str(error)]
            steps.append([case, outcome, sent[before:], pub.published_at, pub.sequence])
        runs.append(steps)
    return {"runs": runs}


def publication_side() -> dict[str, Any]:
    from test_publisher import EDGES, TYPES

    from sextant_movers.ranking import baseline, registry

    side = {
        "Segment": baseline.Segment,
        "BaselineSample": baseline.BaselineSample,
        "BaselineTable": baseline.BaselineTable,
        "Registry": registry.Registry,
        "RegistryRow": registry.RegistryRow,
        "rows": [(n, k, u) for n, (k, u) in registry.ROWS.items()],
        "ReferencePrices": engine.ReferencePrices,
        "AssetState": publisher.AssetState,
        "Coverage": Coverage,
        "CoverageState": CoverageState,
    }
    side["make"] = lambda **kw: RankingPublisher(
        method_version="zz-publish-method",
        eligible_security_types=TYPES,
        price_band_edges=EDGES,
        **kw,
    )
    return side


def publication_counts(answers: dict[str, Any]) -> dict[str, int]:
    steps = [step for run in answers["runs"] for step in run]
    generated = [s for s in steps if s[1] and s[1][0] == "generation"]
    ready = [s for s in generated if json.loads(s[1][1])["coverage"]["state"] == "READY"]
    ranked = [s for s in ready if any(r["rank"] is not None for r in json.loads(s[1][1])["rows"])]
    return {
        "runs": len(answers["runs"]),
        "calls": len(steps),
        "generations": len(generated),
        "ready_ranked": len(ranked),
        "not_ready": len(generated) - len(ready),
        "early": sum(s[1] is None for s in steps),
        "regressed": sum(s[1] is not None and s[1][0] == "raised" for s in steps),
        "sent_views": sum(len(s[2]) for s in steps),
    }


# The canonical digest pins every outcome, both view transcripts and both state marks.
PUBLISH_DIGEST = "9867e58ef4534282a83bb1569f0ab261c6e9539a76700af6bc760609b75991eb"


def test_seeded_publication_answers_are_pinned() -> None:
    import hashlib

    import nautilus_trader.model as nt

    answers = publish_answers(publication_side(), nt)
    counts = publication_counts(answers)
    assert counts["runs"] >= 40 and counts["calls"] >= 300 and counts["ready_ranked"] >= 100
    assert counts["not_ready"] > 0 and counts["early"] > 0 and counts["regressed"] > 0
    payload = json.dumps(answers, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(payload.encode()).hexdigest() == PUBLISH_DIGEST


PUBLISH_PARITY = """
import hashlib, json, os, sys
src = os.environ["MOVERS_REFERENCE_SRC"]
path = os.path.join(src, "sextant", "ranking", "publisher.py")
with open(path, "rb") as handle:
    blob = handle.read()
expected_blob = "498ac186d643a137b32f6ff84f9ca76c657fb0ee"
assert hashlib.sha1(b"blob %d\\0" % len(blob) + blob).hexdigest() == expected_blob
sys.path.insert(0, src)
import nautilus_trader
import nautilus_trader.model as nt
from sextant.ranking import baseline as bl, engine as en, generation as g, publisher as pb
from sextant.ranking import registry as rg
assert os.path.samefile(pb.__file__, path)
pb.METHOD = "zz-publish-method"

class Bus:
    def __init__(self, send):
        self.send = send
    def publish(self, topic, msg):
        self.send(topic, msg)

def make(*, send, **kw):
    return pb.RankingPublisher(bus=Bus(send), **kw)

m = {"Segment": en.Segment, "BaselineSample": bl.BaselineSample,
     "BaselineTable": bl.BaselineTable, "Registry": rg.Registry, "RegistryRow": rg.RegistryRow,
     "rows": [(n, k, u) for n, (k, u) in rg.ROWS.items()],
     "ReferencePrices": en.ReferencePrices, "AssetState": pb.AssetState,
     "Coverage": g.Coverage, "CoverageState": g.CoverageState, "make": make}
print("PARITY " + json.dumps({
    "versions": [list(sys.version_info[:3]), nautilus_trader.__version__],
    "answers": publish_answers(m, nt),
}))
"""


@pytest.mark.local
def test_publication_parity_with_the_reference_in_a_child_interpreter() -> None:
    import inspect

    import nautilus_trader
    import nautilus_trader.model as nt

    src, python = child.reference()
    script = "from typing import Any\n" + inspect.getsource(publish_answers) + PUBLISH_PARITY
    theirs = child.run(python, src, script)
    here = [list(sys.version_info[:3]), nautilus_trader.__version__]
    assert here == theirs["versions"], (
        f"version mismatch: here {here}, reference {theirs['versions']}"
    )
    ours = publish_answers(publication_side(), nt)
    print(f"\npython, engine: here {here}, reference {theirs['versions']}")
    print(f"publication parity: {publication_counts(ours)}")

    def canonical(a: Any) -> str:
        return json.dumps(a, sort_keys=True, separators=(",", ":"))

    assert canonical(ours) == canonical(theirs["answers"])
