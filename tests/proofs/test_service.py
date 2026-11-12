"""Part 6 tick proofs: synthetic bars and temporary leases, no transport.

Acceptance: the reference, src/sextant/tradingview/prestage.py:490-566,633-814
at 06562bec0898a7203f4eaf69086ced83d2477e6b, excluding the carried view.
"""

import ast
import hashlib
import inspect
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
