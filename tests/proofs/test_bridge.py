"""Part 6 bridge: producer-built synthetic documents, pure projection and reader round trip.

Acceptance: the reference, src/sextant/tradingview/bridge.py:58-385 at
06562bec0898a7203f4eaf69086ced83d2477e6b. Bounds below are test input only.
"""

import ast
import copy
import inspect
import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from nautilus_trader.model import TradeTick
from test_engine import START, table
from test_producer import AUTH, SCOPE, SLOT
from test_publisher import ASSET, NS, REFS, new, ready, registry, tick

from sextant_movers.lease import WriterLease
from sextant_movers.ranking.generation import (
    Coverage,
    CoverageState,
    RankingGeneration,
    generation_digest,
)
from sextant_movers.ranking.publisher import universe_digest
from sextant_movers.sessions import SECOND
from sextant_movers.tradingview import bridge as b
from sextant_movers.tradingview import inputs, producer

METHOD = "zz-bridge-method"

BOUNDS = b.DisplayBounds(D("0.05"), D("2"), 60)

SUFFIX = "; lists NOT synced"

FIRST = "One j5 document and sourced display bounds required"

IDENTITY = "Invalid j5 document identity" + SUFFIX

INVALID = "Invalid j5-ranked document" + SUFFIX

EXPIRED = "j5 generation expired or lagged" + SUFFIX

LIFETIME = "j5 generation has no display lifetime" + SUFFIX

FAULTS = {
    "unavailable": "j5 generation is unavailable",
    "identity": "Invalid j5 document identity",
    "generation_object": "j5 ranking generation must be one document",
    "generation_invalid": "Invalid j5 ranking generation",
    "digest": "j5 generation digest mismatch",
    "coverage": "j5 generation coverage incomplete",
    "method": "j5 generation identity mismatch",
    "clocks": "j5 generation clocks inconsistent",
    "source_missing": "j5 generation source missing",
    "source_invalid": "j5 generation source invalid",
    "source_mixed": "j5 generation sources mixed",
    "no_rows": "j5 generation has no rows",
    "source_mismatch": "j5 generation source mismatch",
    "feature_clocks": "j5 feature clocks invalid",
    "score": "j5 score does not match its features",
    "rank_without_score": "j5 rank without score",
    "rank_order": "j5 rank order mismatch",
    "expired": "j5 generation expired or lagged",
    "session": "j5 generation session mismatch",
    "membership_missing": "j5 ranked membership missing",
    "membership_empty": "j5 ranked membership empty or oversize",
    "mapping": "j5 ranked mappings inexact or aliased",
    "partial": "j5 projected row incomplete",
    "focus": "j5 focus qualification incomplete",
    "lifetime": "j5 generation has no display lifetime",
    "catch": "Invalid j5-ranked document",
}


def real_doc(root: Path, count: int = 3, segment: str = "PRE", fraction: int = 100) -> dict:
    """Every base comes from LiveProducer and eligible, synthetic trades."""
    names = tuple(f"ZZB{i:03d}" for i in range(count))
    start = START + {"PRE": 0, "RTH": 19800}[segment] * NS
    reg = registry(
        top_n=count,
        generation_max_age_s=1200,
        input_max_age_s=1200,
        heartbeat_max_age_s=1200,
        mover_min_trades_accel=D("0.01"),
        mover_min_dvol_ratio=D("0.01"),
    )
    base = table(symbols=names, known_at=start - NS)
    base = replace(base, samples=tuple(replace(s, segment=segment) for s in base.samples))
    scope = replace(ready().segment, name=segment, start_ns=start, end_ns=start + 3600 * NS)
    pub = new(registry=reg, universe=names, baseline=base, segment=scope, method_version=METHOD)
    for name in names:
        pub.set_symbol(
            name,
            references=REFS,
            asset=replace(ASSET, known_at=start - NS, expires_at=scope.end_ns),
        )
    lease = WriterLease(SCOPE, root, "zz-bridge")
    lease.acquire()
    try:
        live = producer.LiveProducer(pub, lease, scope=SCOPE, paper_slot=SLOT, sip_authority=AUTH)
        for sec, price in ((0, "2.00"), (60, "2.10"), (300, "2.40"), (360, "2.60")):
            for name in names:
                raw = tick(sec, price, name=name)
                at = start + sec * NS
                view = TradeTick(
                    *(
                        raw.instrument_id,
                        raw.price,
                        raw.size,
                        raw.aggressor_side,
                        raw.trade_id,
                        at,
                        at,
                    )
                )
                assert live.on_trade(
                    view,
                    clocks=producer.Clocks(at, at, "zz-trade"),
                    now=at,
                    price_eligible=True,
                    volume_eligible=True,
                    conditions=(),
                    mask_source="zz-mask",
                )
        pub.set_coverage(Coverage(CoverageState.READY, True, count, count, 0, 0, 0))
        now = start + 360 * NS + fraction
        assert live.heartbeat(at=now, received_ns=now)
        doc = live.build_unit(
            now=now,
            symbol_map={n: f"ZZX:{n}" for n in names},
            mapping_clocks=producer.Clocks(start - NS, now, "zz-map"),
        )
    finally:
        lease.close()
    assert doc["status"] == "fresh", doc.get("reason")
    return doc


@pytest.fixture
def doc(tmp_path):
    return real_doc(tmp_path / "lease")


def project(doc, **over):
    return b.project_display_unit(
        doc, **({"bounds": BOUNDS, "method_version": METHOD, "paper_slot": SLOT} | over)
    )


def generation(doc):
    return RankingGeneration.from_json(doc["ranking_generation"])


def rebuild(doc, **kw):
    result = copy.deepcopy(doc)
    gen = replace(generation(doc), **kw)
    result.update(ranking_generation=gen.to_dict(), generation_sha256=generation_digest(gen))
    return result


def row(doc, **kw):
    gen = generation(doc)
    return rebuild(doc, rows=(replace(gen.rows[0], **kw), *gen.rows[1:]))


def feature(doc, name="price", **kw):
    return row(
        doc,
        features=tuple(
            replace(f, **kw) if f.name == name else f for f in generation(doc).rows[0].features
        ),
    )


def sources(doc, **kw):
    gen = generation(doc)
    rows = tuple(
        replace(
            r,
            reasons=tuple(
                replace(reason, detail=json.dumps(json.loads(reason.detail or "null") | kw))
                if reason.code == "SOURCE"
                else reason
                for reason in r.reasons
            ),
        )
        for r in gen.rows
    )
    return rebuild(doc, rows=rows)


def refuse(call, message):
    with pytest.raises(b.BridgeError) as caught:
        call()
    assert str(caught.value) == message
    assert isinstance(caught.value, inputs.InputError)


def damage(doc, case):
    doc = copy.deepcopy(doc)
    gen, first = generation(doc), generation(doc).rows[0]
    if case in {
        "unavailable",
        "identity",
        "generation_object",
        "generation_invalid",
        "digest",
        "expired",
        "membership_missing",
        "membership_empty",
        "mapping",
        "catch",
    }:
        key, value = {
            "unavailable": ("status", "unavailable"),
            "identity": ("paper_slot", SLOT + 1),
            "generation_object": ("ranking_generation", []),
            "generation_invalid": ("ranking_generation", {}),
            "digest": ("generation_sha256", "0" * 64),
            "expired": ("expires_at_ns", gen.known_at),
            "membership_missing": ("watchlists", {"NS Ranked": [], "zz": []}),
            "membership_empty": ("watchlists", {"NS Ranked": []}),
            "mapping": ("watchlists", {"NS Ranked": ["zz-unqualified"]}),
            "catch": ("watchlists", {"NS Ranked": [[]]}),
        }[case]
        doc[key] = value
        return doc
    if case == "coverage":
        return rebuild(doc, coverage=replace(gen.coverage, complete=False))
    if case == "method":
        return rebuild(doc, method_version="zz-other")
    if case == "clocks":
        return rebuild(doc, input_cutoff=gen.ts_event - 1)
    if case.startswith("source_"):
        if case == "source_mismatch":
            return sources(doc, feed="zz-other")
        reasons = tuple(r for r in first.reasons if r.code != "SOURCE")
        if case != "source_missing":
            reason = next(r for r in first.reasons if r.code == "SOURCE")
            reasons += (replace(reason, detail="{" if case == "source_invalid" else "{}"),)
        return row(doc, reasons=reasons)
    if case == "no_rows":
        return rebuild(
            doc, rows=(), coverage=replace(gen.coverage, expected_symbols=0, observed_symbols=0)
        )
    if case == "feature_clocks":
        return feature(doc, known_at=gen.known_at + 1)
    if case == "score":
        assert first.score is not None
        return row(doc, score=first.score + 1)
    if case == "rank_without_score":
        return row(doc, score=None)
    if case == "rank_order":
        return row(doc, rank=len(gen.rows) + 1)
    if case == "session":
        return rebuild(doc, session_date=gen.session_date + timedelta(days=1))
    if case == "partial":
        return row(doc, partial=True)
    if case == "focus":
        return feature(row(doc, focus_eligible=True), "move_1m", value=D(0))
    assert case == "lifetime"
    doc = rebuild(doc, known_at=gen.ts_event, ts_init=gen.ts_event)
    # Feature knowledge must also fit the narrowed clock.
    gen = generation(doc)
    doc = rebuild(
        doc,
        rows=tuple(
            replace(r, features=tuple(replace(f, known_at=gen.ts_event) for f in r.features))
            for r in gen.rows
        ),
    )
    doc.update(generated_at_ns=gen.ts_event, expires_at_ns=gen.ts_event + NS - 1)
    return doc


def shift(doc, offset):
    gen = generation(doc)
    changed = rebuild(
        doc,
        **{
            k: getattr(gen, k) + offset for k in ("ts_event", "input_cutoff", "known_at", "ts_init")
        },
        rows=tuple(
            replace(
                r,
                features=tuple(
                    replace(
                        f,
                        ts_event=None if f.ts_event is None else f.ts_event + offset,
                        known_at=None if f.known_at is None else f.known_at + offset,
                    )
                    for f in r.features
                ),
            )
            for r in gen.rows
        ),
    )
    source = json.loads(next(r.detail for r in gen.rows[0].reasons if r.code == "SOURCE") or "null")
    changed = sources(
        changed, start_ns=source["start_ns"] + offset, end_ns=source["end_ns"] + offset
    )
    changed.update(
        generated_at_ns=doc["generated_at_ns"] + offset, expires_at_ns=doc["expires_at_ns"] + offset
    )
    return changed


def write_read(root, projected, now):
    for name, document in projected.documents().items():
        (root / name).write_text(json.dumps(document), encoding="utf-8")
    return inputs.read_research_inputs(root, now=now, max_age=3600)


@pytest.mark.parametrize("case", FAULTS)
def test_each_document_refusal_alone(doc, case):
    message = FAULTS[case] + ("" if case == "generation_object" else SUFFIX)
    refuse(lambda: project(damage(doc, case)), message)


@pytest.mark.parametrize("field", ["gap_bound", "rvol_bound"])
@pytest.mark.parametrize("value", [D(0), D(-1), D("NaN"), D("Infinity"), D("-Infinity"), 0.1, True])
def test_decimal_bounds_each_edge(field, value):
    kw: dict[str, Any] = dict(gap_bound=D("0.05"), rvol_bound=D(2), max_age_s=1) | {field: value}
    refuse(lambda: b.DisplayBounds(**kw), "Sourced positive Decimal display bounds required")


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), 1.0, True])
def test_age_bound_each_edge(value):
    refuse(
        lambda: b.DisplayBounds(D(1), D(1), value),
        "Sourced positive Decimal display bounds required",
    )


def test_bounds_frozen_slotted_required_and_positive_edges(doc):
    bounds = b.DisplayBounds(D("1e-100"), D("1e-100"), 1)
    assert project(doc, bounds=bounds).display_lists["bounds"]["max_age_s"] == 1
    assert not hasattr(bounds, "__dict__")
    with pytest.raises(FrozenInstanceError):
        bounds.max_age_s = 2  # type: ignore[misc]
    with pytest.raises(TypeError):
        b.DisplayBounds()  # type: ignore[call-arg]
    refuse(lambda: project(doc, bounds=None), FIRST)
    refuse(lambda: project([]), FIRST)


@pytest.mark.parametrize("key", ["method_version", "paper_slot", "bounds"])
def test_caller_keywords_required(doc, key):
    kw = {"bounds": BOUNDS, "method_version": METHOD, "paper_slot": SLOT}
    del kw[key]
    with pytest.raises(TypeError, match=key):
        b.project_display_unit(doc, **kw)
    assert inspect.signature(b.project_display_unit).parameters[key].default is inspect._empty


@pytest.mark.parametrize("value", [None, "", 2, True])
def test_bad_method_refused_before_document_read(value):
    refuse(lambda: project({}, method_version=value), FIRST)


@pytest.mark.parametrize("value", [None, True, 17.0, "17"])
def test_bad_slot_refused_before_document_read(value):
    refuse(lambda: project({}, paper_slot=value), FIRST)


def test_caller_identity_not_module_constants(doc):
    refuse(
        lambda: project(doc, method_version="zz-wrong"), "j5 generation identity mismatch" + SUFFIX
    )
    refuse(lambda: project(doc, paper_slot=SLOT + 1), IDENTITY)
    changed = rebuild(doc, method_version="zz-new") | {"paper_slot": -9}
    assert project(changed, method_version="zz-new", paper_slot=-9)


@pytest.mark.parametrize("value", [-1, True, 1.0])
def test_rfc_nonnegative_integer_refusal(value):
    refuse(lambda: b._rfc(value), "Generation clocks must be nonnegative nanoseconds")


def test_rfc_zero_and_past_year_9999():
    assert b._rfc(0) == "1970-01-01T00:00:00+00:00"
    refuse(lambda: b._rfc(253402300800 * NS), "Generation clock does not round-trip as RFC3339")


@pytest.mark.parametrize("cause", ["mismatch", "input_error", "bridge_error"])
def test_rfc_each_roundtrip_raise_statement(monkeypatch, cause):
    def bad_ns(text):
        if cause == "mismatch":
            return 1
        raise (inputs.InputError if cause == "input_error" else b.BridgeError)("zz")

    monkeypatch.setattr(b, "_ns", bad_ns)
    refuse(
        lambda: b._rfc(0),
        "zz" if cause == "bridge_error" else "Generation clock does not round-trip as RFC3339",
    )


@pytest.mark.parametrize(
    "kind", [ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OverflowError]
)
def test_last_except_each_class(doc, monkeypatch, kind):
    def fail(*args):
        raise kind("zz-injected")

    monkeypatch.setattr(b, "_generation", fail)
    refuse(lambda: project(doc), INVALID)


@pytest.mark.parametrize("segment", ["PRE", "RTH"])
@pytest.mark.parametrize("fraction", [0, 123456789])
def test_exact_output_and_roundtrip(tmp_path, segment, fraction):
    doc = real_doc(tmp_path / "lease", 3, segment, fraction)
    before = copy.deepcopy(doc)
    unit = project(doc)
    gen = generation(doc)
    clocks = [b._rfc(getattr(gen, k)) for k in ("ts_event", "input_cutoff", "known_at")]
    identity = {"as_of": clocks[0], "sha256": doc["generation_sha256"]}
    common = dict(
        session_date=gen.session_date.isoformat(),
        generated_at=clocks[2],
        as_of=clocks[1],
        as_of_snapshot=clocks[0],
        snapshot_status="observed",
        status="fresh",
        generation=identity,
    )
    movers = [
        {"symbol": r.symbol, "tradingview_symbol": m, "labels": [f"rank:{r.rank}"]}
        for r, m in zip(gen.rows, doc["watchlists"]["NS Ranked"], strict=True)
    ]
    focus = [m for r, m in zip(gen.rows, movers, strict=True) if r.focus_eligible is True]
    assert unit.documents() == {
        "top100.json": {"schema": "morning-pack.candidates/1", **common, "candidates": movers},
        "focus.json": {"schema": "top100.focus/1", **common, "candidates": focus},
        "display-lists.json": {
            "schema": "ns-display-lists/1",
            **common,
            "ns_movers": movers,
            "ns_focus": focus,
            "bounds": {"gap_bound": "0.05", "rvol_bound": "2", "max_age_s": 60},
            "counts": {"ns_movers": len(movers), "ns_focus": len(focus)},
            "excluded_unknown": 0,
            "armed": "UNVERIFIED",
            "sources": {"snapshot": identity, "armed_sets": []},
        },
    }
    assert len({id(d["generation"]) for d in unit.documents().values()}) == 3
    assert not hasattr(unit, "__dict__")
    with pytest.raises(FrozenInstanceError):
        unit.focus = {}  # type: ignore[misc]
    assert doc == before and project(doc).documents() == unit.documents()
    assert clocks[2].endswith(".123456789+00:00" if fraction else "00+00:00")
    result = write_read(tmp_path, unit, inputs.timestamp(clocks[2]) + timedelta(seconds=1))
    assert result.generation == identity and not result.unmapped
    for key, members in (("NS movers", movers), ("NS focus", focus)):
        assert result.memberships[key] == frozenset(m["tradingview_symbol"] for m in members)
    # Reader exposes sets; wire arrays retain rank order.
    assert [m["tradingview_symbol"] for m in movers] == doc["watchlists"]["NS Ranked"]
    with pytest.raises(inputs.InputError, match="Research generation expired; lists NOT synced"):
        write_read(tmp_path, unit, inputs.timestamp(clocks[0]) + timedelta(seconds=61))


def test_source_shared_constants_types_and_pure_boundary():
    tree = ast.parse(Path(b.__file__).read_text())
    allowed = {
        "__future__",
        "json",
        "re",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "typing",
        "sextant_movers.sessions",
        "sextant_movers.ranking.generation",
        "sextant_movers.ranking.publisher",
        "sextant_movers.tradingview.inputs",
        "sextant_movers.tradingview.producer",
    }
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports <= allowed
    assert b.MAX_MEMBERS == inputs.MAX_MEMBERS
    assert b.REFUSED_PREFIXES is inputs.REFUSED_PREFIXES
    assert b.MAX_LAG_NS == inputs.MAX_SNAPSHOT_LAG_S * SECOND
    for name in ("EPOCH", "QUALIFIED", "TICKER", "InputError", "_ns", "_session", "timestamp"):
        assert getattr(b, name) is getattr(inputs, name)
    assert b.RankingGeneration is RankingGeneration and b.universe_digest is universe_digest
    assert b.SIP_FEED == producer.SIP_FEED
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(n):
            n.body = n.body[1:]
    values = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert not {"sip", 600, 100, *inputs.REFUSED_PREFIXES} & set(values)
    forbidden = {
        "open",
        "write",
        "write_text",
        "write_bytes",
        "read",
        "read_text",
        "now",
        "time",
        "sleep",
        "Thread",
        "Timer",
        "socket",
        "__import__",
        "exec",
        "eval",
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            assert (
                n.func.id
                if isinstance(n.func, ast.Name)
                else n.func.attr
                if isinstance(n.func, ast.Attribute)
                else ""
            ) not in forbidden


def test_kept_expired_branch_requires_changing_mapping(doc):
    gen = generation(doc)
    assert gen.ts_event <= gen.known_at < doc["expires_at_ns"]

    class ChangingExpiry(dict):
        reads = 0

        def __getitem__(self, key):
            if key == "expires_at_ns":
                self.reads += 1
                if self.reads == 3:
                    return gen.ts_event
            return super().__getitem__(key)

    refuse(lambda: project(ChangingExpiry(doc)), "j5 generation expired" + SUFFIX)


# Cut 2: remaining unit edges and observations.


@pytest.mark.parametrize("field", ["ranker_version", "profile_version"])
def test_generation_identity_other_fields(doc, field):
    refuse(
        lambda: project(rebuild(doc, **{field: "zz-other"})),
        "j5 generation identity mismatch" + SUFFIX,
    )


@pytest.mark.parametrize("field", ["input_cutoff", "known_at"])
def test_each_generation_clock_order(doc, field):
    gen = generation(doc)
    kw = (
        {"input_cutoff": gen.known_at + 1}
        if field == "input_cutoff"
        else {"known_at": gen.ts_init + 1}
    )
    refuse(lambda: project(rebuild(doc, **kw)), "j5 generation clocks inconsistent" + SUFFIX)


@pytest.mark.parametrize("count", [100, 101])
def test_members_at_bound_and_one_past_untruncated(tmp_path, count):
    doc = real_doc(tmp_path / "lease", count)
    if count == 101:
        refuse(lambda: project(doc), "j5 ranked membership empty or oversize" + SUFFIX)
    else:
        assert len(project(doc).top100["candidates"]) == count


@pytest.mark.parametrize(
    "lag", [inputs.MAX_SNAPSHOT_LAG_S * SECOND, inputs.MAX_SNAPSHOT_LAG_S * SECOND + 1]
)
def test_lag_at_bound_and_one_nanosecond_past(doc, lag):
    gen = generation(doc)
    at = gen.ts_event + lag
    changed = rebuild(doc, input_cutoff=at, known_at=at, ts_init=at)
    changed.update(generated_at_ns=at, expires_at_ns=at + 10 * NS)
    if lag % NS:
        refuse(lambda: project(changed), EXPIRED)
    else:
        assert project(changed)


@pytest.mark.parametrize("life", [NS - 1, NS, NS + 1])
def test_lifetime_one_second_and_each_adjacent_nanosecond(doc, life):
    changed = damage(doc, "lifetime")
    changed["expires_at_ns"] = generation(changed).ts_event + life
    if life < NS:
        refuse(lambda: project(changed), LIFETIME)
    else:
        assert project(changed).display_lists["bounds"]["max_age_s"] == 1


@pytest.mark.parametrize("value", ["duplicate", "prefix", "extra", "second_list"])
def test_membership_faults(doc, value):
    changed = copy.deepcopy(doc)
    members = changed["watchlists"]["NS Ranked"]
    reason = "j5 ranked mappings inexact or aliased"
    if value == "duplicate":
        members[1] = members[0]
    elif value == "prefix":
        members[0] = next(iter(sorted(inputs.REFUSED_PREFIXES))) + ":ZZB000"
    elif value == "extra":
        members.append("ZZX:ZZEXTRA")
        reason = "j5 ranked membership empty or oversize"
    else:
        changed["watchlists"]["zz-second"] = []
        reason = "j5 ranked membership missing"
    refuse(lambda: project(changed), reason + SUFFIX)


@pytest.mark.parametrize("cause", ["move", "dvol_ratio", "accel", "score", "rank"])
def test_score_features_rank_disagreements(doc, cause):
    if cause in {"move", "dvol_ratio"}:
        changed = feature(doc, cause, value=None)
        reason = "j5 score does not match its features"
    elif cause == "accel":
        changed, reason = feature(doc, "trades_accel", value=None), "j5 rank order mismatch"
    elif cause == "score":
        changed, reason = row(doc, score=None), "j5 rank without score"
    else:
        changed, reason = row(doc, rank=None), "j5 rank order mismatch"
    refuse(lambda: project(changed), reason + SUFFIX)


@pytest.mark.parametrize("cause", ["partial", "not_completed", *b.KEY_FEATURES])
def test_each_projected_completeness_check(doc, cause):
    if cause in {"partial", "not_completed"}:
        changed = row(doc, **({"partial": True} if cause == "partial" else {"completed": False}))
    else:
        changed = row(
            doc, features=tuple(f for f in generation(doc).rows[0].features if f.name != cause)
        )
        if cause in {"move", "dvol_ratio", "trades_accel"}:
            # Ranking checks precede projection. An unscored, unpaired row cannot witness this.
            reason = (
                "j5 rank order mismatch"
                if cause == "trades_accel"
                else ("j5 score does not match its features")
            )
            refuse(lambda: project(changed), reason + SUFFIX)
            return
    refuse(lambda: project(changed), "j5 projected row incomplete" + SUFFIX)


@pytest.mark.parametrize(
    "name,value",
    [("move_1m", None), ("move_1m", D(0)), ("move_1m", D(-1)), ("price", D(0)), ("price", D(-1))],
)
def test_focus_requires_positive_minute_move_and_price(doc, name, value):
    changed = feature(row(doc, focus_eligible=True), name, value=value)
    refuse(lambda: project(changed), "j5 focus qualification incomplete" + SUFFIX)


def test_sources_return_is_unused_and_shared_session_bounds(doc, monkeypatch):
    tree = ast.parse(Path(b.__file__).read_text())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "_sources"
    ]
    assert len(calls) == 1
    assert b._sources(generation(doc), doc["params_hash"])["feed"] == producer.SIP_FEED
    monkeypatch.setattr(inputs, "PRE_START", inputs.OVERNIGHT_START)
    refuse(lambda: project(doc), "j5 generation session mismatch" + SUFFIX)


@pytest.mark.parametrize("clock", ["ts_event", "input_cutoff", "known_at"])
def test_overnight_refuses_for_each_clock(doc, clock):
    gen = generation(doc)
    # Move all clocks before PRE; then advance a suffix to PRE to isolate each checked clock.
    changed = shift(doc, -420 * NS)
    night = generation(changed)
    kw = (
        {"input_cutoff": gen.input_cutoff, "known_at": gen.known_at, "ts_init": gen.ts_init}
        if clock == "ts_event"
        else ({"known_at": gen.known_at, "ts_init": gen.ts_init} if clock == "input_cutoff" else {})
    )
    changed = rebuild(changed, **kw)
    changed["generated_at_ns"] = generation(changed).known_at
    assert getattr(night, clock) < START
    refuse(lambda: project(changed), "j5 generation session mismatch" + SUFFIX)


def test_narrowed_age_excluded_unknown_and_paired_focus_subset(doc):
    gen = generation(doc)
    last = gen.rows[-1]
    changed = rebuild(doc, rows=(*gen.rows[:-1], replace(last, score=None, rank=None)))
    changed["watchlists"]["NS Ranked"] = doc["watchlists"]["NS Ranked"][:1]
    changed["expires_at_ns"] = gen.ts_event + 3 * NS + 99
    unit = project(changed)
    assert unit.display_lists["excluded_unknown"] == 1
    assert unit.display_lists["bounds"]["max_age_s"] == 3
    assert len(unit.top100["candidates"]) == len(unit.focus["candidates"]) == 1
    assert unit.focus["candidates"] == unit.top100["candidates"]


def test_kept_lagged_projection_has_no_readable_interior(doc, tmp_path):
    gen = generation(doc)
    at = gen.ts_event + inputs.MAX_SNAPSHOT_LAG_S * NS
    changed = rebuild(doc, input_cutoff=at, known_at=at, ts_init=at)
    changed.update(generated_at_ns=at, expires_at_ns=at + 10 * NS)
    unit = project(changed)
    assert unit.display_lists["bounds"]["max_age_s"] == 60
    # Every now >= generated_at is already beyond the projected display age.
    with pytest.raises(inputs.InputError, match="Research generation expired; lists NOT synced"):
        write_read(tmp_path, unit, inputs.timestamp(unit.display_lists["generated_at"]))


def test_post_source_is_retained_and_focus_false_selects_nothing(doc):
    changed = sources(doc, segment="POST")
    changed = rebuild(
        changed, rows=tuple(replace(r, focus_eligible=False) for r in generation(changed).rows)
    )
    unit = project(changed)
    assert unit.focus["candidates"] == unit.display_lists["ns_focus"] == []


def test_all_reference_refusal_statements_are_kept():
    tree = ast.parse(Path(b.__file__).read_text())
    messages = [
        n.exc.args[0].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Raise)
        and isinstance(n.exc, ast.Call)
        and isinstance(n.exc.func, ast.Name)
        and n.exc.func.id == "BridgeError"
        and isinstance(n.exc.args[0], ast.Constant)
    ]
    expected = [
        text + ("" if case == "generation_object" else SUFFIX) for case, text in FAULTS.items()
    ]
    expected += [
        FIRST,
        "Sourced positive Decimal display bounds required",
        "Generation clocks must be nonnegative nanoseconds",
        "Generation clock does not round-trip as RFC3339",
        "Generation clock does not round-trip as RFC3339",
        "j5 generation expired" + SUFFIX,
    ]
    assert len(messages) == len(expected) == 32
    assert sorted(messages) == sorted(expected)


def test_rank_ties_use_acceleration_then_symbol(doc):
    gen = generation(doc)
    # Equal score/acceleration keeps lexical tie order; raising acceleration changes it.
    assert [r.symbol for r in gen.rows] == sorted(r.symbol for r in gen.rows)
    changed = feature(doc, "trades_accel", value=D("0.0001"))
    refuse(lambda: project(changed), "j5 rank order mismatch" + SUFFIX)
    # A coherent, higher score with the old rank also refuses.
    second = gen.rows[1]
    assert second.score is not None
    move = next(f.value for f in second.features if f.name == "move")
    assert move is not None
    new_features = tuple(
        replace(f, value=move * 2) if f.name == "move" else f for f in second.features
    )
    changed = rebuild(
        doc,
        rows=(
            gen.rows[0],
            replace(second, score=second.score * 2, features=new_features),
            *gen.rows[2:],
        ),
    )
    refuse(lambda: project(changed), "j5 rank order mismatch" + SUFFIX)


@pytest.mark.parametrize("prefix", sorted(inputs.REFUSED_PREFIXES))
def test_every_shared_refused_prefix(doc, prefix):
    changed = copy.deepcopy(doc)
    changed["watchlists"]["NS Ranked"][0] = prefix + ":ZZB000"
    refuse(lambda: project(changed), "j5 ranked mappings inexact or aliased" + SUFFIX)
