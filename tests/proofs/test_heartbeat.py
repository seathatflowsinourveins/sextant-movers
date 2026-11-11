"""Part 5d: the heartbeat emitter, the ranking data type and the universe digest.

Synthetic values only: invented ZZ symbols, made-up registry rows, no fixture file, no network
and no socket. The engine is nautechsystems/nautilus_trader v2.0.0rc8
`d53849b216e20f2c122ca01b4dd0da377638b186`: a topic is the type name, then the metadata pairs
sorted by key as `key=value`, joined by "." (`crates/model/src/data/data_type.rs:154-163,346-361`),
and two data types are equal, and hash equal, when their topics are (`:584-608`;
`crates/model/src/python/data/data_type.rs:49-59`). The reference is
`src/sextant/ranking/heartbeat.py` and `src/sextant/ranking/publisher.py:40-48` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never imports it, and the `local` proof
runs it in a child interpreter.
"""

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_registry import swap

from sextant_movers.ranking import generation as gen
from sextant_movers.ranking import heartbeat, publisher, registry
from sextant_movers.ranking.generation import CoverageState, RankingGeneration
from sextant_movers.ranking.heartbeat import HeartbeatEmitter, heartbeat_signal
from sextant_movers.ranking.publisher import RANKER, ranking_data_type, universe_digest
from sextant_movers.ranking.registry import Registry
from sextant_movers.ranking.window import NS

ROOT = Path(__file__).resolve().parents[2]
AGE = 3  # a made-up heartbeat_max_age_s, in seconds
REGISTRY = Registry(swap("heartbeat_max_age_s", value=AGE))
T0 = 10**15  # a made-up clock, in nanoseconds
SYMBOLS = ("ZZHBA", "ZZHBB", "ZZHBC")


def made(
    m: Any, known_at: int, symbols: tuple[str, ...] = ("ZZHBA", "ZZHBB", "ZZHBC"), **coverage: Any
) -> Any:
    """An eligible generation at `known_at` unless `coverage` changes it; children run this."""
    from datetime import date

    fields = {
        "state": m.CoverageState.READY,
        "complete": True,
        "expected_symbols": len(symbols),
        "observed_symbols": len(symbols),
        "missing_symbols": 0,
        "late_symbols": 0,
        "invalid_symbols": 0,
        **coverage,
    }
    return m.RankingGeneration(
        generation_id=m.GenerationId("ZZHB-epoch", 1),
        session_date=date(2001, 2, 3),
        input_cutoff=known_at - 2,
        ts_event=known_at - 3,
        ts_init=known_at + 1,
        known_at=known_at,
        method_version="zz-method",
        profile_version="zz-profile",
        ranker_version="zz-ranker",
        coverage=m.Coverage(**fields),
        status=m.GenerationStatus.PROVISIONAL,
        rows=tuple(m.RankingRow(symbol) for symbol in symbols),
    )


GOOD: RankingGeneration = made(gen, T0)


class Clock:
    def __init__(self, now: object = T0) -> None:
        self.now, self.calls = now, 0

    def __call__(self) -> Any:
        self.calls += 1
        return self.now


class Sink:
    """Records each call's keywords and answers in turn (an exception is raised), then True."""

    def __init__(self, *answers: object) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers = list(answers)

    def __call__(self, **keywords: Any) -> Any:
        self.calls.append(keywords)
        answer = self.answers.pop(0) if self.answers else True
        if isinstance(answer, BaseException):
            raise answer
        return answer


def beat(clock: Clock | None = None, sink: Sink | None = None) -> tuple[HeartbeatEmitter, Any, Any]:
    clock, sink = clock or Clock(), sink or Sink()
    return HeartbeatEmitter(registry=REGISTRY, clock=clock, sink=sink), clock, sink


def test_one_accepted_beat_sends_at_and_received_ns_as_ints() -> None:
    emitter, clock, sink = beat(Clock(T0 + 5))
    assert emitter.emit(GOOD) is True
    assert sink.calls == [{"at": T0, "received_ns": T0 + 5}]
    assert [type(value) for value in sink.calls[0].values()] == [int, int]
    assert (clock.calls, emitter.sink_failures) == (1, 0)


def chain(**clocks: int) -> RankingGeneration:
    return replace(GOOD, **clocks)


INELIGIBLE: dict[str, RankingGeneration] = {
    **{
        f"state-{state.value.lower()}": made(gen, T0, state=state)
        for state in CoverageState
        if state is not CoverageState.READY
    },
    "not-complete": made(gen, T0, complete=False),
    **{
        f"{name}-{value}": made(gen, T0, **{name: value})
        for name in ("missing_symbols", "invalid_symbols", "late_symbols")
        for value in (1, None)
    },
    **{
        f"{name}-{value}": made(gen, T0, **{name: value})
        for name in ("expected_symbols", "observed_symbols")
        for value in (2, 4, None)
    },
    "fewer-rows": made(gen, T0, SYMBOLS[:2], expected_symbols=3, observed_symbols=3),
    "more-rows": made(gen, T0, (*SYMBOLS, "ZZHBD"), expected_symbols=3, observed_symbols=3),
    "event-after-cutoff": chain(ts_event=T0 - 1),
    "cutoff-after-known-at": chain(input_cutoff=T0 + 1),
    "known-at-after-init": chain(ts_init=T0 - 1),
}


@pytest.mark.parametrize("case", INELIGIBLE)
def test_an_ineligible_generation_touches_neither_clock_nor_sink(case: str) -> None:
    emitter, clock, sink = beat()
    assert emitter.emit(INELIGIBLE[case]) is False
    assert (clock.calls, sink.calls) == (0, [])


@pytest.mark.parametrize(
    "value", [None, GOOD.to_dict(), GOOD.to_json(), (GOOD,)], ids=["none", "dict", "text", "tuple"]
)
def test_a_non_generation_returns_false_without_clock_or_sink(value: Any) -> None:
    emitter, clock, sink = beat()
    assert emitter.emit(value) is False
    assert (clock.calls, sink.calls, emitter.sink_failures) == (0, [], 0)


def test_the_age_bound_is_nanoseconds_on_both_sides() -> None:
    """`Registry.seconds` returns nanoseconds, and `received_ns - at` is nanoseconds."""
    assert REGISTRY.seconds("heartbeat_max_age_s") == AGE * NS == 3_000_000_000


@pytest.mark.parametrize(
    ("offset", "sent"),
    [(0, True), (AGE * NS, True), (AGE * NS + 1, False), (-1, False)],
    ids=["same-instant", "at-the-bound", "one-past", "clock-before-at"],
)
def test_the_receipt_age_at_its_edge_and_a_clock_before_at(offset: int, sent: bool) -> None:
    emitter, clock, sink = beat(Clock(T0 + offset))
    assert emitter.emit(GOOD) is sent
    assert (clock.calls, len(sink.calls)) == (1, int(sent))


def test_an_equal_or_regressed_at_is_refused_and_a_later_one_sent() -> None:
    emitter, _, sink = beat(Clock(T0 + 10))
    assert emitter.emit(made(gen, T0)) is True
    assert emitter.emit(made(gen, T0)) is False
    assert emitter.emit(made(gen, T0 - 1)) is False
    assert emitter.emit(made(gen, T0 + 1)) is True
    assert [call["at"] for call in sink.calls] == [T0, T0 + 1]


def test_a_false_sink_is_not_sent_and_the_last_at_stays() -> None:
    emitter, _, sink = beat(sink=Sink(False))
    assert emitter.emit(GOOD) is False
    assert emitter._last_at is None
    assert emitter.emit(GOOD) is True  # the same `at` again: the refusal did not move the last
    assert (len(sink.calls), emitter.sink_failures, emitter._last_at) == (2, 0, T0)


def test_a_raising_sink_is_counted_not_raised_and_the_next_beat_works() -> None:
    emitter, _, sink = beat(sink=Sink(RuntimeError("ZZ sink down")))
    assert emitter.emit(GOOD) is False
    assert (emitter.sink_failures, emitter._last_at) == (1, None)
    assert emitter.emit(GOOD) is True
    assert (emitter.sink_failures, len(sink.calls)) == (1, 2)


def test_the_signal_name_is_built_from_the_feed() -> None:
    assert heartbeat_signal("sip") == "ranking.heartbeat.sip"


def test_the_type_name_is_the_registered_one() -> None:
    assert ranking_data_type("sip").type_name == gen.TYPE_NAME
    assert gen.TYPE_NAME == RankingGeneration.type_name_static()


def test_the_metadata_and_the_topic() -> None:
    data_type = ranking_data_type("sip")
    assert RANKER == "sextant/fixture-ranking/1"
    assert data_type.metadata == {"feed": "sip", "view": "full", "ranker": RANKER}
    assert (
        data_type.topic == "RankingGeneration.feed=sip.ranker=sextant/fixture-ranking/1.view=full"
    )
    assert data_type.identifier is None
    assert ranking_data_type("sip", "full") == data_type


def test_two_feeds_and_two_views_give_four_topics() -> None:
    topics = {
        ranking_data_type(feed, view).topic for feed in ("sip", "iex") for view in ("full", "top")
    }
    assert len(topics) == 4


def test_equal_arguments_give_equal_hash_equal_data_types() -> None:
    one, two = ranking_data_type("iex", "top"), ranking_data_type("iex", "top")
    assert one is not two
    assert one == two and hash(one) == hash(two)
    assert one != ranking_data_type("iex")


BUS = """
import json, sys
sys.path.insert(0, sys.argv[1])
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import DataActor
from nautilus_trader.model import CustomData
from sextant_movers.ranking import generation as gen
from sextant_movers.ranking.publisher import ranking_data_type

gen.register_ranking_generation()
full, top, sent = ranking_data_type("sip"), ranking_data_type("sip", "top"), made(gen, 10**15)
seen = {"full": [], "top": []}

class Full(DataActor):
    def on_start(self):
        self.subscribe_data(full)
    def on_data(self, data):
        seen["full"].append(data.data is sent)

class Top(DataActor):
    def on_start(self):
        self.subscribe_data(top)
    def on_data(self, data):
        seen["top"].append(data.data is sent)

class Publisher(DataActor):
    def on_start(self):
        self.publish_data(full, CustomData(full, sent))

engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True, run_analysis=False))
try:
    for actor in (Full(), Top(), Publisher()):
        engine.add_actor(actor)
    engine.run()
finally:
    engine.dispose()
print("SEEN " + json.dumps(seen))
"""
PRELUDE = "from typing import Any\n"


def test_a_published_generation_reaches_its_own_view_only() -> None:
    """The part 3d bus shape, in a fresh `-I` child so this process registers nothing."""
    script = PRELUDE + inspect.getsource(made) + BUS
    done = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "src")],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    line = next(x for x in done.stdout.splitlines() if x.startswith("SEEN "))
    assert json.loads(line.removeprefix("SEEN ")) == {"full": [True], "top": []}


EMPTY = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def test_the_universe_digest_ignores_order_counts_repeats_and_pins_the_empty_tuple() -> None:
    digest = universe_digest(SYMBOLS)
    assert digest == hashlib.sha256(b'["ZZHBA","ZZHBB","ZZHBC"]').hexdigest()
    assert universe_digest(tuple(reversed(SYMBOLS))) == digest
    assert universe_digest((*SYMBOLS, "ZZHBA")) != digest
    assert universe_digest(()) == EMPTY


def test_the_universe_digest_validates_nothing() -> None:
    assert universe_digest(("zzhba", " ", "")) == hashlib.sha256(b'[""," ","zzhba"]').hexdigest()


LEFT_OUT = ("LANES_PIN", "METHOD", "AssetState", "environ", "getenv", "socket", "thread")


@pytest.mark.parametrize("module", [heartbeat, publisher], ids=["heartbeat", "publisher"])
def test_the_new_modules_hold_none_of_the_names_left_out(module: Any) -> None:
    """The code is checked without its docstrings, which say what nothing here starts."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            if ast.get_docstring(node) is not None:
                node.body = node.body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    assert [name for name in LEFT_OUT if name in code] == []


def answer_all(hb: Any, pub: Any, g: Any, reg: Any) -> dict[str, Any]:
    """Seeded emitter steps, universe digests and data types; the parity child runs this."""
    import random
    from dataclasses import replace
    from datetime import date

    rng = random.Random(20261111)  # noqa: S311 - a seeded fixture, not a secret
    ns, raise_, names = 1_000_000_000, object(), ("ZZHBA", "ZZHBB", "ZZHBC", "ZZHBD")
    registry = reg.Registry(
        tuple(
            reg.RegistryRow(name, unit, kind(3), "made-up", "UNCALIBRATED", date(2001, 2, 3))
            for name, (kind, unit) in reg.ROWS.items()
        )
    )
    reading, clock_calls = [0], [0]

    def clock() -> Any:
        clock_calls[0] += 1
        return reading[0]

    class Sink:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def __call__(self, **keywords: Any) -> Any:
            self.calls.append(sorted((k, v, type(v).__name__) for k, v in keywords.items()))
            answer = rng.choice((True, True, True, False, raise_, 1, 0, None, ""))
            if answer is raise_:
                raise RuntimeError("ZZ sink down")
            return answer

    def candidate(at: int) -> Any:
        pick = rng.randrange(12)
        if pick < 6:
            return made(g, at, names[: rng.randrange(5)])
        if pick == 6:
            states = [s for s in g.CoverageState if s is not g.CoverageState.READY]
            return made(g, at, state=rng.choice(states))
        if pick == 7:
            return made(g, at, complete=False)
        if pick == 8:
            count = rng.choice(("expected", "observed", "missing", "late", "invalid"))
            return made(g, at, **{f"{count}_symbols": rng.choice((None, 1, 2, 4))})
        if pick == 9:
            return made(g, at, names[:2], expected_symbols=3, observed_symbols=3)
        if pick == 10:
            clocks = ({"ts_event": at - 1}, {"input_cutoff": at + 1}, {"ts_init": at - 1})
            return replace(made(g, at), **rng.choice(clocks))
        return rng.choice((None, "ZZHBA", 7))

    runs = []
    for run in range(6):
        sink, at, steps = Sink(), 10**15 + run, []
        emitter = hb.HeartbeatEmitter(registry=registry, clock=clock, sink=sink)
        for _ in range(60):
            at = at + rng.choice((-ns, -1, 0, 0, 1, 1, ns, 2 * ns))
            offsets = (0, 1, 3 * ns, 3 * ns + 1, -1, rng.randrange(4 * ns))
            reading[0] = rng.choice((*(at + o for o in offsets), True, float(at), None))
            before = clock_calls[0]
            steps.append([emitter.emit(candidate(at)), clock_calls[0] - before, len(sink.calls)])
        runs.append({"steps": steps, "calls": sink.calls, "sink_failures": emitter.sink_failures})
    pool = ("ZZHBA", "ZZHBB", "ZZHBC", "ZZHBD", "ZZHBÉ", "ZZHB中")
    digests = []
    for _ in range(120):
        universe = tuple(rng.choice(pool) for _ in range(rng.randrange(7)))
        digests.append([list(universe), pub.universe_digest(universe)])
    types = []
    for feed in ("sip", "iex", "1", "true", "a.b=c", ""):
        for view in ("full", "top", None):
            dt = pub.ranking_data_type(feed) if view is None else pub.ranking_data_type(feed, view)
            types.append(
                [feed, view, dt.type_name, dt.metadata, dt.topic, hb.heartbeat_signal(feed)]
            )
    return {"emitter": runs, "digests": digests, "types": types, "ranker": pub.RANKER}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


OURS = answer_all(heartbeat, publisher, gen, registry)
ANSWERS = "d5ec9a7fafdd7c08734edf3b425d0c2d3c8bee4012bf9bdeb43264587c5e7be1"


def test_this_sides_answers_are_pinned() -> None:
    steps = [step for run in OURS["emitter"] for step in run["steps"]]
    sent = sum(step[0] for step in steps)
    assert len(steps) == 360 and 0 < sent < len(steps)
    assert sum(run["sink_failures"] for run in OURS["emitter"]) > 0
    assert (len(OURS["digests"]), len(OURS["types"])) == (120, 18)
    assert hashlib.sha256(canonical(OURS).encode()).hexdigest() == ANSWERS


PARITY = """
import json, os, sys
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
import nautilus_trader
from sextant.ranking import generation, heartbeat, publisher, registry
assert os.path.samefile(heartbeat.__file__, os.path.join(SRC, "sextant", "ranking", "heartbeat.py"))
print("PARITY " + json.dumps({
    "versions": [list(sys.version_info[:3]), nautilus_trader.__version__],
    "answers": answer_all(heartbeat, publisher, generation, registry),
}))
"""
PARITY_NEEDS = (
    "set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof; the second "
    "names the reference's own interpreter because its modules import the engine at their top"
)


@pytest.mark.local
def test_heartbeat_parity_with_the_reference_in_a_child_interpreter() -> None:
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip(PARITY_NEEDS)
    script = PRELUDE + inspect.getsource(made) + inspect.getsource(answer_all) + PARITY
    done = subprocess.run(
        [python, "-I", "-B", "-c", script],
        env={"MOVERS_REFERENCE_SRC": src, "PATH": os.environ.get("PATH", os.defpath)},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    line = next(x for x in done.stdout.splitlines() if x.startswith("PARITY "))
    theirs = json.loads(line.removeprefix("PARITY "))
    here = [list(sys.version_info[:3]), sys.modules["nautilus_trader"].__version__]
    steps = sum(len(run["steps"]) for run in theirs["answers"]["emitter"])
    print(f"python, engine: here {here}, reference {theirs['versions']}")
    print(
        f"parity: {steps} emitter steps, {len(theirs['answers']['digests'])} digests, "
        f"{len(theirs['answers']['types'])} data types"
    )
    assert canonical(theirs["answers"]) == canonical(OURS)
