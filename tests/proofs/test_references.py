"""The reference closes: synthetic `ZZ` values, no fixture, no socket; the SDK's own page and
retry code runs against the scripted `_session` of `test_bars.py`. The `local` proof runs the
reference (commit `06562bec0898a7203f4eaf69086ced83d2477e6b`) in a child interpreter.
"""

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
import types
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from exchange_calendars import get_calendar
from sextant_movers.ranking.references import (
    AlpacaDailyBars,
    AlpacaSource,
    collect_references,
    prior_sessions,
)
from test_bars import Count, Session, client, offline, script  # noqa: F401 (autouse fixture)

from sextant_movers.ranking import bars
from sextant_movers.ranking import references as refs

D = Decimal
DAY = date(2026, 1, 7)
URL = "https://data.alpaca.markets/v2/stocks/bars"
REFERENCE_BLOB = "e3345d330725a4ad12d6e517ae74c592d3633ffe"  # prestage.py at the reference commit
# SHA-256 of the canonical answers; the `local` proof shows the reference gives the same.
ANSWERS_DIGEST = "0"


def iso(*days: date) -> list[str]:
    return [day.isoformat() for day in days]


WINDOWS = [  # (day, count 1, count 5): a weekday, a Monday, after a holiday, a Saturday
    (DAY, "2026-01-06", ["2025-12-30", "2025-12-31", "2026-01-02", "2026-01-05", "2026-01-06"]),
    (date(2026, 1, 12), "2026-01-09", [f"2026-01-0{d}" for d in (5, 6, 7, 8, 9)]),
    (date(2026, 1, 20), "2026-01-16", [f"2026-01-1{d}" for d in (2, 3, 4, 5, 6)]),
    (date(2026, 1, 10), "2026-01-09", [f"2026-01-0{d}" for d in (5, 6, 7, 8, 9)]),
]


@pytest.mark.parametrize(("day", "one", "five"), WINDOWS)
def test_prior_sessions_are_the_calendars_window_before_the_day(
    day: date, one: str, five: list[str]
) -> None:
    calendar = get_calendar("XNYS")
    assert iso(*prior_sessions(day, count=1)) == [one]
    got = prior_sessions(day, count=5)
    assert iso(*got) == five and list(got) == sorted(got)
    assert all(s < day and calendar.is_session(s) for s in got)


def test_a_session_day_is_not_in_its_own_answer() -> None:
    assert get_calendar("XNYS").is_session(DAY)
    assert DAY not in prior_sessions(DAY, count=30)


@pytest.mark.parametrize("count", [0, -1, True, 1.5])
def test_every_count_refusal_carries_its_exact_message(count: Any) -> None:
    with pytest.raises(ValueError, match=r"^A prior-session count is at least 1$"):
        prior_sessions(DAY, count=count)


def test_the_count_is_a_required_keyword() -> None:
    with pytest.raises(TypeError):
        prior_sessions(DAY)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        prior_sessions(DAY, 5)  # type: ignore[misc]


def test_a_count_beyond_sixty_days_is_answered_where_the_reference_refuses() -> None:
    got = prior_sessions(DAY, count=45)  # departure: the reference looks back 60 days only
    assert len(got) == 45 and got[0] == date(2025, 10, 31) and (DAY - got[0]).days > 60


def test_a_window_before_the_first_session_is_the_calendars_own_refusal() -> None:
    calendar = get_calendar("XNYS")
    day = calendar.sessions[3].date()
    with pytest.raises(ValueError) as theirs:
        calendar.sessions_window(calendar.sessions[2], -10)
    with pytest.raises(ValueError) as ours:
        prior_sessions(day, count=10)
    assert str(ours.value) == str(theirs.value)
    assert str(ours.value).startswith("Sessions window cannot begin before the first calendar")


def row(t: str, c: float, **extra: Any) -> dict[str, Any]:
    return {"t": t, "o": 1.0, "h": 1.0, "l": 1.0, "c": c, "v": 100, "n": 3, "vw": None} | extra


def daily(answer: Any, *, batch: int = 2, feed: DataFeed = DataFeed.SIP) -> Any:
    paced, pacer, session = client(bars, answer)
    return AlpacaDailyBars(paced, batch=batch, feed=feed), pacer, session


def ok(body: dict[str, Any]) -> Any:
    return lambda params, n: (200, body)


def test_the_daily_closes_need_the_paced_client_and_a_whole_batch() -> None:
    plain = StockHistoricalDataClient(api_key="placeholder", secret_key="placeholder")  # noqa: S106
    with pytest.raises(ValueError) as caught:
        AlpacaDailyBars(plain, batch=1, feed=DataFeed.SIP)  # type: ignore[arg-type]
    assert str(caught.value) == "historical bars need the paced transport, PacedStockBars"
    paced, _, _ = client(bars, script())
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError) as caught:
            AlpacaDailyBars(paced, batch=bad, feed=DataFeed.SIP)  # type: ignore[arg-type]
        assert str(caught.value) == "A daily close batch covers at least 1 symbol"
    assert AlpacaDailyBars(paced, batch=10_000, feed=DataFeed.IEX)._batch == 10_000
    with pytest.raises(TypeError):
        AlpacaDailyBars(paced, feed=DataFeed.SIP)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AlpacaDailyBars(paced, batch=1)  # type: ignore[call-arg]


def test_the_closes_are_requested_once_each_in_sorted_daily_split_chunks() -> None:
    source, pacer, session = daily(ok({"bars": {}}), feed=DataFeed.IEX)
    wanted = ["ZZQEEE", "ZZQAAA", "ZZQCCC", "ZZQAAA", "ZZQBBB"]
    assert source.daily_closes(wanted, day=DAY) == {}
    fixed = {"timeframe": "1Day", "feed": "iex", "adjustment": "split", "sort": "asc"}
    fixed |= {"start": "2026-01-07T05:00:00+00:00", "end": "2026-01-08T05:00:00+00:00"}
    fixed |= {"limit": "10000", "page_token": None}
    chunks = ["ZZQAAA,ZZQBBB", "ZZQCCC,ZZQEEE"]
    assert session.seen == [["GET", URL, fixed | {"symbols": c}] for c in chunks]
    assert pacer.waits == 2


def test_every_page_and_retry_of_the_daily_request_waits_once() -> None:
    page = {"bars": {"ZZQAAA": [row("2026-01-07T05:00:00Z", 10.5)]}, "next_page_token": "zz-2"}
    last = {"bars": {"ZZQBBB": [row("2026-01-07T05:00:00Z", 20.25)]}}
    source, pacer, session = daily(script((429, {"message": "zz"}), (200, page), (200, last)))
    got = source.daily_closes(["ZZQBBB", "ZZQAAA"], day=DAY)
    assert got == {"ZZQAAA": D("10.5"), "ZZQBBB": D("20.25")}
    assert pacer.waits == len(session.seen) == 3


def test_kept_the_symbol_names_are_not_validated() -> None:
    source, _, session = daily(ok({"bars": {}}))
    source.daily_closes(["zz/bad"], day=DAY)
    assert session.seen[0][2]["symbols"] == "zz/bad"


def test_kept_the_vendor_symbol_is_upper_cased() -> None:
    source, _, _ = daily(ok({"bars": {"zzqaaa": [row("2026-01-07T05:00:00Z", 2.5)]}}))
    assert source.daily_closes(["zzqaaa"], day=DAY) == {"ZZQAAA": D("2.5")}


def test_kept_a_timestamp_without_a_zone_is_read_as_utc() -> None:
    rows = [row("2026-01-07T05:00:00", 2.5), row("2026-01-08T04:59:00", 3.5)]
    source, _, _ = daily(ok({"bars": {"ZZQAAA": rows, "ZZQBBB": [row("2026-01-07T04:59:00", 1)]}}))
    assert source.daily_closes(["ZZQAAA", "ZZQBBB"], day=DAY) == {"ZZQAAA": D("3.5")}


def test_kept_only_rows_of_the_new_york_date_count_and_the_last_wins() -> None:
    stamps = ["2026-01-06T05:00:00Z", "2026-01-07T05:00:00Z", "2026-01-07T20:00:00Z"]
    rows = [row(t, c) for t, c in zip(stamps, (1.0, 10.0, 11.0), strict=True)]
    rows.append(row("2026-01-08T05:00:00Z", 12.0))
    source, _, _ = daily(ok({"bars": {"ZZQAAA": rows}}))
    assert source.daily_closes(["ZZQAAA"], day=DAY) == {"ZZQAAA": D("11.0")}


def test_kept_no_symbols_give_no_closes_and_no_request() -> None:
    source, pacer, session = daily(script())
    assert (source.daily_closes([], day=DAY), pacer.waits, session.seen) == ({}, 0, [])


def test_kept_a_day_that_is_not_a_date_is_refused_before_any_request() -> None:
    source, pacer, session = daily(script())
    with pytest.raises(ValueError, match=r"^A daily close request needs a date$"):
        source.daily_closes(["ZZQAAA"], day="2026-01-07")  # type: ignore[arg-type]
    assert (pacer.waits, session.seen) == (0, [])


def test_kept_a_datetime_day_is_requested_and_matches_no_row() -> None:
    source, _, session = daily(ok({"bars": {"ZZQAAA": [row("2026-01-07T05:00:00Z", 2.5)]}}))
    assert source.daily_closes(["ZZQAAA"], day=datetime(2026, 1, 7, 12)) == {}
    assert session.seen[0][2]["start"] == "2026-01-07T05:00:00+00:00"


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        (date(2026, 3, 8), "2026-03-08T05:00:00+00:00", "2026-03-09T04:00:00+00:00"),
        (date(2026, 11, 1), "2026-11-01T04:00:00+00:00", "2026-11-02T05:00:00+00:00"),
    ],
)
def test_on_a_clock_change_the_window_ends_at_the_next_new_york_midnight(
    day: date, start: str, end: str
) -> None:
    source, _, session = daily(ok({"bars": {}}))
    source.daily_closes(["ZZQAAA"], day=day)
    assert (session.seen[0][2]["start"], session.seen[0][2]["end"]) == (start, end)


def test_a_null_bars_body_fails_inside_the_sdk_before_the_bar_set() -> None:
    source, _, _ = daily(ok({"bars": None}))
    with pytest.raises(AttributeError):
        source.daily_closes(["ZZQAAA"], day=DAY)


class Fixed:
    def __init__(self, closes: dict[str, Decimal]) -> None:
        self.closes, self.calls = closes, []

    def daily_closes(self, symbols: Any, *, day: date) -> dict[str, Decimal]:
        self.calls.append((symbols, day))
        return dict(self.closes)


def test_the_references_keep_the_callers_order_and_repeats_and_drop_names_without_a_close() -> None:
    source = Fixed({"ZZQAAA": D("1.5"), "ZZQBBB": D("2"), "ZZQZZZ": D("9")})
    wanted = ("ZZQBBB", "ZZQCCC", "ZZQAAA", "ZZQBBB")
    names, closes = collect_references(source, day=DAY, symbols=wanted)
    assert names == ("ZZQBBB", "ZZQAAA", "ZZQBBB") and source.calls == [(wanted, DAY)]
    assert list(closes.items()) == [("ZZQBBB", D("2")), ("ZZQAAA", D("1.5"))]


def test_no_close_for_any_name_is_refused() -> None:
    for closes in ({}, {"ZZQZZZ": D(1)}):
        with pytest.raises(ValueError) as caught:
            collect_references(Fixed(closes), day=DAY, symbols=["ZZQAAA"])
        assert str(caught.value) == "no prior official close for the universe"
        assert type(caught.value) is ValueError


def test_the_live_source_hands_each_call_to_the_part_it_wraps() -> None:
    seen = []

    class Part:
        def minute_bars(self, *args: Any, **kwargs: Any) -> Any:
            seen.append(("minute_bars", args, kwargs))
            return "bars"

        def daily_closes(self, *args: Any, **kwargs: Any) -> Any:
            seen.append(("daily_closes", args, kwargs))
            return "closes"

    end = datetime(2026, 1, 7, 15)
    live = AlpacaSource(Part(), Part())  # type: ignore[arg-type]
    assert live.minute_bars(["ZZQAAA"], start=end - timedelta(minutes=5), end=end) == "bars"
    assert live.daily_closes(["ZZQBBB"], day=DAY) == "closes"
    window = {"start": end - timedelta(minutes=5), "end": end}
    assert seen == [
        ("minute_bars", (["ZZQAAA"],), window),
        ("daily_closes", (["ZZQBBB"],), {"day": DAY}),
    ]


def test_the_module_imports_the_sdk_data_names_the_calendar_and_the_bars_only() -> None:
    text = Path(refs.__file__).read_text()
    tree = ast.parse(text)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    allowed = {"alpaca.common.enums", "alpaca.data.enums", "alpaca.data.models.bars"}
    allowed |= {"alpaca.data.requests", "alpaca.data.timeframe", "exchange_calendars"}
    outside = {name for name in imported if name.split(".")[0] not in sys.stdlib_module_names}
    assert outside == allowed | {"sextant_movers.ranking.bars"}
    for word in ("environ", "getenv", "credentials", "MAX_BATCH", "REFERENCE_SOURCE"):
        assert word not in text
    for word in ("PrestageError", "KEY_NAMES", "Pacer(", "NS ="):
        assert word not in text


def answer_all(r: Any) -> dict[str, Any]:
    import random
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    from alpaca.data.enums import DataFeed

    rng = random.Random(20261112)  # noqa: S311 (seeded synthetic values, not a secret)
    first = date(2024, 1, 1)

    def out(call: Any) -> list[Any]:
        try:
            return ["ok", call()]
        except Exception as error:
            kind = "ValueError" if isinstance(error, ValueError) else type(error).__name__
            return [kind, str(error)]

    def pairs(closes: Any) -> list[list[str]]:
        return [[name, str(close)] for name, close in closes.items()]

    windows = []
    for _ in range(220):
        day, count = first + timedelta(days=rng.randrange(1096)), rng.randint(1, 30)
        got = out(lambda d=day, c=count: [s.isoformat() for s in r.prior_sessions(d, count=c)])
        windows.append([day.isoformat(), count, got])
    names = ["ZZQAAA", "ZZQBBB", "ZZQCCC", "ZZQ.D", "zzqeee", "ZZQFFF", "zz/bad"]
    closes = []
    for j in range(64):
        batch = rng.choice([1, 2, 3, 5, 200, rng.randint(1, 200)])
        feed = rng.choice([DataFeed.SIP, DataFeed.IEX, DataFeed.DELAYED_SIP])
        wanted = [rng.choice(names) for _ in range(rng.randint(0 if j % 9 else 1, 8))]
        when = first + timedelta(days=rng.randrange(1096))
        day: Any = when.isoformat() if j % 13 == 6 else when
        if j % 13 == 9:
            day = datetime(when.year, when.month, when.day, 12)

        def answer(params: dict[str, Any], n: int, j: int = j, when: date = when) -> Any:
            r_ = random.Random(f"{j}:{n}")  # noqa: S311
            if r_.random() < 0.15:
                return r_.choice([429, 504]), {"message": "zz"}
            body: dict[str, Any] = {"bars": {}}
            for s in params["symbols"].split(","):
                rows = []
                for _ in range(r_.choice([0, 1, 1, 2, 3])):
                    on = when + timedelta(days=r_.choice([-1, 0, 0, 0, 1]))
                    t = f"{on.isoformat()}T0{r_.choice([4, 5])}:00:00{r_.choice(['Z', ''])}"
                    row = {"t": t, "o": 1.0, "h": 1.0, "l": 1.0, "v": 100, "n": 3, "vw": None}
                    rows.append(row | {"c": round(r_.uniform(1, 500), r_.randint(0, 4))})
                if rows or r_.random() < 0.5:
                    body["bars"][r_.choice([s, s.lower()])] = rows
            if n < 12 and r_.random() < 0.25:
                body["next_page_token"] = f"zz-{n}"
            return 200, body

        paced, waits, session = client(r, answer)

        def run(paced: Any = paced, b: int = batch, f: Any = feed, w: Any = wanted, d: Any = day):
            return pairs(r.AlpacaDailyBars(paced, batch=b, feed=f).daily_closes(w, day=d))

        got = out(run)
        closes.append({"requests": session.seen, "waits": waits.waits, "result": got})

    class Fixed:
        def __init__(self, closes: dict[str, Decimal]) -> None:
            self.closes, self.calls = closes, []

        def daily_closes(self, symbols: Any, *, day: date) -> dict[str, Decimal]:
            self.calls.append([list(symbols), day.isoformat()])
            return dict(self.closes)

    references = []
    for _ in range(64):
        fixed = {s: Decimal(rng.randint(1, 10**6)) / 100 for s in names if rng.random() < 0.3}
        wanted = [rng.choice(names) for _ in range(rng.randint(0, 8))]
        day = first + timedelta(days=rng.randrange(1096))
        source = Fixed(fixed)

        def collect(s: Any = source, d: date = day, w: Any = wanted) -> Any:
            got, chosen = r.collect_references(s, day=d, symbols=w)
            return [list(got), pairs(chosen)]

        references.append([out(collect), source.calls])
    return {"prior_sessions": windows, "daily_closes": closes, "references": references}


def canonical(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def this_side() -> Any:
    namespace = {"PacedStockBars": bars.PacedStockBars, "prior_sessions": prior_sessions}
    namespace |= {"AlpacaDailyBars": AlpacaDailyBars, "collect_references": collect_references}
    return types.SimpleNamespace(**namespace)


def test_the_seeded_answers_hold_their_pinned_digest() -> None:
    answers = answer_all(this_side())
    windows, runs, chosen = (
        answers["prior_sessions"],
        answers["daily_closes"],
        answers["references"],
    )
    assert len(windows) >= 200 and len(runs) >= 60 and len(chosen) >= 60
    assert len({w[0][:4] for w in windows}) >= 2 and all(w[2][0] == "ok" for w in windows)
    for kind in ([r["result"] for r in runs], [c[0] for c in chosen]):
        assert 0 < sum(answer[0] == "ok" for answer in kind) < len(kind)
    assert all(r["waits"] == len(r["requests"]) for r in runs)
    assert sum(r["waits"] > len({q[2]["symbols"] for q in r["requests"]}) for r in runs) > 0
    assert hashlib.sha256(canonical(answers).encode()).hexdigest() == ANSWERS_DIGEST


CHILD = """
import json, os, socket, sys, types
from importlib.metadata import version
def offline(*args):
    raise AssertionError("the reference side opened a socket")
socket.socket.connect = socket.socket.connect_ex = offline
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.tradingview import bars, prestage
assert os.path.samefile(prestage.__file__, os.path.join(SRC, "sextant/tradingview/prestage.py"))
names = {"PacedStockBars": bars.PacedStockBars, "prior_sessions": prestage.prior_sessions}
names |= {"AlpacaDailyBars": prestage.AlpacaDailyBars}
names |= {"collect_references": prestage.collect_references}
answers = canonical(answer_all(types.SimpleNamespace(**names)))
found = {"python": sys.version.split()[0], "sdk": version("alpaca-py")}
found |= {"calendar": version("exchange_calendars"), "answers": answers}
print("PARITY " + json.dumps(found))
"""


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter() -> None:
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip("set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof")
    blob = (Path(src) / "sextant" / "tradingview" / "prestage.py").read_bytes()
    git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
    assert git_blob.hexdigest() == REFERENCE_BLOB
    shipped = (Session, Count, client, answer_all, canonical)
    source = "from typing import Any\n" + "".join(map(inspect.getsource, shipped))
    done = subprocess.run(
        [python, "-I", "-B", "-c", source + CHILD],
        env={"MOVERS_REFERENCE_SRC": src},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    line = next(x for x in done.stdout.splitlines() if x.startswith("PARITY "))
    theirs = json.loads(line.removeprefix("PARITY "))
    from importlib.metadata import version

    ours = canonical(answer_all(this_side()))
    mine = (sys.version.split()[0], version("alpaca-py"), version("exchange_calendars"))
    print(f"parity: this side Python {mine[0]} alpaca-py {mine[1]} exchange_calendars {mine[2]}")
    print(f"the reference Python {theirs['python']} alpaca-py {theirs['sdk']}", theirs["calendar"])
    assert theirs["answers"] == ours
