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
from sextant_movers.ranking.baseline import BaselineTable
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


def test_heartbeat_after_segment_end_is_accepted_but_display_is_not(factory):
    live = factory()
    end = SEG.end_ns + 1
    assert live.heartbeat(at=end, received_ns=end)
    assert live.heartbeat_deadline == end + REG.seconds("heartbeat_max_age_s")
    assert unit(live, now=end)["reason"] == SESSION


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


# Cut B: seeded replay; only the local child's process imports the reference.
def producer_inputs() -> dict[str, Any]:
    import random

    template = filled(reg=REG)
    table = template.baseline
    assert isinstance(table, BaselineTable)
    rng = random.Random(20261116)  # noqa: S311 - reproducible synthetic input
    start, runs = SEG.start_ns, []
    damage_names = (
        "mask",
        "price_flag",
        "volume_flag",
        "condition_list",
        "empty_condition",
        "nontext_condition",
        "event_future",
        "market_regression",
        "snapshot_empty",
        "snapshot_nan",
        "snapshot_foreign",
        "snapshot_regression",
        "universe",
        "released",
        "invalidate",
    )
    damage_cycle = []
    for seed in range(64):
        if seed % len(damage_names) == 0:
            damage_cycle = rng.sample(damage_names, len(damage_names))
        steps = []

        def add(call: str, *, steps: list = steps, **kw: Any) -> None:
            steps.append({"call": call, **kw})

        def trade(second: int, name: str, **kw: Any) -> None:
            at = start + second * NS
            add(
                "on_trade",
                name=name,
                event=at,
                market=at,
                acquired=at + 80 * NS,
                now=at,
                price="2.60"
                if second >= 360
                else "2.40"
                if second >= 300
                else "2.10"
                if second >= 60
                else "2.00",
                price_eligible=True,
                volume_eligible=True,
                conditions=[],
                mask_source="zz-mask",
                **kw,
            )

        # The reader's filled helper supplies the template; replay warms both native publishers
        # through their producers so admission, clocks and the resulting entire documents count.
        for second in (0, 60, 300, 360):
            for name in NAMES:
                trade(second, name)
        second = 360
        damage = damage_cycle[seed % len(damage_names)]
        damage_round = rng.randint(5, 12)
        for index in range(16):
            second += rng.choice((0, 1, 1, 2))
            at = start + second * NS
            # Mapping failures do not poison trade intake, unlike refused trades/snapshots.
            mapping = dict(MEMBERS)
            kind = rng.choice(("good", "good", "good", "missing", "alias", "bare", "refused"))
            if kind == "missing":
                mapping.pop(rng.choice(NAMES))
            elif kind == "alias":
                mapping[NAMES[0]] = mapping[NAMES[1]]
            elif kind == "bare":
                mapping[rng.choice(NAMES)] = "ZZBARE"
            elif kind == "refused":
                mapping[rng.choice(NAMES)] = "ALPACA:ZZOTHER"
            add("heartbeat", at=at, received_ns=at + rng.choice((0, 0, 1)))
            add(
                "build_unit",
                now=at,
                mapping=mapping,
                market=at + 1 if index % 7 == 5 else start - NS,
                acquired=at + 80 * NS,
            )
            text = rng.choice(
                ('{"ZZPBA":{}}', ' {"ZZPBB":{"p":1.23456789123}} ', '{"ZZPBA":{},"ZZPBA":{"v":2}}')
            )
            add(
                "on_snapshot",
                text=text,
                market=at + (1 if index % 5 == 4 else 0),
                acquired=at + 80 * NS,
                now=at,
            )
            trade(second, rng.choice(NAMES))
            last = steps[-1]
            last["price_eligible"], last["volume_eligible"] = rng.choice(
                ((True, True), (True, True), (False, True), (True, False), (False, False))
            )
            last["conditions"] = rng.choice(([], [], ["ZZCOND"]))
            if index == damage_round:
                if damage == "released":
                    add("release")
                elif damage == "invalidate":
                    add("invalidate", reason="zz-disconnect")
                elif damage.startswith("snapshot_"):
                    raw = {
                        "snapshot_empty": "{}",
                        "snapshot_nan": '{"ZZPBA":{"p":NaN}}',
                        "snapshot_foreign": '{"ZZOTHER":{}}',
                        "snapshot_regression": '{"ZZPBA":{}}',
                    }[damage]
                    add(
                        "on_snapshot",
                        text=raw,
                        market=start - NS if damage.endswith("regression") else at,
                        acquired=at + 80 * NS,
                        now=at,
                    )
                else:
                    trade(second, "ZZOTHER" if damage == "universe" else NAMES[0])
                    changes = {
                        "mask": {"mask_source": ""},
                        "price_flag": {"price_eligible": 1},
                        "volume_flag": {"volume_eligible": 0},
                        "condition_list": {"conditions_list": True},
                        "empty_condition": {"conditions": [""]},
                        "nontext_condition": {"conditions": [1]},
                        "event_future": {"market": at - 1},
                        "market_regression": {"market": at - 1, "event": at - 1},
                    }
                    steps[-1].update(changes.get(damage, {}))
        # Boundary observations, staleness and lease release after otherwise valid clocks.
        add("heartbeat", at=SEG.end_ns + 1, received_ns=SEG.end_ns + 1)
        add(
            "build_unit",
            now=SEG.end_ns,
            mapping=MEMBERS,
            market=start - NS,
            acquired=SEG.end_ns + 80 * NS,
        )
        runs.append(
            {
                "epoch": f"zz-producer-{seed}",
                "damage": damage,
                "registry": {
                    "top_n": rng.choice((1, 2, 3, 7)),
                    "publish_min_interval_s": rng.choice((1, 2, 4)),
                    "generation_max_age_s": rng.choice((1, 2, 5)),
                    "input_max_age_s": rng.choice((1, 2, 5)),
                    "heartbeat_max_age_s": rng.choice((1, 2, 5)),
                },
                "steps": steps,
            }
        )
    return {
        "method_version": template.method_version,
        "registry": [
            [
                r.name,
                r.unit,
                str(r.value),
                r.source,
                r.calibration_state,
                r.last_change.isoformat(),
                isinstance(r.value, D),
            ]
            for r in REG.rows
        ],
        "segment": [SEG.name, SEG.session_date.isoformat(), SEG.start_ns, SEG.end_ns, SEG.source],
        "baseline": {
            "samples": [
                [
                    s.symbol,
                    s.segment,
                    s.session,
                    s.offset_s,
                    str(s.dollar_volume),
                    str(s.trade_count),
                ]
                for s in table.samples
            ],
            "provenance": table.provenance,
            "known_at": table.known_at,
            "feed": table.feed,
        },
        "references": {
            s: [str(r.prior_close), r.known_at, r.source] for s, r in template.references.items()
        },
        "assets": {s: asdict(a) for s, a in template.assets.items() if a is not None},
        "universe": list(template.universe),
        "universe_source": template.universe_source,
        "scope": ["alpaca-market-data", "paper-slot-2"],
        "paper_slot": 2,
        "authority": AUTH,
        "runs": runs,
    }


def producer_replay(m: dict[str, Any], inputs: dict[str, Any], root: Path) -> list[Any]:
    """Shared replay body: compare every return or exception, whole canonical display JSON."""
    import json
    from datetime import date
    from decimal import Decimal
    from pathlib import Path

    nt = m["nt"]
    rows = tuple(
        m["RegistryRow"](
            n, u, Decimal(v) if decimal else int(v), source, state, date.fromisoformat(day)
        )
        for n, u, v, source, state, day, decimal in inputs["registry"]
    )
    name, day, start, end, source = inputs["segment"]
    segment = m["Segment"](name, date.fromisoformat(day), start, end, source)
    base = inputs["baseline"]
    baseline = m["BaselineTable"](
        tuple(
            m["BaselineSample"](s, g, day, offset, Decimal(v), Decimal(count))
            for s, g, day, offset, v, count in base["samples"]
        ),
        base["provenance"],
        base["known_at"],
        base["feed"],
    )
    answers = []
    for index, run in enumerate(inputs["runs"]):
        reg = m["Registry"](
            tuple(
                m["RegistryRow"](
                    r.name,
                    r.unit,
                    run["registry"].get(r.name, r.value),
                    r.source,
                    r.calibration_state,
                    r.last_change,
                )
                for r in rows
            )
        )
        pub = m["publisher"](
            epoch=run["epoch"],
            feed="sip",
            segment=segment,
            registry=reg,
            universe=tuple(inputs["universe"]),
            universe_source=inputs["universe_source"],
            baseline=baseline,
        )
        for symbol, (close, known, source) in inputs["references"].items():
            pub.set_symbol(
                symbol,
                references=m["ReferencePrices"](Decimal(close), known, source),
                asset=m["AssetState"](**inputs["assets"][symbol]),
            )
        size = len(inputs["universe"])
        pub.set_coverage(m["Coverage"](m["CoverageState"].READY, True, size, size, 0, 0, 0))
        scope = m["scope"](*inputs["scope"])
        lease = m["WriterLease"](scope, Path(root) / str(index), "zz-producer")
        lease.acquire()
        observed = []
        try:
            live = m["producer"](
                pub,
                lease,
                scope=scope,
                paper_slot=inputs["paper_slot"],
                sip_authority=inputs["authority"],
            )
            for step in run["steps"]:
                call = step["call"]
                try:
                    if call == "heartbeat":
                        result = live.heartbeat(at=step["at"], received_ns=step["received_ns"])
                    elif call in {"on_trade", "on_snapshot", "build_unit"}:
                        clocks = m["Clocks"](step["market"], step["acquired"], "zz-clock")
                        if call == "build_unit":
                            result = live.build_unit(
                                now=step["now"], symbol_map=step["mapping"], mapping_clocks=clocks
                            )
                            result = json.dumps(result, sort_keys=True, separators=(",", ":"))
                        elif call == "on_snapshot":
                            result = live.on_snapshot(step["text"], clocks=clocks, now=step["now"])
                        else:
                            iid = nt.InstrumentId.from_str(step["name"] + ".SIM")
                            tick = nt.TradeTick(
                                iid,
                                nt.Price.from_str(step["price"]),
                                nt.Quantity.from_int(10),
                                nt.AggressorSide.BUY,
                                nt.TradeId("zz"),
                                step["event"],
                                step["acquired"],
                            )
                            conditions = (
                                step["conditions"]
                                if step.get("conditions_list")
                                else tuple(step["conditions"])
                            )
                            result = live.on_trade(
                                tick,
                                clocks=clocks,
                                now=step["now"],
                                price_eligible=step["price_eligible"],
                                volume_eligible=step["volume_eligible"],
                                conditions=conditions,
                                mask_source=step["mask_source"],
                            )
                    elif call == "release":
                        result = lease.close()
                    else:
                        assert call == "invalidate"
                        result = live.invalidate(step["reason"])
                except Exception as exc:
                    result = ["raised", type(exc).__name__, str(exc)]
                observed.append(
                    [
                        call,
                        result,
                        live.error,
                        live.reader.reason,
                        live.heartbeat_deadline,
                        pub.coverage.state.value,
                        pub.coverage.complete,
                    ]
                )
        finally:
            lease.close()
        answers.append(observed)
    return answers


def producer_answers(tmp_path: Path) -> list[Any]:
    import nautilus_trader.model as nt
    from test_publisher import EDGES, TYPES

    from sextant_movers.ranking.baseline import BaselineSample, BaselineTable, Segment
    from sextant_movers.ranking.engine import ReferencePrices
    from sextant_movers.ranking.publisher import AssetState, RankingPublisher
    from sextant_movers.ranking.registry import Registry, RegistryRow

    inputs = producer_inputs()
    m = {
        "nt": nt,
        "Registry": Registry,
        "RegistryRow": RegistryRow,
        "Segment": Segment,
        "BaselineSample": BaselineSample,
        "BaselineTable": BaselineTable,
        "ReferencePrices": ReferencePrices,
        "AssetState": AssetState,
        "Coverage": Coverage,
        "CoverageState": CoverageState,
        "scope": AccountScope,
        "WriterLease": WriterLease,
        "Clocks": Clocks,
        "producer": LiveProducer,
    }
    m["publisher"] = lambda **kw: RankingPublisher(
        send=lambda *args: None,
        method_version=inputs["method_version"],
        eligible_security_types=TYPES,
        price_band_edges=EDGES,
        **kw,
    )
    return producer_replay(m, inputs, tmp_path)


def producer_counts(answers: list[Any]) -> dict[str, int]:
    steps = [s for run in answers for s in run]
    # A raised call is recorded as a list, not as a document's text: it counts under "raises".
    documents = [json.loads(s[1]) for s in steps if s[0] == "build_unit" and isinstance(s[1], str)]
    return {
        "sessions": len(answers),
        "calls": len(steps),
        "fresh": sum(d["status"] == "fresh" for d in documents),
        "unavailable": sum(d["status"] == "unavailable" for d in documents),
        "raises": sum(isinstance(s[1], list) and s[1][0] == "raised" for s in steps),
    }


def test_counts_take_a_raised_build_unit():
    counts = producer_counts([[["build_unit", ["raised", "ValueError", "zz"], None]]])
    assert counts == {"sessions": 1, "calls": 1, "fresh": 0, "unavailable": 0, "raises": 1}


PRODUCER_DIGEST = "636612c83a0c9b1c1e11e40651ea7ae04756cc22cde37f087504e0cc2888fc94"


def test_seeded_producer_answers_are_pinned(tmp_path):
    import hashlib

    answers = producer_answers(tmp_path)
    counts = producer_counts(answers)
    assert counts["sessions"] >= 60 and counts["calls"] >= 2000
    assert counts["fresh"] >= 30 and counts["unavailable"] >= 500 and counts["raises"] >= 500
    inputs = producer_inputs()
    assert {run["damage"] for run in inputs["runs"]} >= {
        "condition_list",
        "empty_condition",
        "nontext_condition",
        "market_regression",
        "snapshot_regression",
        "snapshot_nan",
        "released",
        "invalidate",
    }
    excluded = {"G", "U", "Z", "6", "M", "9"}
    assert all(
        not excluded.intersection(step.get("conditions", []))
        for run in inputs["runs"]
        for step in run["steps"]
    )
    assert {s[0] for run in answers for s in run} >= {
        "on_trade",
        "on_snapshot",
        "heartbeat",
        "build_unit",
    }
    digest = hashlib.sha256(json.dumps(answers, sort_keys=True, separators=(",", ":")).encode())
    print(f"producer answers: {counts}, digest {digest.hexdigest()}")
    assert digest.hexdigest() == PRODUCER_DIGEST


PRODUCER_PARITY = r"""
import hashlib, json, os, sys
from pathlib import Path
from tempfile import TemporaryDirectory
src = os.environ["MOVERS_REFERENCE_SRC"]
blobs = {
    "tradingview/producer.py": "02d58845621c786a1ee706e45f6cf7842cffd43a",
    "ranking/reader.py": "70cac528ca4ad16af02fde70c75b74126e358cc7",
    "execution/custody.py": "76d0f8852f233d7b6d9f150b422a6aa08237715e",
}
for path, expected in blobs.items():
    blob = (Path(src) / "sextant" / path).read_bytes()
    assert hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest() == expected, path
sys.path.insert(0, src)
import nautilus_trader
import nautilus_trader.model as nt
from sextant.tradingview import producer as pr
from sextant.ranking import baseline as bl, engine as en, publisher as pb, registry as rg
from sextant.ranking import generation as g, reader as rd
from sextant.execution import custody as cu
assert os.path.samefile(pr.__file__, Path(src) / "sextant/tradingview/producer.py")
inputs = json.loads(sys.stdin.read())
pb.METHOD = rd.METHOD = inputs["method_version"]
assert [pr.SCOPE.broker, pr.SCOPE.account] == inputs["scope"]
assert pr.DISPLAY_PAPER_SLOT == inputs["paper_slot"]
class Bus:
    def publish(self, *args, **kw):
        pass

def producer(pub, lease, *, scope, paper_slot, sip_authority):
    assert scope == pr.SCOPE and paper_slot == pr.DISPLAY_PAPER_SLOT
    return pr.LiveProducer(pub, lease, sip_authority=sip_authority)

m = {"nt": nt, "Registry": rg.Registry, "RegistryRow": rg.RegistryRow,
     "Segment": en.Segment, "BaselineSample": bl.BaselineSample,
     "BaselineTable": bl.BaselineTable, "ReferencePrices": en.ReferencePrices,
     "AssetState": pb.AssetState, "Coverage": g.Coverage, "CoverageState": g.CoverageState,
     "scope": cu.AccountScope, "WriterLease": cu.WriterLease, "Clocks": pr.Clocks,
     "producer": producer, "publisher": lambda **kw: pb.RankingPublisher(bus=Bus(), **kw)}
versions = [list(sys.version_info[:3]), nautilus_trader.__version__]
assert versions == inputs.pop("versions"), "Python/engine version mismatch"
with TemporaryDirectory() as temporary:
    answers = producer_replay(m, inputs, Path(temporary))
print("PARITY " + json.dumps({"versions": versions, "answers": answers}))
"""


@pytest.mark.local
def test_producer_parity_with_the_reference_in_a_child_interpreter(tmp_path):
    import inspect
    import sys

    import nautilus_trader
    import reference_child as child

    src, python = child.reference()
    here = [list(sys.version_info[:3]), nautilus_trader.__version__]
    script = "from typing import Any\nfrom pathlib import Path\n"
    script += inspect.getsource(producer_replay) + PRODUCER_PARITY
    theirs = child.run(
        python, src, script, stdin=json.dumps(producer_inputs() | {"versions": here})
    )
    assert here == theirs["versions"], f"version mismatch: {here}, {theirs['versions']}"
    ours = producer_answers(tmp_path)
    print(f"\npython, engine: here {here}, reference {theirs['versions']}")
    print(f"producer parity: {producer_counts(ours)}")
    assert theirs["answers"] == ours
