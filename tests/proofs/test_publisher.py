"""Part 5e-1: the asset state and the ranking publisher's constructor and trade intake.

Synthetic values only: invented ZZ symbols, made-up registry rows, no fixture file, no network and
no socket; the registry, baseline, segment and ticks come from `test_engine.py`'s helpers. The
reference is `src/sextant/ranking/publisher.py:51-134` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never imports it, and the `local` proof
runs it in a child interpreter. `TYPES` and `EDGES` are the values the reference hardcodes, passed
here as test input, never shipped.
"""

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import nautilus_trader.model as nt
import pytest
from test_engine import NS, POST, SCOPE, START, quote, registry, step, table, tick
from test_heartbeat import GOOD

from sextant_movers.ranking import baseline as bl
from sextant_movers.ranking import generation as gen
from sextant_movers.ranking import publisher
from sextant_movers.ranking import registry as rg
from sextant_movers.ranking.engine import MoverState, ReferencePrices
from sextant_movers.ranking.generation import Coverage, CoverageState
from sextant_movers.ranking.publisher import AssetState, RankingPublisher, universe_digest

TYPES = frozenset({"common", "adr"})  # the reference's hardcoded set, as test input
EDGES = (1, 5)  # the reference's hardcoded band edges, as test input
NAMES = ("ZZPBB", "ZZPBA", "ZZPBC")  # out of order on purpose
REG, BASE = registry(), table(symbols=NAMES)
REFS = ReferencePrices(Decimal(2), START - NS, "fixture close")
ASSET = AssetState(START - NS, SCOPE.end_ns, False, "common", "fixture vendor")
NOW, OMIT, LIST = START + 60 * NS, object(), []  # LIST: an unhashable security type
FIXED = {"eligible_security_types": TYPES, "price_band_edges": EDGES}
NEW = ("send", "method_version", "eligible_security_types", "price_band_edges")


class Send:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


def new(**over: Any) -> RankingPublisher:
    kw = {"send": Send(), "epoch": "zz-epoch", "feed": "sip", "segment": SCOPE, "registry": REG}
    kw |= {"universe": NAMES, "universe_source": "fixture universe", "baseline": BASE}
    kw |= {"method_version": "zz-method", **FIXED}
    return RankingPublisher(**{k: v for k, v in (kw | over).items() if v is not OMIT})


def ready(**over: Any) -> RankingPublisher:
    pub = new(**over)
    for name in NAMES:
        pub.set_symbol(name, references=REFS, asset=ASSET)
    return pub


ELIGIBLE: dict[str, tuple[AssetState, Any, bool]] = {
    "empty source": (replace(ASSET, source=""), NOW, False),
    "known_at below 0": (replace(ASSET, known_at=-1), NOW, False),
    "known_at at 0": (replace(ASSET, known_at=0), NOW, True),
    "known_at at now": (replace(ASSET, known_at=NOW), NOW, True),
    "known_at after now": (replace(ASSET, known_at=NOW + 1), NOW, False),
    "now before expires_at": (ASSET, SCOPE.end_ns - 1, True),
    "now at expires_at": (ASSET, SCOPE.end_ns, False),
    "now past expires_at": (ASSET, SCOPE.end_ns + 1, False),
    "halted": (replace(ASSET, halted=True), NOW, False),
    "halt unknown": (replace(ASSET, halted=None), NOW, False),
    "halted 0 is not False": (replace(ASSET, halted=0), NOW, False),  # type: ignore[arg-type]
    "type outside the set": (replace(ASSET, security_type="etf"), NOW, False),
    "type inside the set": (replace(ASSET, security_type="adr"), NOW, True),
    "a bool now counts as 1": (replace(ASSET, known_at=0, expires_at=2), True, True),
    "unhashable after an empty source": (replace(ASSET, source="", security_type=LIST), NOW, False),
}


@pytest.mark.parametrize("case", ELIGIBLE)
def test_each_link_of_eligible(case: str) -> None:
    asset, now, expected = ELIGIBLE[case]
    assert asset.eligible(now, security_types=TYPES) is expected


def test_eligible_uses_the_supplied_set_and_needs_it_by_keyword() -> None:
    etf = replace(ASSET, security_type="etf")
    assert etf.eligible(NOW, security_types=frozenset({"etf"}))
    assert not ASSET.eligible(NOW, security_types=frozenset({"etf"}))
    for call in (lambda: ASSET.eligible(NOW), lambda: ASSET.eligible(NOW, TYPES)):  # type: ignore[call-arg, misc]
        with pytest.raises(TypeError):
            call()
    with pytest.raises(TypeError, match="unhashable type: 'list'"):  # kept: no check of the type
        replace(ASSET, security_type=LIST).eligible(NOW, security_types=TYPES)


def test_the_asset_state_is_frozen_and_slotted() -> None:
    with pytest.raises(FrozenInstanceError):
        ASSET.halted = True  # type: ignore[misc]
    assert not hasattr(ASSET, "__dict__")
    assert AssetState.__slots__ == ("known_at", "expires_at", "halted", "security_type", "source")


FIRST = "explicit epoch, unique universe and source required"
SECOND = "feed/segment/baseline mismatch"
SEND, VERSION = "send callable required", "explicit method version required"
KINDS, BAND = "eligible security types required", "price band edges required"
BOATS = table(feed="boats", symbols=NAMES)
OVERNIGHT = replace(SCOPE, name="OVERNIGHT")
REFUSED: dict[str, tuple[dict[str, Any], str]] = {
    "empty epoch": ({"epoch": ""}, FIRST),
    "empty source": ({"universe_source": ""}, FIRST),
    "empty universe": ({"universe": ()}, FIRST),
    "repeated name": ({"universe": ("ZZPBA", "ZZPBB", "ZZPBA")}, FIRST),
    "baseline of the other feed": ({"baseline": BOATS}, SECOND),
    "boats outside OVERNIGHT": ({"feed": "boats", "baseline": BOATS}, SECOND),
    "sip with OVERNIGHT": ({"segment": OVERNIGHT}, SECOND),
    "send None": ({"send": None}, SEND),
    "send a string": ({"send": "zz"}, SEND),
    "version empty": ({"method_version": ""}, VERSION),
    "version not a str": ({"method_version": 1}, VERSION),
    "types a set": ({"eligible_security_types": set(TYPES)}, KINDS),
    "types empty": ({"eligible_security_types": frozenset()}, KINDS),
    "types a tuple": ({"eligible_security_types": ("common",)}, KINDS),
    "edges True": ({"price_band_edges": (True, 5)}, BAND),
    "edges equal": ({"price_band_edges": (5, 5)}, BAND),
    "edges reversed": ({"price_band_edges": (5, 1)}, BAND),
    "edges low 0": ({"price_band_edges": (0, 5)}, BAND),
    "edges a float": ({"price_band_edges": (1.0, 5)}, BAND),
    "edges a list": ({"price_band_edges": [1, 5]}, BAND),
    "edges three": ({"price_band_edges": (1, 2, 3)}, BAND),
    "edges empty": ({"price_band_edges": ()}, BAND),
}


@pytest.mark.parametrize("case", REFUSED)
def test_each_constructor_refusal_with_its_message(case: str) -> None:
    over, message = REFUSED[case]
    with pytest.raises(ValueError) as caught:
        new(**over)
    assert type(caught.value) is ValueError and str(caught.value) == message


def test_the_reference_refusals_win_and_the_new_ones_go_in_order() -> None:
    bad = {"send": 1, "method_version": "", "eligible_security_types": {0}, "price_band_edges": 0}
    order = [FIRST, SECOND, SEND, VERSION, KINDS, BAND]
    for i, message in enumerate(order):
        over = {"epoch": ""} if i == 0 else {"segment": OVERNIGHT} if i == 1 else {}
        over |= dict(list(bad.items())[max(i - 2, 0) :])
        with pytest.raises(ValueError, match=message):
            new(**over)


@pytest.mark.parametrize("keyword", NEW)
def test_each_new_keyword_is_required(keyword: str) -> None:
    with pytest.raises(TypeError, match=f"required keyword-only argument: '{keyword}'"):
        new(**{keyword: OMIT})


def test_every_start_value() -> None:
    send, heartbeat = Send(), object()
    pub = new(send=send, heartbeat=heartbeat, eligible_security_types=frozenset({"zz"}))
    assert (pub.send, pub.epoch, pub.feed, pub.segment) == (send, "zz-epoch", "sip", SCOPE)
    assert pub.registry is REG and pub.baseline is BASE and pub.heartbeat is heartbeat
    assert pub.universe == ("ZZPBA", "ZZPBB", "ZZPBC") and pub.universe_source == "fixture universe"
    assert pub.universe_hash == universe_digest(pub.universe)
    assert list(pub.states) == list(NAMES)  # kept: the given order, while `universe` is sorted
    assert all(state == MoverState() for state in pub.states.values())
    assert pub.references == {} and pub.assets == {}
    assert pub.coverage == Coverage(CoverageState.UNKNOWN, False)
    assert pub.published_at is None and pub.sequence == 0
    assert pub.last_event == pub.input_cutoff == START
    assert (pub.method_version, pub.price_band_edges) == ("zz-method", EDGES)
    assert pub.eligible_security_types == frozenset({"zz"}) and send.calls == []
    assert new().heartbeat is None and new(price_band_edges=(2, 3)).price_band_edges == (2, 3)
    night = new(feed="boats", segment=OVERNIGHT, baseline=BOATS)
    assert (night.last_event, night.input_cutoff) == (OVERNIGHT.start_ns, OVERNIGHT.start_ns)


def test_kept_a_list_universe_is_taken_and_a_mixed_one_fails_past_the_refusals() -> None:
    pub = new(universe=list(NAMES))
    assert pub.universe == ("ZZPBA", "ZZPBB", "ZZPBC") and list(pub.states) == list(NAMES)
    assert new(universe=(7, 8)).universe == (7, 8)
    with pytest.raises(TypeError, match="not supported between"):
        new(universe=("ZZPBA", 7))
    with pytest.raises(TypeError, match="unhashable"):
        new(universe=(["ZZPBA"],))


REGISTER = """
import json, sys
sys.path[:0] = sys.argv[1:3]
from nautilus_trader.model import deserialize_custom_from_json
from sextant_movers.ranking import generation as g
from sextant_movers.ranking.publisher import RankingPublisher
from test_engine import SCOPE, registry, table
envelope = sys.stdin.read().encode()
def read():
    try:
        return g.generation_digest(deserialize_custom_from_json(g.TYPE_NAME, envelope).data)
    except ValueError as error:
        return str(error)
kw = dict(send=print, epoch="zz", feed="sip", segment=SCOPE, registry=registry(),
          universe=("ZZPBA",), universe_source="zz", baseline=table(symbols=("ZZPBA",)),
          method_version="zz", eligible_security_types=frozenset({"zz"}), price_band_edges=(1, 5))
answers = [read()]
for bad in ({"epoch": ""}, {"feed": "boats"}, {"send": None}, {"price_band_edges": ()}):
    try:
        RankingPublisher(**{**kw, **bad})
    except ValueError:
        answers.append(read())
RankingPublisher(**kw)
print(json.dumps([*answers, read()]))
"""


def test_the_constructor_registers_the_type_after_its_refusals() -> None:
    """In a fresh `-I` child, so no registration in this process can answer for it."""
    data_type = {"type_name": gen.TYPE_NAME, "metadata": {}, "identifier": None}
    payload, here = json.loads(GOOD.to_json()), Path(__file__).resolve()
    envelope = json.dumps({"type": gen.TYPE_NAME, "data_type": data_type, "payload": payload})
    args = [sys.executable, "-I", "-c", REGISTER, str(here.parents[2] / "src"), str(here.parent)]
    done = subprocess.run(args, input=envelope, capture_output=True, text=True, check=True)
    *before, after = json.loads(done.stdout.splitlines()[-1])
    assert len(before) == 5 and all("is not registered" in answer for answer in before)
    assert after == gen.generation_digest(GOOD)


def test_set_symbol_refuses_a_name_outside_and_set_coverage_stores_anything() -> None:
    pub = new()
    with pytest.raises(ValueError) as caught:
        pub.set_symbol("ZZPBX", references=REFS, asset=ASSET)
    assert str(caught.value) == "symbol outside supplied universe"
    assert pub.references == pub.assets == {}
    pub.set_symbol("ZZPBA", references=REFS, asset=None)
    assert pub.references == {"ZZPBA": REFS} and pub.assets == {"ZZPBA": None}
    pub.set_symbol("ZZPBA", references=REFS, asset=ASSET)
    assert pub.assets["ZZPBA"] is ASSET
    coverage = Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0)
    pub.set_coverage(coverage)
    assert pub.coverage is coverage
    pub.set_coverage("zz")  # type: ignore[arg-type]  # kept: nothing is checked
    assert pub.coverage == "zz"


def test_a_trade_with_a_row_moves_the_state_last_event_and_input_cutoff() -> None:
    pub, trade = ready(), tick(30, name="ZZPBA")
    pub.on_trade(trade)
    state, row = step(MoverState(), trade, refs=REFS, base=BASE)  # the engine called directly
    assert row is not None and pub.states["ZZPBA"] == state and state.latest == row
    assert (pub.last_event, pub.input_cutoff) == (trade.ts_event, row.known_at)
    pub.on_trade(tick(10, name="ZZPBB"))  # another name, earlier: both only move up
    assert (pub.last_event, pub.input_cutoff) == (trade.ts_event, row.known_at)


def test_facts_reach_the_engine_and_a_later_known_at_moves_only_the_cutoff() -> None:
    pub, trade = ready(), tick(30, name="ZZPBA")
    facts = {"known_at": trade.ts_event + 7, "quote": quote(START, "ZZPBA")}
    pub.on_trade(trade, price_eligible=False, **facts)
    held = pub.states["ZZPBA"]
    assert (pub.last_event, pub.input_cutoff) == (trade.ts_event, trade.ts_event + 7)
    assert held.history.buckets == () and held.volume_history.buckets and held.quote is not None


@pytest.mark.parametrize(
    "facts", [{"price_eligible": False, "volume_eligible": False}, {"segment": POST}]
)
def test_a_trade_without_a_row_leaves_everything_as_it_was(facts: dict[str, Any]) -> None:
    pub = ready(segment=facts.pop("segment", SCOPE))
    before = dict(pub.states)
    pub.on_trade(tick(0, name="ZZPBA"), **facts)
    assert pub.states["ZZPBA"] is before["ZZPBA"] and pub.last_event == pub.input_cutoff == START


def trade(second: int, name: str = "ZZPBA", **facts: Any) -> Any:
    return lambda pub: pub.on_trade(tick(second, name=name), **facts)


def without_set_symbol(pub: RankingPublisher) -> None:
    pub.references.pop("ZZPBC")
    pub.on_trade(tick(40, name="ZZPBC"))


def zero_price_then_a_minute(pub: RankingPublisher) -> None:
    pub.on_trade(tick(40, "0.00", name="ZZPBB"))
    pub.set_coverage(Coverage(CoverageState.READY, True))
    pub.on_trade(tick(100, "2.00", name="ZZPBB"))


REFUSED_TRADES: dict[str, tuple[Any, type[Exception], str]] = {
    "regressed": (trade(20), ValueError, "engine.py"),
    "outside the session": (trade(19_800), ValueError, "engine.py"),
    "outside the universe": (trade(40, "ZZPBX"), KeyError, "publisher.py"),
    "no set_symbol": (without_set_symbol, KeyError, "publisher.py"),
    "an unknown fact": (trade(40, zz=1), TypeError, "publisher.py"),
    "a text known_at": (trade(40, known_at="zz"), TypeError, "engine.py"),
    "a zero price a minute back": (zero_price_then_a_minute, ArithmeticError, "engine.py"),
}


@pytest.mark.parametrize("case", REFUSED_TRADES)
def test_a_refused_trade_degrades_the_coverage_and_raises_the_engines_own_error(case: str) -> None:
    call, kind, origin = REFUSED_TRADES[case]
    pub = ready()
    pub.on_trade(tick(30, name="ZZPBA"))
    pub.set_coverage(Coverage(CoverageState.READY, True))
    before, marks = dict(pub.states), (pub.last_event, pub.input_cutoff)
    with pytest.raises(kind) as caught:
        call(pub)
    frames = caught.traceback
    assert caught.value.__context__ is None and Path(str(frames[-1].path)).name == origin
    assert pub.coverage == Coverage(CoverageState.DEGRADED, False)
    if case != "a zero price a minute back":
        assert pub.states == before and (pub.last_event, pub.input_cutoff) == marks


@pytest.mark.parametrize("bad", ["quote", "tick"])
def test_an_error_outside_the_four_classes_leaves_the_coverage(bad: str) -> None:
    """Natural inputs: a quote that is no `QuoteTick` (inside the guard), a non-tick (before)."""
    pub = ready()
    pub.set_coverage(Coverage(CoverageState.READY, True))
    with pytest.raises(AttributeError):
        if bad == "quote":
            pub.on_trade(tick(40, name="ZZPBA"), quote="zz")
        else:
            pub.on_trade("zz")  # type: ignore[arg-type]
    assert pub.coverage == Coverage(CoverageState.READY, True)
    assert pub.states["ZZPBA"] == MoverState()


LEFT_OUT = ("LANES_PIN", "METHOD", "MessageBus", "environ", "getenv", "socket", "thread")


def test_the_module_holds_none_of_the_names_left_out_and_no_publish() -> None:
    """The code is checked without its docstrings, which say what nothing here starts."""
    tree = ast.parse(Path(publisher.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(
            node
        ):
            node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    assert [name for name in LEFT_OUT if name in code] == []
    texts = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
    assert not texts & {"common", "adr"} and not hasattr(RankingPublisher, "publish")


def intake_answers(m: dict[str, Any], nt: Any) -> dict[str, Any]:
    """Seeded eligibility, constructor and intake answers; the parity child runs this too."""
    import random
    from datetime import date
    from decimal import Decimal as D

    rng = random.Random(20261112)  # noqa: S311 - a seeded fixture, not a secret
    ns, day, pick = 1_000_000_000, date(2030, 11, 29), lambda *xs: rng.choice(xs)
    start = (1_922_140_800 + 9 * 3600) * ns
    pre = m["Segment"]("PRE", day, start, start + 19_800 * ns, "fixture")
    night = m["Segment"]("OVERNIGHT", day, start - 5 * 3600 * ns, start, "fixture")
    values = {"mover_min_move": "0.05", "mover_min_trades_accel": "2", "move_1m_window_s": "60"}
    values |= {"mover_min_dvol_ratio": "0.5", "move_5m_window_s": "300"}
    values |= {"trades_accel_window_s": "60"}
    rows = [
        m["RegistryRow"](n, u, k(values.get(n, "5")), "zz", "UNCALIBRATED", day)
        for n, k, u in m["rows"]
    ]
    reg, pool = m["Registry"](tuple(rows)), ("ZZPBA", "ZZPBB", "ZZPBC", "ZZPBD")
    curve = ((0, D(0), D(0)), (600, D(500), D(100)))
    samples = [
        m["BaselineSample"](s, g, "2030-11-27", o, v, c)
        for s in pool
        for g in ("PRE", "OVERNIGHT")
        for o, v, c in curve
    ]
    tables = {
        f: m["BaselineTable"](tuple(samples), "zz", start - 6 * 3600 * ns, f)
        for f in ("sip", "boats")
    }

    def attempt(call: Any, *args: Any, **kw: Any) -> Any:
        try:
            return call(*args, **kw)
        except Exception as exc:
            return [type(exc).__name__, str(exc)]

    def shown(v: Any) -> Any:
        if isinstance(v, D):
            return str(v)
        return [shown(x) for x in v] if isinstance(v, tuple | list) else v

    def row(r: Any) -> Any:
        if r is None:
            return None
        names = ("symbol", "known_at", "score", "rank", "move", "move_1m", "move_5m")
        names += ("dvol_ratio", "trades_accel", "gates_passed", "params_hash")
        seen = {k: shown(v) for k, v in sorted(r.observations.items())}
        return {f: shown(getattr(r, f)) for f in names} | {"observations": seen}

    def started(**kw: Any) -> Any:
        p = m["make"](**kw)
        seg, cov = (p.segment.name, p.segment.start_ns), (p.coverage.state, p.coverage.complete)
        fresh = [[k, v == m["MoverState"]()] for k, v in p.states.items()]
        head = [p.epoch, p.feed, seg, p.registry.params_hash, p.universe, p.universe_source]
        tail = [p.baseline.feed, p.heartbeat, p.universe_hash, fresh, len(p.references)]
        marks = [len(p.assets), cov, p.published_at, p.sequence, p.last_event, p.input_cutoff]
        return [*head, *tail, *marks]

    def asset() -> Any:
        times = pick(-1, 0, 5, 5, 10, 11), pick(0, 5, 10, 11, 20, 20, 20)
        kind = pick("common", "adr", "common", "adr", "etf", "", "Common", ["zz"])
        halted = pick(True, *(False,) * 5, None, 0)
        return m["AssetState"](*times, halted, kind, pick("", *("zz",) * 7))

    eligible = []
    for _ in range(240):
        now = pick(0, 5, 9, 10, 11, 19, 20, True, False, -1)
        eligible.append([now, attempt(m["eligible"], asset(), now)])
    constructed, odd = [], (("ZZPBA",) * 2, ["ZZPBB", "ZZPBA"], ("ZZPBA", 7), (["ZZ"],), (7,), ())
    for _ in range(90):
        names = tuple(rng.sample(pool, rng.randint(1, 4))) if rng.random() < 0.7 else pick(*odd)
        feed = pick("sip", "sip", "boats", "iex")
        kw = {"epoch": pick(*("zz",) * 6, ""), "feed": feed, "registry": reg, "universe": names}
        kw["segment"] = (
            pick(night, pre) if rng.random() < 0.2 else night if feed == "boats" else pre
        )
        base = pick("sip", "boats") if rng.random() < 0.2 else "boats" if feed == "boats" else "sip"
        kw |= {"universe_source": pick(*("zz",) * 6, ""), "baseline": tables[base]}
        constructed.append(attempt(started, heartbeat=pick(None, "zz"), **kw))
    runs, prices = [], ("0.00", *("1.90", "2.00", "2.10", "2.40", "2.60") * 2)
    for i in range(36):
        names, feed = tuple(rng.sample(pool[:3], rng.randint(1, 3))), pick("sip", "boats")
        seg = pre if feed == "sip" else night
        kw = {"epoch": f"zz-{i}", "feed": feed, "segment": seg, "registry": reg, "universe": names}
        p = m["make"](universe_source="zz", baseline=tables[feed], **kw)
        t, steps = seg.start_ns + pick(0, rng.randint(1, 30) * ns), []
        for name in names if rng.random() < 0.8 else ():
            refs = m["ReferencePrices"](pick(None, D(2), D(2), D("1.5")), seg.start_ns - ns, "zz")
            p.set_symbol(name, references=refs, asset=None)
        for _ in range(rng.randint(14, 30)):
            r, name = rng.random(), pick(*names, *names, *names, "ZZPBD")
            if r < 0.1:
                refs = m["ReferencePrices"](pick(None, D(2), D("1.5")), seg.start_ns - ns, "zz")
                outcome = attempt(p.set_symbol, name, references=refs, asset=pick(None, asset()))
            elif r < 0.17:
                cover = m["Coverage"](pick(*m["CoverageState"]), rng.random() < 0.5)
                outcome = attempt(p.set_coverage, cover)
            else:
                t += pick(0, 1, ns, 5 * ns, 30 * ns, 61 * ns)
                when = t - rng.randint(1, 20) * ns if rng.random() < 0.08 else t
                when = seg.end_ns if rng.random() < 0.03 else when
                iid, px = nt.InstrumentId.from_str(name + ".SIM"), nt.Price.from_str(pick(*prices))
                size, side = nt.Quantity.from_int(rng.randint(1, 50)), nt.AggressorSide.BUY
                trade = nt.TradeTick(iid, px, size, side, nt.TradeId("t"), when, when + pick(0, 1))
                one, at = nt.Quantity.from_int(1), max(seg.start_ns, when - pick(0, ns))
                bid, ask = nt.Price.from_str("1.99"), nt.Price.from_str("2.01")
                quote = nt.QuoteTick(iid, bid, ask, one, one, at, at)
                facts = pick({}, {}, {}, {}, {"known_at": when + pick(0, 5 * ns)}, {"quote": quote})
                odd = {"price_eligible": False}, {"volume_eligible": False}, {"quote": "zz"}
                facts = pick(*odd, {"zz": 1}, facts, facts, facts, facts)
                outcome = attempt(p.on_trade, trade, **facts)
            held, cover = p.states.get(name), [p.coverage.state.value, p.coverage.complete]
            steps.append([outcome, cover, p.last_event, p.input_cutoff, row(held and held.latest)])
        runs.append(steps)
    return {"eligible": eligible, "constructed": constructed, "runs": runs, "sent": m["sent"]()}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


SENT: list[Send] = []


def made(**kw: Any) -> RankingPublisher:
    SENT.append(Send())
    return RankingPublisher(send=SENT[-1], method_version="zz-method", **FIXED, **kw)


SIDE = {
    "Registry": rg.Registry,
    "RegistryRow": rg.RegistryRow,
    "rows": [(n, k, u) for n, (k, u) in rg.ROWS.items()],
}
SIDE |= {
    "BaselineSample": bl.BaselineSample,
    "BaselineTable": bl.BaselineTable,
    "Segment": bl.Segment,
}
SIDE |= {"ReferencePrices": ReferencePrices, "MoverState": MoverState, "AssetState": AssetState}
SIDE |= {"Coverage": Coverage, "CoverageState": CoverageState, "make": made}
SIDE |= {
    "eligible": lambda a, now: a.eligible(now, security_types=TYPES),
    "sent": lambda: sum(len(s.calls) for s in SENT),
}
OURS = intake_answers(SIDE, nt)
ANSWERS = "0a9ce2becd912de67667d8176c0840b59512d207beb80315538cd2174b4c3be2"


def test_this_sides_answers_are_pinned() -> None:
    steps = [one for run in OURS["runs"] for one in run]
    raised = {one[0][0] for one in steps if one[0] is not None}
    assert len(OURS["eligible"]) >= 200 and len(OURS["constructed"]) >= 60
    assert len(OURS["runs"]) >= 30 and len(steps) >= 600 and OURS["sent"] == 0
    assert {"ValueError", "KeyError", "TypeError", "AttributeError"} <= raised
    assert raised & {"DivisionByZero", "InvalidOperation", "ZeroDivisionError"}
    assert sum(one[4] is not None for one in steps) > 100
    accepted = sum(len(c) > 2 for c in OURS["constructed"])
    assert 0 < accepted < len(OURS["constructed"])
    assert {e[1] is True for e in OURS["eligible"]} == {True, False}
    for run in OURS["runs"]:  # an AttributeError leaves the coverage the step before held
        for prior, one in pairwise(run):
            assert one[1] == prior[1] or one[0] is None or one[0][0] != "AttributeError"
    assert hashlib.sha256(canonical(OURS).encode()).hexdigest() == ANSWERS


PARITY = """
import hashlib, json, os, sys
SRC = os.environ["MOVERS_REFERENCE_SRC"]
PATH = os.path.join(SRC, "sextant", "ranking", "publisher.py")
with open(PATH, "rb") as handle:
    blob = handle.read()
BLOB = "498ac186d643a137b32f6ff84f9ca76c657fb0ee"
assert hashlib.sha1(b"blob %d\\0" % len(blob) + blob).hexdigest() == BLOB
sys.path.insert(0, SRC)
import nautilus_trader
import nautilus_trader.model as nt
from sextant.ranking import baseline as bl, engine as en, generation as g, publisher as pb
from sextant.ranking import registry as rg
assert os.path.samefile(pb.__file__, PATH)

class Bus:
    def __init__(self):
        self.calls = 0
    def publish(self, *args, **kw):
        self.calls += 1

buses = []
def make(**kw):
    buses.append(Bus())
    return pb.RankingPublisher(bus=buses[-1], **kw)

m = {"Registry": rg.Registry, "RegistryRow": rg.RegistryRow,
     "rows": [(n, k, u) for n, (k, u) in rg.ROWS.items()], "Segment": en.Segment,
     "BaselineSample": bl.BaselineSample, "BaselineTable": bl.BaselineTable,
     "ReferencePrices": en.ReferencePrices, "MoverState": en.MoverState,
     "AssetState": pb.AssetState,
     "Coverage": g.Coverage, "CoverageState": g.CoverageState, "make": make,
     "eligible": lambda a, now: a.eligible(now), "sent": lambda: sum(b.calls for b in buses)}
print("PARITY " + json.dumps({
    "versions": [list(sys.version_info[:3]), nautilus_trader.__version__],
    "answers": intake_answers(m, nt),
}))
"""
PARITY_NEEDS = "set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof"


@pytest.mark.local
def test_intake_parity_with_the_reference_in_a_child_interpreter() -> None:
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip(PARITY_NEEDS)
    script = "from typing import Any\n" + inspect.getsource(intake_answers) + PARITY
    env = {"MOVERS_REFERENCE_SRC": src, "PATH": os.environ.get("PATH", os.defpath)}
    done = subprocess.run(
        [python, "-I", "-B", "-c", script], env=env, capture_output=True, text=True, timeout=600
    )
    assert done.returncode == 0, done.stderr[-2000:]
    line = next(x for x in done.stdout.splitlines() if x.startswith("PARITY "))
    theirs = json.loads(line.removeprefix("PARITY "))
    here = [list(sys.version_info[:3]), sys.modules["nautilus_trader"].__version__]
    steps = sum(len(run) for run in theirs["answers"]["runs"])
    print(f"\npython, engine: here {here}, reference {theirs['versions']}")
    counts = [len(theirs["answers"][k]) for k in ("eligible", "constructed", "runs")]
    print(f"parity: eligible, constructed, runs {counts}, steps {steps}")
    assert canonical(theirs["answers"]) == canonical(OURS)
