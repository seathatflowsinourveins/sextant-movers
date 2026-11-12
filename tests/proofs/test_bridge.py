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


def test_feed_comes_from_producer():
    assert b.SIP_FEED == producer.SIP_FEED
    tree = ast.parse(Path(b.__file__).read_text())
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "sextant_movers.tradingview.producer"
        and any(a.name == "SIP_FEED" for a in n.names)
        for n in tree.body
    )


def test_bounds_come_from_reader_and_session_second():
    assert b.MAX_MEMBERS == inputs.MAX_MEMBERS
    assert b.REFUSED_PREFIXES is inputs.REFUSED_PREFIXES
    assert b.MAX_LAG_NS == inputs.MAX_SNAPSHOT_LAG_S * SECOND
    tree = ast.parse(Path(b.__file__).read_text())
    lag = next(
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "MAX_LAG_NS" for t in n.targets)
    )
    assert ast.unparse(lag) == "MAX_SNAPSHOT_LAG_S * SECOND"


def test_package_generation_and_clock_helpers():
    from sextant_movers.ranking.publisher import RANKER

    for name in ("EPOCH", "QUALIFIED", "TICKER", "InputError", "_ns", "_session", "timestamp"):
        assert getattr(b, name) is getattr(inputs, name)
    assert b.RankingGeneration is RankingGeneration and b.universe_digest is universe_digest
    assert b.generation_digest is generation_digest and b.CoverageState is CoverageState
    assert b.RANKER == RANKER


def test_header_names_public_pins_and_package_sources():
    header = ast.get_docstring(ast.parse(Path(b.__file__).read_text()))
    assert header is not None and "/home/" not in header
    assert "src/sextant/tradingview/bridge.py:58-385" in header
    assert "06562bec0898a7203f4eaf69086ced83d2477e6b" in header
    assert "nautechsystems/nautilus_trader@v2.0.0rc8" in header
    assert "python/cpython@v3.13.18" in header
    assert all(
        name in header
        for name in (
            "producer.SIP_FEED",
            "inputs.py",
            "sessions.SECOND",
            "ranking/generation.py",
            "publisher.py",
        )
    )
