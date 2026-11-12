"""Part 6: reader proofs on synthetic publisher output, no fixture file or network.

Build on the reference's `src/sextant/ranking/reader.py:20-270` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; only the local child imports the reference.
"""

import ast
import hashlib
import inspect
import json
import logging
import random
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import reference_child as child
from nautilus_trader.model import CustomData, DataType
from test_publish import NOW, filled, full
from test_publisher import NAMES, NS, TYPES, registry, tick

from sextant_movers.ranking import reader as module
from sextant_movers.ranking.baseline import Segment
from sextant_movers.ranking.generation import (
    CoverageState,
    GenerationId,
    RankingGeneration,
    generation_digest,
)
from sextant_movers.ranking.publisher import ranking_data_type, universe_digest
from sextant_movers.ranking.reader import GenerationReader
from sextant_movers.ranking.registry import Registry, RegistryRow

# Lower fixture thresholds give each row a qualifying focus rank; no authority is shipped.
REG = registry(mover_min_trades_accel=D("0.01"), mover_min_dvol_ratio=D("0.01"))
PUB = filled(reg=REG)
GEN = full(PUB)
SEG = PUB.segment
METHOD = GEN.method_version
TOPIC = ranking_data_type(PUB.feed)
SYMBOL = GEN.rows[0].symbol


def make(**over: Any) -> GenerationReader:
    kw: dict[str, Any] = dict(
        registry=REG,
        feed=PUB.feed,
        segment=SEG,
        universe_hash=PUB.universe_hash,
        baseline_hash=PUB.baseline.identity,
        method_version=METHOD,
        eligible_security_types=TYPES,
        alarm=lambda _: None,
    )
    return GenerationReader(**(kw | over))


def envelope(gen: Any = GEN, topic: DataType = TOPIC) -> CustomData:
    return CustomData(topic, gen)


def accepted(**over: Any) -> GenerationReader:
    r = make(**over)
    assert r.accept_generation(envelope(), now=NOW)
    assert r.accept_heartbeat(at=NOW, received_ns=NOW)
    return r


def row_change(g: RankingGeneration, **kw: Any) -> RankingGeneration:
    rows = list(g.rows)
    rows[0] = replace(rows[0], **kw)
    return replace(g, rows=tuple(rows))


def feature_change(g: RankingGeneration, name: str = "price", **kw: Any) -> RankingGeneration:
    return row_change(
        g, features=tuple(replace(f, **kw) if f.name == name else f for f in g.rows[0].features)
    )


def reason_change(g: RankingGeneration, code: str, detail: Any) -> RankingGeneration:
    return row_change(
        g,
        reasons=tuple(
            replace(r, detail=detail) if r.code == code else r for r in g.rows[0].reasons
        ),
    )


def source_change(g: RankingGeneration, **kw: Any) -> RankingGeneration:
    detail = next(r.detail for r in g.rows[0].reasons if r.code == "SOURCE")
    return reason_change(g, "SOURCE", json.dumps(json.loads(detail or "null") | kw))


def lineage(g: RankingGeneration, sequence: int, epoch: str | None = None) -> RankingGeneration:
    return replace(
        g,
        generation_id=GenerationId(
            epoch or g.generation_id.producer_epoch, sequence, sequence - 1 if sequence else None
        ),
    )


IDENTITY = "generation identity mismatch"
INCOMPLETE = "generation incomplete"
CLOCKS = "generation session/future clocks"
AGE = "generation/input stale"
SOURCE = "source/feed/session mismatch"
FEATURE = "feature future or outside session"
SCORE = "score/feature mismatch"
FOCUS = "entry qualification incomplete"
SEQUENCE = "generation sequence discontinuity"
EPOCH = "producer epoch changed; require coherent generation and new heartbeat"


def generation_cases(g: RankingGeneration = GEN) -> dict[str, tuple[Any, int, DataType, str]]:
    """Each altered publisher generation reaches one check, without an earlier refusal."""
    cases: dict[str, tuple[Any, int, DataType, str]] = {}

    def add(name: str, gen: Any, reason: str, at: int = NOW, topic: DataType = TOPIC) -> None:
        cases[name] = gen, at, topic, reason

    add("topic_feed", g, "wrong generation topic", topic=ranking_data_type("zz-other"))
    add("topic_projection", g, "wrong generation topic", topic=ranking_data_type(PUB.feed, "top"))
    for key in ("ranker_version", "method_version", "profile_version"):
        add(key, replace(g, **{key: "zz-other"}), IDENTITY)
    for key, value in (
        ("state", CoverageState.DEGRADED),
        ("complete", False),
        ("expected_symbols", len(g.rows) + 1),
        ("observed_symbols", len(g.rows) + 1),
        ("missing_symbols", 1),
        ("invalid_symbols", 1),
        ("late_symbols", 1),
    ):
        add(key, replace(g, coverage=replace(g.coverage, **{key: value})), INCOMPLETE)
    clocks = {
        "start_event": {"ts_event": SEG.start_ns - 1},
        "event_cutoff": {"input_cutoff": g.ts_event - 1},
        "cutoff_known": {"input_cutoff": g.known_at + 1},
        "known_init": {"known_at": g.ts_init + 1},
        "init_now": {"ts_init": NOW + 1},
        "session_date": {"session_date": SEG.session_date + timedelta(days=1)},
    }
    for name, kw in clocks.items():
        add(name, replace(g, **kw), CLOCKS)
    add("now_end", g, CLOCKS, SEG.end_ns)
    add("generation_age", g, AGE, g.known_at + REG.seconds("generation_max_age_s") + 1)
    add(
        "input_age",
        replace(g, known_at=NOW + NS, ts_init=NOW + NS),
        AGE,
        g.ts_event + REG.seconds("input_max_age_s") + 1,
    )
    add("universe", row_change(g, symbol="ZZOTHER"), "universe identity mismatch")
    reasons = g.rows[0].reasons
    add(
        "source_absent",
        row_change(g, reasons=tuple(r for r in reasons if r.code != "SOURCE")),
        "source missing",
    )
    add("source_repeated", row_change(g, reasons=(*reasons, reasons[0])), "source missing")
    add("source_null", reason_change(g, "SOURCE", None), "source missing")
    for key, value in (
        ("feed", "zz-other"),
        ("segment", "zz-other"),
        ("start_ns", 0),
        ("end_ns", 1),
        ("session_source", "zz-other"),
        ("params_hash", "zz-other"),
        ("universe_hash", "zz-other"),
        ("baseline_hash", "zz-other"),
        ("universe_source", ""),
        ("baseline_source", ""),
    ):
        add("source_" + key, source_change(g, **{key: value}), SOURCE)
    add(
        "mixed_sources",
        source_change(g, universe_source="zz-another-source"),
        "mixed generation sources",
    )
    for name, kw in {
        "valued_no_known": {"known_at": None},
        "valued_no_event": {"ts_event": None},
        "null_known_no_event": {"value": None, "ts_event": None},
        "feature_start": {"ts_event": SEG.start_ns - 1},
        "feature_event_known": {"known_at": g.ts_event - 1},
        "feature_known_generation": {"known_at": g.known_at + 1},
    }.items():
        add(name, feature_change(g, **kw), FEATURE)
    for name in ("move", "dvol_ratio"):
        add("score_missing_" + name, feature_change(g, name, value=None), SCORE)
    add("score_product", row_change(g, score=D("999")), SCORE)
    add("rank_without_score", row_change(g, score=None), "rank without score")
    for key, value in (("partial", True), ("completed", False), ("rank", None)):
        add("focus_" + key, row_change(g, **{key: value}), FOCUS)
    for name in ("price", "move_1m", "move", "dvol_ratio", "trades_accel"):
        for suffix, value in (("missing", None), ("below", D(0))):
            base, reason = g, FOCUS
            if name in {"move", "dvol_ratio"}:
                # Missing score inputs are refused before qualification can inspect them.
                # A zero product keeps the below-threshold cases independently reachable.
                base, reason = (g, SCORE) if value is None else (row_change(g, score=D(0)), FOCUS)
            add("focus_" + name + "_" + suffix, feature_change(base, name, value=value), reason)
    add("rank_order", row_change(g, rank=2), "rank order mismatch")
    return cases


CASES = generation_cases()


@pytest.mark.parametrize("key", ["method_version", "eligible_security_types"])
def test_new_keywords_are_required(key: str) -> None:
    kw: dict[str, Any] = dict(
        feed=PUB.feed,
        segment=SEG,
        universe_hash="zz",
        baseline_hash="zz",
        method_version=METHOD,
        eligible_security_types=TYPES,
    )
    kw.pop(key)
    with pytest.raises(TypeError, match=key):
        GenerationReader(REG, **kw)


@pytest.mark.parametrize(
    "key,value,message",
    [
        ("method_version", "", "explicit method version required"),
        ("method_version", 7, "explicit method version required"),
        ("eligible_security_types", frozenset(), "eligible security types required"),
        ("eligible_security_types", set(TYPES), "eligible security types required"),
        ("eligible_security_types", None, "eligible security types required"),
    ],
)
def test_new_constructor_refusals(key: str, value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        make(**{key: value})


def test_constructor_fields_and_default_logger() -> None:
    r = make(alarm=None)
    assert r.registry is REG and r.segment is SEG and r.feed == PUB.feed
    assert (r.universe_hash, r.baseline_hash) == (PUB.universe_hash, PUB.baseline.identity)
    assert (r.method_version, r.eligible_security_types) == (METHOD, TYPES)
    assert r.generation is r.generation_key is r._heartbeat is r._epoch is r._sequence is None
    assert r.reason == "generation_missing" and r._retired_epochs == set()
    assert r._last_alarm is None and r._alarm == logging.getLogger(module.__name__).warning


BEATS = {
    "at_bool": (True, NOW),
    "at_float": (float(NOW), NOW),
    "at_none": (None, NOW),
    "receipt_bool": (NOW, True),
    "receipt_float": (NOW, float(NOW)),
    "receipt_none": (NOW, None),
    "negative": (-1, NOW),
    "future": (NOW + 1, NOW),
    "age_past": (NOW, NOW + REG.seconds("heartbeat_max_age_s") + 1),
    "regression": (NOW - 1, NOW),
}


@pytest.mark.parametrize("case", BEATS)
def test_each_heartbeat_refusal_clears_the_held_beat(case: str) -> None:
    r = accepted()
    at, receipt = BEATS[case]
    assert not r.accept_heartbeat(at=at, received_ns=receipt)
    assert r._heartbeat is None and r.reason == "heartbeat_invalid"
    assert r.generation is GEN  # heartbeat refusal alone does not invalidate the payload


def test_heartbeat_edge_equal_at_and_zero() -> None:
    r = make()
    assert r.accept_heartbeat(at=0, received_ns=0)
    edge = REG.seconds("heartbeat_max_age_s")
    assert r.accept_heartbeat(at=NOW, received_ns=NOW + edge)
    assert r.accept_heartbeat(at=NOW, received_ns=NOW + 1)
    assert r._heartbeat == (NOW, NOW + 1)  # equal at is accepted, receipt may regress


def test_fresh_refusal_precedence_and_alarm_once_per_changed_reason() -> None:
    alarms: list[str] = []
    r = accepted(alarm=alarms.append)
    # Every later defect is also present until the earlier one is repaired.
    r._heartbeat = None
    r.generation = replace(GEN, known_at=NOW - 3 * NS, ts_event=NOW - 4 * NS)
    r.segment = replace(SEG, end_ns=NOW)
    assert not r.fresh(NOW) and r.reason == "heartbeat_missing"
    assert not r.fresh(NOW)
    r._heartbeat = (NOW - 3 * NS, NOW - 3 * NS)
    assert not r.fresh(NOW) and r.reason == "heartbeat_stale"
    r._heartbeat = (NOW, NOW)
    saved, r.generation = r.generation, None
    r.reason = "zz-invalidated"
    assert not r.fresh(NOW) and r.reason == "zz-invalidated"
    r.generation = saved
    assert not r.fresh(NOW) and r.reason == "session_mismatch"
    r.segment = SEG
    assert not r.fresh(NOW) and r.reason == "generation_stale"
    r.generation = replace(GEN, ts_event=NOW - 4 * NS)
    assert not r.fresh(NOW) and r.reason == "input_stale"
    r.generation = GEN
    assert r.fresh(NOW) and r.reason == "fresh"
    r._heartbeat = None
    assert not r.fresh(NOW)
    assert alarms == [
        "heartbeat_missing",
        "heartbeat_stale",
        "zz-invalidated",
        "session_mismatch",
        "generation_stale",
        "input_stale",
        "heartbeat_missing",
    ]


@pytest.mark.parametrize("which", ["heartbeat_event", "heartbeat_receipt", "generation", "input"])
@pytest.mark.parametrize("delta,ok", [(-1, False), (0, True), (2 * NS, True), (2 * NS + 1, False)])
def test_fresh_each_clock_at_both_edges(which: str, delta: int, ok: bool) -> None:
    r = accepted()
    r._heartbeat = (NOW, NOW)
    if which.startswith("heartbeat"):
        # Isolate each member even though ingress normally enforces their order and age.
        r._heartbeat = (NOW - delta, NOW) if which.endswith("event") else (NOW, NOW - delta)
        reason = "heartbeat_stale"
    else:
        key = "known_at" if which == "generation" else "ts_event"
        r.generation = replace(GEN, **{key: NOW - delta})
        reason = which + "_stale"
    assert r.fresh(NOW) is ok
    assert r.reason == ("fresh" if ok else reason)


@pytest.mark.parametrize(
    "at,ok",
    [(SEG.start_ns - 1, False), (SEG.start_ns, True), (SEG.end_ns - 1, True), (SEG.end_ns, False)],
)
def test_fresh_session_bounds(at: int, ok: bool) -> None:
    r = accepted()
    r._heartbeat = (at, at)
    r.generation = replace(GEN, ts_event=at, known_at=at)
    assert r.fresh(at) is ok
    assert r.reason == ("fresh" if ok else "session_mismatch")


@pytest.mark.parametrize("case", CASES)
def test_each_generation_check_alone(case: str) -> None:
    gen, now, topic, reason = CASES[case]
    r = make()
    assert not r.accept_generation(envelope(gen, topic), now=now)
    assert r.reason == reason
    assert r.generation is r.generation_key is None


@pytest.mark.parametrize("bad", [None, "zz", 7])
def test_wrong_envelope_type(bad: Any) -> None:
    r = make()
    assert not r.accept_generation(bad, now=NOW) and r.reason == "wrong generation topic"


def test_accepted_generation_digest_and_reason() -> None:
    assert all(row.focus_eligible for row in GEN.rows)
    r = make()
    assert r.accept_generation(envelope(), now=NOW)
    assert r.generation is GEN and r.generation_key == generation_digest(GEN)
    assert r.reason == "accepted"


@pytest.mark.parametrize("clock", ["generation", "input"])
@pytest.mark.parametrize("past", [0, 1])
def test_accept_generation_each_age_edge(clock: str, past: int) -> None:
    r = make()
    if clock == "generation":
        at = NOW + REG.seconds("generation_max_age_s") + past
        # Increase only the input limit to isolate the generation age.
        r.registry = registry(
            mover_min_trades_accel=D("0.01"), mover_min_dvol_ratio=D("0.01"), input_max_age_s=10
        )
    else:
        at = GEN.ts_event + REG.seconds("input_max_age_s") + past
    gen = replace(GEN, profile_version="registry/" + r.registry.params_hash)
    gen = source_change(gen, params_hash=r.registry.params_hash)
    # The SOURCE change must apply identically to every row.
    source = next(x for x in gen.rows[0].reasons if x.code == "SOURCE")
    gen = replace(
        gen,
        rows=tuple(
            replace(row, reasons=tuple(source if x.code == "SOURCE" else x for x in row.reasons))
            for row in gen.rows
        ),
    )
    if clock == "input":
        gen = replace(gen, known_at=at, ts_init=at)
    assert r.accept_generation(envelope(gen), now=at) is (past == 0)
    assert r.reason == ("accepted" if not past else AGE)


def test_no_fallback_after_invalid_generation() -> None:
    r = accepted()
    assert not r.accept_generation(
        envelope(lineage(replace(GEN, method_version="zz-bad"), 1)), now=NOW
    )
    assert r.generation is r.generation_key is None
    assert not r.fresh(NOW) and r.reason == IDENTITY


def test_epoch_change_retires_old_epoch_and_clears_heartbeat() -> None:
    r = accepted()
    other = lineage(GEN, 0, "zz-restarted")
    assert not r.accept_generation(envelope(other), now=NOW) and r.reason == EPOCH
    assert r._retired_epochs == {GEN.generation_id.producer_epoch}
    assert r._heartbeat is r.generation is r.generation_key is None
    assert r.accept_generation(envelope(lineage(other, 1)), now=NOW)
    assert not r.fresh(NOW) and r.reason == "heartbeat_missing"
    assert r.accept_heartbeat(at=NOW, received_ns=NOW) and r.fresh(NOW)
    assert not r.accept_generation(envelope(lineage(GEN, 2)), now=NOW)
    assert r.reason == "retired producer epoch replay"


@pytest.mark.parametrize("sequence,previous", [(0, None), (3, 2), (2, 0), (1, None)])
def test_sequence_replay_gap_and_predecessor_move_the_high_water_mark(
    sequence: int,
    previous: int | None,
) -> None:
    r = accepted()
    bad = replace(GEN, generation_id=GenerationId(r._epoch or "zz", sequence, previous))
    assert not r.accept_generation(envelope(bad), now=NOW) and r.reason == SEQUENCE
    assert r._sequence == max(0, sequence) and r.generation is r.generation_key is None
    assert r.accept_generation(envelope(lineage(GEN, max(0, sequence) + 1)), now=NOW)


def test_first_sequence_need_not_start_at_zero_and_invalidate_keeps_lineage_and_beat() -> None:
    r = make()
    assert r.accept_generation(envelope(lineage(GEN, 7)), now=NOW)
    assert r.accept_heartbeat(at=NOW, received_ns=NOW)
    r.invalidate("zz-invalid")
    assert r.generation is r.generation_key is None and r.reason == "zz-invalid"
    assert r._sequence == 7 and r._heartbeat == (NOW, NOW)
    assert r.accept_generation(envelope(lineage(GEN, 8)), now=NOW)


def asset_change(**kw: Any) -> RankingGeneration:
    text = next(r.detail for r in GEN.rows[0].reasons if r.code == "ASSET")
    return reason_change(GEN, "ASSET", json.dumps(json.loads(text or "null") | kw))


ASSETS = {
    "source": {"source": ""},
    "halted": {"halted": True},
    "unknown": {"halted": None},
    "zero_not_false": {"halted": 0},
    "type": {"security_type": "zz-other"},
    "known_negative": {"known_at": -1},
    "known_future": {"known_at": NOW + 1},
    "expiry": {"expires_at": NOW},
    "unhashable_type": {"security_type": []},
}


@pytest.mark.parametrize("case", ASSETS)
def test_entry_each_asset_link_alone(case: str) -> None:
    r = accepted()
    r.generation = asset_change(**ASSETS[case])
    assert r.entry_rank(SYMBOL, now=NOW) is None and r.reason == "fresh"


@pytest.mark.parametrize("detail", [None, "{", "{}"])
def test_entry_malformed_asset_returns_none(detail: Any) -> None:
    r = accepted()
    r.generation = reason_change(GEN, "ASSET", detail)
    assert r.entry_rank(SYMBOL, now=NOW) is None


@pytest.mark.parametrize("detail", ["null", "0", '"zz"', "[]", "true"])
def test_entry_non_object_asset_returns_none(detail: str) -> None:
    r = accepted()
    r.generation = reason_change(GEN, "ASSET", detail)
    assert r.entry_rank(SYMBOL, now=NOW) is None and r.reason == "fresh"


@pytest.mark.parametrize("count", [0, 2])
def test_entry_requires_exactly_one_asset_reason(count: int) -> None:
    r = accepted()
    asset = next(x for x in GEN.rows[0].reasons if x.code == "ASSET")
    others = tuple(x for x in GEN.rows[0].reasons if x.code != "ASSET")
    r.generation = row_change(GEN, reasons=others + (asset,) * count)
    assert r.entry_rank(SYMBOL, now=NOW) is None


@pytest.mark.parametrize(
    "key,value",
    [("partial", True), ("completed", False), ("focus_eligible", False), ("focus_eligible", None)],
)
def test_entry_each_row_link_alone(key: str, value: Any) -> None:
    r = accepted()
    r.generation = row_change(GEN, **{key: value})
    assert r.entry_rank(SYMBOL, now=NOW) is None


def test_entry_requires_fresh_generation_and_matching_symbol_and_supplied_types() -> None:
    r = accepted(eligible_security_types=frozenset({"zz-other"}))
    assert r.entry_rank(SYMBOL, now=NOW) is None
    r.eligible_security_types = TYPES
    assert r.entry_rank("ZZABSENT", now=NOW) is None
    assert r.entry_rank(SYMBOL, now=NOW) == GEN.rows[0].rank
    r._heartbeat = None
    assert r.entry_rank(SYMBOL, now=NOW) is None


@pytest.mark.parametrize("name", ["price", "move", "dvol_ratio", "trades_accel"])
@pytest.mark.parametrize("delta", [None, -1, 2 * NS, 2 * NS + 1])
def test_entry_each_feature_clock(name: str, delta: int | None) -> None:
    r = accepted()
    at = None if delta is None else NOW - delta
    r.generation = feature_change(GEN, name, ts_event=at)
    ok = delta == 2 * NS
    assert r.entry_rank(SYMBOL, now=NOW) == (GEN.rows[0].rank if ok else None)
    assert r.reason == ("fresh" if ok else "symbol_input_stale")


def test_entry_cannot_return_rank_past_generation_age_even_with_fresh_features() -> None:
    r = accepted()
    at = NOW + REG.seconds("generation_max_age_s") + 1
    r._heartbeat = (at, at)
    r.generation = row_change(
        GEN, features=tuple(replace(f, ts_event=at, known_at=at) for f in GEN.rows[0].features)
    )
    assert r.entry_rank(SYMBOL, now=at) is None and r.reason == "generation_stale"


class Copy:
    ts_event, ts_init = GEN.ts_event, GEN.ts_init

    def to_json(self) -> str:
        return GEN.to_json()


def test_foreign_class_with_to_json_is_rebuilt() -> None:
    r = make()
    assert r.accept_generation(envelope(Copy()), now=NOW)
    assert r.generation == GEN and r.generation is not GEN
    assert r.generation_key == generation_digest(GEN)


@pytest.mark.parametrize(
    "error", [ValueError, KeyError, TypeError, AttributeError, ArithmeticError]
)
def test_generation_catches_each_exception_class(error: type[Exception]) -> None:
    class Broken(Copy):
        def to_json(self) -> str:
            raise error("zz-broken")

    r = accepted()
    assert not r.accept_generation(envelope(Broken()), now=NOW)
    assert r.reason == str(error("zz-broken")) and r.generation is r.generation_key is None


@pytest.mark.parametrize(
    "now,ok",
    [
        (True, False),
        (False, False),
        (float(NOW), False),
        (float(NOW + NS), True),
    ],
)
def test_now_bool_and_float_are_not_type_checked(now: Any, ok: bool) -> None:
    r = make()
    assert r.accept_generation(envelope(), now=now) is ok
    if ok:
        assert r.accept_heartbeat(at=NOW, received_ns=NOW)
        assert r.fresh(now) and r.entry_rank(SYMBOL, now=now) == 1
    else:
        assert r.reason == CLOCKS


def test_zero_rows_are_accepted_for_the_empty_universe() -> None:
    r = make(universe_hash=universe_digest(()))
    g = replace(
        GEN, rows=(), coverage=replace(GEN.coverage, expected_symbols=0, observed_symbols=0)
    )
    assert r.accept_generation(envelope(g), now=NOW)
    assert r.accept_heartbeat(at=NOW, received_ns=NOW) and r.fresh(NOW)


def test_duplicate_symbol_is_refused_before_reader() -> None:
    with pytest.raises(ValueError, match="duplicate symbol identity"):
        replace(GEN, rows=(GEN.rows[0], GEN.rows[0]))


def test_alarm_raise_propagates_and_is_retried_before_last_alarm_is_set() -> None:
    alarms: list[str] = []

    def alarm(reason: str) -> None:
        alarms.append(reason)
        raise RuntimeError("zz-alarm")

    r = accepted(alarm=alarm)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="zz-alarm"):
            r.invalidate("zz-invalid")
    assert alarms == ["zz-invalid", "zz-invalid"]
    assert r.generation is r.generation_key is r._last_alarm is None
    assert r.reason == "zz-invalid"


def test_source_boundaries_and_no_fixed_method_or_security_types() -> None:
    tree = ast.parse(Path(module.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(
            node
        ):
            node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    assert all(x not in code for x in ("METHOD", "socket", "thread", "environ", "getenv"))
    constants = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert not constants & {"common", "adr", METHOD}
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert all(n != "sextant" and not n.startswith("sextant.") for n in imports)
    assert all(not any(x in n for x in ("trading", "execution", "adapter")) for n in imports)


@cache
def seeded_inputs() -> dict[str, Any]:
    """Publisher output and altered copies; the child's reader consumes the same JSON."""
    runs = []
    for seed in range(40):
        rng = random.Random(20261114 + seed)  # noqa: S311 - reproducible synthetic input
        pub = filled(reg=REG, epoch=f"zz-reader-{seed}")
        gen = full(pub)
        steps: list[Any] = []

        def add(call: str, *args: Any, steps: list[Any] = steps) -> None:
            steps.append([call, *args])

        def reset(**kw: Any) -> None:
            add("reset", kw)

        def beat(at: int = NOW, receipt: Any = None) -> None:
            add("accept_heartbeat", at, at if receipt is None else receipt)

        def send(
            g: RankingGeneration = gen, at: Any = NOW, dt: DataType = TOPIC, kind: str = "native"
        ) -> None:
            add("accept_generation", g.to_json(), at, dt.metadata, kind)

        def usable(g: RankingGeneration = gen, at: int = NOW, **kw: Any) -> None:
            reset(**kw)
            beat(at)
            send(g, at)

        # Every independent generation refusal, shuffled deterministically per run.
        cases = list(generation_cases(gen).items())
        rng.shuffle(cases)
        for _label, (g, at, dt, _) in cases:
            reset()
            send(g, at, dt)
            add("fresh", at)
            add("entry_rank", SYMBOL, at)
        for at, received in BEATS.values():
            reset()
            beat()
            add("accept_heartbeat", at, received)
            add("fresh", NOW)
        reset()
        add("fresh", NOW)  # missing heartbeat precedes missing generation
        beat()
        add("fresh", NOW)  # generation_missing
        add("accept_heartbeat", NOW, NOW + REG.seconds("heartbeat_max_age_s"))
        add("fresh", NOW + REG.seconds("heartbeat_max_age_s") + 1)
        usable()
        add("accept_heartbeat", NOW + 3 * NS, NOW + 3 * NS)
        add("fresh", NOW + 3 * NS)  # generation_stale, heartbeat is fresh
        later = replace(gen, known_at=NOW + NS, ts_init=NOW + NS)
        usable(later, NOW + NS)
        at = gen.ts_event + REG.seconds("input_max_age_s") + 1
        beat(at)
        add("fresh", at)  # input_stale while generation is fresh
        end = SEG.end_ns - 1
        late = replace(gen, ts_event=end, input_cutoff=end, known_at=end, ts_init=end)
        usable(late, end)
        beat(end + 1)
        add("fresh", end + 1)  # session_mismatch before either payload age

        # Accept successive, independently published generations, with real publisher lineage.
        reset()
        for index in range(4):
            at = NOW + index * NS
            if index:
                for name in NAMES:
                    price = rng.choice(("2.70", "2.80", "2.90"))
                    pub.on_trade(tick(360 + index, price, name=name))
                gen_at = full(pub, at)
            else:
                gen_at = gen
            beat(at)
            send(gen_at, at)
            add("fresh", at)
            add("entry_rank", rng.choice(sorted(NAMES)), at)
        # Original-good removal, alarm deduplication, sequence gaps and epoch retirement.
        usable()
        add("invalidate", "zz-invalidated")
        add("invalidate", "zz-invalidated")
        add("fresh", NOW)
        send(lineage(gen, 1))
        send(lineage(gen, 4))
        send(lineage(gen, 5))
        send(gen)
        restarted = lineage(gen, 7, f"zz-restart-{seed}")
        send(restarted)
        send(lineage(restarted, 8))
        add("fresh", NOW)
        beat()
        add("fresh", NOW)
        send(lineage(gen, 9))
        for sequence, previous in ((0, None), (2, 0), (1, None)):
            usable()
            send(replace(gen, generation_id=GenerationId(pub.epoch, sequence, previous)))
        # Topic/type, rebuild and caught-exception paths (no reference import in this process).
        for kind in (
            "none",
            "foreign",
            "ValueError",
            "KeyError",
            "TypeError",
            "AttributeError",
            "ArithmeticError",
        ):
            reset()
            send(kind=kind)
        for detail in ("{", "null", "[]"):
            reset()
            send(reason_change(gen, "SOURCE", detail))
        for at in (True, False, float(NOW), float(NOW + NS)):
            reset()
            send(at=at)

        # Entry asset text: deliberately exclude non-object JSON, the ruled departure.
        assets = []
        for kw in ASSETS.values():
            text = next(r.detail for r in gen.rows[0].reasons if r.code == "ASSET")
            assets.append(reason_change(gen, "ASSET", json.dumps(json.loads(text or "null") | kw)))
        assets += [reason_change(gen, "ASSET", text) for text in (None, "{", "{}")]
        for g in assets:
            usable(g)
            add("entry_rank", SYMBOL, NOW)
        for count in (0, 2):
            asset = next(x for x in gen.rows[0].reasons if x.code == "ASSET")
            others = tuple(x for x in gen.rows[0].reasons if x.code != "ASSET")
            usable(row_change(gen, reasons=others + (asset,) * count))
            add("entry_rank", SYMBOL, NOW)
        for name in ("price", "move", "dvol_ratio", "trades_accel"):
            for event in (None, NOW + 1, NOW - 2 * NS, NOW - 2 * NS - 1):
                # A null value lets a missing event reach entry's own clock check.
                # Unranked, non-focus rows isolate those generation checks from qualification.
                features = tuple(
                    replace(f, ts_event=event, known_at=None, value=None) if f.name == name else f
                    for f in gen.rows[0].features
                )
                g = row_change(gen, features=features, score=None, rank=None, focus_eligible=False)
                usable(g)
                add("entry_rank", SYMBOL, NOW)
        # A stale symbol in an otherwise fresh generation reaches symbol_input_stale.
        old = feature_change(gen, "price", ts_event=NOW - 2 * NS - 1)
        usable(old)
        add("entry_rank", SYMBOL, NOW)
        add("entry_rank", "ZZABSENT", NOW)
        usable()
        add("entry_rank", SYMBOL, NOW)
        runs.append(steps)
    rows = [
        [
            r.name,
            r.unit,
            str(r.value),
            r.source,
            r.calibration_state,
            r.last_change.isoformat(),
            "decimal" if isinstance(r.value, D) else "int",
        ]
        for r in REG.rows
    ]
    return {
        "registry": rows,
        "segment": [SEG.name, SEG.session_date.isoformat(), SEG.start_ns, SEG.end_ns, SEG.source],
        "universe_hash": PUB.universe_hash,
        "baseline_hash": PUB.baseline.identity,
        "runs": runs,
    }


def replay(m: dict[str, Any], inputs: dict[str, Any]) -> list[Any]:
    """Compare return, reason, digest and all alarms after every call, including raises."""
    from datetime import date
    from decimal import Decimal

    reg = m["Registry"](
        tuple(
            m["RegistryRow"](
                n,
                u,
                Decimal(v) if kind == "decimal" else int(v),
                source,
                state,
                date.fromisoformat(day),
            )
            for n, u, v, source, state, day, kind in inputs["registry"]
        )
    )
    name, day, start, end, source = inputs["segment"]
    segment = m["Segment"](name, date.fromisoformat(day), start, end, source)
    answers = []
    for steps in inputs["runs"]:
        alarms: list[str] = []
        observed, r = [], None
        for call, *args in steps:
            if call == "reset":
                kw = dict(
                    registry=reg,
                    feed="sip",
                    segment=segment,
                    universe_hash=inputs["universe_hash"],
                    baseline_hash=inputs["baseline_hash"],
                    alarm=alarms.append,
                )
                r = m["make"](**(kw | args[0]))
                continue
            assert r is not None
            try:
                if call == "accept_generation":
                    text, at, metadata, kind = args
                    # Each interpreter rebuilds with its own record before its own wrapper.
                    g = m["RankingGeneration"].from_json(text)
                    if kind == "foreign":

                        class Foreign:
                            ts_event, ts_init = g.ts_event, g.ts_init

                            def to_json(self, text: str = text) -> str:
                                return text

                        g = Foreign()
                    elif kind not in {"native", "none"}:
                        error = {
                            e.__name__: e
                            for e in (
                                ValueError,
                                KeyError,
                                TypeError,
                                AttributeError,
                                ArithmeticError,
                            )
                        }[kind]

                        class Broken:
                            ts_event, ts_init = g.ts_event, g.ts_init

                            def to_json(self, error: Any = error) -> str:
                                raise error("zz-broken")

                        g = Broken()
                    wrapped = (
                        None
                        if kind == "none"
                        else m["CustomData"](
                            m["DataType"]("RankingGeneration", metadata=metadata), g
                        )
                    )
                    result = r.accept_generation(wrapped, now=at)
                elif call == "accept_heartbeat":
                    result = r.accept_heartbeat(at=args[0], received_ns=args[1])
                elif call == "entry_rank":
                    result = r.entry_rank(args[0], now=args[1])
                elif call == "fresh":
                    result = r.fresh(args[0])
                else:
                    assert call == "invalidate"
                    result = r.invalidate(args[0])
            except Exception as exc:
                result = ["raised", type(exc).__name__, str(exc)]
            observed.append([call, result, r.reason, r.generation_key, list(alarms)])
        answers.append(observed)
    return answers


@cache
def seeded_answers() -> list[Any]:
    m: dict[str, Any] = dict(
        Registry=Registry,
        RegistryRow=RegistryRow,
        Segment=Segment,
        RankingGeneration=RankingGeneration,
        CustomData=CustomData,
        DataType=DataType,
    )
    m["make"] = lambda **kw: GenerationReader(
        method_version=METHOD, eligible_security_types=TYPES, **kw
    )
    return replay(m, seeded_inputs())


def reader_counts(answers: list[Any]) -> dict[str, int]:
    steps = [s for run in answers for s in run]
    return {
        "runs": len(answers),
        "calls": len(steps),
        "accepted": sum(s[0] == "accept_generation" and s[1] is True for s in steps),
        "refused": sum(s[0] == "accept_generation" and s[1] is False for s in steps),
    }


READER_DIGEST = "8faf01ec9b15a108dfd7c87667f17707093d94772087aa323231b915dfc197db"


def test_seeded_reader_answers_are_pinned() -> None:
    answers = seeded_answers()
    counts = reader_counts(answers)
    assert counts["runs"] >= 40 and counts["calls"] >= 400 and counts["accepted"] >= 100
    steps = [s for run in answers for s in run]
    assert {s[0] for s in steps} == {
        "accept_heartbeat",
        "accept_generation",
        "fresh",
        "entry_rank",
        "invalidate",
    }
    reasons = {s[2] for s in steps}
    assert {x[3] for x in CASES.values()} <= reasons
    assert {
        "heartbeat_invalid",
        "heartbeat_missing",
        "heartbeat_stale",
        "generation_missing",
        "session_mismatch",
        "generation_stale",
        "input_stale",
        "symbol_input_stale",
        "retired producer epoch replay",
        EPOCH,
        SEQUENCE,
        "accepted",
        "fresh",
    } <= reasons
    payload = json.dumps(answers, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(payload.encode()).hexdigest() == READER_DIGEST


READER_PARITY = """
import hashlib, json, os, sys
src = os.environ["MOVERS_REFERENCE_SRC"]
path = os.path.join(src, "sextant", "ranking", "reader.py")
with open(path, "rb") as handle:
    blob = handle.read()
expected = "70cac528ca4ad16af02fde70c75b74126e358cc7"
assert hashlib.sha1(b"blob %d\\0" % len(blob) + blob).hexdigest() == expected
sys.path.insert(0, src)
import nautilus_trader
from nautilus_trader.model import CustomData, DataType
from sextant.ranking import reader as rd, generation as g, registry as rg
from sextant.ranking.engine import Segment
assert os.path.samefile(rd.__file__, path)
inputs = json.loads(sys.stdin.read())
rd.METHOD = inputs.pop("method_version")
m = dict(Registry=rg.Registry, RegistryRow=rg.RegistryRow, Segment=Segment,
         RankingGeneration=g.RankingGeneration, CustomData=CustomData, DataType=DataType,
         make=rd.GenerationReader)
print("PARITY " + json.dumps({
    "versions": [list(sys.version_info[:3]), nautilus_trader.__version__],
    "answers": replay(m, inputs),
}))
"""


@pytest.mark.local
def test_reader_parity_with_the_reference_in_a_child_interpreter() -> None:
    import nautilus_trader

    src, python = child.reference()
    script = "from typing import Any\n" + inspect.getsource(replay) + READER_PARITY
    theirs = child.run(
        python, src, script, stdin=json.dumps(seeded_inputs() | {"method_version": METHOD})
    )
    here = [list(sys.version_info[:3]), nautilus_trader.__version__]
    assert here == theirs["versions"], (
        f"version mismatch: here {here}, reference {theirs['versions']}"
    )
    ours = seeded_answers()
    print(f"\npython, engine: here {here}, reference {theirs['versions']}")
    print(f"reader parity: {reader_counts(ours)}")
    assert theirs["answers"] == ours
