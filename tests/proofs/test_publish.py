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
