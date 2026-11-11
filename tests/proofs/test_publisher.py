"""Part 5e-1: the asset state and the ranking publisher's constructor and trade intake.

Synthetic values only: invented ZZ symbols, made-up registry rows, no fixture file, no network and
no socket; the registry, baseline, segment and ticks come from `test_engine.py`'s helpers. The
reference is `src/sextant/ranking/publisher.py:51-134` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never imports it, and the seeded `local`
parity proof against it is the next change. `TYPES` and `EDGES` are the values the reference
hardcodes, passed here as test input, never shipped.
"""

import ast
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_engine import NS, POST, SCOPE, START, quote, registry, step, table, tick
from test_heartbeat import GOOD

from sextant_movers.ranking import generation as gen
from sextant_movers.ranking import publisher
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
