"""Part 6: compact session curves, synthetic bars only, no request or stream.

Acceptance: the reference at 06562bec0898a7203f4eaf69086ced83d2477e6b,
`src/sextant/tradingview/baseline.py:25-212`, `prestage.py:208-282`.
Standard library median/array semantics are pinned in the module header.
"""

import ast
import hashlib
import json
from array import array
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any, get_protocol_members

import pytest
from test_publisher import ASSET, REFS, SCOPE, START, ready, tick

from sextant_movers.ranking import curves
from sextant_movers.ranking.bars import MINUTE_NS, Bar, nanos
from sextant_movers.ranking.baseline import NS, Baseline, Segment
from sextant_movers.ranking.curves import SessionCurves, build_curves
from sextant_movers.ranking.generation import Coverage, CoverageState
from sextant_movers.sessions import ET

DAY = date(2026, 10, 19)
NAMES = ("ZZCVA", "ZZCVB")


def segment(name: str = "PRE", minutes: int = 4) -> Segment:
    hour, minute = {"PRE": (4, 0), "RTH": (9, 30), "POST": (16, 0)}[name]
    start = nanos(datetime(2026, 11, 30, hour, minute, tzinfo=ET))
    return Segment(name, date(2026, 11, 30), start, start + minutes * MINUTE_NS, "fixture")


def bar(day: date = DAY, minute: int = 0, *, seg: Segment | None = None, **kw: Any) -> Bar:
    scope = seg or segment()
    clock = datetime.fromtimestamp(scope.start_ns // NS, ET).timetz()
    start = nanos(datetime.combine(day, clock)) + minute * MINUTE_NS
    values: dict[str, Any] = dict(
        symbol=NAMES[0], start_ns=start, open=D(7), close=D("3.25"), volume=8, trades=2
    )
    return Bar(**(values | kw))


def build(sessions: Any = None, **kw: Any) -> SessionCurves:
    values: dict[str, Any] = dict(
        symbols=NAMES, segment=segment(), known_at=0, provenance="fixture", feed="sip"
    )
    return build_curves([(DAY, [bar()])] if sessions is None else sessions, **(values | kw))


def constructor(**kw: Any) -> SessionCurves:
    values: dict[str, Any] = dict(
        segment="PRE", end_s=120, sessions=(DAY,), symbols=(NAMES[0],), known_at=0
    )
    values |= dict(provenance="fixture", dv=(array("q", [0, 10000, 30000]),))
    values |= dict(counts=(array("i", [0, 2, 6]),), data_digest="fixture digest", feed="sip")
    return SessionCurves(**(values | kw))


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"provenance": ""}, "needs provenance"),
        ({"segment": ""}, "names its segment"),
        ({"end_s": 0}, "ends on a whole minute"),
        ({"end_s": -60}, "ends on a whole minute"),
        ({"end_s": 61}, "ends on a whole minute"),
        ({"end_s": True}, "ends on a whole minute"),
        ({"end_s": D(120)}, "ends on a whole minute"),
        ({"sessions": ()}, "stores distinct prior sessions"),
        ({"sessions": (DAY, DAY)}, "stores distinct prior sessions"),
        ({"symbols": ()}, "stores distinct symbols"),
        ({"symbols": (NAMES[0],) * 2}, "stores distinct symbols"),
        ({"dv": ()}, "one curve per session"),
        ({"counts": ()}, "one curve per session"),
        ({"dv": (array("q", [0]),)}, "covers every symbol and point"),
        ({"counts": (array("i", [0]),)}, "covers every symbol and point"),
        ({"feed": ""}, "feed"),
        ({"feed": None}, "feed"),
        ({"feed": True}, "feed"),
    ],
)
def test_constructor_refusals_alone(kw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        constructor(**kw)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, (0, 0)),
        (60, (1, 2)),
        (D("90.5"), (D(1) + D(61) / 60, D(2) + D(61) / 30)),
        (120, (3, 6)),
        (121, None),
        (-1, None),
    ],
)
def test_cumulative_minutes_fraction_zero_end_and_no_extrapolation(
    offset: Any, expected: Any
) -> None:
    value = constructor().cumulative(NAMES[0], "PRE", offset)
    if expected is None:
        assert value is None
    else:
        assert value is not None
        assert (value.dollar_volume, value.trade_count) == expected
        assert (value.sessions, value.known_at, value.provenance) == (1, 0, "fixture")


def test_other_segment_and_unknown_symbol_have_no_answer() -> None:
    curve = constructor()
    assert curve.cumulative(NAMES[0], "POST", 60) is None
    assert curve.cumulative("ZZUNKNOWN", "PRE", 60) is None


@pytest.mark.parametrize(("values", "expected"), [([1, 7, 3], 3), ([1, 7, 3, 9], 5)])
def test_median_after_interpolation_for_odd_and_even_sessions(
    values: list[int], expected: int
) -> None:
    days = tuple(DAY + timedelta(days=i) for i in range(len(values)))
    curve = constructor(
        sessions=days,
        dv=tuple(array("q", [0, v * 10000, 0]) for v in values),
        counts=tuple(array("i", [0, v, 0]) for v in values),
    )
    value = curve.cumulative(NAMES[0], "PRE", 30)
    assert value is not None
    assert (value.dollar_volume, value.trade_count) == (D(expected) / 2,) * 2


@pytest.mark.parametrize(
    "part", ["segment", "end_s", "sessions", "symbols", "known_at", "provenance", "data_digest"]
)
def test_identity_changes_with_each_of_its_seven_parts_alone(part: str) -> None:
    changes = dict(
        segment="POST",
        end_s=60,
        sessions=(DAY + timedelta(days=1),),
        symbols=(NAMES[1],),
        known_at=1,
        provenance="other",
        data_digest="other",
    )
    # A shorter span needs a shorter array too; digest supplied unchanged, not re-derived.
    kw = {part: changes[part]}
    if part == "end_s":
        kw |= {"dv": (array("q", [0, 10000]),), "counts": (array("i", [0, 2]),)}
    assert constructor(**kw).identity != constructor().identity


def test_identity_is_the_reference_sorted_compact_json() -> None:
    payload = dict(
        segment="PRE",
        end_s=120,
        sessions=[DAY.isoformat()],
        symbols=[NAMES[0]],
        known_at=0,
        provenance="fixture",
        data="fixture digest",
    )
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert constructor().identity == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(
    ("sessions", "kw", "message"),
    [
        (None, {"provenance": ""}, "needs provenance"),
        (None, {"known_at": -1}, "non-negative known-at"),
        (None, {"known_at": True}, "non-negative known-at"),
        (None, {"known_at": "0"}, "non-negative known-at"),
        ([], {}, "distinct prior sessions"),
        ([(DAY, []), (DAY, [])], {}, "strictly ascending"),
        ([(DAY, []), (DAY - timedelta(days=1), [])], {}, "strictly ascending"),
        (None, {"symbols": ()}, "distinct symbols"),
        (None, {"symbols": (NAMES[0],) * 2}, "distinct symbols"),
        (None, {"segment": replace(segment(), end_ns=segment().end_ns + 1)}, "whole minutes"),
        (None, {"segment": replace(segment(), name="OVERNIGHT")}, "PRE, RTH or POST"),
        ([(DAY, [bar(symbol="ZZOUTSIDE")])], {}, "outside the baseline universe"),
        (None, {"feed": ""}, "feed"),
        (None, {"feed": 7}, "feed"),
    ],
)
def test_builder_refusals_alone(sessions: Any, kw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build(sessions, **kw)


def test_feed_and_other_authority_arguments_have_no_defaults() -> None:
    import inspect

    for call in (SessionCurves, build_curves):
        params = inspect.signature(call).parameters
        for name in ("feed", "known_at", "provenance", "sessions"):
            assert params[name].default is inspect.Parameter.empty
        assert params["feed"].kind is inspect.Parameter.KEYWORD_ONLY


def test_skips_before_start_last_point_and_later_but_carries_missing_minutes() -> None:
    items = [bar(minute=i) for i in (-1, 0, 2, 4, 5)]
    curve = build([(DAY, items)])
    assert list(curve.dv[0][:5]) == [0, 260000, 260000, 520000, 520000]
    assert list(curve.counts[0][:5]) == [0, 2, 2, 4, 4]
    assert curve.dv[0].typecode == "q" and curve.counts[0].typecode == "i"
    at_end = curve.cumulative(NAMES[0], "PRE", curve.end_s)
    assert at_end is not None and at_end.dollar_volume == 52


def test_symbol_without_bars_has_a_flat_zero_curve_and_still_answers() -> None:
    curve = build()
    assert list(curve.dv[0][curve.points :]) == [0] * curve.points
    value = curve.cumulative(NAMES[1], "PRE", 120)
    assert value is not None and value.dollar_volume == value.trade_count == 0


def test_prior_day_uses_current_segments_new_york_clock_even_across_dst() -> None:
    scope = segment()
    prior = date(2026, 10, 30)
    start = curves._session_start(prior, scope)
    assert start == nanos(datetime(2026, 10, 30, 4, tzinfo=ET))
    assert datetime.fromtimestamp(start // NS, ET).utcoffset() == timedelta(hours=-4)
    assert datetime.fromtimestamp(scope.start_ns // NS, ET).utcoffset() == timedelta(hours=-5)
    assert build([(prior, [bar(prior)])]).dv[0][1] == 260000


def test_finishes_each_sessions_arrays_before_requesting_the_next() -> None:
    events: list[str] = []

    def items(day: date) -> Iterator[Bar]:
        events.append("bar")
        yield bar(day)
        events.append("exhausted")

    def sessions() -> Iterator[tuple[date, Iterator[Bar]]]:
        for i in range(3):
            if i:
                assert events[-1] == "digested"
            events.append("session")
            day = DAY + timedelta(days=i)
            yield day, items(day)

    class Digest:
        def __init__(self) -> None:
            self.updates = 0

        def update(self, value: bytes) -> None:
            self.updates += 1
            if self.updates % 3 == 0:
                assert events[-1] == "exhausted"
                events.append("digested")

        def hexdigest(self) -> str:
            return "fixture"

    from unittest.mock import patch

    with patch.object(curves, "sha256", return_value=Digest()):
        # Constructor also hashes; avoid mocking its JSON hash interface.
        with patch.object(curves, "SessionCurves", return_value=None):
            build(sessions())
    assert events == ["session", "bar", "exhausted", "digested"] * 3


def test_generator_and_materialized_sessions_give_the_same_identity_and_bytes() -> None:
    rows = [(DAY + timedelta(days=i), []) for i in range(7)]
    one, streamed = build(rows), build((day, iter(bars)) for day, bars in rows)
    assert one.identity == streamed.identity
    assert [a.tobytes() for a in one.dv] == [a.tobytes() for a in streamed.dv]


def test_feed_only_difference_is_kept_out_of_identity() -> None:
    a, b = build(feed="fixture-a"), build(feed="fixture-b")
    assert a.feed != b.feed and a.identity == b.identity


@pytest.mark.parametrize("field", ["volume", "trades"])
def test_array_overflow_is_not_caught(field: str) -> None:
    code = "q" if field == "volume" else "i"
    too_large = 1 << (8 * array(code).itemsize - 1)
    with pytest.raises(OverflowError):
        build([(DAY, [bar(volume=too_large) if field == "volume" else bar(trades=too_large)])])


def test_duplicate_bar_is_summed_twice() -> None:
    item = bar()
    curve = build([(DAY, [item, item])])
    assert curve.dv[0][1] == 520000 and curve.counts[0][1] == 4


def test_early_close_prior_post_uses_current_clock_and_keeps_1600_to_2000() -> None:
    scope = segment("POST", 240)
    prior = date(2026, 11, 27)  # XNYS early close at 13:00 on this calendar date.
    items = [bar(prior, m, seg=scope) for m in (-180, -1, 0, 239, 240)]
    curve = build([(prior, items)], segment=scope)
    assert curve.dv[0][1] == 260000 and curve.dv[0][curve.points - 1] == 520000
    assert curves._session_start(prior, scope) == nanos(datetime(2026, 11, 27, 16, tzinfo=ET))


def test_bool_known_at_is_accepted_by_class_but_refused_by_builder() -> None:
    assert constructor(known_at=True).known_at is True
    with pytest.raises(ValueError, match="known-at"):
        build(known_at=True)


def test_sub_scale_dollars_are_truncated_per_bar_before_summing() -> None:
    item = bar(close=D("0.00009"), volume=1)
    assert build([(DAY, [item, item])]).dv[0][1] == 0


def test_baseline_protocol_has_only_the_consumed_members() -> None:
    assert get_protocol_members(Baseline) == {
        "cumulative",
        "provenance",
        "identity",
        "known_at",
        "feed",
        "latest_session",
    }


def test_publisher_accepts_curves_and_source_reason_carries_their_identity() -> None:
    names = ("ZZPBA", "ZZPBB", "ZZPBC")
    prior = SCOPE.session_date - timedelta(days=1)
    baseline: Baseline = build(
        [(prior, [bar(prior, seg=SCOPE, symbol=s) for s in names])],
        symbols=names,
        segment=SCOPE,
        known_at=START - NS,
    )
    pub = ready(baseline=baseline)
    assert pub.references[names[0]] is REFS and pub.assets[names[0]] is ASSET
    for second, price in ((0, "2.00"), (60, "2.10"), (300, "2.40"), (360, "2.60")):
        for name in names:
            pub.on_trade(tick(second, price, name=name))
    pub.set_coverage(Coverage(CoverageState.READY, True, 3, 3, 0, 0, 0))
    generation = pub.publish(now=START + 360 * NS + 100)
    assert generation is not None and generation.rows
    source = next(r for r in generation.rows[0].reasons if r.code == "SOURCE")
    assert json.loads(source.detail or "null")["baseline_hash"] == baseline.identity
    assert json.loads(source.detail or "null")["baseline_source"] == baseline.provenance


def test_zero_baseline_keeps_engine_ratio_unavailable() -> None:
    names = ("ZZPBA", "ZZPBB", "ZZPBC")
    prior = SCOPE.session_date - timedelta(days=1)
    baseline: Baseline = build([(prior, [])], symbols=names, segment=SCOPE, known_at=START - NS)
    pub = ready(baseline=baseline)
    pub.on_trade(tick(0, name=names[0]))
    pub.on_trade(tick(60, name=names[0]))
    row = pub.states[names[0]].latest
    assert row is not None and row.dvol_ratio is None
    assert "baseline_zero_dollar_volume" in row.observations["unknown_reasons"]


def test_module_has_no_feed_literal_session_count_or_forbidden_import() -> None:
    tree = ast.parse(Path(curves.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and ast.get_docstring(
            node
        ):
            node.body = node.body[1:] or [ast.Pass()]
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert "sip" not in constants and "boats" not in constants and 5 not in constants
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [n.name for n in node.names]
        if isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert not any(n.split(".")[0] in {"sextant", "alpaca", "socket"} for n in imports)
    assert not any("trading" in n for n in imports)


def test_interpolation_precedes_median_when_session_curves_cross() -> None:
    days = tuple(DAY + timedelta(days=i) for i in range(3))
    values = ([0, 0, 100], [0, 100, 0], [0, 60, 60])
    curve = constructor(
        sessions=days,
        dv=tuple(array("q", [v * 10000 for v in row]) for row in values),
        counts=tuple(array("i", row) for row in values),
    )
    value = curve.cumulative(NAMES[0], "PRE", 90)
    assert value is not None
    # Session answers are 50, 50, 60; interpolating the point medians would give 60.
    assert value.dollar_volume == value.trade_count == 50


# The seeded extension is kept last so the unit/code portion can be reviewed separately.
def seeded_cases() -> list[dict[str, Any]]:
    import random

    rng = random.Random(20261119)  # noqa: S311 - synthetic fixtures, not a secret
    days = [date(2026, 10, 26) + timedelta(days=i) for i in range(12)]
    cases = []
    for case_index in range(42):
        name = ("PRE", "RTH", "POST")[case_index % 3]
        minutes = {"PRE": 330, "RTH": 390, "POST": 240}[name]
        scope = segment(name, minutes)
        if case_index % 10 == 0:
            # Target on an early-close day: caller supplies the shorter RTH or earlier POST.
            hour, minute = {"PRE": (4, 0), "RTH": (9, 30), "POST": (13, 0)}[name]
            start = nanos(datetime(2026, 11, 27, hour, minute, tzinfo=ET))
            minutes = 210 if name == "RTH" else minutes
            scope = Segment(name, date(2026, 11, 27), start, start + minutes * MINUTE_NS, "zz")
        picked = sorted(rng.sample(days, case_index % 7 + 1))
        names = list(NAMES if case_index % 2 else reversed(NAMES))
        sessions = []
        for day in picked:
            clock = datetime.fromtimestamp(scope.start_ns // NS, ET).timetz()
            start = nanos(datetime.combine(day, clock))
            bars = []
            for symbol in names:
                dense = case_index % 4 == 0
                offsets = list(range(minutes)) if dense else rng.sample(range(minutes), 4)
                offsets += [-1, minutes, minutes + 1]
                for offset in offsets:
                    close = str(D(rng.randint(1, 900_000)).scaleb(-rng.randint(0, 6)))
                    bars.append(
                        [
                            symbol,
                            start + offset * MINUTE_NS,
                            "9.75",
                            close,
                            rng.randint(0, 400),
                            rng.randint(0, 80),
                            "1.125",
                        ]
                    )
            rng.shuffle(bars)
            sessions.append([day.isoformat(), bars])
        queries = []
        for symbol in (*NAMES, "ZZUNKNOWN"):
            for offset in ("-1", "0", "60", "90.125", str(minutes * 60), str(minutes * 60 + 1)):
                queries.append([symbol, name, offset])
        queries.append([NAMES[0], "OVERNIGHT", "60"])
        for i in range(60):
            offset = (
                str(rng.randint(0, minutes) * 60)
                if i % 2
                else str(D(rng.randint(0, minutes * 60)) + D("0.375"))
            )
            queries.append([rng.choice(NAMES), name, offset])
        cases.append(
            {
                "segment": [
                    scope.name,
                    scope.session_date.isoformat(),
                    scope.start_ns,
                    scope.end_ns,
                    scope.source,
                ],
                "symbols": names,
                "sessions": sessions,
                "known_at": scope.start_ns - NS,
                "provenance": f"fixture curves {case_index}",
                "queries": queries,
            }
        )
    return cases


def case_answer(
    make: Any, bar_type: Any, segment_type: Any, case: dict[str, Any]
) -> dict[str, Any]:
    """Shared field conversion and answer presentation, never shared curve arithmetic."""
    name, day, start, end, source = case["segment"]
    scope = segment_type(name, date.fromisoformat(day), start, end, source)
    sessions = []
    for day, fields in case["sessions"]:
        bars = [bar_type(s, t, D(o), D(c), v, n, D(w)) for s, t, o, c, v, n, w in fields]
        sessions.append((date.fromisoformat(day), bars))
    curve = make(
        sessions,
        symbols=case["symbols"],
        segment=scope,
        known_at=case["known_at"],
        provenance=case["provenance"],
    )
    answers = []
    for symbol, name, offset in case["queries"]:
        value = curve.cumulative(symbol, name, D(offset) if "." in offset else int(offset))
        answers.append(
            None
            if value is None
            else [
                str(value.dollar_volume),
                str(value.trade_count),
                value.sessions,
                value.known_at,
                value.provenance,
            ]
        )
    return {
        "identity": curve.identity,
        "dv": [a.tobytes().hex() for a in curve.dv],
        "counts": [a.tobytes().hex() for a in curve.counts],
        "answers": answers,
    }


def seeded_answers(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def make(*args: Any, **kw: Any) -> SessionCurves:
        return build_curves(*args, feed="sip", **kw)

    return [case_answer(make, Bar, Segment, case) for case in cases]


def answer_digest(answers: Any) -> str:
    return hashlib.sha256(
        json.dumps(answers, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


SEEDED_DIGEST = "ed3479bffd44d9744bde2bfa8a50ae0410104fc51dd235ca36221df6bfe82cfb"


def test_seeded_builds_bytes_and_query_answers_are_pinned() -> None:
    cases = seeded_cases()
    assert len(cases) >= 40 and sum(len(c["queries"]) for c in cases) >= 2000
    assert {len(c["sessions"]) for c in cases} == set(range(1, 8))
    assert {c["segment"][0] for c in cases} == {"PRE", "RTH", "POST"}
    assert answer_digest(seeded_answers(cases)) == SEEDED_DIGEST


REFERENCE_SCRIPT = r"""
import hashlib, json, os, platform, sys
from array import array
from datetime import date
from decimal import Decimal as D
from typing import Any
SRC = os.environ["MOVERS_REFERENCE_SRC"]
paths = {
    "baseline.py": "33ae6220ed6ecda61709ed4c6b4847ad73f7e41e",
    "prestage.py": "e3345d330725a4ad12d6e517ae74c592d3633ffe",
    "bars.py": "8ae03acec28cabae0425cf6af1bce38b08910e60",
}
for filename, expected in paths.items():
    with open(os.path.join(SRC, "sextant", "tradingview", filename), "rb") as handle:
        blob = handle.read()
    assert hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest() == expected
# Stop before computing reference answers on a different Python runtime.
assert platform.python_version() == sys.argv[1], (platform.python_version(), sys.argv[1])
sys.path.insert(0, SRC)
from sextant.tradingview import baseline as b
from sextant.tradingview.bars import Bar
from sextant.ranking.engine import Segment
try:
    from sextant.tradingview.prestage import build_segment_baseline
except ImportError as exc:
    build_segment_baseline = None
    import_error = f"{type(exc).__name__}: {exc}"
else:
    import_error = None

cases = json.loads(sys.stdin.read())
answers, streamed = [], 0
for case in cases:
    answers.append(case_answer(b.build_baseline, Bar, Segment, case))
    if len(case["sessions"]) != 5 or build_segment_baseline is None:
        continue
    name, day, start, end, source = case["segment"]
    scope = Segment(name, date.fromisoformat(day), start, end, source)
    sessions = []
    by_day = {}
    for day, fields in case["sessions"]:
        parsed = date.fromisoformat(day)
        sessions.append(parsed)
        by_day[parsed] = [Bar(s, t, D(o), D(c), v, n, D(w)) for s, t, o, c, v, n, w in fields]
    class Source:
        def minute_bars(self, symbols, *, start, end):
            return by_day[start.date()]
    result = build_segment_baseline(Source(), segment=scope, symbols=case["symbols"],
                                    sessions=sessions, known_at=case["known_at"],
                                    provenance=case["provenance"])
    assert result.identity == answers[-1]["identity"]
    assert [a.tobytes().hex() for a in result.dv] == answers[-1]["dv"]
    assert [a.tobytes().hex() for a in result.counts] == answers[-1]["counts"]
    streamed += 1
print("PARITY " + json.dumps({"python": platform.python_version(),
    "representation": [sys.byteorder, array("q").itemsize, array("i").itemsize],
    "answers": answers, "streamed": streamed, "import_error": import_error}))
"""


@pytest.mark.local
def test_seeded_parity_with_the_reference_in_a_child_interpreter() -> None:
    import inspect
    import platform
    import sys

    import reference_child as child

    src, python = child.reference()
    cases = seeded_cases()
    # Function definitions resolve names when called, after the child's checked imports.
    script = (
        "from __future__ import annotations\n" + inspect.getsource(case_answer) + REFERENCE_SCRIPT
    )
    theirs = child.run(python, src, script, platform.python_version(), stdin=json.dumps(cases))
    assert theirs["python"] == platform.python_version()
    assert theirs["representation"] == [sys.byteorder, array("q").itemsize, array("i").itemsize]
    assert theirs["answers"] == seeded_answers(cases)
    expected = sum(len(c["sessions"]) == 5 for c in cases)
    assert theirs["import_error"] is None, theirs["import_error"]
    assert theirs["streamed"] == expected
    print(f"\nPython here/reference: {platform.python_version()}/{theirs['python']}")
    queries = sum(len(c["queries"]) for c in cases)
    arrays = 2 * sum(len(c["sessions"]) for c in cases)
    print(
        f"parity: {len(cases)} builds, {arrays} arrays, {queries} queries; "
        f"streamed {theirs['streamed']}; digest {answer_digest(theirs['answers'])}"
    )


# Session choice: the caller supplies both counts; dates come from the pinned calendar.
def choice(day: date, scope: Segment, **kw: Any) -> tuple[date, ...]:
    choose: Any = curves.baseline_sessions
    return choose(day, scope, **kw)


def calendar_choice():
    from exchange_calendars import get_calendar

    return get_calendar("XNYS")


def actual_segment(day: date, hour: int, minute: int = 0) -> Segment:
    from sextant_movers.ranking.baseline import segment_start

    found = segment_start(
        nanos(
            datetime.combine(day, datetime.min.time(), ET).replace(
                hour=hour, minute=minute, second=1
            )
        )
    )[0]
    assert found is not None
    return found


def early_and_next() -> tuple[date, date]:
    cal = calendar_choice()
    early = next(d.date() for d in reversed(cal.early_closes) if d.year == 2026)
    following = cal.sessions[cal.sessions.get_loc(str(early)) + 1].date()
    return early, following


@pytest.mark.parametrize("count", [0, -1, True, 1.0, "1", None])
def test_session_choice_count_refused_first_with_exact_text(count: Any) -> None:
    with pytest.raises(ValueError) as error:
        choice(DAY, segment(), count=count, candidates=0)
    assert str(error.value) == "A prior-session count is at least 1"


@pytest.mark.parametrize("candidates", [0, -1, True, "1"])
def test_session_choice_candidates_checked_by_prior_sessions(candidates: Any) -> None:
    with pytest.raises(ValueError) as error:
        choice(DAY, segment(), count=1, candidates=candidates)
    assert str(error.value) == "A prior-session count is at least 1"


@pytest.mark.parametrize("kw", [{}, {"count": 1}, {"candidates": 1}])
def test_session_choice_requires_both_keywords(kw: Any) -> None:
    with pytest.raises(TypeError):
        choice(DAY, segment(), **kw)


def test_session_choice_newest_usable_oldest_first_and_candidate_edges() -> None:
    from sextant_movers.ranking.references import prior_sessions

    early, day = early_and_next()
    pre = actual_segment(day, 4)
    expected = prior_sessions(day, count=3)
    assert expected[-1] == early
    assert choice(day, pre, count=3, candidates=3) == expected
    assert choice(day, pre, count=3, candidates=8) == expected
    with pytest.raises(ValueError) as error:
        choice(day, pre, count=3, candidates=2)
    assert str(error.value) == "fewer than 3 usable prior PRE sessions"
    assert (day - early).days > 1  # weekend; the calendar also skips the intervening holiday.
    assert (early - expected[-2]).days > 1


@pytest.mark.parametrize("hour,minute", [(9, 30), (16, 0)])
def test_session_choice_skips_early_close_for_full_rth_and_1600_post(hour, minute) -> None:
    from sextant_movers.ranking.references import prior_sessions

    early, day = early_and_next()
    scope = actual_segment(day, hour, minute)
    prior = prior_sessions(day, count=4)
    assert choice(day, scope, count=3, candidates=4) == prior[:-1]
    assert early not in choice(day, scope, count=3, candidates=4)
    with pytest.raises(ValueError) as error:
        choice(day, scope, count=3, candidates=3)
    assert str(error.value) == f"fewer than 3 usable prior {scope.name} sessions"


def test_session_choice_on_early_day_short_rth_and_1300_post() -> None:
    from sextant_movers.ranking.references import prior_sessions

    early, _ = early_and_next()
    rth, post = actual_segment(early, 9, 30), actual_segment(early, 13)
    assert (rth.end_ns - rth.start_ns) // NS == 210 * 60
    assert choice(early, rth, count=3, candidates=3) == prior_sessions(early, count=3)
    with pytest.raises(ValueError) as error:
        choice(early, post, count=1, candidates=15)
    assert str(error.value) == "fewer than 1 usable prior POST sessions"
    previous = max(d.date() for d in calendar_choice().early_closes if d.date() < early)
    window = len(calendar_choice().sessions_in_range(str(previous), str(early))) - 1
    assert choice(early, post, count=1, candidates=window) == (previous,)


def test_session_choice_weeks_after_both_dst_changes() -> None:
    from sextant_movers.ranking.references import prior_sessions

    from itertools import pairwise

    sessions = [s.date() for s in calendar_choice().sessions if s.year == 2026]
    changes = [
        b
        for a, b in pairwise(sessions)
        if datetime.combine(a, datetime.min.time(), ET).utcoffset()
        != datetime.combine(b, datetime.min.time(), ET).utcoffset()
    ]
    assert len(changes) == 2
    for first in changes:
        for day in sessions[sessions.index(first) : sessions.index(first) + 5]:
            for hour, minute in ((4, 0), (9, 30), (16, 0)):
                scope = actual_segment(day, hour, minute)
                assert choice(day, scope, count=3, candidates=3) == prior_sessions(day, count=3)


def test_session_choice_overnight_is_not_rejected_by_name() -> None:
    from sextant_movers.ranking.baseline import segment_start

    _, day = early_and_next()
    scope = segment_start(nanos(datetime.combine(day, datetime.min.time(), ET)))[0]
    assert scope is not None and scope.name == "OVERNIGHT"
    result = choice(day, scope, count=2, candidates=12)
    assert len(result) == 2 and result == tuple(sorted(result))
    assert all(actual_segment(d, 20).name == "OVERNIGHT" for d in result)
