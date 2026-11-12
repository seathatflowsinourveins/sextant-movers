"""Part 4c: the baseline builder, from vendor minute bars and a caller's coverage receipts.

Synthetic symbols and values only. The reference is `build_baseline` in
`src/sextant/ranking/baseline.py` at `06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never
imports it. The `local` proof runs it in a child interpreter, as `tests/proofs/test_baseline.py`
does. The median, dropped-session, cap, first POST minute, quiet-session, early-close, overnight
and round-trip cases are ported from the reference's tests at the same commit.
"""

import copy
import hashlib
import inspect
import json
import platform
import random
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import reference_child as child

from sextant_movers.ranking import baseline
from sextant_movers.ranking.baseline import (
    MINUTE_NS,
    NS,
    BaselineCoverage,
    BaselineSample,
    MinuteBarRow,
    build_baseline,
    dump_baseline,
    load_baseline,
    segment_start,
)

D = Decimal
ET = ZoneInfo("America/New_York")
REFERENCE_BLOB = "3da751ea1d2650819ac467abfb53eb8bd8b05dab"  # git blob of the reference file
GOOD_DIGEST = "1c11dd3852f192cc59b3e22e92b189c4d72e6d9c1c1e5d1fc820571184b34e21"
AS_OF = date(2026, 10, 27)
SESSIONS = ("10-22", "10-23", "10-26")
FLOAT: Any = 1.5


def et(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=ET).timestamp()) * NS


def at(text: str) -> int:
    """`MM-DDTHH:MM` ET in 2026."""
    m, d, h, mi = map(int, text.replace("T", "-").replace(":", "-").split("-"))
    return et(2026, m, d, h, mi)


KNOWN_AT = at("10-27T12:00")


def bar(moment: str, volume: int = 10, vwap: str = "2", trades: int = 1) -> MinuteBarRow:
    return MinuteBarRow("ZZZT", at(moment), volume, D(vwap), trades)


def cover(start: str, end: str) -> BaselineCoverage:
    return BaselineCoverage("ZZZT", at(start), at(end))


def receipts(*days: str) -> list[BaselineCoverage]:
    """The reference caller's receipts: PRE and RTH whole, POST from the close plus one minute."""
    spans = (("04:00", "09:30"), ("09:30", "16:00"), ("16:01", "20:00"))
    return [cover(f"{d}T{a}", f"{d}T{b}") for d in days for a, b in spans]


def build(bars: Any, coverage: Any, *, known_at: int = KNOWN_AT, prior: int = 20, **kw: Any) -> Any:
    kw = {"as_of": AS_OF, "provenance": "fixture XNYS bars", "feed": "sip", **kw}
    return build_baseline(bars, coverage=coverage, known_at=known_at, prior_sessions=prior, **kw)


# The rows.
ROW = {"symbol": "ZZZT", "ts_event": 0, "volume": 1, "vwap": D(1), "trade_count": 1}
ROW_REFUSALS = [
    ("ts_event", FLOAT, TypeError, "bar timestamps must be integer nanoseconds"),
    ("ts_event", True, TypeError, "bar timestamps must be integer nanoseconds"),
    ("ts_event", -MINUTE_NS, ValueError, "negative bar timestamp"),
    ("ts_event", MINUTE_NS + 1, ValueError, "bar timestamps must be minute-aligned"),
    ("volume", FLOAT, TypeError, "bar volume must be an integer"),
    ("volume", True, TypeError, "bar volume must be an integer"),
    ("volume", -1, ValueError, "negative bar volume"),
    ("vwap", FLOAT, TypeError, "bar vwap must be Decimal"),
    ("vwap", "2", TypeError, "bar vwap must be Decimal"),
    ("vwap", D("NaN"), ValueError, "invalid bar vwap"),
    ("vwap", D("Infinity"), ValueError, "invalid bar vwap"),
    ("vwap", D("-0.01"), ValueError, "invalid bar vwap"),
    ("trade_count", FLOAT, TypeError, "bar trade count must be an integer"),
    ("trade_count", True, TypeError, "bar trade count must be an integer"),
    ("trade_count", -1, ValueError, "negative bar trade count"),
]


@pytest.mark.parametrize(("name", "value", "error", "message"), ROW_REFUSALS)
def test_a_minute_bar_row_refuses(name: str, value: Any, error: type, message: str) -> None:
    with pytest.raises(error, match=f"^{message}$"):
        MinuteBarRow(**{**ROW, name: value})


@pytest.mark.parametrize(
    ("start", "end", "error", "message"),
    [
        (FLOAT, MINUTE_NS, TypeError, "coverage timestamps must be integer nanoseconds"),
        (0, "60", TypeError, "coverage timestamps must be integer nanoseconds"),
        (MINUTE_NS, MINUTE_NS, ValueError, "empty or reversed baseline coverage"),
        (MINUTE_NS, 0, ValueError, "empty or reversed baseline coverage"),
    ],
)
def test_a_coverage_receipt_refuses(start: Any, end: Any, error: type, message: str) -> None:
    with pytest.raises(error, match=f"^{message}$"):
        BaselineCoverage("ZZZT", start, end)


# The builder's refusals, in its own order.
DAY = "10-26"
PRE = cover(f"{DAY}T04:00", f"{DAY}T09:30")
POST = cover(f"{DAY}T16:00", f"{DAY}T20:00")
REFUSALS = {
    "prior_sessions must be positive": [([], [PRE], {"prior": 0}), ([], [PRE], {"prior": -1})],
    "baseline coverage not known at the supplied time": [
        ([], [PRE], {"known_at": at(f"{DAY}T09:29")})
    ],
    "baseline coverage must be minute-aligned": [
        ([], [BaselineCoverage("ZZZT", PRE.start_ts + 1, PRE.end_ts)], {}),
        ([], [BaselineCoverage("ZZZT", PRE.start_ts, PRE.end_ts - 1)], {}),
    ],
    "baseline coverage crosses a segment boundary": [
        ([], [cover(f"{DAY}T04:00", f"{DAY}T09:31")], {}),
        ([], [cover(f"{DAY}T16:01", f"{DAY}T20:01")], {}),
    ],
    "baseline coverage must begin at the segment's usable start": [
        ([], [cover(f"{DAY}T04:01", f"{DAY}T09:30")], {}),
        ([], [cover(f"{DAY}T16:02", f"{DAY}T20:00")], {}),
        ([], [cover(f"{DAY}T16:00", f"{DAY}T16:01")], {}),
    ],
    "duplicate baseline coverage": [
        ([], [PRE, cover(f"{DAY}T04:00", f"{DAY}T05:00")], {}),
        ([], [POST, cover(f"{DAY}T16:01", f"{DAY}T20:00")], {}),
    ],
    "baseline contains a bar not known at the supplied time": [
        ([bar(f"{DAY}T10:00")], [PRE], {"known_at": at(f"{DAY}T10:00")}),
        ([bar("10-27T12:00")], [], {"as_of": date(2026, 10, 28)}),
    ],
    # Before the span is unreachable: every span starts at its segment's start, except a POST
    # span from the close plus one minute, and a bar in that minute is left out first.
    "vendor minute bar outside declared coverage": [
        ([bar(f"{DAY}T10:00")], [PRE], {}),  # no receipt for its segment
        ([bar(f"{DAY}T05:00")], [BaselineCoverage("ZZZU", PRE.start_ts, PRE.end_ts)], {}),
        ([bar(f"{DAY}T05:00")], [cover(f"{DAY}T04:00", f"{DAY}T05:00")], {}),  # past the span
        ([bar(f"{DAY}T19:59")], [cover(f"{DAY}T16:01", f"{DAY}T19:59")], {}),
    ],
    "duplicate vendor minute bar": [([bar(f"{DAY}T05:00")] * 2, [PRE], {})],
}
CASES = [(m, *case) for m, cases in REFUSALS.items() for case in cases]


@pytest.mark.parametrize(("message", "bars", "coverage", "kw"), CASES)
def test_the_builder_refuses(message: str, bars: Any, coverage: Any, kw: Any) -> None:
    with pytest.raises(ValueError, match=f"^{message}$"):
        build(bars, coverage, **kw)


def test_prior_sessions_is_required() -> None:
    call: Any = build_baseline
    with pytest.raises(TypeError, match="prior_sessions"):
        call([], coverage=[PRE], as_of=AS_OF, known_at=KNOWN_AT, provenance="p", feed="sip")


def test_a_bar_off_its_segments_minute_boundary_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreachable from rows (a row is minute-aligned and every segment starts on a minute);
    kept for parity and reached here by shifting the bar's segment start one nanosecond."""
    real, moment = baseline.segment_start, at(f"{DAY}T05:00")

    def shift(ts: int) -> Any:
        return (*real(ts)[:2], real(ts)[2] + (ts == moment))

    monkeypatch.setattr(baseline, "segment_start", shift)
    with pytest.raises(ValueError, match=r"^baseline bars must be aligned to minute boundaries$"):
        build([bar(f"{DAY}T05:00")], [PRE])


def test_a_negative_bar_value_is_refused_only_past_the_row() -> None:
    """Unreachable from rows (each refuses a negative); kept for parity, reached by force."""
    row = bar(f"{DAY}T05:00")
    object.__setattr__(row, "volume", -1)
    with pytest.raises(ValueError, match=r"^negative vendor minute-bar value$"):
        build([row], [PRE])


def test_a_duplicate_bar_passes_in_a_session_the_cap_drops() -> None:
    """Kept for parity: a duplicate is refused only while building a kept session's samples."""
    table = build([bar("10-22T05:00")] * 2 + [bar("10-26T05:00")], receipts(*SESSIONS), prior=1)
    assert table.sessions("ZZZT", "PRE") == ("2026-10-26",)


# The ported cases.
def test_median_of_three_sessions() -> None:  # ported
    bars = [
        bar(f"{d}T04:00", vwap=v, trades=n)
        for d, v, n in zip(SESSIONS, "246", (1, 2, 3), strict=True)
    ]
    table = build(bars, receipts(*SESSIONS))
    value = table.cumulative("ZZZT", "PRE", 60)
    assert (value.dollar_volume, value.trade_count, value.sessions) == (40, 2, 3)
    assert table.window("ZZZT", "PRE", 60, 60).dollar_volume == 40
    for offset, expected in ((0, 0), (30, 20), (120, 40), (19_800, 40)):  # to the receipt's end
        assert table.cumulative("ZZZT", "PRE", offset).dollar_volume == expected
    assert table.cumulative("ZZZT", "PRE", 19_801) is None
    assert table.provenance == "fixture XNYS bars" + baseline._PROVENANCE_SUFFIX
    assert baseline._PROVENANCE_SUFFIX == (
        "; v*vw, n; explicit complete coverage; linear within minute; first POST minute omitted"
    )


def test_current_and_future_sessions_are_dropped() -> None:  # ported
    days = (*SESSIONS, "10-27", "10-28")
    bars = [bar(f"{d}T04:00", volume=1000 if d >= "10-27" else 10) for d in days]
    coverage = [*receipts(*SESSIONS), cover("10-27T04:00", "10-27T09:30")]
    table = build(bars, coverage)  # 10-27 runs past known_at and has no receipt known: dropped
    assert table.cumulative("ZZZT", "PRE", 60).dollar_volume == 20
    assert table.sessions("ZZZT", "PRE") == tuple(f"2026-{d}" for d in SESSIONS)


def test_the_cap_keeps_the_most_recent_sessions() -> None:  # ported
    days = ("10-21", *SESSIONS)
    bars = [bar(f"{d}T04:00", vwap=str(i)) for i, d in enumerate(days, 1)]
    value = build(bars, receipts(*days), prior=3).cumulative("ZZZT", "PRE", 60)
    assert (value.dollar_volume, value.trade_count, value.sessions) == (30, 1, 3)


def test_the_first_post_minute_is_left_out_and_both_post_starts_are_accepted() -> None:
    """The reference's own choice, from no paper (ported, with the receipt from the close)."""
    for start in ("16:00", "16:01"):
        bars = [bar(f"{DAY}T16:00", volume=1000), bar(f"{DAY}T16:01")]
        table = build(bars, [cover(f"{DAY}T{start}", f"{DAY}T20:00")])
        assert table.cumulative("ZZZT", "POST", 0) is None
        assert table.cumulative("ZZZT", "POST", 60).dollar_volume == 0
        value = table.cumulative("ZZZT", "POST", 120)
        assert (value.dollar_volume, value.trade_count) == (20, 1)


def test_a_quiet_session_with_a_receipt_counts_zero_and_one_without_counts_nothing() -> None:
    bars = [bar("10-23T04:00")]
    value = build(bars, receipts("10-23", "10-26"), prior=2).cumulative("ZZZT", "PRE", 60)
    assert (value.dollar_volume, value.trade_count, value.sessions) == (10, D("0.5"), 2)
    value = build(bars, receipts("10-23"), prior=2).cumulative("ZZZT", "PRE", 60)
    assert (value.dollar_volume, value.trade_count, value.sessions) == (20, 1, 1)


def test_an_early_close_post_segment() -> None:  # ported
    table = build(
        [bar("11-27T13:01")],
        [cover("11-27T13:01", "11-27T17:00")],
        as_of=date(2026, 11, 30),
        known_at=at("11-30T00:00"),
    )
    assert table.cumulative("ZZZT", "POST", 0) is None
    assert table.cumulative("ZZZT", "POST", 120).dollar_volume == 20
    assert segment_start(at("11-27T17:30"))[0] is None


def test_overnight_goes_to_the_next_session_and_a_holiday_is_closed() -> None:  # ported
    bars = [bar("11-08T20:00"), bar("11-26T12:00")]
    coverage = [cover("11-08T20:00", "11-09T04:00"), cover("11-26T12:00", "11-26T12:05")]
    coverage.append(BaselineCoverage("ZZZT", at("11-26T13:00") + 1, at("12-30T00:00")))
    table = build(bars, coverage, as_of=date(2026, 11, 30), known_at=at("11-30T00:00"))
    assert table.sessions("ZZZT", "OVERNIGHT") == ("2026-11-09",)
    assert table.covers("ZZZT", "OVERNIGHT") and not table.covers("ZZZT", "CLOSED")
    # The holiday receipts are skipped unchecked, the misaligned one not known at known_at too.
    assert {s.session for s in table.samples} == {"2026-11-09"}


def test_dump_then_load_gives_an_equal_table() -> None:  # ported
    table = build([bar(f"{d}T04:00") for d in SESSIONS], receipts(*SESSIONS))
    assert load_baseline(dump_baseline(table)) == table


def test_the_samples_of_one_session_by_hand() -> None:
    """The usable start, a point before and after each bar, and the receipt's end."""
    bars = [bar(f"{DAY}T04:03", 5, "4", 2), bar(f"{DAY}T04:01")]
    table = build(bars, [cover(f"{DAY}T04:00", f"{DAY}T04:05")])
    points = [(s.offset_s, s.dollar_volume, s.trade_count) for s in table.samples]
    assert points == [(0, 0, 0), (60, 0, 0), (120, 20, 1), (180, 20, 1), (240, 40, 3), (300, 40, 3)]
    assert all(isinstance(s, BaselineSample) and s.session == "2026-10-26" for s in table.samples)


# The parity proof. `run_case` and `shown` run on both sides from this source, so they cannot
# drift; each side reaches its module only through its argument. The cases are data.
PRELUDE = "from datetime import date\nfrom decimal import Decimal\nfrom typing import Any\n"


def shown(v: Any) -> Any:
    if v is None:
        return None
    return [str(v.dollar_volume), str(v.trade_count), v.sessions, v.known_at, v.provenance]


def run_case(mod: Any, case: dict[str, Any]) -> list[Any]:
    def vwap(v: Any) -> Any:
        return Decimal(v) if isinstance(v, str) else v

    try:
        table = mod.build_baseline(
            [mod.MinuteBarRow(s, t, v, vwap(w), n) for s, t, v, w, n in case["bars"]],
            coverage=[mod.BaselineCoverage(*c) for c in case["coverage"]],
            as_of=date.fromisoformat(case["as_of"]),
            known_at=case["known_at"],
            provenance=case["provenance"],
            feed=case["feed"],
            prior_sessions=case["prior"],
        )
    except Exception as exc:
        return [type(exc).__name__, str(exc)]
    answers = []
    for symbol, segment, offset, width in case["queries"]:
        point = Decimal(offset) if "." in offset else int(offset)
        answers += [shown(table.cumulative(symbol, segment, point))]
        answers += [shown(table.window(symbol, segment, point, width))]
    return ["ok", table.identity, mod.dump_baseline(table), answers]


SYMBOLS = ("ZZZA", "ZZZB", "ZZZÉ")
NAMES = ("PRE", "RTH", "POST", "OVERNIGHT")
DAYS = (  # regular days, the DST changes, the days before Thanksgiving and Christmas, early closes
    *("03-05", "03-06", "03-09", "03-10", "10-29", "10-30", "11-02", "11-03"),
    *("11-24", "11-25", "11-27", "11-30", "12-23", "12-24", "12-28"),
)


def segments(text: str) -> dict[str, Any]:
    pre = segment_start(at(f"{text}T04:00"))[0]
    assert pre is not None
    rth: Any = segment_start(pre.end_ns)[0]
    found = {"PRE": pre, "OVERNIGHT": segment_start(pre.start_ns - 1)[0], "RTH": rth}
    found["POST"] = segment_start(rth.end_ns)[0]
    assert {k: (v.name, v.session_date) for k, v in found.items()} == {
        k: (k, pre.session_date) for k in found
    }
    return found


def good_case(rng: random.Random) -> dict[str, Any]:
    days = sorted(rng.sample(DAYS, rng.randint(1, 5)))
    pick = rng.randint(1, len(days))
    as_of = days[pick] if pick < len(days) else None
    bars: list[list[Any]] = []
    coverage: list[list[Any]] = []

    def add(symbol: str, ts: int) -> None:
        volume = rng.choice((0, rng.randint(1, 10 ** rng.randint(1, 7))))
        vwap = str(D(rng.randint(0, 10**6)).scaleb(-rng.randint(0, 4)))
        bars.append([symbol, ts, volume, vwap, rng.randint(0, 500)])

    symbols = rng.sample(SYMBOLS, rng.randint(1, 3))
    for symbol in symbols:
        for text in days:
            for name, seg in segments(text).items():
                if rng.random() < 0.2:
                    continue
                late = name == "POST" and rng.random() < 0.5
                minutes = (seg.end_ns - seg.start_ns) // MINUTE_NS
                first = name == "POST"
                end = rng.choice((minutes, rng.randint(first + 1, minutes)))
                start = seg.start_ns + late * MINUTE_NS
                coverage.append([symbol, start, seg.start_ns + end * MINUTE_NS])
                if first and rng.random() < 0.3:
                    add(symbol, seg.start_ns)  # the close minute, left out
                if rng.random() < 0.25:
                    continue  # a quiet session
                for m in rng.sample(range(first, end), min(end - first, rng.randint(1, 8))):
                    add(symbol, seg.start_ns + m * MINUTE_NS)
        if rng.random() < 0.3:  # a closed moment and a later session, skipped unchecked
            coverage.append([symbol, at("11-26T12:00"), at("11-26T12:05")])
            add(symbol, at("11-26T12:01"))
            add(symbol, et(2027, 1, 4, 5))
    ends = [c[2] for c in coverage] + [b[1] + MINUTE_NS for b in bars if b[1] < et(2027, 1, 1)]
    queries = []
    for symbol in (*symbols, "ZZZQ"):
        for name in NAMES:
            for offset in ("0", "60", str(rng.randint(0, 600) * 60), f"{rng.randint(0, 9999)}.25"):
                queries.append([symbol, name, offset, rng.choice((1, 60, 61, 3600))])
    rng.shuffle(bars)
    return {
        "bars": bars,
        "coverage": coverage,
        "as_of": f"2026-{as_of}" if as_of else "2026-12-31",
        "known_at": max(ends) + rng.choice((0, 1, 7 * MINUTE_NS)),
        "provenance": rng.choice(("fixture bars", "fixtüre ☃", 'a"b\\c')),
        "feed": rng.choice(("sip", "boats")),
        "prior": rng.choice((1, 2, 3, 5, 20)),
        "queries": queries,
    }


def good_cases() -> list[dict[str, Any]]:
    rng = random.Random(20261111)  # noqa: S311 - a seeded fixture, not a secret
    return [good_case(rng) for _ in range(220)]


def live(case: dict[str, Any], key: str) -> list[list[Any]]:
    """The rows on an open moment of a session before `as_of`, which the builder checks, less
    the bars in the first POST minute."""
    as_of, out = date.fromisoformat(case["as_of"]), []
    for row in case[key]:
        seg, day, start = segment_start(row[1])
        first = key == "bars" and seg is not None and seg.name == "POST" and row[1] == start
        if seg is not None and day < as_of and not first:
            out.append(row)
    return out


BAD_ROWS = [
    (1, FLOAT), (1, True), (1, -MINUTE_NS), (1, MINUTE_NS + 1), (2, FLOAT), (2, True), (2, -1),
    (3, FLOAT), (3, None), (3, "NaN"), (3, "-1"), (3, "Infinity"), (3, "sNaN"),
    (4, FLOAT), (4, True), (4, -1),
]  # fmt: skip
BAD_COVERAGE = [(1, FLOAT), (2, "60"), (1, True), (2, 0), (2, -MINUTE_NS)]


def bad_case(rng: random.Random, kind: int) -> tuple[dict[str, Any], str | None]:
    """One good case, broken one way; the message the builder should give, if one is certain."""
    while True:
        case = copy.deepcopy(good_case(rng))
        receipts, bars = live(case, "coverage"), live(case, "bars")
        if receipts and bars:
            break
    r, b = rng.choice(receipts), rng.choice(bars)
    seg: Any
    seg, _, start = segment_start(r[1])
    if kind == 0:
        case["prior"] = rng.choice((0, -1))
        return case, "prior_sessions must be positive"
    if kind == 1:
        case["known_at"] = max(c[2] for c in receipts) - rng.choice((1, MINUTE_NS))
        return case, "baseline coverage not known at the supplied time"
    if kind == 2:
        r[1:] = rng.choice(([r[1] + 1, r[2]], [r[1], r[2] - 1]))
        return case, "baseline coverage must be minute-aligned"
    if kind == 3:
        r[2] = seg.end_ns + MINUTE_NS
        case["known_at"] = max(case["known_at"], r[2])
        return case, "baseline coverage crosses a segment boundary"
    if kind == 4:
        r[1] = start + rng.choice((2, 3)) * MINUTE_NS
        r[2] = max(r[2], start + 4 * MINUTE_NS)
        case["known_at"] = max(case["known_at"], r[2])
        return case, "baseline coverage must begin at the segment's usable start"
    if kind == 5:
        case["coverage"].append(list(r))
        return case, "duplicate baseline coverage"
    if kind == 6:
        case["as_of"] = "2027-01-15"
        case["bars"].append([b[0], et(2026, 12, 31, 5), 1, "1", 1])
        return case, "baseline contains a bar not known at the supplied time"
    if kind == 7:
        own = segment_start(b[1])[2]
        case["coverage"] = [
            c for c in case["coverage"] if not (c[0] == b[0] and segment_start(c[1])[2] == own)
        ]
        return case, "vendor minute bar outside declared coverage"
    if kind == 8:
        case["bars"].append([r[0], r[2] - (r[2] == seg.end_ns) * MINUTE_NS, 1, "1", 1])
        if r[2] == seg.end_ns:
            r[2] -= MINUTE_NS
        case["known_at"] = max(case["known_at"], r[2] + MINUTE_NS)
        usable = start + (seg.name == "POST") * MINUTE_NS
        ok = r[2] > usable
        return case, "vendor minute bar outside declared coverage" if ok else None
    if kind == 9:
        case["prior"] = 20
        case["bars"].append(list(b))
        return case, "duplicate vendor minute bar"
    if kind == 10:
        index, value = rng.choice(BAD_ROWS)
        rng.choice(case["bars"])[index] = value
        return case, None
    if kind == 11:
        index, value = rng.choice(BAD_COVERAGE)
        row = rng.choice(case["coverage"])
        row[index] = row[1] + value if type(value) is int and index == 2 else value
        return case, None
    case["coverage"].insert(0, [r[0], -MINUTE_NS * rng.randint(1, 5), 0])
    return case, "integer event nanoseconds required"


def bad_cases() -> list[tuple[dict[str, Any], str | None]]:
    rng = random.Random(4)  # noqa: S311 - a seeded fixture, not a secret
    return [bad_case(rng, i % 13) for i in range(260)]


def digest(out: list[Any]) -> dict[str, Any]:
    text = json.dumps(out, separators=(",", ":"))
    return {"count": len(out), "digest": hashlib.sha256(text.encode()).hexdigest()}


def test_the_good_cases_give_the_pinned_answers() -> None:
    """The answers the `local` proof found equal to the reference's, pinned."""
    out = [run_case(baseline, case) for case in good_cases()]
    assert all(r[0] == "ok" for r in out)
    assert digest(out) == {"count": 220, "digest": GOOD_DIGEST}


CHILD = """
import json, os, sys, platform
from importlib.metadata import version
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.ranking import baseline as ref
assert os.path.samefile(ref.__file__, os.path.join(SRC, "sextant", "ranking", "baseline.py"))
cases = json.load(sys.stdin)
print("PARITY " + json.dumps({
    "versions": [
        platform.python_version(), version("nautilus_trader"),
        version("exchange_calendars"), version("pandas"),
    ],
    "results": [run_case(ref, case) for case in cases],
}))
"""


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter() -> None:
    from importlib.metadata import version

    src, python = child.reference()
    blob = (Path(src) / "sextant" / "ranking" / "baseline.py").read_bytes()
    git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
    assert git_blob.hexdigest() == REFERENCE_BLOB
    good, bad = good_cases(), bad_cases()
    cases = good + [case for case, _ in bad]
    script = PRELUDE + inspect.getsource(shown) + inspect.getsource(run_case) + CHILD
    theirs = child.run(python, src, script, stdin=json.dumps(cases), timeout=900)
    mine = [
        platform.python_version(),
        version("nautilus_trader"),
        version("exchange_calendars"),
        version("pandas"),
    ]
    assert mine == theirs["versions"]
    ours = [run_case(baseline, case) for case in cases]
    print(f"\npython, engine, calendar, pandas: here {mine}, reference {theirs['versions']}")
    found = Counter(r[1] for r in ours[len(good) :] if r[0] != "ok")
    expected = [(m, r) for (_, m), r in zip(bad, ours[len(good) :], strict=True) if m is not None]
    print(f"good {digest(ours[: len(good)])}; bad {len(bad)}, refused {sum(found.values())}")
    print(f"refusals: {dict(sorted(found.items()))}")
    assert all(r[0] == "ok" for r in ours[: len(good)])
    assert all(r[1] == m for m, r in expected), [(m, r) for m, r in expected if r[1] != m]
    assert ours == theirs["results"]
