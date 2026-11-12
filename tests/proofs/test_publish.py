"""Part 5e-2: publication and both engine views, with synthetic inputs only.

Build on the reference's `src/sextant/ranking/publisher.py:136-309` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`. The injected send uses the engine's
`DataActor.publish_data`, `CustomData` and `DataType` as shipped at `d53849b2`.
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


def populated(**kw: Any) -> RankingPublisher:
    pub = ready(registry=registry(), **kw)
    for name in NAMES:
        for second, price in ((0, "2.00"), (60, "2.10"), (300, "2.40"), (360, "2.60")):
            pub.on_trade(tick(second, price, name=name))
    pub.set_coverage(COVER)
    return pub


def full(pub: RankingPublisher, now: int = NOW) -> RankingGeneration:
    generation = pub.publish(now=now)
    assert generation is not None
    return generation


def changed(pub: RankingPublisher, name: str = "ZZPBA", **kw: Any) -> None:
    state = pub.states[name]
    assert state.latest is not None
    pub.states[name] = replace(state, latest=replace(state.latest, **kw))


def suppressed(pub: RankingPublisher, now: int = NOW) -> RankingGeneration:
    generation = full(pub, now)
    assert generation.coverage == replace(
        pub.coverage, state=CoverageState.DEGRADED, complete=False
    )
    assert all(
        r.score is r.rank is None
        and not r.features
        and not r.completed
        and r.partial
        and not r.focus_eligible
        for r in generation.rows
    )
    return generation


def test_gate_names_are_the_reference_order_as_test_input_only() -> None:
    assert engine.GATES == (
        "mover_min_move",
        "move_1m_positive",
        "mover_min_trades_accel",
        "mover_min_dvol_ratio",
    )


@pytest.mark.parametrize("state", [s for s in CoverageState if s is not CoverageState.READY])
def test_ready_requires_the_ready_state(state: CoverageState) -> None:
    pub = populated()
    pub.set_coverage(replace(COVER, state=state))
    suppressed(pub)


def test_ready_requires_complete() -> None:
    pub = populated()
    pub.set_coverage(replace(COVER, complete=False))
    suppressed(pub)


@pytest.mark.parametrize("count", [None, 2, 4])
def test_ready_requires_expected_count_equal_to_universe(count: int | None) -> None:
    pub = populated()
    pub.set_coverage(replace(COVER, expected_symbols=count))
    suppressed(pub)


@pytest.mark.parametrize("count", [None, 2, 4])
def test_ready_requires_observed_count_equal_to_universe(count: int | None) -> None:
    pub = populated()
    pub.set_coverage(replace(COVER, observed_symbols=count))
    suppressed(pub)


@pytest.mark.parametrize("count", [None, 1])
def test_ready_requires_zero_missing(count: int | None) -> None:
    pub = populated()
    pub.set_coverage(replace(COVER, missing_symbols=count))
    suppressed(pub)


@pytest.mark.parametrize("count", [None, 1])
def test_ready_requires_zero_invalid(count: int | None) -> None:
    pub = populated()
    pub.set_coverage(replace(COVER, invalid_symbols=count))
    suppressed(pub)


@pytest.mark.parametrize("count", [None, 1])
def test_ready_requires_zero_late(count: int | None) -> None:
    pub = populated()
    pub.set_coverage(replace(COVER, late_symbols=count))
    suppressed(pub)


def test_ready_requires_event_at_or_after_segment_start() -> None:
    pub = populated()
    pub.last_event = START - 1
    suppressed(pub, START)


def test_ready_requires_event_at_or_before_input_cutoff() -> None:
    pub = populated()
    pub.input_cutoff = pub.last_event - 1
    suppressed(pub)


def test_ready_requires_cutoff_at_or_before_now_and_constructor_keeps_incoherence() -> None:
    pub = populated()
    pub.input_cutoff = NOW + 1
    generation = suppressed(pub)
    assert generation.input_cutoff > generation.known_at
    assert (pub.sequence, pub.published_at) == (1, NOW)


def test_ready_requires_now_before_segment_end() -> None:
    pub = populated()
    pub.last_event = pub.input_cutoff = pub.segment.end_ns
    suppressed(pub, pub.segment.end_ns)


def test_ready_accepts_all_equal_time_links_at_segment_start() -> None:
    pub = ready()
    pub.set_coverage(COVER)
    assert full(pub, START).coverage is COVER


def test_ready_accepts_input_age_at_the_edge() -> None:
    pub = populated()
    assert full(pub, pub.last_event + pub.registry.seconds("input_max_age_s")).coverage is COVER


def test_ready_refuses_input_age_one_past_the_edge() -> None:
    pub = populated()
    suppressed(pub, pub.last_event + pub.registry.seconds("input_max_age_s") + 1)


def test_ranking_is_called_only_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Any] = []
    original = engine.rank_movers

    def rank(rows: Any) -> Any:
        rows = tuple(rows)
        called.append(rows)
        return original(rows)

    monkeypatch.setattr(publisher, "rank_movers", rank)
    pub = populated()
    full(pub)
    assert called == [tuple(s.latest for s in pub.states.values())]
    pub.set_coverage(replace(COVER, complete=False))
    suppressed(pub, NOW + NS)
    assert len(called) == 1


@pytest.mark.parametrize("failed", [None, *engine.GATES])
@pytest.mark.parametrize("asset_kind", ["unknown", "ineligible", "eligible"])
def test_each_row_reason_in_order_and_each_focus_link(failed: str | None, asset_kind: str) -> None:
    pub = populated()
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
    pub = populated()
    changed(pub, gates_passed=())
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    assert tuple(r.detail for r in row.reasons if r.code == "gate_failed") == engine.GATES


def test_each_feature_value_and_its_two_times_including_last_price_time() -> None:
    pub = populated()
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
    pub = populated(price_band_edges=edges)
    mover = pub.states["ZZPBA"].latest
    assert mover is not None
    low, high = edges
    prices = {"below": D(low) - D("0.01"), "low": D(low), "high": D(high), "unknown": None}
    changed(pub, observations=dict(mover.observations) | {"price": prices[which]})
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    labels = json.loads(next(r.detail for r in row.reasons if r.code == "LABELS") or "null")
    expected = {"below": f"<{low}", "low": f"{low}-{high}", "high": f">={high}", "unknown": None}
    assert labels["price_band"] == expected[which]


def test_kept_never_traded_name_in_ready_generation_has_no_score_or_focus() -> None:
    pub = populated()
    pub.states["ZZPBA"] = engine.MoverState()
    row = next(r for r in full(pub).rows if r.symbol == "ZZPBA")
    assert row.score is row.rank is None and row.features == ()
    assert row.completed and not row.partial and not row.focus_eligible
    assert [r.code for r in row.reasons] == ["SOURCE", "ASSET"]


def test_generation_identity_versions_times_and_status_over_three_publications() -> None:
    pub = populated(method_version="zz-caller-method")
    for sequence in range(3):
        now = NOW + sequence * NS
        generation = full(pub, now)
        assert generation.generation_id == GenerationId(
            "zz-epoch", sequence, sequence - 1 if sequence else None
        )
        assert generation.session_date == pub.segment.session_date
        assert (generation.ts_event, generation.input_cutoff) == (pub.last_event, pub.input_cutoff)
        assert (generation.known_at, generation.ts_init) == (now, now)
        assert generation.method_version == "zz-caller-method"
        assert generation.profile_version == "registry/" + pub.registry.params_hash
        assert generation.ranker_version == RANKER
        assert generation.status is GenerationStatus.PROVISIONAL
        assert (pub.published_at, pub.sequence) == (now, sequence + 1)


def test_kept_top_projects_generation_order_which_constructor_sorts_by_rank() -> None:
    send, pub = Send(), populated()
    pub.send = send
    for name, score in zip(sorted(NAMES), (D(1), D(3), D(2)), strict=True):
        changed(pub, name, score=score)
    generation = full(pub)
    assert [r.symbol for r in generation.rows] == ["ZZPBB", "ZZPBC", "ZZPBA"]
    assert [r.rank for r in generation.rows] == [1, 2, 3]
    assert len(send.calls) == 2
    for (dt, envelope), view in zip(send.calls, ("full", "top"), strict=True):
        assert dt == envelope.data_type == ranking_data_type(pub.feed, view)
        assert dt.metadata == {"feed": pub.feed, "view": view, "ranker": RANKER}
    assert send.calls[0][1].data is generation
    top = send.calls[1][1].data
    assert top == replace(
        generation,
        rows=generation.rows[:2],
        coverage=replace(COVER, complete=False, reasons=(RankingReason("TOP_N_PROJECTION"),)),
    )
    assert (
        top.coverage.state is CoverageState.READY and top.generation_id == generation.generation_id
    )


def test_kept_not_ready_projection_is_empty_and_retains_degraded_counts() -> None:
    pub, send = populated(), Send()
    pub.send = send
    pub.set_coverage(replace(COVER, missing_symbols=1))
    suppressed(pub)
    top = send.calls[1][1].data
    assert top.rows == ()
    assert top.coverage == replace(
        pub.coverage,
        state=CoverageState.DEGRADED,
        complete=False,
        reasons=(RankingReason("TOP_N_PROJECTION"),),
    )


def test_kept_equal_clock_is_not_regression_and_early_call_sends_nothing() -> None:
    pub, send = populated(), Send()
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

    def emit(self, generation: RankingGeneration) -> bool:
        self.events.append(("beat", generation))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_heartbeat_runs_once_after_both_sends_with_full_generation_and_updated_marks() -> None:
    events: list[Any] = []
    pub = populated(heartbeat=Beat(events))

    def send(dt: Any, data: Any) -> None:
        assert pub.published_at is None and pub.sequence == 0
        events.append((dt.metadata["view"], data.data))

    pub.send = send
    generation = full(pub)
    assert [event[0] for event in events] == ["full", "top", "beat"]
    assert events[0][1] is events[2][1] is generation
    assert pub.publish(now=NOW) is None and len(events) == 3


@pytest.mark.parametrize("fail_on", [1, 2])
def test_kept_send_failure_preserves_marks_and_heartbeat_but_full_may_have_sent(
    fail_on: int,
) -> None:
    events: list[Any] = []
    pub = populated(heartbeat=Beat(events))
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
    pub, send = populated(heartbeat=Beat(events, result)), Send()
    pub.send = send
    if isinstance(result, Exception):
        with pytest.raises(RuntimeError, match=r"^zz-heartbeat$"):
            pub.publish(now=NOW)
    else:
        full(pub)
    assert (pub.published_at, pub.sequence) == (NOW, 1)
    assert len(send.calls) == 2 and len(events) == 1


@pytest.mark.parametrize("now", [True, float(NOW), -1, 2**63])
def test_generation_refuses_non_int64_publication_clock_before_any_send(now: Any) -> None:
    events: list[Any] = []
    pub, send = populated(heartbeat=Beat(events)), Send()
    pub.send = send
    with pytest.raises(ValueError, match="ts_init must be an int64 >= 0"):
        pub.publish(now=now)
    assert (pub.published_at, pub.sequence) == (None, 0)
    assert not send.calls and not events


@pytest.mark.parametrize("top_n", [0, -1])
def test_nonpositive_top_n_is_refused_by_registry_before_publication(top_n: int) -> None:
    with pytest.raises(ValueError, match="top_n: non-positive value"):
        registry(top_n=top_n)


def test_unserializable_asset_raises_without_sends_or_state_changes() -> None:
    events: list[Any] = []
    pub, send = populated(heartbeat=Beat(events)), Send()
    pub.send = send
    pub.assets["ZZPBA"] = replace(ASSET, source=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="is not JSON serializable"):
        pub.publish(now=NOW)
    assert (pub.published_at, pub.sequence) == (None, 0)
    assert not send.calls and not events


def test_kept_ready_rule_does_not_check_each_states_latest_age() -> None:
    pub = populated()
    mover = pub.states["ZZPBA"].latest
    assert mover is not None
    obs = dict(mover.observations) | {"ts_event": START, "ts_init": START, "last_price_ts": START}
    changed(pub, known_at=START, observations=obs)
    generation = full(pub)
    row = next(r for r in generation.rows if r.symbol == "ZZPBA")
    assert generation.coverage is COVER and row.rank is not None
    assert all(f.known_at == START for f in row.features)
    assert NOW - START > pub.registry.seconds("input_max_age_s")


BUS = """
import json, sys
sys.path[:0] = sys.argv[1:3]
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import DataActor
from test_publish import populated, NOW
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
        publisher = populated(send=self.publish_data)
        sent.append(publisher.publish(now=NOW))

engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True, run_analysis=False))
try:
    for actor in (Full(), Top(), Publisher()):
        engine.add_actor(actor)
    engine.run()
    assert len(seen["full"]) == len(seen["top"]) == 1
    generation, projection = seen["full"][0], seen["top"][0]
    assert generation is sent[0] and projection is not generation
    assert projection.generation_id == generation.generation_id
    assert projection.rows == generation.rows[:2] and not projection.coverage.complete
    assert generation.coverage.complete and len(generation.rows) == 3
finally:
    engine.dispose()
print("BUS two views, one generation each, full identity and top projection")
"""


def test_publisher_reaches_both_own_view_subscribers_in_an_isolated_engine() -> None:
    here = Path(__file__).resolve()
    done = subprocess.run(
        [sys.executable, "-I", "-c", BUS, str(here.parents[2] / "src"), str(here.parent)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "BUS two views, one generation each, full identity and top projection" in done.stdout
