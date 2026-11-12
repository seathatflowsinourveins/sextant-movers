"""Part 6 host proofs: made-up bars, publishers and temporary leases; no transport.

Acceptance: the reference, src/sextant/tradingview/host.py:10-275 at
06562bec0898a7203f4eaf69086ced83d2477e6b. Refusals and retained edges are deliberate.
"""

import ast
import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from nautilus_trader.model import AggressorSide, InstrumentId, Price, Quantity, TradeId, TradeTick
from test_producer import AUTH, CONSTRUCTOR, MEMBERS, SCOPE, SLOT
from test_publisher import NAMES, NS, ready, registry

from sextant_movers.lease import AccountScope, WriterLease
from sextant_movers.ranking.bars import MINUTE_NS, Bar, BarPoll
from sextant_movers.ranking.engine import rank_movers
from sextant_movers.ranking.generation import Coverage, CoverageState
from sextant_movers.tradingview import host as module
from sextant_movers.tradingview.host import BarFidelityHost, load_symbol_map
from sextant_movers.tradingview.inputs import REFUSED_PREFIXES
from sextant_movers.tradingview.producer import Clocks

NAME = sorted(NAMES)[0]
OTHER = sorted(NAMES)[1]
START = ready().segment.start_ns
MAP_ERROR = "empty ranking or missing/ambiguous exact mapping"


@pytest.fixture
def factory(tmp_path, request):
    made = []

    def make(*, pub=None, **over):
        lease = WriterLease(SCOPE, tmp_path / str(len(made)), "zz-host")
        lease.acquire()
        request.addfinalizer(lease.close)
        made.append(lease)
        return BarFidelityHost(
            ready() if pub is None else pub,
            **(
                {
                    "lease": lease,
                    "scope": SCOPE,
                    "paper_slot": SLOT,
                    "sip_authority": AUTH,
                    "symbol_map": MEMBERS,
                }
                | over
            ),
        )

    return make


def bar(minute=0, *, symbol=NAME, **over):
    return Bar(
        **(
            {
                "symbol": symbol,
                "start_ns": START + minute * MINUTE_NS,
                "open": D("1.90"),
                "close": D("2.10"),
                "volume": 10,
                "trades": 3,
            }
            | over
        )
    )


def ingest(host, bars=(), *, minute=1, requested=NAMES, failed=(), **over):
    at = START + minute * MINUTE_NS
    return host.ingest(
        BarPoll(tuple(bars), tuple(requested), tuple(failed)),
        **({"market_ns": at, "received_ns": at} | over),
    )


def refuse(message, call):
    with pytest.raises(ValueError) as caught:
        call()
    assert str(caught.value) == message


@pytest.mark.parametrize("mapping", [{}, [], None])
def test_constructor_refuses_missing_mapping(factory, mapping):
    refuse("a verified symbol map is required", lambda: factory(symbol_map=mapping))


@pytest.mark.parametrize("key", ["scope", "paper_slot"])
def test_scope_and_slot_required_keywords(factory, key):
    host = factory()
    kw: dict[str, Any] = dict(
        lease=host.lease, scope=SCOPE, paper_slot=SLOT, sip_authority=AUTH, symbol_map=MEMBERS
    )
    kw.pop(key)
    with pytest.raises(TypeError, match=key):
        BarFidelityHost(host.publisher, **kw)


def test_wrong_scope_raises_producers_own_refusal(factory):
    refuse(CONSTRUCTOR, lambda: factory(scope=AccountScope("zz-other", "zz-other")))


def test_slot_reaches_document_unchanged(factory, tmp_path):
    host = factory(paper_slot=-17)
    assert host.producer.paper_slot == -17
    assert host.publish(tmp_path / "unit", now=START + MINUTE_NS)["paper_slot"] == -17


@pytest.mark.parametrize("field", ["market_ns", "received_ns"])
@pytest.mark.parametrize("value", [True, 1.0])
def test_each_clock_must_be_integer(factory, field, value):
    refuse(
        "market and receive clocks must be integers", lambda: ingest(factory(), **{field: value})
    )


def test_receive_equal_accepted_one_nanosecond_before_refused(factory):
    host = factory()
    assert ingest(host, [bar()]) == 1
    refuse(
        "the receive clock never precedes the market clock",
        lambda: ingest(host, received_ns=START + MINUTE_NS - 1),
    )


def test_poll_unknown_requested_name_refused(factory):
    refuse(
        "bar poll names symbols outside the universe: ['ZZOTHER']",
        lambda: ingest(factory(), requested=("ZZOTHER",)),
    )


def test_bar_outside_universe_refused_independently(factory):
    refuse("bar outside the universe: ZZOTHER", lambda: ingest(factory(), [bar(symbol="ZZOTHER")]))


def test_bar_at_segment_start_accepted_one_minute_before_refused(factory):
    host = factory()
    assert ingest(host, [bar()]) == 1
    refuse(f"bar outside the segment: {NAME}", lambda: ingest(factory(), [bar(-1)]))


def test_bar_closing_at_market_accepted_one_minute_after_refused(factory):
    assert ingest(factory(), [bar()]) == 1
    refuse(f"bar close is after the market clock: {NAME}", lambda: ingest(factory(), [bar(1)]))


@pytest.mark.parametrize("past", [0, 1])
def test_bar_closing_at_or_after_segment_end_skipped_before_future_check(factory, past):
    host = factory()
    end = host.publisher.segment.end_ns
    item = bar(start_ns=end + (past - 1) * MINUTE_NS)
    assert ingest(host, [item]) == 0
    assert host.producer.error is None and host._last_bar == {}


def test_missing_reference_close_refused(factory):
    host = factory()
    host.publisher.references.pop(NAME)
    refuse("a reference close is required before the first bar", lambda: ingest(host, [bar()]))


def test_missing_prior_close_refused(factory):
    host = factory()
    host.publisher.references[NAME] = replace(host.publisher.references[NAME], prior_close=None)
    refuse("a reference close is required before the first bar", lambda: ingest(host, [bar()]))


def test_identical_repeat_and_older_bar_skipped(factory):
    host = factory()
    assert ingest(host, [bar(1)], minute=2) == 1
    before = host.publisher.states[NAME]
    assert ingest(host, [bar(1), bar()], minute=2) == 0
    assert host.publisher.states[NAME] is before


@pytest.mark.parametrize("over", [{"close": D("2.20")}, {"volume": 11}, {"trades": 4}])
def test_revised_bar_invalidates_epoch(factory, over):
    host = factory()
    ingest(host, [bar()])
    refuse(f"revised bar requires a new epoch: {NAME}", lambda: ingest(host, [bar(**over)]))
    assert host.producer.error == "a revised bar requires a new publisher epoch"
    assert host.publisher.coverage == Coverage(CoverageState.DEGRADED, False)


def test_open_and_vwap_revision_ignored(factory):
    host = factory()
    ingest(host, [bar()])
    assert ingest(host, [bar(open=D("9"), vw=D("9"))]) == 0


def test_zero_volume_remembered_without_feed(factory):
    host = factory()
    assert ingest(host, [bar(volume=0)]) == 0
    assert NAME in host._last_bar and NAME not in host._anchored
    assert host.publisher.states[NAME].latest is None


def test_zero_volume_revised_to_volume_invalidates(factory):
    host = factory()
    ingest(host, [bar(volume=0)])
    refuse(f"revised bar requires a new epoch: {NAME}", lambda: ingest(host, [bar()]))


def test_missing_reference_is_remembered_and_retry_is_skipped(factory):
    host = factory()
    ref = host.publisher.references.pop(NAME)
    refuse("a reference close is required before the first bar", lambda: ingest(host, [bar()]))
    host.publisher.references[NAME] = ref
    assert ingest(host, [bar()]) == 0
    assert host.publisher.states[NAME].latest is None
    assert host.publisher.coverage.state is CoverageState.READY


def test_mid_batch_error_retains_earlier_feed_without_setting_coverage(factory, monkeypatch):
    host = factory()
    setter = Mock(wraps=host.publisher.set_coverage)
    monkeypatch.setattr(host.publisher, "set_coverage", setter)
    host.publisher.references.pop(OTHER)
    refuse(
        "a reference close is required before the first bar",
        lambda: ingest(host, [bar(symbol=OTHER), bar()]),
    )
    assert host.publisher.states[NAME].latest is not None
    assert host.publisher.states[OTHER].latest is None
    setter.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_failed_or_unrequested_symbol_holds_earliest_gap(factory, failed):
    host = factory()
    kw: dict[str, Any] = (
        {"failed": (NAME,)} if failed else {"requested": tuple(n for n in NAMES if n != NAME)}
    )
    ingest(host, **kw)
    ingest(host, minute=2, **kw)
    assert host._gaps == {NAME: START + MINUTE_NS}
    assert ingest(host, [bar(1)], minute=2) == 0
    assert host.publisher.coverage == Coverage(CoverageState.DEGRADED, False, 3, 2, 1, 0, 0)
    assert ingest(host, [bar(), bar(1)], minute=2) == 2
    assert host._gaps == {}
    assert host.publisher.coverage == Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0)


def test_gap_clears_only_complete_poll_at_same_market_clock(factory):
    host = factory()
    ingest(host, failed=(NAME,))
    ingest(host, minute=2)
    assert host._gaps == {NAME: START + MINUTE_NS}
    assert host.publisher.coverage.state is CoverageState.DEGRADED
    ingest(host)
    assert host._gaps == {} and host.publisher.coverage.state is CoverageState.READY


def test_older_bar_before_gap_skipped(factory):
    host = factory()
    ingest(host, minute=2, failed=(NAME,))
    assert ingest(host, [bar()], minute=3) == 0
    assert host._gaps == {NAME: START + 2 * MINUTE_NS}


def test_ready_all_counts_and_returned_bar_count(factory):
    host = factory()
    assert ingest(host, [bar(symbol=n) for n in NAMES]) == len(NAMES)
    assert host.publisher.coverage == Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0)


def test_eligibility_masks_and_trade_ids_for_anchor_and_print(factory, monkeypatch):
    host = factory()
    seen = []
    original = host.producer.on_trade

    def record(tick, **facts):
        seen.append((tick, facts))
        return original(tick, **facts)

    monkeypatch.setattr(host.producer, "on_trade", record)
    assert ingest(host, [bar()]) == 1
    assert [str(t.trade_id) for t, _ in seen] == [f"anchor:{NAME}", f"bar:{NAME}:{START}"]
    for tick, facts in seen:
        assert facts == dict(
            clocks=Clocks(START + MINUTE_NS, START + MINUTE_NS, module.RECEIPT_SOURCE),
            now=START + MINUTE_NS,
            price_eligible=True,
            volume_eligible=True,
            conditions=(),
            mask_source="sip minute bars carry no condition or aggressor mask",
        )
        assert tick.aggressor_side == AggressorSide.NO_AGGRESSOR
        assert str(tick.instrument_id.venue) == module.VENUE == "SIM"


def test_anchor_once_open_at_start_else_prior_close_and_dollar_volume_removed(factory):
    host = factory()
    ingest(host, [bar(), bar(1, symbol=OTHER)], minute=2)
    for name, price in [(NAME, D("1.90")), (OTHER, D("2"))]:
        state = host.publisher.states[name]
        anchor = state.history.price_at(START)
        assert anchor.price.as_decimal() == price and anchor.size.as_decimal() == 1
        assert state.segment_dollar_volume == D("21")
    ingest(host, [bar(2)], minute=3)
    assert (
        len(
            [
                t
                for b in host.publisher.states[NAME].history.buckets
                for t in b.ticks
                if str(t.trade_id).startswith("anchor:")
            ]
        )
        == 1
    )


def test_post_anchor_one_nanosecond_inside_and_covered(factory):
    pub = ready()
    pub.segment = replace(pub.segment, name="POST")
    host = factory(pub=pub)
    ingest(host, [bar()])
    state = pub.states[NAME]
    anchor = state.history.price_at(START + 1)
    assert anchor is not None
    assert anchor.ts_event == START + 1 and state.segment_covered
    assert state.segment_dollar_volume == D("21")


@pytest.mark.parametrize("count", [0, 1, 5])
def test_counted_twins_plus_one_print_including_zero_trade_count(factory, count):
    host = factory()
    ingest(host, [bar(trades=count)])
    state = host.publisher.states[NAME]
    assert state.volume_history.totals(START, START + MINUTE_NS) == (D("21"), max(count, 1))
    ticks = state.volume_history.buckets[-1].ticks
    assert len(ticks) == max(count, 1)
    assert [t.size.as_decimal() for t in ticks] == [D(1)] * max(count - 1, 0) + [D(10)]


@pytest.mark.parametrize(
    "over", [{"move_1m_window_s": 900}, {"move_5m_window_s": 901}, {"trades_accel_window_s": 902}]
)
def test_keep_s_derived_from_three_registry_windows(factory, over):
    reg = registry(**over)
    host = factory(pub=ready(registry=reg))
    assert (
        host.keep_s
        == max(
            int(reg.move_1m_window_s),
            int(reg.move_5m_window_s),
            int(reg.trades_accel_window_s) + 60 * int(reg.trades_accel_baseline_min),
        )
        + 1
    )


def test_poll_refused_heartbeat_raises(factory, tmp_path, monkeypatch):
    host = factory()
    monkeypatch.setattr(host.producer, "heartbeat", lambda **kw: False)
    refuse(
        "the heartbeat was refused",
        lambda: host.poll(
            BarPoll((), NAMES), market_ns=START, received_ns=START, path=tmp_path / "unit"
        ),
    )


@pytest.mark.parametrize("late", [0, 1])
def test_poll_publishes_at_later_clock(factory, tmp_path, monkeypatch, late):
    host = factory()
    publish = Mock(return_value={"zz": True})
    monkeypatch.setattr(host.producer, "publish_unit", publish)
    at, path = START + MINUTE_NS, tmp_path / "unit"
    assert host.poll(BarPoll((bar(),), NAMES), market_ns=at, received_ns=at + late, path=path) == (
        1,
        {"zz": True},
    )
    publish.assert_called_once_with(
        path,
        now=at + late,
        symbol_map=MEMBERS,
        mapping_clocks=Clocks(at + late, at + late, module.RECEIPT_SOURCE),
    )


def test_publish_explicit_mapping_clocks_and_default_clocks(factory, tmp_path, monkeypatch):
    host = factory()
    publish = Mock(return_value={})
    monkeypatch.setattr(host.producer, "publish_unit", publish)
    clocks = Clocks(START, START + NS, "zz-map")
    host.publish(tmp_path / "unit", now=START + NS, mapping_clocks=clocks)
    assert publish.call_args.kwargs["mapping_clocks"] is clocks


@pytest.mark.parametrize(
    "raw,message",
    [
        ("{", "the symbol map must be readable JSON"),
        ("{}", "the symbol map must be a non-empty JSON object"),
        ("[]", "the symbol map must be a non-empty JSON object"),
        ('{"":null}', "symbol map keys must be tickers"),
        ('{"ZZMAP":17}', "symbol map values must be exchange-prefixed mappings"),
        ('{"ZZMAP":"bare"}', "symbol map values must be exchange-prefixed mappings"),
        ('{"ZZMAP":"ZZX:ZZMAP","ZZOTHER":"ZZX:ZZMAP"}', "symbol map targets must be unique"),
    ],
)
def test_symbol_map_each_refusal_alone(tmp_path, raw, message):
    path = tmp_path / "map"
    path.write_text(raw, encoding="utf-8")
    refuse(message, lambda: load_symbol_map(path))


@pytest.mark.parametrize("prefix", sorted(REFUSED_PREFIXES))
def test_symbol_map_shared_routing_prefix_refused(tmp_path, prefix):
    path = tmp_path / "map"
    path.write_text(json.dumps({NAME: prefix + ":" + NAME}), encoding="utf-8")
    refuse("symbol map values need a venue exchange, not routing", lambda: load_symbol_map(path))


def test_symbol_map_missing_file_and_non_utf8_caught_as_value_error(tmp_path):
    path = tmp_path / "map"
    for raw in [None, b"\xff"]:
        if raw is not None:
            path.write_bytes(raw)
        with pytest.raises(ValueError) as caught:
            load_symbol_map(path)
        assert str(caught.value) == "the symbol map must be readable JSON"
        assert isinstance(
            caught.value.__cause__, FileNotFoundError if raw is None else UnicodeDecodeError
        )


def test_map_extra_name_null_and_duplicate_json_keys_retained(tmp_path):
    path = tmp_path / "map"
    path.write_text('{"ZZOTHER":"ZZX:ZZOTHER","ZZMAP":"ZZX:ZZOLD","ZZMAP":null}', encoding="utf-8")
    assert load_symbol_map(path) == {"ZZOTHER": "ZZX:ZZOTHER", "ZZMAP": None}


def test_independent_direct_replay_ranks_and_no_venue_in_document(factory, tmp_path):
    host, direct = factory(), ready()
    unit: dict[str, Any] = {}
    at = START
    for minute in range(8):
        items = [
            bar(minute, symbol=n, close=D(2) + D(minute + i) / 100, trades=3 + i)
            for i, n in enumerate(sorted(NAMES))
        ]
        at = START + (minute + 1) * MINUTE_NS
        for item in items:

            def tick(price, size, event, identity, item=item):
                return TradeTick(
                    InstrumentId.from_str(item.symbol + ".ZZVENUE"),
                    Price.from_str(format(price, "f")),
                    Quantity.from_int(size),
                    AggressorSide.NO_AGGRESSOR,
                    TradeId(identity),
                    event,
                    event,
                )

            if minute == 0:
                direct.on_trade(tick(item.open, 1, START, "zz-anchor"), known_at=at)
                state = direct.states[item.symbol]
                direct.states[item.symbol] = replace(
                    state, segment_dollar_volume=state.segment_dollar_volume - item.open
                )
            printed = tick(item.close, item.volume, at, "zz-print")
            state = direct.states[item.symbol]
            direct.states[item.symbol] = replace(
                state,
                volume_history=state.volume_history.append_count(
                    printed, max(item.trades - 1, 0), host.keep_s
                ),
            )
            direct.on_trade(printed, known_at=at)
        assert ingest(host, items, minute=minute + 1) == len(items)

        def ranks(pub):
            return [
                (r.symbol, r.rank)
                for r in rank_movers(s.latest for s in pub.states.values() if s.latest is not None)
            ]

        assert ranks(host.publisher) == ranks(direct)
        host.heartbeat(at=at, received_ns=at)
        unit = host.publish(tmp_path / "unit", now=at)
        if minute >= 6:
            assert unit["status"] == "fresh", unit
            assert unit["paper_slot"] == SLOT
            assert all("venue" not in key.lower() for key in unit)
            assert ".SIM" not in json.dumps(unit) and "ZZVENUE" not in json.dumps(unit)
    selected = unit["watchlists"]["NS Ranked"][0].split(":", 1)[1]
    host.symbol_map[selected] = None
    assert host.publish(tmp_path / "unit", now=at + NS)["reason"] == MAP_ERROR


def test_price_precision_and_quantity_overflow_remembered(factory):
    for over in [{"close": D("2.123456789012345678901")}, {"volume": 10**40}]:
        host = factory()
        with pytest.raises((ValueError, OverflowError)):
            ingest(host, [bar(**over)])
        assert NAME in host._last_bar and NAME in host._anchored
        assert ingest(host, [bar(**over)]) == 0


def test_source_boundaries_and_departures():
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    forbidden = {
        "sextant",
        "trading",
        "client",
        "socket",
        "urllib",
        "http",
        "requests",
        "alpaca",
        "mcp",
        "time",
        "threading",
    }
    assert not any(forbidden.intersection(name.split(".")) for name in imports)
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(n):
            n.body = n.body[1:]
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert not set(constants).intersection(
        REFUSED_PREFIXES | {"alpaca-market-data", "paper-slot-2"}
    )
    assert 2 not in constants
    assert not hasattr(module, "SCOPE") and not hasattr(module, "DISPLAY_PAPER_SLOT")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    attrs = [n.func.attr for n in calls if isinstance(n.func, ast.Attribute)]
    assert not {"now", "sleep", "Timer", "Thread", "write_text", "open"}.intersection(attrs)
    assert attrs.count("read_text") == attrs.count("publish_unit") == 1
    read = next(
        n for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "read_text"
    )
    assert [(kw.arg, ast.literal_eval(kw.value)) for kw in read.keywords] == [("encoding", "utf-8")]
    loads = [
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "self"
        and isinstance(n.ctx, ast.Load)
    ]
    assert "lease" not in loads and "last_close" in loads and "symbol_map" in loads
    # last_close is subscript assignment only; symbol_map is read by publish, lease only stored.
    assert source.count("self.last_close") == 2
    assert source.count("self.lease") == 1
    assert module.RECEIPT_SOURCE == ("bar fidelity SIP minute bars through the shared REST pacer")
    assert module.Bar is Bar and module.BarPoll is BarPoll
    assert module.WriterLease is WriterLease and module.AccountScope is AccountScope
    assert module.REFUSED_PREFIXES is REFUSED_PREFIXES


# Cut B: seeded sessions. Only the isolated reference child imports the reference.
def host_inputs():
    import random

    from test_producer import producer_inputs

    template = producer_inputs()
    rng = random.Random(20261121)  # noqa: S311 - reproducible synthetic polls
    runs = []
    kinds = (
        "failed",
        "unrequested",
        "repeat",
        "older",
        "zero",
        "missing",
        "late",
        "early_receive",
        "before_start",
        "future_close",
    )
    for index in range(60):
        segment = ("PRE", "RTH", "POST")[index % 3]
        start = START + (index % 3) * 6 * 3600 * NS
        events = dict(
            zip(rng.sample(range(8, 30), len(kinds)), rng.sample(kinds, len(kinds)), strict=True)
        )
        revision_minute = rng.randint(31, 33)
        steps = []
        last = {}
        backfill = []

        def row(minute, name, start=start):
            return {
                "symbol": name,
                "start_ns": start + minute * MINUTE_NS,
                "open": "1.90",
                "close": str(D(2) + D(minute) / 100 + D(NAMES.index(name)) / 1000),
                "volume": rng.randint(1, 30),
                "trades": rng.randint(0, 7),
            }

        for minute in range(36):
            at = start + (minute + 1) * MINUTE_NS
            bars = [row(minute, name) for name in NAMES]
            requested, failed = list(NAMES), []
            received = at + rng.choice((0, 0, 0, 1))
            name = rng.choice(NAMES)
            kind = "revised" if minute == revision_minute else events.get(minute, "complete")
            carried, backfill = backfill, []
            missing = None
            if kind in {"failed", "unrequested"}:
                backfill = [b for b in bars if b["symbol"] == name]
                bars = [b for b in bars if b["symbol"] != name]
                if kind == "failed":
                    failed = [name]
                else:
                    requested.remove(name)
            elif kind == "repeat":
                bars.append(dict(last[name]))
            elif kind == "older":
                bars.append(row(0, name))
            elif kind == "zero":
                next(b for b in bars if b["symbol"] == name)["volume"] = 0
            elif kind == "missing":
                missing = name
            elif kind == "late":
                received = at + rng.choice((1, NS, 3 * NS))
            elif kind == "early_receive":
                received = at - 1
            elif kind == "before_start":
                bars.append(row(-1, name))
            elif kind == "future_close":
                bars.append(row(minute + 1, name))
            elif kind == "revised":
                bars.append(dict(last[name]) | {"close": "4.10"})
            for b in bars:
                if b["start_ns"] == start + minute * MINUTE_NS:
                    last[b["symbol"]] = dict(b)
            steps.append(
                {
                    "bars": carried + bars,
                    "requested": requested,
                    "failed": failed,
                    "market": at,
                    "received": received,
                    "missing": missing,
                    "kind": kind,
                }
            )
            if missing is not None:
                # reference_retry: the retried bar is skipped as a repeat (kept host behaviour).
                # Restore the reference and retry exactly the remembered bar at the same minute.
                steps.append(
                    {
                        "bars": [dict(last[missing])],
                        "requested": list(NAMES),
                        "failed": [],
                        "market": at,
                        "received": at + 1,
                        "missing": None,
                        "kind": "reference_retry",
                    }
                )
        runs.append(
            {
                "segment": [
                    segment,
                    template["segment"][1],
                    start,
                    start + 36 * MINUTE_NS,
                    "zz-segment",
                ],
                "epoch": f"zz-host-{index}",
                "steps": steps,
            }
        )
    return {
        key: template[key]
        for key in (
            "registry",
            "method_version",
            "universe",
            "universe_source",
            "scope",
            "paper_slot",
            "authority",
        )
    } | {"runs": runs}


def host_replay(m, inputs, root):
    """Drive both native hosts with identical polls; every step carries all four answers."""
    import json
    from dataclasses import asdict
    from datetime import date
    from decimal import Decimal
    from pathlib import Path

    rows = tuple(
        m["RegistryRow"](
            n, u, Decimal(v) if decimal else int(v), source, state, date.fromisoformat(day)
        )
        for n, u, v, source, state, day, decimal in inputs["registry"]
    )
    answers = []
    for index, run in enumerate(inputs["runs"]):
        name, day, start, end, source = run["segment"]
        segment = m["Segment"](name, date.fromisoformat(day), start, end, source)
        samples = tuple(
            m["BaselineSample"](
                symbol, name, "2030-11-27", offset, Decimal(dollars), Decimal(count)
            )
            for symbol in inputs["universe"]
            for offset, dollars, count in ((0, "0", "0"), (3600, "3000", "600"))
        )
        baseline = m["BaselineTable"](samples, "zz-curves", start - 1, "sip")
        pub = m["publisher"](
            epoch=run["epoch"],
            feed="sip",
            segment=segment,
            registry=m["Registry"](rows),
            universe=tuple(inputs["universe"]),
            universe_source=inputs["universe_source"],
            baseline=baseline,
        )
        for symbol in inputs["universe"]:
            pub.set_symbol(
                symbol,
                references=m["ReferencePrices"](Decimal(2), start - 1, "zz-close"),
                asset=m["AssetState"](start - 1, end, False, "common", "zz-asset"),
            )
        scope = m["scope"](*inputs["scope"])
        lease = m["WriterLease"](scope, Path(root) / str(index), "zz-host")
        lease.acquire()
        observed = []
        try:
            host = m["host"](
                pub,
                lease=lease,
                scope=scope,
                paper_slot=inputs["paper_slot"],
                sip_authority=inputs["authority"],
                symbol_map={s: "ZZX:" + s for s in inputs["universe"]},
            )
            for step in run["steps"]:
                bars = tuple(
                    m["Bar"](
                        symbol=b["symbol"],
                        start_ns=b["start_ns"],
                        open=Decimal(b["open"]),
                        close=Decimal(b["close"]),
                        volume=b["volume"],
                        trades=b["trades"],
                    )
                    for b in step["bars"]
                )
                poll = m["BarPoll"](bars, tuple(step["requested"]), tuple(step["failed"]))
                removed = pub.references.pop(step["missing"]) if step["missing"] else None
                try:
                    intake = host.ingest(
                        poll, market_ns=step["market"], received_ns=step["received"]
                    )
                except Exception as exc:
                    intake = [type(exc).__name__, str(exc)]
                if removed is not None:
                    pub.references[step["missing"]] = removed
                coverage = asdict(pub.coverage)
                coverage["state"] = pub.coverage.state.value
                coverage = json.loads(json.dumps(coverage))
                try:
                    beat = host.heartbeat(at=step["market"], received_ns=step["received"])
                except Exception as exc:
                    beat = [type(exc).__name__, str(exc)]
                try:
                    doc = host.publish(
                        Path(root) / "unit", now=max(step["market"], step["received"])
                    )
                    document = json.dumps(doc, sort_keys=True, separators=(",", ":"))
                except Exception as exc:
                    document = [type(exc).__name__, str(exc)]
                observed.append([intake, coverage, beat, document])
        finally:
            lease.close()
        answers.append(observed)
    return answers


def host_answers(inputs, tmp_path):
    from test_publisher import EDGES, TYPES

    from sextant_movers.ranking.baseline import BaselineSample, BaselineTable, Segment
    from sextant_movers.ranking.engine import ReferencePrices
    from sextant_movers.ranking.publisher import AssetState, RankingPublisher
    from sextant_movers.ranking.registry import Registry, RegistryRow

    m = {
        "Registry": Registry,
        "RegistryRow": RegistryRow,
        "Segment": Segment,
        "BaselineSample": BaselineSample,
        "BaselineTable": BaselineTable,
        "ReferencePrices": ReferencePrices,
        "AssetState": AssetState,
        "scope": AccountScope,
        "WriterLease": WriterLease,
        "host": BarFidelityHost,
        "Bar": Bar,
        "BarPoll": BarPoll,
    }
    m["publisher"] = lambda **kw: RankingPublisher(
        send=lambda *args: None,
        method_version=inputs["method_version"],
        eligible_security_types=TYPES,
        price_band_edges=EDGES,
        **kw,
    )
    return host_replay(m, inputs, tmp_path)


def host_counts(answers):
    steps = [step for run in answers for step in run]
    documents = [json.loads(s[3]) for s in steps if isinstance(s[3], str)]
    return {
        "sessions": len(answers),
        "polls": len(steps),
        "fresh": sum(d["status"] == "fresh" for d in documents),
        "unavailable": sum(d["status"] == "unavailable" for d in documents),
        "exceptions": sum(isinstance(s[i], list) for s in steps for i in (0, 2, 3)),
    }


HOST_DIGEST = "2e3fb4d1eb2df4caa931f4533c4ac41df0dee4439eb2145842202602cc61905e"


def test_seeded_host_answers_are_pinned(tmp_path):
    import hashlib

    inputs = host_inputs()
    assert {r["segment"][0] for r in inputs["runs"]} == {"PRE", "RTH", "POST"}
    assert all(len({s["market"] for s in r["steps"]}) >= 30 for r in inputs["runs"])
    assert {s["kind"] for r in inputs["runs"] for s in r["steps"]} == {
        "complete",
        "failed",
        "unrequested",
        "repeat",
        "older",
        "zero",
        "missing",
        "late",
        "early_receive",
        "before_start",
        "future_close",
        "revised",
        "reference_retry",
    }
    # Every run has its own exact-end bar, alongside all three supported segment names.
    assert all(r["steps"][-1]["market"] == r["segment"][3] for r in inputs["runs"])
    answers = host_answers(inputs, tmp_path)
    counts = host_counts(answers)
    assert counts["sessions"] >= 60 and counts["polls"] >= 1800
    assert counts["fresh"] > 0 and counts["unavailable"] > 0 and counts["exceptions"] > 0
    digest = hashlib.sha256(json.dumps(answers, sort_keys=True, separators=(",", ":")).encode())
    print(f"host answers: {counts}, digest {digest.hexdigest()}")
    assert digest.hexdigest() == HOST_DIGEST


HOST_PARITY = r"""
import hashlib, json, os, sys
from pathlib import Path
from tempfile import TemporaryDirectory
src = os.environ["MOVERS_REFERENCE_SRC"]
blobs = {
    "tradingview/host.py": "334748530508928f7d9bdcd173dce4144f2b7450",
    "tradingview/producer.py": "02d58845621c786a1ee706e45f6cf7842cffd43a",
    "tradingview/bars.py": "8ae03acec28cabae0425cf6af1bce38b08910e60",
    "ranking/window.py": "4180e5aa8466eb4c548488218d3826fac10fa0ef",
    "ranking/publisher.py": "498ac186d643a137b32f6ff84f9ca76c657fb0ee",
    "ranking/engine.py": "15e1907ac6c3e24218e6ae945001ea3ab0c1492f",
}
for path, expected in blobs.items():
    blob = (Path(src) / "sextant" / path).read_bytes()
    assert hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest() == expected, path
sys.path.insert(0, src)
import nautilus_trader
from sextant.tradingview import host as h, producer as pr, bars as b
from sextant.ranking import baseline as bl, engine as en, publisher as pb, registry as rg
from sextant.ranking import reader as rd
from sextant.execution import custody as cu
assert os.path.samefile(h.__file__, Path(src) / "sextant/tradingview/host.py")
inputs = json.loads(sys.stdin.read())
versions = [list(sys.version_info[:3]), nautilus_trader.__version__]
assert versions == inputs.pop("versions"), "Python/engine version mismatch"
pb.METHOD = rd.METHOD = inputs["method_version"]
assert [pr.SCOPE.broker, pr.SCOPE.account] == inputs["scope"]
assert pr.DISPLAY_PAPER_SLOT == inputs["paper_slot"]
class Bus:
    def publish(self, *args, **kw):
        pass

def host(pub, *, lease, scope, paper_slot, sip_authority, symbol_map):
    assert scope == pr.SCOPE and paper_slot == pr.DISPLAY_PAPER_SLOT
    return h.BarFidelityHost(pub, lease=lease, sip_authority=sip_authority, symbol_map=symbol_map)

m = {"Registry": rg.Registry, "RegistryRow": rg.RegistryRow, "Segment": en.Segment,
     "BaselineSample": bl.BaselineSample, "BaselineTable": bl.BaselineTable,
     "ReferencePrices": en.ReferencePrices, "AssetState": pb.AssetState,
     "scope": cu.AccountScope, "WriterLease": cu.WriterLease, "host": host,
     "Bar": b.Bar, "BarPoll": b.BarPoll,
     "publisher": lambda **kw: pb.RankingPublisher(bus=Bus(), **kw)}
with TemporaryDirectory() as temporary:
    answers = host_replay(m, inputs, Path(temporary))
print("PARITY " + json.dumps({"versions": versions, "answers": answers}))
"""


@pytest.mark.local
def test_host_parity_with_reference_child(tmp_path):
    import inspect
    import sys

    import nautilus_trader
    import reference_child as child

    src, python = child.reference()
    versions = [list(sys.version_info[:3]), nautilus_trader.__version__]
    inputs = host_inputs()
    script = inspect.getsource(host_replay) + HOST_PARITY
    theirs = child.run(python, src, script, stdin=json.dumps(inputs | {"versions": versions}))
    assert versions == theirs["versions"], f"version mismatch: {versions}, {theirs['versions']}"
    ours = host_answers(inputs, tmp_path)
    print(f"\npython, engine: here {versions}, reference {theirs['versions']}")
    print(f"host parity: {host_counts(ours)}")
    assert len(ours) == len(theirs["answers"])
    for index, (a, b) in enumerate(zip(ours, theirs["answers"], strict=True)):
        assert len(a) == len(b)
        for step, (left, right) in enumerate(zip(a, b, strict=True)):
            assert left == right, f"session {index}, poll {step}: four-answer mismatch"
