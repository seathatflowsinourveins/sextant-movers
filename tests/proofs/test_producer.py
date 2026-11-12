"""Part 6 producer proofs: synthetic records and temporary leases, no transport.

Acceptance: the reference, src/sextant/tradingview/producer.py:50-263 at
06562bec0898a7203f4eaf69086ced83d2477e6b. Caller eligibility intentionally passes through.
"""

import ast
import json
from dataclasses import FrozenInstanceError, asdict, replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from alpaca.data.enums import DataFeed
from nautilus_trader.model import TradeTick
from test_publisher import NAMES, NS, registry, tick
from test_reader import NOW, REG, SEG, feature_change, filled, row_change

from sextant_movers.lease import AccountScope, WriterLease
from sextant_movers.ranking.generation import Coverage, CoverageState, generation_digest
from sextant_movers.tradingview import producer as module
from sextant_movers.tradingview.inputs import REFUSED_PREFIXES
from sextant_movers.tradingview.producer import Clocks, LiveProducer

SCOPE = AccountScope("zz-broker", "zz-scope")
SLOT = 17
AUTH = "zz-invented-feed-authority"
MAPPING = Clocks(SEG.start_ns - NS, NOW + 80 * NS, "zz-mapping")
MEMBERS = {name: f"ZZX:{name}" for name in NAMES}
CONSTRUCTOR = "fresh slot-2 SIP publisher, entitlement and held lease required"
INVALID_TRADE = "invalid_trade; recover a new epoch"
INVALID_SNAPSHOT = "invalid_snapshot; recover a new epoch"
ADMIT = "publisher invalid or stream lease released"
SESSION = "explicit clock inside supplied session required"
MAPPING_ERROR = "empty ranking or missing/ambiguous exact mapping"


@pytest.fixture
def factory(tmp_path: Path, request: pytest.FixtureRequest):
    """Build on the reader's filled publisher; every held lease has a temporary root."""

    def make(*, pub=None, reg=REG, held=True, lease_scope=SCOPE, **over):
        lease = WriterLease(lease_scope, tmp_path / str(len(request.node.fixturenames)), "zz")
        if held:
            lease.acquire()
        request.addfinalizer(lease.close)
        return LiveProducer(
            filled(reg=reg) if pub is None else pub,
            lease,
            **({"scope": SCOPE, "paper_slot": SLOT, "sip_authority": AUTH} | over),
        )

    return make


def unit(live: LiveProducer, **over: Any) -> dict:
    return live.build_unit(
        **({"now": NOW, "symbol_map": MEMBERS, "mapping_clocks": MAPPING} | over)
    )


def observe(live: LiveProducer, second: int = 361, **over: Any) -> bool:
    raw = over.pop("tick", tick(second, "2.70", name=NAMES[0]))
    return live.on_trade(
        raw,
        **(
            {
                "clocks": Clocks(raw.ts_event, raw.ts_init, "zz-trade"),
                "now": raw.ts_event,
                "price_eligible": True,
                "volume_eligible": True,
                "conditions": (),
                "mask_source": "zz-mask",
            }
            | over
        ),
    )


def fresh(live: LiveProducer) -> dict:
    assert live.heartbeat(at=NOW, received_ns=NOW)
    result = unit(live)
    assert result["status"] == "fresh", result
    return result


def poisoned(live: LiveProducer, reason: str) -> None:
    assert live.error == live.reader.reason == reason
    assert live.reader.generation is live.reader.generation_key is None
    assert live.publisher.coverage == Coverage(CoverageState.DEGRADED, False)
    assert unit(live)["reason"] == ADMIT
    with pytest.raises(ValueError, match=ADMIT):
        live._admit(MAPPING, NOW)


@pytest.mark.parametrize("field", ["MARKET_AVAILABLE_AT", "ACQUIRED_AT"])
@pytest.mark.parametrize("value", [-1, True, 1.0])
def test_clocks_refuse_each_bad_clock(field, value):
    with pytest.raises(ValueError, match="explicit integer clocks and source required"):
        Clocks(**({"MARKET_AVAILABLE_AT": 0, "ACQUIRED_AT": 0, "source": "zz"} | {field: value}))


@pytest.mark.parametrize("source", ["", " \t"])
def test_clocks_refuse_blank_source(source):
    with pytest.raises(ValueError, match="explicit integer clocks and source required"):
        Clocks(0, 0, source)


def test_clocks_are_frozen_and_keep_independent_acquisition_time():
    clocks = Clocks(NOW, 0, "zz")
    assert asdict(clocks) == {"MARKET_AVAILABLE_AT": NOW, "ACQUIRED_AT": 0, "source": "zz"}
    assert not hasattr(clocks, "__dict__")
    with pytest.raises(FrozenInstanceError):
        clocks.source = "zz-other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "case",
    [
        "scope",
        "released",
        "authority",
        "feed",
        "night",
        "published",
        "top_n",
        "bool_slot",
        "float_slot",
    ],
)
def test_constructor_each_refusal_alone(factory, case):
    pub, kw = filled(reg=REG), {}
    if case == "scope":
        kw["scope"] = AccountScope("zz-other", "zz-scope")
    elif case == "released":
        kw["held"] = False
    elif case == "authority":
        kw["sip_authority"] = " \t"
    elif case == "feed":
        pub.feed = "zz-other"
    elif case == "night":
        pub.segment = replace(SEG, name="OVERNIGHT")
    elif case == "published":
        assert pub.publish(now=NOW) is not None
    elif case == "top_n":
        pub.registry = registry(top_n=module.MAX_WATCHLIST_SYMBOLS + 1)
    else:
        kw["paper_slot"] = True if case == "bool_slot" else 17.0
    with pytest.raises(ValueError, match=CONSTRUCTOR):
        factory(pub=pub, **kw)


@pytest.mark.parametrize("key", ["scope", "paper_slot"])
def test_scope_and_slot_are_required_keywords(factory, key):
    live = factory()
    kw = {"scope": SCOPE, "paper_slot": SLOT, "sip_authority": AUTH}
    kw.pop(key)
    with pytest.raises(TypeError, match=key):
        LiveProducer(live.publisher, live.lease, **kw)


def test_caller_scope_slot_feed_and_reader_inputs(factory):
    scope = AccountScope("zz-other", "zz-other-scope")
    live = factory(scope=scope, lease_scope=scope, paper_slot=-7)
    assert module.SIP_FEED == DataFeed.SIP.value
    assert module.MAX_WATCHLIST_SYMBOLS == 1000
    assert unit(live)["paper_slot"] == -7
    reader, pub = live.reader, live.publisher
    assert (reader.registry, reader.feed, reader.segment) == (pub.registry, module.SIP_FEED, SEG)
    assert (reader.method_version, reader.eligible_security_types) == (
        pub.method_version,
        pub.eligible_security_types,
    )
    assert (reader.universe_hash, reader.baseline_hash) == (
        pub.universe_hash,
        pub.baseline.identity,
    )
    assert live.provenance == {} and live.snapshot is live.error is None
    assert live.last_market == live.heartbeat_deadline == 0


def test_watchlist_bound_inclusive_and_registered(factory):
    live = factory(reg=registry(top_n=module.MAX_WATCHLIST_SYMBOLS))
    assert fresh(live)["status"] == "fresh"
    manifest = Path("docs/manifest.md").read_text()
    row = next(
        line for line in manifest.splitlines() if line.startswith("| `MAX_WATCHLIST_SYMBOLS` |")
    )
    assert "| symbols | 1000 |" in row and "UNCALIBRATED" in row and "2026-11-11" in row
    assert "www.tradingview.com" in row and "producer.py:76" in row


def test_trade_rebuilt_with_market_init_and_exact_provenance(factory, monkeypatch):
    live = factory()
    raw = tick(361, "2.70", name=NAMES[0])
    archived = TradeTick(
        raw.instrument_id,
        raw.price,
        raw.size,
        raw.aggressor_side,
        raw.trade_id,
        raw.ts_event,
        raw.ts_init + 80 * NS,
    )
    clocks = Clocks(raw.ts_event + 7, archived.ts_init, "zz-market")
    seen, original = [], live.publisher.on_trade

    def capture(view, **facts):
        seen.append((view, facts))
        original(view, **facts)

    monkeypatch.setattr(live.publisher, "on_trade", capture)
    assert observe(live, tick=archived, clocks=clocks, now=clocks.MARKET_AVAILABLE_AT)
    view, facts = seen[0]
    assert view.ts_init == clocks.MARKET_AVAILABLE_AT and view.ts_event == raw.ts_event
    assert (view.instrument_id, view.price, view.size, view.aggressor_side, view.trade_id) == (
        raw.instrument_id,
        raw.price,
        raw.size,
        raw.aggressor_side,
        raw.trade_id,
    )
    assert facts == {
        "known_at": clocks.MARKET_AVAILABLE_AT,
        "price_eligible": True,
        "volume_eligible": True,
    }
    assert live.last_market == clocks.MARKET_AVAILABLE_AT
    assert live.provenance == {
        NAMES[0]: {**asdict(clocks), "native_ts_init": archived.ts_init, "mask_source": "zz-mask"}
    }


def test_condition_code_does_not_override_caller_eligibility(factory, monkeypatch):
    live, seen = factory(), []
    monkeypatch.setattr(live.publisher, "on_trade", lambda view, **facts: seen.append(facts))
    assert observe(live, conditions=("G",))
    assert set(seen[0]) == {"known_at", "price_eligible", "volume_eligible"}
    assert seen[0]["price_eligible"] is seen[0]["volume_eligible"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("mask_source", ""),
        ("price_eligible", 1),
        ("volume_eligible", 0),
        ("conditions", []),
        ("conditions", ("",)),
        ("conditions", (1,)),
        ("clocks", Clocks(NOW, NOW, "zz-before-event")),
    ],
)
def test_trade_each_authority_or_clock_refusal_invalidates(factory, field, value):
    live = factory()
    fresh(live)
    with pytest.raises(ValueError, match="invalid market clocks or condition authority"):
        observe(live, **{field: value})
    poisoned(live, INVALID_TRADE)


@pytest.mark.parametrize("now", [True, float(NOW), SEG.start_ns - 1, SEG.end_ns])
def test_trade_each_admission_clock_refusal_invalidates(factory, now):
    live = factory()
    with pytest.raises(ValueError, match=SESSION):
        observe(live, now=now)
    poisoned(live, INVALID_TRADE)


def test_market_clock_cannot_regress_across_symbols_and_poison_never_recovers(factory):
    live = factory()
    assert observe(live, 362)
    with pytest.raises(ValueError, match="invalid market clocks"):
        observe(live, 361, tick=tick(361, name=NAMES[1]))
    poisoned(live, INVALID_TRADE)
    live.publisher.set_coverage(Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0))
    assert live.heartbeat(at=NOW, received_ns=NOW)
    assert unit(live)["status"] == "unavailable" and live.error == INVALID_TRADE


@pytest.mark.parametrize("method", ["on_trade", "on_snapshot"])
def test_future_market_clock_returns_false_without_invalidation(factory, method):
    live = factory()
    before = dict(live.publisher.states)
    clocks = Clocks(NOW + NS, NOW + NS, "zz-future")
    result = (
        observe(live, clocks=clocks, now=NOW)
        if method == "on_trade"
        else live.on_snapshot('{"ZZPBA":{}}', clocks=clocks, now=NOW)
    )
    assert result is False and live.error is None and live.publisher.states == before
    assert live.provenance == {} and live.snapshot is None
    fresh(live)


@pytest.mark.parametrize("kind", [ValueError, KeyError, TypeError, ArithmeticError])
def test_trade_publisher_error_classes_invalidate(factory, monkeypatch, kind):
    live = factory()

    def fail(*args, **kw):
        raise kind("zz-error")

    monkeypatch.setattr(live.publisher, "on_trade", fail)
    with pytest.raises(kind):
        observe(live)
    poisoned(live, INVALID_TRADE)


def test_trade_outside_universe_invalidates(factory):
    live = factory()
    with pytest.raises(KeyError):
        observe(live, tick=tick(361, name="ZZOTHER"))
    poisoned(live, INVALID_TRADE)


@pytest.mark.parametrize("method", ["on_trade", "on_snapshot"])
@pytest.mark.parametrize("cause", ["released", "invalidated"])
def test_admit_error_or_released_lease_raises_and_refusal_stays_poisoned(factory, method, cause):
    live = factory()
    fresh(live)
    if cause == "released":
        live.lease.close()
    else:
        live.invalidate("zz-gap")
    with pytest.raises(ValueError, match=ADMIT):
        if method == "on_trade":
            observe(live)
        else:
            live.on_snapshot('{"ZZPBA":{}}', clocks=MAPPING, now=NOW)
    poisoned(live, INVALID_TRADE if method == "on_trade" else INVALID_SNAPSHOT)


def test_snapshot_exact_text_diagnostics_only_duplicate_keys_accepted(factory):
    live = factory()
    raw = ' {"ZZPBA":{"p":1.123456789123},"ZZPBA":{"p":2}} '
    before, refs = dict(live.publisher.states), dict(live.publisher.references)
    assert live.on_snapshot(raw, clocks=MAPPING, now=NOW)
    assert live.snapshot == {**asdict(MAPPING), "response_json": raw}
    assert live.publisher.states == before and live.publisher.references == refs
    assert live.provenance == {}
    result = fresh(live)
    assert result["provenance"]["snapshot"] == live.snapshot


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        "null",
        "{}",
        '{"ZZOTHER":{}}',
        '{"ZZPBA":[]}',
        '{"ZZPBA":{"x":NaN}}',
        '{"ZZPBA":{"x":Infinity}}',
        '{"ZZPBA":{"x":-Infinity}}',
    ],
)
def test_snapshot_each_payload_refusal_invalidates(factory, raw):
    live = factory()
    fresh(live)
    with pytest.raises(ValueError):
        live.on_snapshot(raw, clocks=MAPPING, now=NOW)
    poisoned(live, INVALID_SNAPSHOT)


@pytest.mark.parametrize("now", [True, float(NOW), SEG.start_ns - 1, SEG.end_ns])
def test_snapshot_each_admission_clock_refusal_invalidates(factory, now):
    live = factory()
    with pytest.raises(ValueError, match=SESSION):
        live.on_snapshot('{"ZZPBA":{}}', clocks=MAPPING, now=now)
    poisoned(live, INVALID_SNAPSHOT)


def test_snapshot_market_regression_invalidates_but_equal_clock_is_accepted(factory):
    live = factory()
    raw = '{"ZZPBA":{}}'
    assert live.on_snapshot(raw, clocks=MAPPING, now=NOW)
    assert live.on_snapshot(raw + " ", clocks=MAPPING, now=NOW)
    with pytest.raises(ValueError, match="invalid or regressed snapshot"):
        live.on_snapshot(
            raw,
            clocks=replace(MAPPING, MARKET_AVAILABLE_AT=MAPPING.MARKET_AVAILABLE_AT - 1),
            now=NOW,
        )
    poisoned(live, INVALID_SNAPSHOT)


@pytest.mark.parametrize("kind", [KeyError, TypeError, ArithmeticError])
def test_snapshot_error_classes_invalidate(factory, monkeypatch, kind):
    live = factory()

    def fail(*args, **kw):
        raise kind("zz-error")

    monkeypatch.setattr(module.json, "loads", fail)
    with pytest.raises(kind):
        live.on_snapshot("{}", clocks=MAPPING, now=NOW)
    poisoned(live, INVALID_SNAPSHOT)


def test_heartbeat_deadline_only_when_reader_accepts_and_lease_held(factory):
    live = factory()
    assert live.heartbeat(at=NOW, received_ns=NOW)
    assert live.heartbeat_deadline == NOW + REG.seconds("heartbeat_max_age_s")
    assert not live.heartbeat(at=NOW + 1, received_ns=NOW)
    assert live.heartbeat_deadline == 0
    assert live.heartbeat(at=NOW, received_ns=NOW)
    live.lease.close()
    assert not live.heartbeat(at=NOW, received_ns=NOW) and live.heartbeat_deadline == 0


def test_fresh_document_key_by_key_and_captured_unit_stable(factory):
    live = factory()
    assert observe(live, 360, now=NOW)
    result = fresh(live)
    gen = live.reader.generation
    assert gen is not None
    assert result == {
        "schema": "sextant.j5-ranked/1",
        "display_only": True,
        "paper_slot": SLOT,
        "status": "fresh",
        "generation_sha256": generation_digest(gen),
        "params_hash": REG.params_hash,
        "generated_at_ns": gen.known_at,
        "expires_at_ns": min(
            gen.known_at + REG.seconds("generation_max_age_s"),
            gen.ts_event + REG.seconds("input_max_age_s"),
            live.heartbeat_deadline,
            SEG.end_ns,
        ),
        "ranking_generation": gen.to_dict(),
        "watchlists": {
            "NS Ranked": [MEMBERS[row.symbol] for row in gen.rows if row.rank is not None][
                : int(REG.top_n)
            ]
        },
        "provenance": {
            "sip_authority": AUTH,
            "trades": live.provenance.copy(),
            "snapshot": None,
            "mapping": asdict(MAPPING),
        },
    }
    before = json.dumps(result, sort_keys=True)
    assert observe(live)
    assert json.dumps(result, sort_keys=True) == before


@pytest.mark.parametrize(
    "case,reason",
    [
        ("no_heartbeat", "heartbeat_missing"),
        ("invalid_heartbeat", "heartbeat_missing"),
        ("coverage", "generation incomplete"),
        ("future_mapping", "mapping unavailable"),
        ("mapping_after_generation", "expired generation or future mapping"),
        ("expiry", "expired generation or future mapping"),
        ("end", SESSION),
        ("released", ADMIT),
        ("invalidated", ADMIT),
    ],
)
def test_build_unavailable_each_reason_alone(factory, case, reason):
    live = factory()
    kw = {}
    if case != "no_heartbeat":
        assert live.heartbeat(at=NOW, received_ns=NOW)
    if case == "invalid_heartbeat":
        assert not live.heartbeat(at=NOW + 1, received_ns=NOW)
    elif case == "coverage":
        live.publisher.set_coverage(Coverage(CoverageState.DEGRADED, False))
    elif case == "future_mapping":
        kw["mapping_clocks"] = replace(MAPPING, MARKET_AVAILABLE_AT=NOW + 1)
    elif case == "mapping_after_generation":
        fresh(live)
        kw = {"now": NOW + 1, "mapping_clocks": replace(MAPPING, MARKET_AVAILABLE_AT=NOW + 1)}
    elif case == "expiry":
        live.heartbeat_deadline = NOW
    elif case == "end":
        kw["now"] = SEG.end_ns
    elif case == "released":
        live.lease.close()
    elif case == "invalidated":
        live.invalidate("zz-gap")
    assert unit(live, **kw) == {
        "schema": "sextant.j5-ranked/1",
        "display_only": True,
        "paper_slot": SLOT,
        "status": "unavailable",
        "watchlists": None,
        "reason": reason,
    }


@pytest.mark.parametrize("limit", ["generation", "input", "heartbeat", "segment"])
def test_each_expiry_limit_is_uniquely_least(factory, limit):
    values = {"generation_max_age_s": 20, "input_max_age_s": 20, "heartbeat_max_age_s": 20}
    if limit in {"generation", "input", "heartbeat"}:
        values[limit + "_max_age_s"] = 1
    live = factory(reg=registry(**values))
    if limit == "segment":
        live.publisher.segment = live.reader.segment = replace(SEG, end_ns=NOW + NS)
    result = fresh(live)
    gen = live.reader.generation
    limits = [
        gen.known_at + live.publisher.registry.seconds("generation_max_age_s"),
        gen.ts_event + live.publisher.registry.seconds("input_max_age_s"),
        live.heartbeat_deadline,
        live.publisher.segment.end_ns,
    ]
    index = ["generation", "input", "heartbeat", "segment"].index(limit)
    assert limits[index] < min(v for i, v in enumerate(limits) if i != index)
    assert result["expires_at_ns"] == limits[index]


@pytest.mark.parametrize("partial,completed", [(True, True), (False, False)])
def test_selected_partial_or_incomplete_row_is_unavailable(factory, partial, completed):
    live = factory()
    fresh(live)
    live.reader.generation = row_change(
        live.reader.generation, partial=partial, completed=completed
    )
    assert unit(live)["reason"] == "partial display row"


@pytest.mark.parametrize("name", ["price", "move", "dvol_ratio", "trades_accel"])
@pytest.mark.parametrize("damage", ["event_missing", "value_missing", "stale", "future"])
def test_each_projected_feature_refusal(factory, name, damage):
    live = factory()
    fresh(live)
    kw = {
        "event_missing": {"ts_event": None},
        "value_missing": {"value": None},
        "stale": {"ts_event": NOW - REG.seconds("input_max_age_s") - 1},
        "future": {"ts_event": NOW + 1},
    }[damage]
    live.reader.generation = feature_change(live.reader.generation, name, **kw)
    assert unit(live)["reason"] == "projected symbol input stale"


@pytest.mark.parametrize(
    "case",
    ["empty", "missing", "unqualified", "null", "nontext", "alias", *sorted(REFUSED_PREFIXES)],
)
def test_each_selected_mapping_refusal_makes_whole_unit_unavailable(factory, case):
    live = factory()
    mapping: dict[str, Any] = dict(MEMBERS)
    if case == "empty":
        mapping = {}
    elif case == "missing":
        mapping.pop(NAMES[0])
    elif case == "unqualified":
        mapping[NAMES[0]] = NAMES[0]
    elif case == "null":
        mapping[NAMES[0]] = None
    elif case == "nontext":
        mapping[NAMES[0]] = 7
    elif case == "alias":
        mapping[NAMES[0]] = mapping[NAMES[1]]
    else:
        mapping[NAMES[0]] = case + ":" + NAMES[0]
    assert live.heartbeat(at=NOW, received_ns=NOW)
    assert unit(live, symbol_map=mapping)["reason"] == MAPPING_ERROR


def test_unselected_refused_prefix_ignored_and_short_selection_accepted(factory):
    live = factory(reg=registry(top_n=1))
    first = fresh(live)
    selected = first["watchlists"]["NS Ranked"]
    outside = next(s for s, qualified in MEMBERS.items() if qualified not in selected)
    mapping = MEMBERS | {outside: sorted(REFUSED_PREFIXES)[0] + ":" + outside}
    assert unit(live, symbol_map=mapping)["status"] == "fresh"
    # Independently inspect a fresh producer; no registry mutation is needed in production.
    live.lease.close()
    shorter = factory(reg=registry(top_n=100))
    assert len(fresh(shorter)["watchlists"]["NS Ranked"]) < 100


@pytest.mark.parametrize("value", [True, 2.5, D("2.5")])
def test_noninteger_top_n_refused_by_registry_before_producer(value):
    with pytest.raises(TypeError, match="top_n must be int"):
        registry(top_n=value)


def test_no_selected_rows_unavailable_but_absent_feature_object_is_ignored(factory):
    live = factory()
    fresh(live)
    gen = live.reader.generation
    live.reader.generation = row_change(
        gen, features=tuple(f for f in gen.rows[0].features if f.name != "price")
    )
    assert unit(live)["status"] == "fresh"  # kept: projection iterates only existing features
    live.reader.generation = replace(gen, rows=tuple(replace(r, rank=None) for r in gen.rows))
    assert unit(live)["reason"] == MAPPING_ERROR


def test_throttled_publish_keeps_older_generation_time(factory):
    live = factory()
    before = fresh(live)
    later = unit(live, now=NOW + 1)
    assert live.publisher.sequence == 1
    assert later["status"] == "fresh" and later["generated_at_ns"] == NOW
    assert later == before


@pytest.mark.parametrize("kind", [ValueError, KeyError, TypeError, ArithmeticError])
def test_build_catches_only_listed_errors_as_unavailable(factory, monkeypatch, kind):
    live = factory()

    def fail(**kw):
        raise kind("zz-error")

    monkeypatch.setattr(live.publisher, "publish", fail)
    assert unit(live)["reason"] == str(kind("zz-error"))


def test_build_attribute_error_escapes_from_nonmapping_symbol_map(factory):
    live = factory()
    fresh(live)
    with pytest.raises(AttributeError, match="has no attribute 'get'"):
        unit(live, symbol_map=[])


@pytest.mark.parametrize("available", [True, False])
def test_publish_unit_writes_exact_document_including_unavailable(factory, tmp_path, available):
    live = factory()
    if available:
        live.heartbeat(at=NOW, received_ns=NOW)
    path = tmp_path / "unit.json"
    result = live.publish_unit(path, now=NOW, symbol_map=MEMBERS, mapping_clocks=MAPPING)
    assert json.loads(path.read_text()) == result
    assert result["status"] == ("fresh" if available else "unavailable")


def test_provenance_retains_one_entry_per_seen_symbol_and_release_after_beat_refuses(factory):
    live = factory()
    for index, symbol in enumerate(NAMES):
        assert observe(live, tick=tick(361 + index, name=symbol))
    assert set(live.provenance) == set(NAMES)
    assert observe(live, 364)
    assert len(live.provenance) == len(
        NAMES
    )  # bounded by the caller's universe, no session eviction
    assert live.heartbeat(at=NOW, received_ns=NOW)
    live.lease.close()
    assert unit(live)["reason"] == ADMIT


def test_source_boundaries_no_clients_requests_condition_scope_or_slot_literals():
    tree = ast.parse(Path(module.__file__).read_text())
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert all(n != "sextant" and not n.startswith("sextant.") for n in imports)
    assert not any(
        any(
            x in n.split(".")
            for x in ("client", "requests", "socket", "urllib", "http", "mcp", "trading")
        )
        for n in imports
    )
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(
            node
        ):
            node.body = node.body[1:] or [ast.Pass()]
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert not set(constants) & {
        "G",
        "U",
        "Z",
        "6",
        "M",
        "9",
        *REFUSED_PREFIXES,
        "alpaca-market-data",
        "paper-slot-2",
    }
    assert constants.count("sip") == 1 and 2 not in constants
    assert not hasattr(module, "SCOPE")
    assert not hasattr(module, "snapshot_request") and not hasattr(module, "snapshot_pacer")


@pytest.mark.parametrize("price,volume", [(True, False), (False, True), (False, False)])
def test_publisher_receives_only_clock_and_caller_eligibility(factory, monkeypatch, price, volume):
    live, seen = factory(), []
    monkeypatch.setattr(live.publisher, "on_trade", lambda view, **facts: seen.append(facts))
    raw = tick(361, name=NAMES[0])
    assert observe(
        live, tick=raw, conditions=("ZZCOND",), price_eligible=price, volume_eligible=volume
    )
    assert seen == [{"known_at": raw.ts_event, "price_eligible": price, "volume_eligible": volume}]


@pytest.mark.parametrize(
    "reason", ["heartbeat_stale", "generation_missing", "generation_stale", "input_stale"]
)
def test_reader_unavailability_reason_survives_projection(factory, monkeypatch, reason):
    live = factory()
    fresh(live)
    monkeypatch.setattr(live.publisher, "publish", lambda **kw: None)
    if reason == "heartbeat_stale":
        live.reader._heartbeat = (NOW - 3 * NS, NOW - 3 * NS)
    elif reason == "generation_missing":
        live.reader.invalidate(reason)
    else:
        field = "known_at" if reason == "generation_stale" else "ts_event"
        live.reader.generation = replace(live.reader.generation, **{field: NOW - 3 * NS})
    assert unit(live)["reason"] == reason
