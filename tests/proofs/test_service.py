"""Part 6 tick proofs: synthetic bars and temporary leases, no transport.

Acceptance: the reference, src/sextant/tradingview/prestage.py:490-566,633-814
at 06562bec0898a7203f4eaf69086ced83d2477e6b, excluding the carried view.
"""

import ast
import hashlib
import inspect
import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from nautilus_trader.core.datetime import unix_nanos_to_dt
from test_host import NAME, OTHER, START, bar
from test_producer import AUTH, MEMBERS, SCOPE, SLOT
from test_publisher import EDGES, NAMES, NS, TYPES, Send, ready, registry

from sextant_movers.lease import WriterLease
from sextant_movers.ranking.bars import MINUTE_NS
from sextant_movers.ranking.baseline import BaselineSample, BaselineTable
from sextant_movers.ranking.generation import CoverageState
from sextant_movers.tradingview import host as host_module
from sextant_movers.tradingview import service as module
from sextant_movers.tradingview.bridge import DisplayBounds
from sextant_movers.tradingview.host import BarFidelityHost
from sextant_movers.tradingview.inputs import InputError, read_research_inputs
from sextant_movers.tradingview.producer import Clocks


class Source:
    def __init__(self, bars):
        self.bars = tuple(bars)
        self.calls = []

    def minute_bars(self, symbols, *, start, end):
        self.calls.append((symbols, start, end))
        return self.bars

    def daily_closes(self, symbols, *, day):
        raise AssertionError("tick must not fetch daily closes")


def bars_for(segment, minutes=8):
    return tuple(
        bar(
            symbol=name,
            start_ns=segment.start_ns + minute * MINUTE_NS,
            close=D(2) + D(minute + i) / 100,
        )
        for minute in range(minutes)
        for i, name in enumerate(NAMES)
    )


@pytest.fixture
def kw(tmp_path, request):
    segment = ready().segment
    lease = WriterLease(SCOPE, tmp_path / "lease", "zz-service")
    lease.acquire()
    request.addfinalizer(lease.close)
    samples = tuple(
        BaselineSample(name, segment.name, "2030-11-27", offset, D(dollars), D(count))
        for name in NAMES
        for offset, dollars, count in ((0, "0", "0"), (3600, "3000", "600"))
    )
    return dict(
        lease=lease,
        scope=SCOPE,
        paper_slot=SLOT,
        source=Source(bars_for(segment)),
        segment=segment,
        baseline=BaselineTable(samples, "zz-curves", START - 1, "sip"),
        symbols=NAMES,
        prior_closes=dict.fromkeys(NAMES, D(2)),
        prior_close_ns=START - 1,
        symbol_map=MEMBERS,
        registry=registry(),
        epoch="zz-service",
        universe_source="zz-universe",
        now_ns=START + 8 * MINUTE_NS,
        unit_dir=tmp_path / "unit",
        j5_path=tmp_path / "j5.json",
        bounds=DisplayBounds(D("0.1"), D(2), 1200),
        sip_authority=AUTH,
        mapping_source="zz-mapping",
        reference_source="zz-reference",
        asset_source="zz-asset",
        max_age=1200,
        feed="sip",
        send=Send(),
        method_version="zz-method",
        eligible_security_types=TYPES,
        price_band_edges=EDGES,
    )


def tick(kw, **over):
    return module.publish_tick(**(kw | over))


def refusal(kw, message, **over):
    with pytest.raises(module.ServiceError) as caught:
        tick(kw, **over)
    assert str(caught.value) == message


def visible(unit):
    return unit.readlink().as_posix(), {
        path.name: path.read_bytes() for path in sorted(unit.iterdir())
    }


def test_error_is_value_error_and_no_view_parameter():
    assert issubclass(module.ServiceError, ValueError)
    assert "view" not in inspect.signature(module.publish_tick).parameters


@pytest.mark.parametrize(
    "key",
    [
        "scope",
        "paper_slot",
        "sip_authority",
        "mapping_source",
        "reference_source",
        "asset_source",
        "max_age",
        "feed",
        "method_version",
        "eligible_security_types",
        "price_band_edges",
        "send",
    ],
)
def test_each_caller_keyword_required(kw, key):
    args = kw.copy()
    args.pop(key)
    with pytest.raises(TypeError, match=key):
        module.publish_tick(**args)
    assert inspect.signature(module.publish_tick).parameters[key].default is inspect.Parameter.empty


def test_clock_alone_optional_and_baseline_is_protocol():
    sig = inspect.signature(module.publish_tick)
    assert sig.parameters["clock"].default is None
    assert sig.parameters["baseline"].annotation == "Baseline"
    assert "cast(" not in inspect.getsource(module)


def test_unsupported_segment_refused_before_clock(kw):
    refusal(
        kw,
        "the producer does not write OVERNIGHT units",
        segment=replace(kw["segment"], name="OVERNIGHT"),
        now_ns=0,
    )


@pytest.mark.parametrize("at", [START - 1, ready().segment.end_ns])
def test_initial_clock_outside_segment(kw, at):
    refusal(kw, "the publish clock is outside the supplied segment", now_ns=at)
    assert kw["source"].calls == []


@pytest.mark.parametrize("at", [START, START + MINUTE_NS - 1])
def test_first_minute_not_closed(kw, at):
    refusal(kw, "no completed minute bars for this segment", now_ns=at)
    assert kw["source"].calls == []


def test_source_returns_no_completed_bars(kw):
    refusal(kw, "no completed minute bars for this segment", source=Source(()))


@pytest.mark.parametrize("name", ["PRE", "RTH", "POST"])
def test_supported_segment_written(kw, name):
    segment = replace(kw["segment"], name=name)
    base = replace(
        kw["baseline"], samples=tuple(replace(s, segment=name) for s in kw["baseline"].samples)
    )
    receipt = tick(kw, segment=segment, baseline=base)
    assert receipt["segment"] == name and kw["unit_dir"].is_symlink()


def test_completed_drops_before_start_after_end_and_open_minute(kw):
    segment = kw["segment"]
    last = START + 8 * MINUTE_NS
    items = [bar(-1), bar(0), bar(7), bar(8), bar(start_ns=segment.end_ns)]
    kept = module._completed(items, segment, last)
    assert kept == {START + MINUTE_NS: [items[1]], last: [items[2]]}
    # Exact end passes this filter (the host skips it); one minute past is dropped.
    end_bar = bar(start_ns=segment.end_ns - MINUTE_NS)
    assert module._completed([end_bar, items[-1]], segment, segment.end_ns + MINUTE_NS) == {
        segment.end_ns: [end_bar]
    }


def test_fetch_exact_tuple_and_completed_window_at_minute_boundary(kw):
    receipt = tick(kw)
    symbols, start, end = kw["source"].calls[0]
    assert symbols == tuple(kw["symbols"])
    assert int(start.timestamp()) * NS == START
    assert int(end.timestamp()) * NS == kw["now_ns"] == receipt["publish_clock_ns"]


def test_fetch_window_ends_at_the_last_closed_minute_not_at_the_clock(kw):
    ages = registry(input_max_age_s=120, heartbeat_max_age_s=120, generation_max_age_s=120)
    receipt = tick(kw, now_ns=kw["now_ns"] + 59 * NS, registry=ages)
    end = kw["source"].calls[0][2]
    assert int(end.timestamp()) * NS == kw["now_ns"] < receipt["publish_clock_ns"]


@pytest.mark.parametrize("delta", [0, 1])
def test_first_closed_minute_is_replayed_even_when_ranking_unavailable(kw, monkeypatch, delta):
    spy = Mock(wraps=BarFidelityHost.ingest)
    original = BarFidelityHost.ingest

    def ingest(host, poll, **clocks):
        spy(host, poll, **clocks)
        return original(host, poll, **clocks)

    spy.side_effect = lambda *a, **k: 0
    monkeypatch.setattr(BarFidelityHost, "ingest", ingest)
    with pytest.raises(module.ServiceError, match="the J5 unit is not fresh:"):
        tick(kw, now_ns=START + MINUTE_NS + delta)
    assert spy.call_count == 1
    assert spy.call_args.kwargs == dict(market_ns=START + MINUTE_NS, received_ns=START + MINUTE_NS)


@pytest.mark.parametrize("at", [START - 1, ready().segment.end_ns, ready().segment.end_ns + 1])
def test_second_clock_outside_segment(kw, at):
    refusal(kw, "the PRE segment closed before the tick could publish", clock=lambda: at)
    assert not kw["j5_path"].exists() and kw["send"].calls == []


def test_second_clock_behind_replayed_bars(kw):
    at = kw["now_ns"] - 1
    refusal(
        kw,
        f"the publication clock moved behind the replayed bars: {at} < {kw['now_ns']}",
        clock=lambda: at,
    )


def test_second_clock_at_segment_start_is_behind_the_bars_not_closed(kw):
    refusal(
        kw,
        f"the publication clock moved behind the replayed bars: {START} < {kw['now_ns']}",
        clock=lambda: START,
    )


def test_second_clock_can_precede_now_if_not_replayed_bars(kw):
    at = kw["now_ns"]
    receipt = tick(kw, now_ns=at + 1, clock=lambda: at)
    assert receipt["publish_clock_ns"] == at < receipt["now_ns"]


def test_clock_read_after_replay_heartbeat_and_mapping_use_both_clocks(kw, monkeypatch):
    events = []
    original_ingest, original_beat, original_publish = (
        BarFidelityHost.ingest,
        BarFidelityHost.heartbeat,
        BarFidelityHost.publish,
    )

    def ingest(host, *a, **k):
        events.append("ingest")
        return original_ingest(host, *a, **k)

    def clock():
        assert len(events) == 8
        events.append("clock")
        return kw["now_ns"] + 1

    def beat(host, **k):
        assert events[-1] == "clock"
        assert k == dict(at=kw["now_ns"], received_ns=kw["now_ns"] + 1)
        return original_beat(host, **k)

    def publish(host, *a, **k):
        assert k == dict(
            now=kw["now_ns"] + 1,
            mapping_clocks=Clocks(kw["now_ns"], kw["now_ns"] + 1, "zz-mapping"),
        )
        return original_publish(host, *a, **k)

    monkeypatch.setattr(BarFidelityHost, "ingest", ingest)
    monkeypatch.setattr(BarFidelityHost, "heartbeat", beat)
    monkeypatch.setattr(BarFidelityHost, "publish", publish)
    assert tick(kw, clock=clock)["publish_clock_ns"] == kw["now_ns"] + 1


def test_heartbeat_refused(kw, monkeypatch):
    monkeypatch.setattr(BarFidelityHost, "heartbeat", lambda *a, **k: False)
    refusal(kw, "the bar-fidelity heartbeat was refused")


def test_document_not_fresh_uses_its_reason(kw, monkeypatch):
    monkeypatch.setattr(BarFidelityHost, "publish", lambda *a, **k: {"reason": "zz-refused"})
    refusal(kw, "the J5 unit is not fresh: zz-refused")


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_existing_unit_must_be_symlink(kw, kind):
    unit = kw["unit_dir"]
    unit.write_text("zz") if kind == "file" else unit.mkdir()
    refusal(kw, "the display unit is not a generation pointer; move it aside once")
    assert len(kw["send"].calls) == 2


@pytest.mark.parametrize(
    "event,price,kept",
    [
        (None, START, False),
        (True, START, False),
        (START, START, True),
        (START - 1, START, False),
        (START, START - 1, False),
        (START, None, True),
        (D(START), START, False),
        (START, True, True),
    ],
)
def test_drop_stale_equal_age_stays_one_nanosecond_past_loses(event, price, kept):
    latest = SimpleNamespace(observations={"ts_event": event, "last_price_ts": price})
    state = replace(ready().states[NAME], latest=latest)
    pub = ready()
    pub.states = {NAME: state, OTHER: replace(state, latest=None)}
    module._drop_stale(pub, START + 2 * NS, 2 * NS)
    assert (pub.states[NAME].latest is latest) is kept
    assert pub.states[OTHER].latest is None


def test_drop_stale_uses_existing_input_age_registry(kw, monkeypatch):
    spy = Mock(wraps=module._drop_stale)
    monkeypatch.setattr(module, "_drop_stale", spy)
    tick(kw)
    assert spy.call_args.args[1:] == (kw["now_ns"], kw["registry"].seconds("input_max_age_s"))


def test_drop_stale_takes_the_publication_clock_and_the_input_age_row(kw, monkeypatch):
    spy = Mock(wraps=module._drop_stale)
    monkeypatch.setattr(module, "_drop_stale", spy)
    tick(kw, clock=lambda: kw["now_ns"] + 1, registry=registry(input_max_age_s=3))
    assert spy.call_args.args[1:] == (kw["now_ns"] + 1, 3 * NS)


def test_caller_identity_and_reference_asset_facts_pass_unchanged(kw, monkeypatch):
    publisher = Mock(wraps=module.RankingPublisher)
    host = Mock(wraps=module.BarFidelityHost)
    bridge = Mock(wraps=module.project_display_unit)
    monkeypatch.setattr(module, "RankingPublisher", publisher)
    monkeypatch.setattr(module, "BarFidelityHost", host)
    monkeypatch.setattr(module, "project_display_unit", bridge)
    tick(kw)
    for name in (
        "send",
        "baseline",
        "method_version",
        "eligible_security_types",
        "price_band_edges",
    ):
        assert publisher.call_args.kwargs[name] is kw[name]
    for name in ("scope", "paper_slot", "sip_authority"):
        assert host.call_args.kwargs[name] is kw[name]
    for name in ("method_version", "paper_slot"):
        assert bridge.call_args.kwargs[name] is kw[name]
    pub = host.call_args.args[0]
    for name in NAMES:
        ref, asset = pub.references[name], pub.assets[name]
        assert (ref.prior_close, ref.known_at, ref.source) == (D(2), START - 1, "zz-reference")
        assert (
            asset.known_at,
            asset.expires_at,
            asset.halted,
            asset.security_type,
            asset.source,
        ) == (START - 1, kw["segment"].end_ns, None, "common", "zz-asset")


def test_all_polls_complete_at_own_minute_missing_bar_holds_no_gap(kw, monkeypatch):
    seen = []
    original = BarFidelityHost.ingest

    def ingest(host, poll, *, market_ns, received_ns):
        count = original(host, poll, market_ns=market_ns, received_ns=received_ns)
        assert poll.requested == tuple(NAMES) and poll.failed == ()
        assert market_ns == received_ns == poll.bars[0].close_ns
        assert host._gaps == {} and host.publisher.coverage.state is CoverageState.READY
        seen.append(market_ns)
        return count

    monkeypatch.setattr(BarFidelityHost, "ingest", ingest)
    source = Source(b for b in kw["source"].bars if not (b.symbol == OTHER and b.start_ns == START))
    tick(kw, source=source)
    assert seen == [START + i * MINUTE_NS for i in range(1, 9)]


@pytest.mark.parametrize("kind", ["unknown", "revision"])
def test_refused_intake_keeps_unit_bytes_and_next_tick_equals_fresh_replay(kw, kind):
    tick(kw)
    before = visible(kw["unit_dir"])
    extra = bar(symbol="ZZUNASKED") if kind == "unknown" else bar(close=D(4))
    with pytest.raises(ValueError, match=r"outside the universe|revised bar"):
        tick(kw, source=Source((*kw["source"].bars, extra)))
    assert visible(kw["unit_dir"]) == before
    tick(kw)
    first = kw["unit_dir"].parent / "fresh"
    tick(kw, unit_dir=first)
    assert visible(first)[1] == visible(kw["unit_dir"])[1]
    assert first.resolve().name == kw["unit_dir"].resolve().name


def test_generation_writes_reader_and_relative_pointer_swap_order(kw, monkeypatch):
    events = []
    write, read, symlink, move = (
        module.write_atomic,
        module.read_research_inputs,
        module.os.symlink,
        module.os.replace,
    )

    def writing(path, document):
        assert not kw["unit_dir"].is_symlink()
        events.append(path.name)
        return write(path, document)

    def reading(path, **facts):
        assert len(events) == 3 and not kw["unit_dir"].is_symlink()
        assert facts == dict(now=unix_nanos_to_dt(kw["now_ns"]), max_age=kw["max_age"])
        events.append("read")
        return read(path, **facts)

    def linking(target, pointer, **facts):
        assert not Path(target).is_absolute()
        assert pointer == kw["unit_dir"].parent / ".unit.pointer"
        assert facts == {"target_is_directory": True} and events[-1] == "read"
        events.append("symlink")
        return symlink(target, pointer, **facts)

    def moving(src, dst):
        # atomic.write_atomic also uses this os module; only inspect the pointer swap.
        if dst == kw["unit_dir"]:
            assert events[-1] == "symlink"
            events.append("replace")
        return move(src, dst)

    monkeypatch.setattr(module, "write_atomic", writing)
    monkeypatch.setattr(module, "read_research_inputs", reading)
    monkeypatch.setattr(module.os, "symlink", linking)
    monkeypatch.setattr(module.os, "replace", moving)
    receipt = tick(kw)
    assert events == [
        "top100.json",
        "focus.json",
        "display-lists.json",
        "read",
        "symlink",
        "replace",
    ]
    assert kw["unit_dir"].readlink() == Path("unit.generations") / receipt["generation_sha256"]


def test_receipt_eleven_keys_schema_and_counts(kw):
    receipt = tick(kw)
    assert set(receipt) == {
        "schema",
        "segment",
        "session_date",
        "now_ns",
        "publish_clock_ns",
        "bars",
        "ingested",
        "expires_at_ns",
        "unit_dir",
        "generation_sha256",
        "universe_source",
    }
    assert receipt["schema"] == "sextant.tv-prestage-receipt/1"
    assert receipt["bars"] == receipt["ingested"] == 24
    assert receipt["expires_at_ns"] == kw["now_ns"] + 2 * NS
    assert receipt["session_date"] == kw["segment"].session_date.isoformat()
    assert receipt["unit_dir"] == str(kw["unit_dir"])
    assert receipt["universe_source"] == kw["universe_source"]


def test_source_boundaries_fresh_hosts_and_allowed_writes():
    source = inspect.getsource(module)
    tree = ast.parse(source)
    imports = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    forbidden = {
        "sextant",
        "socket",
        "urllib",
        "http",
        "requests",
        "alpaca",
        "mcp",
        "time",
        "threading",
        "argparse",
    }
    assert not any(n.split(".")[0] in forbidden for n in imports)
    assert not any(
        s in source
        for s in (
            "MessageBus",
            "TraderId",
            "datetime.now",
            "sleep(",
            "getenv",
            "environ",
            "Timer(",
            "Thread(",
            "cast(",
        )
    )
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            assert not isinstance(value, (ast.Dict, ast.List, ast.Set))
            assert not (isinstance(value, ast.Call) and ast.unparse(value.func) == "Path")
    func = module.publish_tick
    assert func.__closure__ is None
    assert not {"host", "publisher", "view"} & set(inspect.signature(func).parameters)
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not any(
        s in c
        for c in calls
        for s in ("open", "write_text", "write_bytes", "mkdir", "rmdir", "unlink", "remove")
        if c != "pointer.unlink"
    )
    assert {"write_atomic", "os.symlink", "os.replace", "pointer.unlink", "host.publish"} <= calls
    assert "sextant_movers.ranking.references" in imports
    assert not any(isinstance(n, ast.ClassDef) and n.name == "Source" for n in tree.body)


def test_host_statements_unchanged_and_header_documents_caller_rules():
    tree = ast.parse(inspect.getsource(host_module))
    digest = hashlib.sha256(ast.dump(ast.Module(body=tree.body[1:], type_ignores=[])).encode())
    assert digest.hexdigest() == "2b061285f0a0f7079a487b8e0cad58bf62906cd4e97be395df29a28968a53b3b"
    doc = inspect.getdoc(host_module)
    assert doc is not None
    assert all(word in doc for word in ("discard", "replay", "repeat", "same market clock"))


# Retained edges and fault observations; this suffix is independently cuttable.


def test_same_digest_reader_refusal_keeps_pointer_but_rewrites_live_files(kw, monkeypatch):
    """Read-back refusal retains the pointer after writing the repeated live generation."""
    receipt = tick(kw)
    before = visible(kw["unit_dir"])
    monkeypatch.setattr(module, "read_research_inputs", Mock(side_effect=OSError("zz-read-fault")))
    mapping = {name: "ZZY:" + name for name in kw["symbols"]}
    with pytest.raises(OSError, match="zz-read-fault"):
        tick(kw, symbol_map=mapping)
    after = visible(kw["unit_dir"])
    assert after[0] == before[0]
    assert {name for name in before[1] if before[1][name] != after[1][name]} == {
        "top100.json",
        "display-lists.json",
    }
    generations = list((kw["unit_dir"].parent / "unit.generations").iterdir())
    assert len(generations) == 1 and generations[0].name == receipt["generation_sha256"]


def test_missing_prior_close_is_key_error_not_caught_as_value_error(kw):
    with pytest.raises(KeyError) as caught:
        tick(kw, prior_closes={})
    assert caught.value.args == (NAMES[0],)
    assert not isinstance(caught.value, ValueError)


def test_unasked_symbol_reaches_host_refusal(kw):
    with pytest.raises(ValueError, match=r"^bar outside the universe: ZZUNASKED$"):
        tick(kw, source=Source([bar(symbol="ZZUNASKED")]))


def test_repeated_bar_skipped_zero_volume_not_ingested_and_open_minute_dropped(kw):
    bars = list(kw["source"].bars)
    bars[0] = replace(bars[0], volume=0)
    source = Source([*reversed(bars), bars[1], bar(8)])
    receipt = tick(kw, source=source)
    assert receipt["bars"] == 25 and receipt["ingested"] == 23


def test_out_of_order_bars_replay_to_same_documents(kw):
    tick(kw)
    before = visible(kw["unit_dir"])
    tick(kw, source=Source(reversed(kw["source"].bars)))
    assert visible(kw["unit_dir"]) == before


@pytest.mark.parametrize("where", ["reader", "replace"])
def test_fault_after_files_retains_old_unit_and_orphan_generation(kw, monkeypatch, where):
    tick(kw)
    before = visible(kw["unit_dir"])
    parent = kw["unit_dir"].parent / "unit.generations"
    move = module.os.replace

    def fail(*a, **k):
        raise OSError("zz-injected")

    def replace_pointer(src, dst):
        if dst == kw["unit_dir"]:
            fail()
        return move(src, dst)

    if where == "reader":
        monkeypatch.setattr(module, "read_research_inputs", fail)
    else:
        monkeypatch.setattr(module.os, "replace", replace_pointer)
    with pytest.raises(OSError, match="zz-injected"):
        tick(kw, epoch="zz-second")
    assert visible(kw["unit_dir"]) == before and len(list(parent.iterdir())) == 2
    assert all(len(list(p.iterdir())) == 3 for p in parent.iterdir())
    read_research_inputs(kw["unit_dir"], now=unix_nanos_to_dt(kw["now_ns"]), max_age=1200)
    assert (kw["unit_dir"].parent / ".unit.pointer").is_symlink() is (where == "replace")


def test_existing_digest_rewritten_crash_pointer_removed_and_dangling_unit_replaced(kw):
    first = tick(kw)
    unit = kw["unit_dir"]
    (unit / "top100.json").write_text("zz-corrupt")
    pointer = unit.parent / ".unit.pointer"
    pointer.write_text("zz-crash")
    assert tick(kw) == first
    assert not pointer.exists() and json.loads((unit / "top100.json").read_text())
    unit.unlink()
    unit.symlink_to("zz-missing")
    assert not unit.exists() and unit.is_symlink()
    assert tick(kw) == first and unit.exists()
    assert len(list((unit.parent / "unit.generations").iterdir())) == 1


def test_old_generations_never_removed_after_three_ticks(kw):
    for i in range(3):
        tick(kw, epoch=f"zz-epoch-{i}")
    assert len(list((kw["unit_dir"].parent / "unit.generations").iterdir())) == 3


@pytest.mark.parametrize("age", [1, 2, 3])
def test_readback_uses_minimum_of_caller_and_bridge_narrowed_age(kw, age):
    # Producer expiry narrows the projected bound to two seconds.
    options = dict(
        now_ns=kw["now_ns"],
        clock=lambda: kw["now_ns"] + NS,
        max_age=age,
        registry=registry(input_max_age_s=10, heartbeat_max_age_s=10),
        bounds=DisplayBounds(D("0.1"), D(2), 2),
    )
    if age == 1:
        with pytest.raises(InputError, match="Research generation expired; lists NOT synced"):
            tick(kw, **options)
    else:
        tick(kw, **options)
        doc = json.loads((kw["unit_dir"] / "display-lists.json").read_text())
        assert doc["bounds"]["max_age_s"] == 2


def test_nanosecond_before_segment_end_publishes_latest_complete_minute(kw):
    segment = replace(kw["segment"], end_ns=START + 9 * MINUTE_NS)
    receipt = tick(
        kw,
        segment=segment,
        now_ns=segment.end_ns - 1,
        registry=registry(input_max_age_s=120, heartbeat_max_age_s=120, generation_max_age_s=120),
    )
    assert receipt["publish_clock_ns"] == segment.end_ns - 1


@pytest.mark.parametrize(
    "key", ["sip_authority", "mapping_source", "reference_source", "asset_source"]
)
def test_empty_source_text_delegated_without_tick_validation(kw, key):
    if key == "sip_authority":
        with pytest.raises(ValueError, match="fresh slot-2 SIP publisher, entitlement"):
            tick(kw, **{key: ""})
    elif key == "mapping_source":
        with pytest.raises(ValueError, match="explicit integer clocks and source required"):
            tick(kw, **{key: ""})
    elif key == "reference_source":
        with pytest.raises(
            ValueError, match="official split-adjusted completed-close source required"
        ):
            tick(kw, **{key: ""})
    else:
        tick(kw, **{key: ""})


def test_source_texts_in_files_and_send_full_and_top_types(kw):
    tick(kw)
    j5 = kw["j5_path"].read_text()
    documents = "".join(p.read_text() for p in kw["unit_dir"].iterdir())
    for text in (AUTH, "zz-mapping", "zz-reference", "zz-asset"):
        assert text in j5
    assert all(text not in documents for text in (AUTH, "zz-mapping", "zz-reference", "zz-asset"))
    sent = kw["send"].calls
    assert len(sent) == 2
    assert [t.metadata["view"] for t, _ in sent] == ["full", "top"]
    assert all(type(data).__name__ == "CustomData" for _, data in sent)


@pytest.mark.parametrize("where", ["document", "bridge", "reader", "unit", "replace"])
def test_send_already_observed_when_display_publication_refused(kw, monkeypatch, where):
    move = module.os.replace

    def fail(*a, **k):
        raise ValueError("zz-display-refusal")

    def replace_pointer(src, dst):
        if dst == kw["unit_dir"]:
            fail()
        return move(src, dst)

    if where == "document":
        kw["symbol_map"] = dict.fromkeys(NAMES)
    elif where in {"bridge", "reader"}:
        monkeypatch.setattr(
            module,
            {"bridge": "project_display_unit", "reader": "read_research_inputs"}[where],
            fail,
        )
    elif where == "unit":
        kw["unit_dir"].write_text("zz-existing")
    else:
        monkeypatch.setattr(module.os, "replace", replace_pointer)
    with pytest.raises(ValueError):
        tick(kw)
    assert len(kw["send"].calls) == 2


def test_no_tick_lock_shared_pointer_has_no_concurrency_guard():
    source = inspect.getsource(module)
    assert not any(token in source for token in ("flock", "Lock(", "acquire(", "uuid", "mktemp"))
    assert 'f".{unit_dir.name}.pointer"' in source


def service_inputs():
    """Seeded tapes: every fetch spans at least thirty complete minutes."""
    import random

    from test_host import host_inputs

    template = host_inputs()
    rng = random.Random(6101)  # noqa: S311 - deterministic synthetic tapes
    runs = []
    kinds = [
        "complete",
        "missing",
        "zero",
        "outside",
        "unknown",
        "repeat",
        "revision",
        "late",
        "boundary",
        "stale",
        "backwards",
        "closed",
    ]
    for index in range(6):
        segment = ("PRE", "RTH", "POST")[index % 3]
        start = START + (0, 330, 720)[index % 3] * MINUTE_NS
        steps = []
        for step, kind in enumerate(kinds):
            minutes = 32 + step
            bars = []
            for minute in range(minutes):
                for i, name in enumerate(NAMES):
                    # A complete poll verifies this missing bar without creating a gap.
                    if minute == 3 and i == 1:
                        continue
                    if kind == "stale" and i == 2 and minute >= minutes - 3:
                        continue
                    bars.append(
                        dict(
                            symbol=name,
                            start_ns=start + minute * MINUTE_NS,
                            open="1.90",
                            close=str(D(2) + D(minute) / 100 + D(i) / 1000),
                            volume=0 if minute == 4 and i == 0 else rng.randint(1, 30),
                            trades=rng.randint(0, 7),
                        )
                    )
            # Retained filters and identical duplicates occur alongside ordinary minutes.
            bars.extend(
                [
                    dict(bars[0], start_ns=start - MINUTE_NS),
                    dict(bars[0], start_ns=start + 60 * MINUTE_NS),
                    dict(bars[-1], start_ns=start + minutes * MINUTE_NS),
                    dict(bars[2]),
                ]
            )
            if kind == "unknown":
                bars.append(dict(bars[0], symbol="ZZUNASKED"))
            elif kind == "revision":
                bars.append(dict(bars[0], close="4.10"))
            elif kind == "missing":
                bars = [
                    b
                    for b in bars
                    if not (b["symbol"] == NAME and b["start_ns"] == start + 7 * MINUTE_NS)
                ]
            elif kind == "zero":
                bars = [
                    dict(b, volume=0)
                    if b["symbol"] == OTHER and b["start_ns"] == start + 9 * MINUTE_NS
                    else b
                    for b in bars
                ]
            rng.shuffle(bars)
            now = start + minutes * MINUTE_NS
            publication = None
            if kind == "late":
                publication = now + 3 * NS
            elif kind == "backwards":
                publication = now - 1
            elif kind == "closed":
                publication = start + 60 * MINUTE_NS
            elif kind != "boundary":
                now += rng.choice((0, 1, 123))
            steps.append(dict(kind=kind, bars=bars, now=now, publication=publication))
        runs.append(
            dict(
                segment=[
                    segment,
                    str(ready().segment.session_date),
                    start,
                    start + 60 * MINUTE_NS,
                    "zz-segment",
                ],
                steps=steps,
            )
        )
    return {k: template[k] for k in ("registry", "method_version", "scope", "paper_slot")} | {
        "symbols": list(NAMES),
        "runs": runs,
    }


def service_replay(m, inputs, root, facts):
    """Common driver, executed natively on each side; compare every visible answer."""
    from datetime import date
    from decimal import Decimal
    from pathlib import Path

    rows = tuple(
        m["RegistryRow"](n, u, Decimal(v) if dec else int(v), src, state, date.fromisoformat(day))
        for n, u, v, src, state, day, dec in inputs["registry"]
    )
    answers = []
    for index, run in enumerate(inputs["runs"]):
        name, day, start, end, provenance = run["segment"]
        segment = m["Segment"](name, date.fromisoformat(day), start, end, provenance)
        samples = tuple(
            m["BaselineSample"](s, name, "2030-11-27", off, Decimal(dv), Decimal(tc))
            for s in inputs["symbols"]
            for off, dv, tc in ((0, "0", "0"), (3600, "3000", "600"))
        )
        baseline = m["BaselineTable"](samples, "zz-curves", start - 1, "sip")
        scope = m["scope"](*inputs["scope"])
        directory = Path(root) / str(index)
        lease = m["WriterLease"](scope, directory / "lease", "zz-service-seeded")
        lease.acquire()
        try:
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

                class Source:
                    def __init__(self, bars):
                        self.bars = bars

                    def minute_bars(self, symbols, *, start, end):
                        assert symbols == tuple(inputs["symbols"])
                        return self.bars

                unit, j5 = directory / "unit", directory / "j5.json"
                kw: dict = dict(
                    lease=lease,
                    source=Source(bars),
                    segment=segment,
                    baseline=baseline,
                    symbols=tuple(inputs["symbols"]),
                    prior_closes=dict.fromkeys(inputs["symbols"], Decimal(2)),
                    prior_close_ns=start - 1,
                    symbol_map={s: "ZZX:" + s for s in inputs["symbols"]},
                    registry=m["Registry"](rows),
                    epoch=f"zz-service-{index}",
                    universe_source="zz-universe",
                    now_ns=step["now"],
                    unit_dir=unit,
                    j5_path=j5,
                    bounds=m["DisplayBounds"](Decimal("0.1"), Decimal(2), 1200),
                    feed=facts["feed"],
                    max_age=facts["max_age"],
                )
                if step["publication"] is not None:
                    at = step["publication"]
                    kw["clock"] = lambda at=at: at
                try:
                    receipt = m["tick"](kw, scope, inputs, facts)
                    receipt.pop("unit_dir")
                    result = receipt
                except Exception as exc:
                    kind = type(exc).__name__
                    result = ["PrestageError" if kind == "ServiceError" else kind, str(exc)]
                files = (
                    {p.name: p.read_text() for p in sorted(unit.iterdir())} if unit.exists() else {}
                )
                answers.append(
                    [
                        result,
                        files,
                        j5.read_text() if j5.exists() else None,
                        unit.resolve().name if unit.is_symlink() else None,
                    ]
                )
        finally:
            lease.close()
    return answers


def service_answers(inputs, tmp_path, facts):
    from sextant_movers.ranking.baseline import BaselineSample, BaselineTable, Segment
    from sextant_movers.ranking.registry import Registry, RegistryRow

    def native(kw, scope, inputs, facts):
        return module.publish_tick(
            **kw,
            scope=scope,
            paper_slot=inputs["paper_slot"],
            send=Send(),
            method_version=inputs["method_version"],
            eligible_security_types=frozenset(facts["eligible_security_types"]),
            price_band_edges=tuple(facts["price_band_edges"]),
            **{
                k: facts[k]
                for k in ("sip_authority", "mapping_source", "reference_source", "asset_source")
            },
        )

    m = dict(
        Registry=Registry,
        RegistryRow=RegistryRow,
        Segment=Segment,
        BaselineSample=BaselineSample,
        BaselineTable=BaselineTable,
        scope=type(SCOPE),
        WriterLease=WriterLease,
        Bar=type(bar()),
        DisplayBounds=DisplayBounds,
        tick=native,
    )
    return service_replay(m, inputs, tmp_path, facts)


def service_counts(answers) -> dict:
    from collections import Counter

    errors = Counter(a[0][0] + ": " + a[0][1] for a in answers if isinstance(a[0], list))
    return dict(
        ticks=len(answers),
        published=sum(isinstance(a[0], dict) for a in answers),
        refused=sum(errors.values()),
        exceptions=dict(sorted(errors.items())),
    )


SERVICE_DIGEST = "71d6b2764f0c24bed3d891443b9ab623b23672fa813fe10fe2e60f416ad68072"


def test_seeded_service_answers_are_pinned(tmp_path):
    inputs = service_inputs()
    assert {r["segment"][0] for r in inputs["runs"]} == {"PRE", "RTH", "POST"}
    assert sum(len(r["steps"]) for r in inputs["runs"]) >= 60
    assert all(
        len({b["start_ns"] for b in s["bars"]}) >= 30 for r in inputs["runs"] for s in r["steps"]
    )
    facts = dict(
        sip_authority=AUTH,
        mapping_source="zz-mapping",
        reference_source="zz-reference",
        asset_source="zz-asset",
        feed="sip",
        max_age=1200,
        eligible_security_types=sorted(TYPES),
        price_band_edges=list(EDGES),
    )
    answers = service_answers(inputs, tmp_path, facts)
    counts = service_counts(answers)
    assert counts["published"] > 0 and counts["refused"] > 0
    assert len({a[3] for a in answers if isinstance(a[0], dict)}) > len(inputs["runs"])
    digest = hashlib.sha256(json.dumps(answers, sort_keys=True, separators=(",", ":")).encode())
    print(f"service answers: {counts}, digest {digest.hexdigest()}")
    assert digest.hexdigest() == SERVICE_DIGEST


SERVICE_PARITY = r"""
import ast, hashlib, inspect, json, os, sys
from pathlib import Path
from tempfile import TemporaryDirectory
src = os.environ["MOVERS_REFERENCE_SRC"]
blobs = {
    "tradingview/prestage.py": "e3345d330725a4ad12d6e517ae74c592d3633ffe",
    "tradingview/host.py": "334748530508928f7d9bdcd173dce4144f2b7450",
    "tradingview/bridge.py": "29f8c37cd021082ae43f1d5c70c834456b6c0a8c",
    "tradingview/inputs.py": "83bb85dfb9e2833430b6d95b010dced8f6e47a49",
    "tradingview/producer.py": "02d58845621c786a1ee706e45f6cf7842cffd43a",
    "tradingview/bars.py": "8ae03acec28cabae0425cf6af1bce38b08910e60",
    "ranking/publisher.py": "498ac186d643a137b32f6ff84f9ca76c657fb0ee",
}
for path, expected in blobs.items():
    blob = (Path(src) / "sextant" / path).read_bytes()
    assert hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest() == expected, path
sys.path.insert(0, src)
import nautilus_trader
from sextant.tradingview import prestage as p, producer as pr, bridge as br
from sextant.ranking import baseline as bl, engine as en, publisher as pb, registry as rg
from sextant.ranking import reader as rd
from sextant.execution import custody as cu
inputs = json.loads(sys.stdin.read())
versions = [list(sys.version_info[:3]), nautilus_trader.__version__]
assert versions == inputs.pop("versions"), "Python/engine version mismatch"
pb.METHOD = rd.METHOD = br.METHOD = inputs["method_version"]
pr.SCOPE = cu.AccountScope(*inputs["scope"])
pr.DISPLAY_PAPER_SLOT = br.DISPLAY_PAPER_SLOT = inputs["paper_slot"]
tree = ast.parse(inspect.getsource(pb))
asset = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AssetState")
types = next(ast.literal_eval(n) for n in ast.walk(asset) if isinstance(n, ast.Set))
edges = sorted({n.comparators[0].value for n in ast.walk(tree)
                if isinstance(n, ast.Compare) and isinstance(n.left, ast.Name)
                and n.left.id == "price" and isinstance(n.ops[0], ast.GtE)
                and isinstance(n.comparators[0], ast.Constant)})
assert len(edges) == 2
sig = inspect.signature(p.publish_tick)
facts = dict(sip_authority=p.SIP_AUTHORITY, mapping_source=p.MAPPING_SOURCE,
             reference_source=p.REFERENCE_SOURCE, asset_source=p.ASSET_SOURCE,
             feed=sig.parameters["feed"].default, max_age=sig.parameters["max_age"].default,
             eligible_security_types=sorted(types), price_band_edges=edges)
def native(kw, scope, inputs, facts):
    return p.publish_tick(**kw)
m = dict(Registry=rg.Registry, RegistryRow=rg.RegistryRow, Segment=en.Segment,
         BaselineSample=bl.BaselineSample, BaselineTable=bl.BaselineTable,
         scope=cu.AccountScope, WriterLease=cu.WriterLease, Bar=p.Bar,
         DisplayBounds=p.DisplayBounds, tick=native)
with TemporaryDirectory() as root:
    answers = service_replay(m, inputs, root, facts)
print("PARITY " + json.dumps(dict(facts=facts, versions=versions, answers=answers)))
"""


@pytest.mark.local
def test_service_parity_with_reference_child(tmp_path):
    import sys

    import nautilus_trader
    import reference_child as child

    src, python = child.reference()
    inputs = service_inputs()
    versions = [list(sys.version_info[:3]), nautilus_trader.__version__]
    script = inspect.getsource(service_replay) + SERVICE_PARITY
    theirs = child.run(python, src, script, stdin=json.dumps(inputs | {"versions": versions}))
    assert versions == theirs["versions"], "Python/engine version mismatch"
    # Reference input facts stay in memory and pytest's temporary output, never source text.
    ours = service_answers(inputs, tmp_path, theirs["facts"])
    print(f"\npython, engine: here {versions}, reference {theirs['versions']}")
    print(f"service parity: {service_counts(ours)}")
    assert len(ours) == len(theirs["answers"])
    for index, (left, right) in enumerate(zip(ours, theirs["answers"], strict=True)):
        assert left == right, f"tick {index}: receipt/error, display files, J5 or pointer differs"
