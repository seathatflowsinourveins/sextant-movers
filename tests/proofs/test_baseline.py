"""Part 4b: the XNYS segment of a moment and the time-of-day baseline table.

Synthetic symbols and values only. The reference is `src/sextant/ranking/baseline.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b` (its `Segment` is in `src/sextant/ranking/engine.py`);
this process never imports it. The `local` proof runs it in a child interpreter, as
`tests/proofs/test_generation.py` does. The nanosecond tests and the median, coverage, overnight
and closed-interval cases are ported from the reference's tests at the same commit.
"""

import hashlib
import inspect
import json
import random
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import reference_child as child
from nautilus_trader.core.datetime import dt_to_unix_nanos

from sextant_movers.ranking import baseline
from sextant_movers.ranking.baseline import (
    NS,
    BaselineSample,
    BaselineTable,
    BaselineValue,
    Segment,
    closed_until,
    dump_baseline,
    load_baseline,
    segment_start,
)
from sextant_movers.sessions import Session, session_at

D = Decimal
ET = ZoneInfo("America/New_York")
REFERENCE_BLOB = "3da751ea1d2650819ac467abfb53eb8bd8b05dab"  # git blob of the reference file


def et(y: int, m: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, m, d, h, mi, tzinfo=ET).timestamp()) * NS


def at(text: str) -> int:
    """`MM-DDTHH:MM` ET in 2026; a trailing `-` is one nanosecond earlier."""
    m, d, h, mi = map(int, text.rstrip("-").replace("T", "-").replace(":", "-").split("-"))
    return et(2026, m, d, h, mi) - text.endswith("-")


def day(text: str) -> date:
    return date(2026, *map(int, text.split("-")))


@pytest.mark.parametrize("name", ["PRE", "RTH", "POST", "OVERNIGHT"])
def test_a_segment_has_four_names(name: str) -> None:
    assert Segment(name, day("11-10"), 0, 1, "xnys").name == name


BOUNDS = "explicit sourced session boundaries required"


@pytest.mark.parametrize(
    ("name", "start", "end", "source", "message"),
    [
        ("CLOSED", 0, 1, "xnys", "unsupported segment"),
        ("pre", 0, 1, "xnys", "unsupported segment"),
        ("PRE", 0, 1, "", BOUNDS),
        ("PRE", -1, 1, "xnys", BOUNDS),
        ("PRE", 1, 1, "xnys", BOUNDS),
        ("PRE", 2, 1, "xnys", BOUNDS),
    ],
)
def test_a_segment_refuses(name: str, start: int, end: int, source: str, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        Segment(name, day("11-10"), start, end, source)
    assert str(caught.value) == message


# Moment, segment, session date, start, end; a closed moment gives CLOSED and its local date.
# 11-10 is a Tuesday, 11-14 a Saturday, 11-26 Thanksgiving, 11-27 and 12-24 close at 13:00,
# 12-25 a holiday; the clocks change on 03-08 and 11-01.
POINTS = """
11-10T04:00- OVERNIGHT 11-10 11-09T20:00 11-10T04:00
11-10T04:00 PRE 11-10 11-10T04:00 11-10T09:30
11-10T09:30- PRE 11-10 11-10T04:00 11-10T09:30
11-10T09:30 RTH 11-10 11-10T09:30 11-10T16:00
11-10T16:00- RTH 11-10 11-10T09:30 11-10T16:00
11-10T16:00 POST 11-10 11-10T16:00 11-10T20:00
11-10T17:00 POST 11-10 11-10T16:00 11-10T20:00
11-10T20:00- POST 11-10 11-10T16:00 11-10T20:00
11-10T20:00 OVERNIGHT 11-11 11-10T20:00 11-11T04:00
11-27T13:00- RTH 11-27 11-27T09:30 11-27T13:00
11-27T13:00 POST 11-27 11-27T13:00 11-27T17:00
11-27T17:00- POST 11-27 11-27T13:00 11-27T17:00
11-27T17:00 CLOSED 11-27
11-27T20:00 CLOSED 11-27
12-24T13:00 POST 12-24 12-24T13:00 12-24T17:00
12-24T17:00 CLOSED 12-24
12-25T12:00 CLOSED 12-25
11-25T20:00 CLOSED 11-25
11-26T12:00 CLOSED 11-26
11-26T20:00 OVERNIGHT 11-27 11-26T20:00 11-27T04:00
11-14T12:00 CLOSED 11-14
11-15T20:00- CLOSED 11-15
11-15T20:00 OVERNIGHT 11-16 11-15T20:00 11-16T04:00
03-06T16:00 POST 03-06 03-06T16:00 03-06T20:00
03-08T03:00 CLOSED 03-08
03-08T20:00 OVERNIGHT 03-09 03-08T20:00 03-09T04:00
03-09T04:00 PRE 03-09 03-09T04:00 03-09T09:30
11-01T01:00 CLOSED 11-01
11-01T20:00 OVERNIGHT 11-02 11-01T20:00 11-02T04:00
11-02T09:30 RTH 11-02 11-02T09:30 11-02T16:00
""".strip().splitlines()


@pytest.mark.parametrize("line", POINTS)
def test_segment_start_on_hand_checked_points(line: str) -> None:
    moment, name, local, *bounds = line.split()
    ts = at(moment)
    if name == "CLOSED":
        assert segment_start(ts) == (None, day(local), ts) and session_at(ts) is Session.CLOSED
        return
    start, end = map(at, bounds)
    assert segment_start(ts) == (Segment(name, day(local), start, end, "xnys"), day(local), start)
    assert start <= ts < end and session_at(ts).name == name
    assert session_at(start).name == session_at(end - 1).name == name


def test_the_close_nanosecond_starts_post_and_lies_in_no_two_segments() -> None:
    # The closing print is POST's first nanosecond here; keeping it out of the extended curve is
    # the builder's job (part 4c), as in the reference.
    close = at("11-10T16:00")
    rth, post = segment_start(close - 1)[0], segment_start(close)[0]
    assert rth is not None and post is not None
    assert (rth.name, rth.end_ns, post.name, post.start_ns) == ("RTH", close, "POST", close)


@pytest.mark.parametrize(
    ("moment", "until"),
    [
        ("11-13T20:30", "11-15T20:00"),  # ported: Friday night
        ("11-14T21:00", "11-15T20:00"),  # ported: Saturday night
        ("11-27T17:30", "11-29T20:00"),  # ported: the early-close gap into a weekend
        ("12-25T12:00", "12-27T20:00"),  # ported: a holiday into a weekend
        ("11-25T20:00", "11-26T20:00"),
        ("11-26T12:00", "11-26T20:00"),
        ("12-24T17:30", "12-27T20:00"),
        ("03-07T21:00", "03-08T20:00"),
        ("10-31T21:00", "11-01T20:00"),
    ],
)
def test_every_closed_interval_ends_at_the_next_overnight_start(moment: str, until: str) -> None:
    ts, end = at(moment), at(until)
    assert segment_start(ts)[0] is None and segment_start(end - 1)[0] is None
    assert closed_until(ts) == end
    following = segment_start(end)[0]
    assert following is not None and (following.name, following.start_ns) == ("OVERNIGHT", end)


def test_overnight_belongs_to_the_next_session() -> None:  # ported
    sunday = at("11-08T20:00")
    assert (
        segment_start(sunday)[1:] == segment_start(at("11-09T03:00"))[1:] == (day("11-09"), sunday)
    )


# Ported: each side of both 2026 changes (the repeated 01:00 with fold=1), a microsecond-bearing
# instant and one before 1970; the literals are the reference test's own.
NANOS = [
    (datetime(2026, 3, 8, 1, 59, 59, 999999, tzinfo=ET), 1772953199999999000),
    (datetime(2026, 3, 8, 3, 0, 0, tzinfo=ET), 1772953200000000000),
    (datetime(2026, 11, 1, 1, 59, 59, 999999, tzinfo=ET), 1793512799999999000),
    (datetime(2026, 11, 1, 1, 0, 0, tzinfo=ET, fold=1), 1793512800000000000),
    (datetime(2026, 11, 1, 2, 0, 0, tzinfo=ET), 1793516400000000000),
    (datetime(2026, 6, 15, 12, 34, 56, 789012, tzinfo=ET), 1781541296789012000),
    (datetime(1969, 7, 20, 20, 17, 40, 123456, tzinfo=ET), -14168539876544000),
]


def test_the_function_is_the_installed_engines_module_member() -> None:
    assert dt_to_unix_nanos.__module__ == "nautilus_trader.core.datetime"
    found = inspect.getsourcefile(dt_to_unix_nanos)
    assert found is not None
    assert Path(found).parts[-4:] == ("site-packages", "nautilus_trader", "core", "datetime.py")


@pytest.mark.parametrize(("value", "expected"), NANOS)
def test_the_helper_equals_the_engine(value: datetime, expected: int) -> None:
    assert dt_to_unix_nanos(value) == baseline._nanos(value) == expected


def test_the_helper_gets_its_value_from_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    assert baseline.dt_to_unix_nanos is dt_to_unix_nanos
    monkeypatch.setattr(baseline, "dt_to_unix_nanos", lambda _value: 7_012_345_678_901_234_567)
    assert baseline._nanos(NANOS[0][0]) == 7_012_345_678_901_234_567


def sample(day: str, offset: int, dv: str, tc: str = "0", symbol: str = "ZZZT") -> BaselineSample:
    return BaselineSample(symbol, "PRE", f"2026-{day}", offset, D(dv), D(tc))


def table(*samples: BaselineSample, feed: str = "sip") -> BaselineTable:
    return BaselineTable(samples, "fixture", 7, feed)


def value(dv: Any, tc: Any, sessions: int) -> BaselineValue:
    return BaselineValue(D(dv), D(tc), sessions, 7, "fixture")


def test_cumulative_is_linear_within_the_minute_and_none_outside_coverage() -> None:
    t = table(
        sample("10-22", 60, "0"), sample("10-22", 120, "60", "6"), sample("10-22", 180, "180", "12")
    )
    assert t.cumulative("ZZZT", "PRE", 120) == value(60, 6, 1)
    found = [t.cumulative("ZZZT", "PRE", x) for x in (90, D("150.6"), D("180.000"))]
    assert found == [value(30, 3, 1), value("121.2", "9.06", 1), value(180, 12, 1)]
    assert [t.cumulative("ZZZT", "PRE", x) for x in (59, D("180.001"))] == [None, None]
    assert t.cumulative("ZZZT", "RTH", 120) is None and t.cumulative("ZZZQ", "PRE", 120) is None
    assert t.window("ZZZT", "PRE", 120, 61) is None and not t.covers("ZZZQ", "PRE")


def test_the_median_across_odd_and_even_session_counts() -> None:
    days = [(f"10-{20 + i}", dv) for i, dv in enumerate(("10", "40", "100", "1000"))]
    odd = table(*(s for d, dv in days[:3] for s in (sample(d, 0, "0"), sample(d, 60, dv, dv))))
    even = table(*(s for d, dv in days for s in (sample(d, 0, "0"), sample(d, 60, dv, dv))))
    assert odd.cumulative("ZZZT", "PRE", 60) == value(40, 40, 3)
    assert even.window("ZZZT", "PRE", 60, 60) == value(70, 70, 4)
    assert even.cumulative("ZZZT", "PRE", 30) == value(35, 35, 4)


def test_time_matched_baseline_is_median_of_session_windows_and_no_extrapolation() -> None:
    curves = (("10-22", 0, 100), ("10-23", 100, 105), ("10-26", 50, 200))  # ported
    t = table(*(sample(d, o, str(v)) for d, a, b in curves for o, v in ((0, 0), (60, a), (120, b))))
    cumulative, window = t.cumulative("ZZZT", "PRE", 60), t.window("ZZZT", "PRE", 120, 60)
    assert cumulative is not None and cumulative.dollar_volume == D(50)
    assert window is not None and window.dollar_volume == D(100)
    assert t.cumulative("ZZZT", "PRE", 121) is None
    assert t.cumulative("ZZZT", "OVERNIGHT", 60) is None


def test_a_window_counts_only_the_sessions_that_cover_both_ends() -> None:
    t = table(
        sample("10-22", 0, "0"), sample("10-22", 120, "12"),
        sample("10-23", 60, "0"), sample("10-23", 120, "30"),
    )  # fmt: skip
    assert t.window("ZZZT", "PRE", 120, 60) == value(18, 0, 2)
    assert t.window("ZZZT", "PRE", 60, 60) == value(6, 0, 1)


def test_a_table_reports_the_slices_it_holds() -> None:  # ported
    t = table(
        sample("11-05", 0, "10", "1"), sample("11-05", 60, "25", "3"),
        sample("11-06", 0, "12", "2"), sample("11-06", 0, "7", "1", "ZZZB.B"),
    )  # fmt: skip
    assert t.covers("ZZZT", "PRE") and t.covers("ZZZB.B", "PRE")
    assert not t.covers("ZZZQ", "PRE") and not t.covers("ZZZT", "RTH")
    assert t.sessions("ZZZT", "PRE") == ("2026-11-05", "2026-11-06")
    assert t.sessions("ZZZQ", "PRE") == () and t.latest_session == date(2026, 11, 6)


FEED, DOWN = "baseline source and feed required", "cumulative baseline decreases"
NOT_DECIMAL, TWICE = "baseline values must be Decimal", "duplicate baseline offset"
FLOAT: Any = 1.5


@pytest.mark.parametrize(
    ("make", "error", "message"),
    [
        (lambda: sample("10-22", -1, "0"), ValueError, "negative baseline offset"),
        (lambda: BaselineSample("Z", "PRE", "x", 0, FLOAT, D(0)), TypeError, NOT_DECIMAL),
        (lambda: sample("10-22", 0, "NaN"), ValueError, "invalid baseline value"),
        (lambda: sample("10-22", 0, "0", "-1"), ValueError, "invalid baseline value"),
        (lambda: BaselineTable((), "", 7, "sip"), ValueError, FEED),
        (lambda: table(sample("10-22", 0, "1"), sample("10-22", 0, "1")), ValueError, TWICE),
        (lambda: table(sample("10-22", 0, "2"), sample("10-22", 60, "1")), ValueError, DOWN),
        (lambda: table(sample("10-22", 0, "0", "2"), sample("10-22", 9, "1")), ValueError, DOWN),
    ],
)  # fmt: skip
def test_construction_refuses(make: Any, error: type[Exception], message: str) -> None:
    with pytest.raises(error) as caught:
        make()
    assert type(caught.value) is error and str(caught.value) == message


@pytest.mark.parametrize("feed", ["iex", "delayed_sip", "otc", "overnight", "SIP", "sip ", ""])
def test_a_table_names_only_the_sip_or_boats_feed(feed: str) -> None:
    with pytest.raises(ValueError, match=r"^baseline source and feed required$"):
        table(feed=feed)


def test_identity_covers_every_field_and_not_the_input_order() -> None:
    a, b = sample("10-22", 0, "1", "1"), sample("10-23", 60, "2", "2")
    base = table(a, b)
    assert table(b, a) == base and table(b, a).identity == base.identity
    changes = [("symbol", "ZZZU"), ("segment", "RTH"), ("session", "2026-10-24"), ("offset_s", 61)]
    changes += [("dollar_volume", D(3)), ("trade_count", D(3)), ("dollar_volume", D("2.0"))]
    variants = [replace(base, provenance="other"), replace(base, known_at=8)]
    variants += [replace(base, feed="boats")] + [table(a, replace(b, **{k: v})) for k, v in changes]
    assert len({base.identity, *(v.identity for v in variants)}) == len(variants) + 1


def test_dump_then_load_gives_an_equal_table() -> None:
    t = table(
        sample("10-22", 0, "0"),
        sample("10-22", 60, "1.50", "2"),
        sample("10-23", 0, "0E-3", "0", "ZZZÉ"),
    )
    text = dump_baseline(t)
    assert load_baseline(text) == t and load_baseline(text).identity == t.identity
    assert dump_baseline(load_baseline(text)) == text


GOOD = dump_baseline(table(sample("10-22", 0, "0"), sample("10-22", 60, "1.50", "2")))
TOP = "baseline JSON must have ['feed', 'known_at', 'provenance', 'samples']"
KEYS = "baseline JSON samples must have ['dollar_volume', 'offset_s', 'segment', 'session', "
KEYS += "'symbol', 'trade_count']"
DV, TC = (f"baseline JSON {k} must be a decimal string" for k in ("dollar_volume", "trade_count"))
OFFSET, KNOWN = (f"baseline JSON {k} must be an integer" for k in ("offset", "known_at"))
STRINGS = "baseline JSON symbol, segment and session must be strings"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('{"feed"', '{"x":1,"feed"', TOP),
        ('"known_at":7,', "", TOP),
        ('{"dollar_volume"', '{"x":1,"dollar_volume"', KEYS),
        ('"offset_s":0,', "", KEYS),
        ('"1.50"', "1.5", DV),
        ('"1.50"', "NaN", DV),
        ('"1.50"', "true", DV),
        ('"1.50"', '"1.5.0"', DV),
        ('"1.50"', '"NaN"', "invalid baseline value"),
        ('"1.50"', '"-Infinity"', "invalid baseline value"),
        ('"trade_count":"2"', '"trade_count":null', TC),
        ('"offset_s":60', '"offset_s":60.0', OFFSET),
        ('"offset_s":60', '"offset_s":true', OFFSET),
        ('"known_at":7', '"known_at":7.0', KNOWN),
        ('"known_at":7', '"known_at":"7"', KNOWN),
        ('"symbol":"ZZZT"', '"symbol":1', STRINGS),
        ('"feed":"sip"', '"feed":1', "baseline JSON feed must be a string"),
        ('"feed":"sip"', '"feed":"iex"', FEED),
        ('"provenance":"fixture"', '"provenance":[]', "baseline JSON provenance must be a string"),
        ('"samples":[', '"samples":{"a":[', "baseline JSON samples must be a list"),
    ],
)  # fmt: skip
def test_load_refuses(old: str, new: str, message: str) -> None:
    text = GOOD.replace(old, new, 1) + ("}" if new.startswith('"samples":{') else "")
    assert text != GOOD
    with pytest.raises(ValueError) as caught:
        load_baseline(text)
    assert type(caught.value) is ValueError and str(caught.value) == message


def test_load_refuses_text_that_is_not_json_or_not_an_object() -> None:
    with pytest.raises(json.JSONDecodeError):
        load_baseline(GOOD[:-1])
    for text in ("[]", "null", '"x"'):
        with pytest.raises(ValueError, match="baseline JSON must have"):
            load_baseline(text)


# The parity proof. Every helper below runs on both sides from this source (`inspect.getsource`),
# so the two cannot drift; each side reaches its module only through its argument.
PRELUDE = (
    "import hashlib, json, random\nfrom datetime import datetime, timedelta\n"
    "from decimal import Decimal\nfrom typing import Any\nfrom zoneinfo import ZoneInfo\n"
)


def grid() -> list[int]:
    """Every minute ET from 2026-03-01 to 2027-01-01, plus one nanosecond either side of each
    04:00, 09:30, 13:00, 16:00, 17:00 and 20:00."""
    zone, ns = ZoneInfo("America/New_York"), 1_000_000_000
    day, end = datetime(2026, 3, 1, tzinfo=zone), datetime(2027, 1, 1, tzinfo=zone)
    points = list(range(int(day.timestamp()) * ns, int(end.timestamp()) * ns, 60 * ns))
    while day < end:
        for hour, minute in ((4, 0), (9, 30), (13, 0), (16, 0), (17, 0), (20, 0)):
            edge = int(day.replace(hour=hour, minute=minute).timestamp()) * ns
            points += [edge - 1, edge + 1]
        day += timedelta(days=1)
    return points


def digest(out: list[Any]) -> dict[str, Any]:
    text = json.dumps(out, separators=(",", ":"))
    return {"count": len(out), "digest": hashlib.sha256(text.encode()).hexdigest()}


def refusal(call: Any, value: Any, show: Any) -> list[Any]:
    try:
        return ["ok", show(call(value))]
    except Exception as exc:
        return [type(exc).__name__, str(exc)]


def segment_answers(mod: Any) -> dict[str, Any]:
    out = []
    for ts in grid():
        s, day, start = mod.segment_start(ts)
        if s is not None:
            s = [s.name, s.session_date.isoformat(), s.start_ns, s.end_ns, s.source]
        out.append([s, day.isoformat(), start, mod.closed_until(ts)])
    out += [refusal(mod.segment_start, bad, str) for bad in (-1, 1.5, True, "1", None)]
    out += [refusal(mod.closed_until, bad, str) for bad in ("1", None)]
    return digest(out)


def build_tables(mod: Any) -> list[Any]:
    rng, tables = random.Random(20261111), []  # noqa: S311 - a seeded fixture, not a secret
    for _ in range(240):
        samples = []
        for symbol in rng.sample(("ZZZA", "ZZZB", "ZZZC", "ZZZÉ"), rng.randint(0, 3)):
            for segment in rng.sample(("PRE", "RTH", "POST", "OVERNIGHT"), rng.randint(1, 2)):
                for day in rng.sample(range(1, 29), rng.randint(1, 5)):
                    offset, dv, tc = rng.randint(0, 120), Decimal(0), Decimal(0)
                    for _ in range(rng.randint(1, 6)):
                        point = (symbol, segment, f"2026-10-{day:02d}", offset, dv, tc)
                        samples.append(mod.BaselineSample(*point))
                        offset += rng.choice((1, 60, 60, 61, 3600))
                        dv += Decimal(rng.randint(0, 10 ** rng.randint(1, 12))).scaleb(
                            -rng.randint(0, 4)
                        )
                        tc += rng.randint(0, 50)
        rng.shuffle(samples)
        provenance, feed = (
            rng.choice(("fixture", "fixtüre ☃", 'a"b\\c')),
            rng.choice(("sip", "boats")),
        )
        tables.append(
            mod.BaselineTable(tuple(samples), provenance, rng.randint(0, 2**63 - 1), feed)
        )
    return tables


def shown(v: Any) -> Any:
    if v is None:
        return None
    return [str(v.dollar_volume), str(v.trade_count), v.sessions, v.known_at, v.provenance]


def table_answers(mod: Any, tables: list[Any]) -> dict[str, Any]:
    rng, out = random.Random(4), []  # noqa: S311 - a seeded fixture, not a secret
    for t in tables:
        latest = t.latest_session.isoformat() if t.latest_session else None
        row: list[Any] = [t.identity, mod.dump_baseline(t), latest]
        for symbol in ("ZZZA", "ZZZB", "ZZZC", "ZZZÉ", "ZZZQ"):
            for segment in ("PRE", "RTH", "POST", "OVERNIGHT", "CLOSED"):
                row += [t.covers(symbol, segment), t.sessions(symbol, segment)]
                for _ in range(3):
                    whole, part = rng.randint(0, 4000), Decimal(rng.randint(0, 400_000)).scaleb(-2)
                    for offset in (whole, part):
                        row.append(shown(t.cumulative(symbol, segment, offset)))
                        row.append(
                            shown(t.window(symbol, segment, offset, rng.choice((1, 60, 61))))
                        )
        out.append(row)
    return digest(out)


def bad_texts(texts: list[str], good: str) -> list[str]:
    rng = random.Random(9)  # noqa: S311 - a seeded fixture, not a secret
    out = ["", "{", "[]", "null", '{"samples":[]}', good.replace('"known_at":7', '"known_at":NaN')]
    top = json.loads(
        '[["extra",1],["samples",{}],["provenance",1],["provenance",""],["known_at",1.5],'
        '["known_at",true],["known_at","1"],["feed","iex"],["feed",3],["feed",null]]'
    )
    inner = json.loads(
        '[["extra",1],["symbol",3],["segment",null],["session","2026-13-01"],["session","x"],'
        '["offset_s",1.0],["offset_s",true],["offset_s",-1],["offset_s","60"],'
        '["dollar_volume",1.5],["dollar_volume","NaN"],["dollar_volume","Infinity"],'
        '["dollar_volume","-1"],["dollar_volume","1.2.3"],["dollar_volume",true],'
        '["dollar_volume",null],["trade_count","sNaN"],["trade_count",10],'
        '["trade_count","1e999999"]]'
    )
    full = [json.loads(text) for text in texts if json.loads(text)["samples"]]
    for _ in range(400):
        payload = json.loads(json.dumps(rng.choice(full)))
        kind, entry = rng.randrange(5), rng.choice(payload["samples"])
        if kind == 0:
            key, value = rng.choice(top)
            payload[key] = value
        elif kind == 1:
            del payload[rng.choice(sorted(payload))]
        elif kind == 2:
            key, value = rng.choice(inner)
            entry[key] = value
        elif kind == 3:
            del entry[rng.choice(sorted(entry))]
        else:
            payload["samples"].append(dict(entry))
        out.append(json.dumps(payload))
    return out


CHILD = """
import os, sys
from importlib.metadata import version
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.ranking import baseline as ref
from sextant.ranking.engine import Segment
assert os.path.samefile(ref.__file__, os.path.join(SRC, "sextant", "ranking", "baseline.py"))
inputs = json.load(sys.stdin)
tables = build_tables(ref)
print("PARITY " + json.dumps({
    "versions": [
        sys.version.split()[0], version("nautilus_trader"),
        version("exchange_calendars"), version("pandas"),
    ],
    "segment_class": Segment.__module__,
    "segments": segment_answers(ref),
    "tables": table_answers(ref, tables),
    "texts": [ref.dump_baseline(t) for t in tables],
    "reread": [ref.load_baseline(t).identity for t in inputs["texts"]],
    "bad": [refusal(ref.load_baseline, t, lambda t: t.identity) for t in inputs["bad"]],
}))
"""
HELPERS = (grid, digest, refusal, segment_answers, build_tables, shown, table_answers)


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter() -> None:
    from importlib.metadata import version

    src, python = child.reference()
    blob = (Path(src) / "sextant" / "ranking" / "baseline.py").read_bytes()
    git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
    assert git_blob.hexdigest() == REFERENCE_BLOB
    tables = build_tables(baseline)
    ours = [dump_baseline(t) for t in tables]
    bad = bad_texts(ours, GOOD)
    script = PRELUDE + "".join(inspect.getsource(f) for f in HELPERS) + CHILD
    theirs = child.run(
        python, src, script, stdin=json.dumps({"texts": ours, "bad": bad}), timeout=900
    )
    mine = [
        sys.version.split()[0],
        version("nautilus_trader"),
        version("exchange_calendars"),
        version("pandas"),
    ]
    assert mine == theirs["versions"]
    refusals = [refusal(load_baseline, t, lambda t: t.identity) for t in bad]
    identities = [t.identity for t in tables]
    results = {
        "(a) segments": segment_answers(baseline) == theirs["segments"],
        "(b) tables": table_answers(baseline, tables) == theirs["tables"],
        "(b) texts": ours == theirs["texts"],
        "(c) reference texts read here": [load_baseline(t).identity for t in theirs["texts"]]
        == identities,
        "(c) these texts read there": theirs["reread"] == identities,
        "(d) refusals": refusals == theirs["bad"],
    }
    refused = sum(r[0] != "ok" for r in refusals)
    print(f"\npython, engine, calendar, pandas: here {mine}, reference {theirs['versions']}")
    print(
        f"(a) {theirs['segments']['count']} answers; (b) {len(tables)} tables; "
        f"(d) {len(bad)} texts, {refused} refused; Segment from {theirs['segment_class']}"
    )
    print(f"parity: {results}")
    assert len(tables) == 240 and refused > len(bad) // 2
    assert all(results.values()), results
