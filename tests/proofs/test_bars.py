"""Paced one-minute bars: synthetic `ZZ` values, no fixture, no socket; the SDK's own page and
retry code runs against a scripted `_session`. The `local` proof runs the reference (commit
`06562bec0898a7203f4eaf69086ced83d2477e6b`) in a child interpreter.
"""

import ast
import calendar
import hashlib
import inspect
import json
import socket
import sys
import time
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from importlib.metadata import version
from itertools import product
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import reference_child as child
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar as AlpacaBar
from alpaca.data.models.bars import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from pydantic import ValidationError

from sextant_movers.ranking import bars as m
from sextant_movers.ranking.bars import (
    MINUTE_NS,
    AlpacaBarSource,
    Bar,
    PacedStockBars,
    Pacer,
    _bar,
    decimal_value,
    nanos,
    symbol_name,
)
from sextant_movers.ranking.window import NS

D = Decimal
START = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
START_NS = calendar.timegm(START.timetuple()) * NS
NY = ZoneInfo("America/New_York")
REFERENCE_BLOBS = {  # git blob ids at the reference commit
    "tradingview/bars.py": "8ae03acec28cabae0425cf6af1bce38b08910e60",
    "data/alpaca_history.py": "d1fc0542a33008f19a3a7d6d2c4d25f0f59eb63b",
}
# SHA-256 of the canonical answers; the `local` proof shows the reference gives the same.
ANSWERS_DIGEST = "e5c298fa92b53006d95d574dacf478fc73430df0736eda6ead479fe004960629"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object) -> None:
        raise AssertionError("a test opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class Session:
    """Stands in for the SDK's `requests.Session`; `answer(params, n)` gives status and body."""

    def __init__(self, answer: Any) -> None:
        self.answer, self.seen = answer, []

    def request(self, method: str, url: str, **opts: Any) -> Any:
        import json
        from enum import Enum

        from requests import Response

        params = {
            k: v.value if isinstance(v, Enum) else None if v is None else str(v)
            for k, v in opts["params"].items()
        }
        self.seen.append([method, url, params])
        status, body = self.answer(params, len(self.seen))
        response = Response()
        response.status_code, response.url = status, url
        response._content = json.dumps(body).encode()
        return response


class Count:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


def client(m: Any, answer: Any) -> tuple[Any, Count, Session]:
    pacer = Count()
    paced = m.PacedStockBars(pacer=pacer, api_key="placeholder", secret_key="placeholder")  # noqa: S106
    paced._session = session = Session(answer)
    # The retry sleep reads `_retry_wait` (alpaca/common/rest.py:133); the constructor refuses 0.
    paced._retry_wait = 0
    return paced, pacer, session


def script(*answers: tuple[int, Any]) -> Any:
    return lambda params, n: answers[n - 1]


def raw(minute: int, *, c: float = 10.25, n: Any = 7, vw: Any = 10.1) -> dict[str, Any]:
    t = (START + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
    return {"t": t, "o": 10.0, "h": 11.0, "l": 9.5, "c": c, "v": 1200, "n": n, "vw": vw}


GOOD: dict[str, Any] = {"symbol": "ZZQAAA", "start_ns": START_NS, "open": D("10")}
GOOD |= {"close": D("10.25"), "volume": 1200, "trades": 7}


def test_a_good_bar_closes_one_minute_after_it_starts_and_is_immutable() -> None:
    bar = Bar(**GOOD)
    assert (bar.vw, bar.close_ns, MINUTE_NS) == (None, START_NS + 60 * NS, 60 * NS)
    assert Bar(**GOOD, vw=D(0)).vw == D(0)
    with pytest.raises(FrozenInstanceError):
        bar.close = D(1)  # type: ignore[misc]
    assert not hasattr(bar, "__dict__")


BAR_REFUSALS = [
    ("symbol", "", "A bar needs a symbol"),
    ("symbol", 5, "A bar needs a symbol"),
    ("symbol", "zzqaaa", "Invalid equity symbol: 'zzqaaa'"),
    *[("start_ns", v, "A bar starts on a whole minute") for v in (-MINUTE_NS, START_NS + 1)],
    *[("start_ns", v, "A bar starts on a whole minute") for v in (True, float(START_NS))],
    *[("open", v, "A bar needs a positive Decimal open") for v in (D(0), D("NaN"), 10.25)],
    ("close", D(-1), "A bar needs a positive Decimal close"),
    ("close", D("Infinity"), "A bar needs a positive Decimal close"),
    *[("volume", v, "A bar needs a non-negative integer volume") for v in (-1, True, 1.0)],
    *[("trades", v, "A bar needs a non-negative integer trade count") for v in (-1, 2.0)],
    *[("vw", v, "A bar vw must be a non-negative Decimal or None") for v in (D("-0.01"), 1.5)],
    ("vw", D("Infinity"), "A bar vw must be a non-negative Decimal or None"),
]


@pytest.mark.parametrize(("field", "value", "message"), BAR_REFUSALS)
def test_every_bar_refusal_carries_its_exact_message(field: str, value: Any, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        Bar(**GOOD | {field: value})
    assert str(caught.value) == message


def test_symbol_name_keeps_a_ticker_and_refuses_what_could_be_a_path() -> None:
    for good in ("ZZQAAA", "ZZQ.B", "ZZQ-BB", "ZZQ_C", "9ZZQA", "Z" * 32):
        assert symbol_name(good) == good
    for bad in ("", "zzqaa", ".ZZQA", "ZZQ/A", "ZZQ A", "ZZQA\n", "Z" * 33):
        with pytest.raises(ValueError) as caught:
            symbol_name(bad)
        assert str(caught.value) == f"Invalid equity symbol: {bad!r}"
    with pytest.raises(TypeError):  # kept: a non-string reaches `re.fullmatch` unchecked
        symbol_name(5)  # type: ignore[arg-type]


def test_decimal_value_keeps_a_decimal_and_reads_anything_else_through_its_text() -> None:
    same = D("1.50")
    assert decimal_value(same) is same
    assert (decimal_value(10.1), decimal_value(0.1 + 0.2)) == (D("10.1"), D("0.30000000000000004"))
    assert (decimal_value(7), decimal_value("1e3")) == (D(7), D(1000))
    assert decimal_value(float("nan")).is_nan()  # kept: a NaN float becomes a NaN Decimal
    for bad in ("zz", True, None):  # kept: `str(True)` is not a number
        with pytest.raises(InvalidOperation):
            decimal_value(bad)


def test_nanos_is_exact_at_the_microsecond_in_any_time_zone() -> None:
    exact = START.replace(microsecond=123_456)
    want = START_NS + 123_456_000
    assert nanos(exact) == want
    assert nanos(exact.astimezone(NY)) == want
    assert nanos(exact.astimezone(timezone(timedelta(hours=5, minutes=30)))) == want
    assert nanos(datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)) == -1000


def test_nanos_reads_a_naive_datetime_in_the_host_time_zone() -> None:
    # kept: `astimezone` reads a naive value as host local time
    seen = []
    with pytest.MonkeyPatch.context() as patch:
        for zone in ("UTC", "Asia/Tokyo"):
            patch.setenv("TZ", zone)
            time.tzset()
            seen.append(nanos(datetime(2026, 1, 2, 14, 30)))
    time.tzset()
    assert seen == [START_NS, START_NS - 9 * 3600 * NS]


def test_bar_reads_the_sdk_model_built_from_a_raw_mapping() -> None:
    bar = _bar("ZZQAAA", AlpacaBar("ZZQAAA", raw(0)))
    want: dict[str, Any] = GOOD | {"open": D("10.0"), "vw": D("10.1")}
    assert bar == Bar(**want)
    assert _bar("ZZQAAA", AlpacaBar("ZZQAAA", raw(1, vw=None))).vw is None
    with pytest.raises(ValueError) as caught:
        _bar("ZZQAAA", AlpacaBar("ZZQAAA", raw(0, n=None)))
    assert str(caught.value) == "Alpaca bar lacks the vendor trade count: ZZQAAA"
    with pytest.raises(ValidationError):  # the SDK refuses an absent key
        AlpacaBar("ZZQAAA", {k: v for k, v in raw(0).items() if k != "n"})
    fraction = AlpacaBar("ZZQAAA", raw(0) | {"v": 1200.75, "n": 7.5})
    assert (_bar("ZZQAAA", fraction).volume, _bar("ZZQAAA", fraction).trades) == (1200, 7)  # kept
    with pytest.raises(ValueError, match=r"^A bar starts on a whole minute$"):
        _bar("ZZQAAA", AlpacaBar("ZZQAAA", raw(0) | {"t": "2026-01-02T14:30:01Z"}))


def scripted_pacer(rate: Decimal, *times: float) -> tuple[Pacer, list[float]]:
    clock, sleeps = iter(times), []
    return Pacer(rate, clock=lambda: next(clock), sleep=sleeps.append), sleeps


def test_the_pacer_sleeps_only_the_remainder_of_its_interval() -> None:
    pacer, sleeps = scripted_pacer(D(120), 100.0, 100.0, 100.125, 100.5, 101.0, 101.0, 102.5, 102.5)
    assert pacer.interval == 0.5
    pacer.wait()
    assert sleeps == []
    pacer.wait()
    assert sleeps == [0.375]
    pacer.wait()
    pacer.wait()
    assert sleeps == [0.375]
    assert Pacer(D(10000)).interval == 0.006
    assert (Pacer(D(1)).clock, Pacer(D(1)).sleep) == (time.monotonic, time.sleep)


@pytest.mark.parametrize("rate", ["0", "-1", "10000.01", "NaN", "Infinity", "-Infinity"])
def test_every_pacer_refusal_carries_its_exact_message(rate: str) -> None:
    with pytest.raises(ValueError) as caught:
        Pacer(D(rate))
    assert str(caught.value) == "Data calls per minute must be positive and at most 10000"


def test_the_rate_is_required_and_a_non_decimal_fails_on_its_first_method() -> None:
    with pytest.raises(TypeError):
        Pacer()  # type: ignore[call-arg]
    with pytest.raises(AttributeError):  # kept: an int or float has no `is_finite`
        Pacer(200)  # type: ignore[arg-type]


def request(*symbols: str) -> StockBarsRequest:
    minute = TimeFrame.Minute
    assert isinstance(minute, TimeFrame)
    end = START + timedelta(minutes=5)
    return StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=minute, start=START, end=end)


def test_the_paced_client_hands_its_arguments_to_the_sdk_and_keeps_the_hook_signature() -> None:
    paced, _, _ = client(m, script())
    assert isinstance(paced, StockHistoricalDataClient)
    assert (paced._api_key, paced._secret_key) == ("placeholder", "placeholder")
    hook = inspect.signature(PacedStockBars._one_request)
    assert list(hook.parameters) == ["self", "method", "url", "opts", "retry"]


def test_each_page_waits_once() -> None:
    page = {"bars": {"ZZQAAA": [raw(0)]}, "next_page_token": "zz-2"}
    paced, pacer, session = client(m, script((200, page), (200, {"bars": {"ZZQAAA": [raw(1)]}})))
    got = paced.get_stock_bars(request("ZZQAAA"))
    assert isinstance(got, BarSet)
    assert len(got.data["ZZQAAA"]) == 2
    assert [call[2]["page_token"] for call in session.seen] == [None, "zz-2"]
    assert pacer.waits == len(session.seen) == 2


@pytest.mark.parametrize("code", [429, 504])
def test_each_retry_waits_once(code: int) -> None:
    ok = (200, {"bars": {"ZZQAAA": [raw(0)]}})
    paced, pacer, session = client(m, script((code, {"message": "zz"}), ok))
    paced.get_stock_bars(request("ZZQAAA"))
    assert pacer.waits == len(session.seen) == 2


def test_the_last_retry_raises_after_four_paced_requests() -> None:
    paced, pacer, session = client(m, script(*[(429, {"message": "zz"})] * 4))
    with pytest.raises(APIError):
        paced.get_stock_bars(request("ZZQAAA"))
    assert pacer.waits == len(session.seen) == 4


def test_the_wait_comes_before_the_wire_request() -> None:
    paced, _, session = client(m, script())

    class Refuse:
        def wait(self) -> None:
            raise RuntimeError("zz")

    paced._pacer = Refuse()
    with pytest.raises(RuntimeError):
        paced.get_stock_bars(request("ZZQAAA"))
    assert session.seen == []


def test_the_source_needs_the_paced_client_and_a_whole_batch() -> None:
    plain = StockHistoricalDataClient(api_key="placeholder", secret_key="placeholder")  # noqa: S106
    with pytest.raises(ValueError) as caught:
        AlpacaBarSource(plain, batch=1, feed=DataFeed.SIP)  # type: ignore[arg-type]
    assert str(caught.value) == "historical bars need the paced transport, PacedStockBars"
    paced, _, _ = client(m, script())
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError) as caught:
            AlpacaBarSource(paced, batch=bad, feed=DataFeed.SIP)  # type: ignore[arg-type]
        assert str(caught.value) == "Bar batches must cover at least 1 symbol"
    assert AlpacaBarSource(paced, batch=10_000, feed=DataFeed.IEX).batch == 10_000
    with pytest.raises(TypeError):
        AlpacaBarSource(paced, feed=DataFeed.SIP)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        AlpacaBarSource(paced, batch=1)  # type: ignore[call-arg]


END = START + timedelta(minutes=5)
WINDOW_REFUSALS = [
    ({"start": START.replace(tzinfo=None)}, "Bar window start needs a timezone"),
    ({"end": END.replace(tzinfo=None)}, "Bar window end needs a timezone"),
    ({"end": START}, "Bar window ends before it starts"),
    ({"end": START - timedelta(microseconds=1)}, "Bar window ends before it starts"),
    ({"symbols": ["ZZQAAA", "zzqaaa"]}, "Invalid equity symbol: 'zzqaaa'"),
]


@pytest.mark.parametrize(("change", "message"), WINDOW_REFUSALS)
def test_a_bad_window_or_symbol_is_refused_before_any_request(change: Any, message: str) -> None:
    paced, pacer, session = client(m, script())
    source = AlpacaBarSource(paced, batch=2, feed=DataFeed.SIP)
    with pytest.raises(ValueError) as caught:
        source.minute_bars(**{"symbols": ["ZZQAAA"], "start": START, "end": END} | change)
    assert (str(caught.value), pacer.waits, session.seen) == (message, 0, [])


def test_no_symbols_make_no_request() -> None:
    paced, pacer, session = client(m, script())
    source = AlpacaBarSource(paced, batch=2, feed=DataFeed.SIP)
    assert (source.minute_bars((), start=START, end=END), pacer.waits, session.seen) == ((), 0, [])


def answer_each_symbol(params: dict[str, Any], n: int) -> tuple[int, Any]:
    names = params["symbols"].split(",")
    return 200, {"bars": {s: [raw(4 - k, c=10.0 + k) for k in range(2)] for s in names}}


def test_the_symbols_are_requested_once_each_in_sorted_chunks() -> None:
    paced, pacer, session = client(m, answer_each_symbol)
    source = AlpacaBarSource(paced, batch=2, feed=DataFeed.IEX)
    wanted = ["ZZQEEE", "ZZQAAA", "ZZQCCC", "ZZQAAA", "ZZQBBB", "ZZQDDD"]
    got = source.minute_bars(wanted, start=START.astimezone(NY), end=END)
    fixed = {"timeframe": "1Min", "feed": "iex", "adjustment": "raw", "sort": "asc"}
    fixed |= {"start": START.isoformat(), "end": END.isoformat(), "limit": "10000"}
    chunks = ["ZZQAAA,ZZQBBB", "ZZQCCC,ZZQDDD", "ZZQEEE"]
    url = "https://data.alpaca.markets/v2/stocks/bars"
    assert session.seen == [
        ["GET", url, fixed | {"symbols": c, "page_token": None}] for c in chunks
    ]
    assert pacer.waits == 3
    order = [(b.start_ns, b.symbol) for b in got]
    assert order == sorted(order) and len(got) == 10
    first = {"start_ns": START_NS + 3 * MINUTE_NS, "open": D("10.0"), "close": D("11.0")}
    assert got[0] == Bar(**GOOD | first, vw=D("10.1"))


def test_a_batch_above_two_hundred_is_one_request() -> None:
    names = ["ZZ" + "".join(three) for three in product("ABCDEFGHIJ", repeat=3)][:201]
    assert len(names) == 201
    paced, pacer, session = client(m, lambda params, n: (200, {"bars": {}}))
    source = AlpacaBarSource(paced, batch=201, feed=DataFeed.SIP)
    assert source.minute_bars(names, start=START, end=END) == ()
    assert len(session.seen) == pacer.waits == 1
    assert session.seen[0][2]["symbols"] == ",".join(sorted(names))


def test_the_module_imports_the_sdk_data_names_and_the_window_only() -> None:
    tree = ast.parse(Path(m.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    sdk = {f"alpaca.{name}" for name in ("common.enums", "data.enums", "data.requests")}
    sdk |= {f"alpaca.data.{name}" for name in ("historical.stock", "models.bars", "timeframe")}
    outside = {name for name in imported if name.split(".")[0] not in sys.stdlib_module_names}
    assert outside <= sdk | {"sextant_movers.ranking.window"}
    assert "sextant_movers.ranking.window" in outside
    text = Path(m.__file__).read_text()
    for word in ("environ", "getenv", "MAX_BATCH", "AlpacaHistory", "credentials"):
        assert word not in text
    for word in ("universe", "Listing"):
        assert word not in text
    stores = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]
    assert "NS" not in {n.id for n in stores}


def answer_all(m: Any) -> dict[str, Any]:
    import random
    from datetime import UTC, datetime, timedelta, timezone
    from decimal import Decimal as D
    from zoneinfo import ZoneInfo

    from alpaca.data.enums import DataFeed
    from alpaca.data.models.bars import Bar as AlpacaBar

    rng = random.Random(20261111)  # noqa: S311 (seeded synthetic values, not a secret)
    start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    zones: list[Any] = [
        UTC,
        timezone(timedelta(hours=5, minutes=30)),
        timezone(-timedelta(hours=4)),
    ]
    zones.append(ZoneInfo("America/New_York"))

    def show(v: Any) -> Any:
        if isinstance(v, m.Bar):
            fields = (v.symbol, v.start_ns, v.open, v.close, v.volume, v.trades, v.vw, v.close_ns)
            return [show(f) for f in fields]
        if isinstance(v, D):
            return str(v)
        return [show(x) for x in v] if isinstance(v, tuple | list) else v

    def out(call: Any) -> list[Any]:
        try:
            return ["ok", show(call())]
        except Exception as error:
            return [type(error).__name__, str(error)]

    minute = m.MINUTE_NS
    pools = {
        "symbol": (["ZZQAAA", "ZZQ.B", "ZZQ-BB", "9ZZQA"], ["", "zzqaa", "ZZQ/A", 5, None]),
        "start_ns": ([0, minute, 29_449_710 * minute], [minute + 1, -minute, True, float(minute)]),
        "open": ([D("10.25"), D("0.0001"), D("123.456789")], [D(0), D(-1), D("NaN"), 1.5, "2"]),
        "close": ([D("9.75"), D("1E+3")], [D(0), D("-Infinity"), D("sNaN"), None]),
        "volume": ([0, 1200, 10**12], [-1, True, 1.0, None]),
        "trades": ([0, 7, 10**9], [-1, False, 2.5]),
        "vw": ([None, D(0), D("10.1")], [D("-0.01"), D("NaN"), 10.1]),
    }
    values: list[Any] = []
    for _ in range(140):
        broken = rng.choice([None, None, *pools])
        args = {k: rng.choice(bad if k == broken else good) for k, (good, bad) in pools.items()}
        values.append(out(lambda args=args: m.Bar(**args)))
    letters = "ZQAB09.-_/ az\n"
    for i in range(40):
        name = "ZZ" + "".join(rng.choice(letters) for _ in range(rng.randint(0, 33)))
        values.append(out(lambda name=name[i % 3 :]: m.symbol_name(name)))
    numbers: list[Any] = [D("1.50"), 7, "1e3", "zz", True, None, float("nan"), float("inf")]
    numbers.append(0.1 + 0.2)
    numbers += [rng.uniform(0, 1000) for _ in range(21)]
    values += [out(lambda x=x: m.decimal_value(x)) for x in numbers]
    for _ in range(30):
        y, mo, d = rng.randint(1960, 2030), rng.randint(1, 12), rng.randint(1, 28)
        when = datetime(y, mo, d, rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
        when = when.replace(microsecond=rng.randrange(10**6), tzinfo=rng.choice(zones))
        values.append(out(lambda when=when: m.nanos(when)))
    values.append(out(lambda: m.nanos("2026-01-02")))
    for _ in range(40):
        stamp = start + timedelta(minutes=rng.randint(-500, 500), seconds=rng.choice([0] * 9 + [1]))
        row = {"t": stamp.isoformat(), "h": 1.0, "l": 1.0, "v": rng.choice([0, 1200, 1200.75])}
        row |= {"o": round(rng.uniform(0, 500), rng.randint(0, 6)), "c": rng.choice([0.0, 9.99])}
        row |= {"n": rng.choice([7, 7.5, None, 0]), "vw": rng.choice([None, 10.1, 1 / 3])}
        name = rng.choice(["ZZQAAA", "ZZQAAA", "zzqaaa"])
        values.append(out(lambda name=name, row=row: m._bar(name, AlpacaBar(name, row))))
    pacer: list[Any] = []
    for j in range(48):
        rate: Any = (
            D(rng.randint(1, 10000)) if j % 6 else rng.choice([D(0), D(10001), D("NaN"), 200])
        )
        if j % 7 == 3:
            rate = rng.choice([D("0.5"), D("123.45"), D(10000)])
        steps = [
            rng.choice([0.0, rng.uniform(0, 0.01), rng.uniform(0, 1), rng.uniform(0, 90)])
            for _ in range(12)
        ]
        now, sleeps = [rng.uniform(0, 1000)], []

        def clock(now: list[float] = now, steps: list[float] = steps) -> float:
            now[0] += steps.pop()
            return now[0]

        def sleep(delay: float, now: list[float] = now, sleeps: list[float] = sleeps) -> None:
            sleeps.append(delay)
            now[0] += delay

        def run(
            rate: Any = rate, clock: Any = clock, sleep: Any = sleep, sleeps: Any = sleeps
        ) -> Any:
            paced = m.Pacer(rate, clock=clock, sleep=sleep)
            for _ in range(6):
                paced.wait()
            return [paced.interval, sleeps]

        pacer.append(out(run))
    names = ["ZZQAAA", "ZZQBBB", "ZZQCCC", "ZZQ.D", "ZZQ-E", "ZZQFFF", "ZZQGGG", "ZZQHHH"]
    runs: list[Any] = []
    for j in range(48):
        batch = rng.choice([1, 2, 3, 5, 200, rng.randint(1, 200)])
        feed = rng.choice([DataFeed.SIP, DataFeed.IEX, DataFeed.DELAYED_SIP])
        wanted = [rng.choice(names) for _ in range(rng.randint(0 if j % 9 else 1, 9))]
        if j % 10 == 4:
            wanted.insert(rng.randrange(len(wanted) + 1), "zzqbad")
        begin = start + timedelta(minutes=rng.randint(-300, 300))
        end = begin + timedelta(minutes=rng.randint(1, 30))
        if j % 12 == 5:
            begin = begin.replace(tzinfo=None)
        if j % 12 == 7:
            end = begin - timedelta(minutes=rng.randint(0, 2))
        if j % 6 == 1:
            begin = begin.astimezone(ZoneInfo("America/New_York"))

        def answer(params: dict[str, Any], n: int, j: int = j, begin: datetime = begin) -> Any:
            r = random.Random(f"{j}:{n}")  # noqa: S311
            if r.random() < 0.15:
                return r.choice([429, 504]), {"message": "zz"}
            body: dict[str, Any] = {"bars": {}}
            for s in params["symbols"].split(","):
                rows = []
                for _ in range(r.choice([0, 1, 1, 2, 3])):
                    t = begin + timedelta(minutes=r.randint(0, 29))
                    row = {"t": t.isoformat(), "o": round(r.uniform(1, 500), r.randint(0, 4))}
                    row |= {"h": 1.0, "l": 1.0, "c": round(r.uniform(1, 500), 2)}
                    row |= {
                        "v": r.randint(0, 10**6),
                        "n": r.choice([r.randint(0, 5000)] * 30 + [None]),
                    }
                    rows.append(row | {"vw": r.choice([None, round(r.uniform(1, 500), 4)])})
                if rows or r.random() < 0.5:
                    body["bars"][s] = rows
            if n < 12 and r.random() < 0.25:
                body["next_page_token"] = f"zz-{n}"
            return 200, body

        paced, waits, session = client(m, answer)
        source = m.AlpacaBarSource(paced, batch=batch, feed=feed)
        got = out(lambda s=source, w=wanted, b=begin, e=end: s.minute_bars(w, start=b, end=e))
        runs.append({"requests": session.seen, "waits": waits.waits, "result": got})
    return {"values": values, "pacer": pacer, "bars": runs}


def canonical(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def test_the_seeded_answers_hold_their_pinned_digest() -> None:
    answers = answer_all(m)
    values, pacer, runs = answers["values"], answers["pacer"], answers["bars"]
    assert len(values) >= 200 and len(pacer) == len(runs) == 48
    for kind in (values, pacer, [r["result"] for r in runs]):
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
from sextant.data import alpaca_history
from sextant.tradingview import bars
assert os.path.samefile(bars.__file__, os.path.join(SRC, "sextant", "tradingview", "bars.py"))
names = {"MINUTE_NS": bars, "Bar": bars, "_bar": bars, "PacedStockBars": bars}
names |= {"AlpacaBarSource": bars, "symbol_name": alpaca_history, "decimal_value": alpaca_history}
names |= {"nanos": alpaca_history, "Pacer": alpaca_history}
ref = types.SimpleNamespace(**{name: getattr(home, name) for name, home in names.items()})
answers = canonical(answer_all(ref))
found = {"python": sys.version.split()[0], "sdk": version("alpaca-py"), "answers": answers}
found |= {"pydantic": version("pydantic"), "requests": version("requests")}
print("PARITY " + json.dumps(found))
"""


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter() -> None:
    src, python = child.reference()
    for name, blob_id in REFERENCE_BLOBS.items():
        blob = (Path(src) / "sextant" / name).read_bytes()
        git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
        assert git_blob.hexdigest() == blob_id, name
    ours = canonical(answer_all(m))
    shipped = (Session, Count, client, answer_all, canonical)
    source = "from typing import Any\n" + "".join(map(inspect.getsource, shipped))
    theirs = child.run(python, src, source + CHILD)
    mine = [sys.version.split()[0], version("alpaca-py"), version("pydantic"), version("requests")]
    assert mine == [theirs[k] for k in ("python", "sdk", "pydantic", "requests")]
    answers = json.loads(ours)

    print(
        f"parity: {len(answers['values'])} values, {len(answers['pacer'])} pacer runs, "
        f"{len(answers['bars'])} minute_bars runs; this side Python {sys.version.split()[0]} "
        f"alpaca-py {version('alpaca-py')}, the reference Python {theirs['python']} "
        f"alpaca-py {theirs['sdk']}"
    )
    assert theirs["answers"] == ours


@pytest.mark.parametrize(
    "over",
    [
        {"bars": []},
        {"bars": (object(),)},
        {"requested": []},
        {"requested": ("",)},
        {"requested": (7,)},
        {"failed": []},
        {"failed": ("",)},
        {"failed": (7,)},
        {"requested": ("ZZPOLL", "ZZPOLL")},
        {"failed": ("ZZOTHER",)},
    ],
)
def test_bar_poll_refusals_alone(over: Any) -> None:
    from sextant_movers.ranking.bars import BarPoll

    with pytest.raises(ValueError):
        BarPoll(**({"bars": (), "requested": ("ZZPOLL",)} | over))


def test_bar_poll_keeps_records_default_failures_and_reference_loose_membership() -> None:
    from dataclasses import FrozenInstanceError

    from sextant_movers.ranking.bars import BarPoll

    item = Bar("ZZOTHER", 0, D(3), D(4), 7, 2)
    poll = BarPoll((item,), ("ZZPOLL",))
    assert poll.bars == (item,) and poll.requested == ("ZZPOLL",) and poll.failed == ()
    # Kept: duplicate failed names and bars outside requested names are accepted.
    assert BarPoll((item,), ("ZZPOLL",), ("ZZPOLL",) * 2).failed == ("ZZPOLL",) * 2
    with pytest.raises(FrozenInstanceError):
        poll.failed = ()  # type: ignore[misc]
    assert not hasattr(poll, "__dict__")
