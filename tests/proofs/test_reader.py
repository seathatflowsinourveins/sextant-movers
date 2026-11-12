"""Part 6: reader proofs on synthetic publisher output, no fixture file or network.

Build on the reference's `src/sextant/ranking/reader.py:20-270` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; only the local child imports the reference.
"""

import ast
import json
import logging
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model import CustomData, DataType
from sextant_movers.ranking.reader import GenerationReader
from test_publish import NOW, filled, full
from test_publisher import NS, TYPES, registry

from sextant_movers.ranking import reader as module
from sextant_movers.ranking.generation import (
    CoverageState,
    GenerationId,
    RankingGeneration,
    generation_digest,
)
from sextant_movers.ranking.publisher import ranking_data_type, universe_digest

# Lower fixture thresholds give each row a qualifying focus rank; no authority is shipped.
REG = registry(mover_min_trades_accel=D("0.01"), mover_min_dvol_ratio=D("0.01"))
PUB = filled(reg=REG)
GEN = full(PUB)
SEG = PUB.segment
METHOD = GEN.method_version
TOPIC = ranking_data_type(PUB.feed)
SYMBOL = GEN.rows[0].symbol


def make(**over: Any) -> GenerationReader:
    kw = dict(
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


def row_change(g: RankingGeneration, index: int = 0, **kw: Any) -> RankingGeneration:
    rows = list(g.rows)
    rows[index] = replace(rows[index], **kw)
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
            # Removing the score and rank isolates qualification from score validation.
            base = g if name not in {"move", "dvol_ratio"} else row_change(g, score=None, rank=None)
            add("focus_" + name + "_" + suffix, feature_change(base, name, value=value), FOCUS)
    add("rank_order", row_change(g, rank=2), "rank order mismatch")
    return cases


CASES = generation_cases()


@pytest.mark.parametrize("key", ["method_version", "eligible_security_types"])
def test_new_keywords_are_required(key: str) -> None:
    kw = dict(
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


@pytest.mark.parametrize("detail", [None, "{", "{}", "null", "0", '"zz"', "[]"])
def test_entry_malformed_asset_returns_none(detail: Any) -> None:
    r = accepted()
    r.generation = reason_change(GEN, "ASSET", detail)
    # A non-object JSON raises AttributeError in the reference: kept and reported.
    if detail in {"null", "0", '"zz"', "[]"}:
        with pytest.raises(AttributeError):
            r.entry_rank(SYMBOL, now=NOW)
    else:
        assert r.entry_rank(SYMBOL, now=NOW) is None


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


@pytest.mark.parametrize("now,ok", [(True, False), (False, False), (float(NOW), True)])
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
