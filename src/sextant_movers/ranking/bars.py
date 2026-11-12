"""Paced one-minute bars on the official SDK: the bar record, the pacer and the bar source.

Sources. alpacahq/alpaca-py v0.44.0 `cc4cb3b7ba50ae250e621983c2779047fb16bb28`:
`StockHistoricalDataClient.get_stock_bars` (`alpaca/data/historical/stock.py:25,75`), its
`page_token` loop (`alpaca/common/rest.py:368`), its retry loop on 429 and 504
(`alpaca/common/rest.py:85,131`, `alpaca/common/constants.py:11-13`) and the one wire request
`_one_request` (`alpaca/common/rest.py:176`); `StockBarsRequest` (`alpaca/data/requests.py:86`),
`DataFeed` and `Adjustment` (`alpaca/data/enums.py:56,76`), `Sort` (`alpaca/common/enums.py:36`),
`TimeFrame.Minute` (`alpaca/data/timeframe.py:105`), the SDK's `Bar` and `BarSet`
(`alpaca/data/models/bars.py:10,54`). Alpaca, "About Market Data API", read 2026-11-11: 200
historical calls a minute on Basic, 10,000 on Algo Trader Plus. Alpaca, "Historical bars", read
2026-11-11: `symbols` is a comma-separated list with no stated maximum. The behaviour is the
reference's `src/sextant/tradingview/bars.py` and `src/sextant/data/alpaca_history.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`, except that the rate, the batch and the feed are the
caller's, with no default, and a batch has no upper bound. BarPoll records one poll,
value for value from the reference's `src/sextant/tradingview/bars.py:88-108`.

Every wire request, each page and each retry, waits on the one pacer, because both paths go
through `_one_request`. Nothing here places an order or reads an account.
"""

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.models.bars import Bar as AlpacaBar
from alpaca.data.models.bars import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from sextant_movers.ranking.window import NS

MINUTE_NS = 60 * NS
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def symbol_name(symbol: str) -> str:
    """A vendor ticker kept intact, never one that could be read as a path."""
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", symbol):
        raise ValueError(f"Invalid equity symbol: {symbol!r}")
    return symbol


def decimal_value(value: Any) -> Decimal:
    """A Decimal as it is; anything else through its `str` text."""
    return value if isinstance(value, Decimal) else Decimal(str(value))


def nanos(value: datetime) -> int:
    """Epoch nanoseconds in integer arithmetic, exact at the microsecond."""
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * NS + delta.microseconds * 1000


@dataclass(frozen=True, slots=True)
class Bar:
    """One vendor one-minute bar: prices, volume and the vendor's trade count."""

    symbol: str
    start_ns: int
    open: Decimal
    close: Decimal
    volume: int
    trades: int
    vw: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("A bar needs a symbol")
        symbol_name(self.symbol)
        if type(self.start_ns) is not int or self.start_ns < 0 or self.start_ns % MINUTE_NS:
            raise ValueError("A bar starts on a whole minute")
        for name, value in (("open", self.open), ("close", self.close)):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"A bar needs a positive Decimal {name}")
        if type(self.volume) is not int or self.volume < 0:
            raise ValueError("A bar needs a non-negative integer volume")
        if type(self.trades) is not int or self.trades < 0:
            raise ValueError("A bar needs a non-negative integer trade count")
        vw = self.vw
        if vw is not None and (not isinstance(vw, Decimal) or not vw.is_finite() or vw < 0):
            raise ValueError("A bar vw must be a non-negative Decimal or None")

    @property
    def close_ns(self) -> int:
        return self.start_ns + MINUTE_NS


@dataclass(frozen=True, slots=True)
class BarPoll:
    """One complete poll of the universe: what came back and what failed."""

    bars: tuple[Bar, ...]
    requested: tuple[str, ...]
    failed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.bars, tuple) or any(not isinstance(item, Bar) for item in self.bars):
            raise ValueError("A poll carries Bars")
        for names in (self.requested, self.failed):
            if not isinstance(names, tuple) or any(
                not isinstance(name, str) or not name for name in names
            ):
                raise ValueError("A poll carries symbol names")
        if len(set(self.requested)) != len(self.requested):
            raise ValueError("A poll requests each symbol once")
        if not set(self.failed) <= set(self.requested):
            raise ValueError("A failed symbol must have been requested")


def _bar(symbol: str, row: AlpacaBar) -> Bar:
    trade_count = row.trade_count
    if trade_count is None:
        raise ValueError(f"Alpaca bar lacks the vendor trade count: {symbol}")
    return Bar(
        symbol=symbol,
        start_ns=nanos(row.timestamp),
        open=decimal_value(row.open),
        close=decimal_value(row.close),
        volume=int(row.volume),
        trades=int(trade_count),
        vw=None if row.vwap is None else decimal_value(row.vwap),
    )


class Waitable(Protocol):
    """The one pacer method the paced client needs."""

    def wait(self) -> None: ...


class Pacer:
    """A minimum interval between wire requests: 60 seconds over the caller's rate."""

    def __init__(
        self,
        calls_per_minute: Decimal,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not calls_per_minute.is_finite() or not 0 < calls_per_minute <= 10000:
            raise ValueError("Data calls per minute must be positive and at most 10000")
        self.interval = float(Decimal(60) / calls_per_minute)
        self.clock = clock
        self.sleep = sleep
        self.last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last is not None:
            delay = self.interval - (now - self.last)
            if delay > 0:
                self.sleep(delay)
        self.last = self.clock()


class PacedStockBars(StockHistoricalDataClient):
    """The SDK's stock client, with one wait on the pacer before each wire request."""

    def __init__(self, *, pacer: Waitable, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pacer = pacer

    def _one_request(self, method: str, url: str, opts: dict, retry: int) -> dict:
        self._pacer.wait()
        return super()._one_request(method, url, opts, retry)


class AlpacaBarSource:
    """One-minute bars through the paced client, `batch` sorted symbols to a request."""

    def __init__(self, client: PacedStockBars, *, batch: int, feed: DataFeed) -> None:
        if not isinstance(client, PacedStockBars):
            raise ValueError("historical bars need the paced transport, PacedStockBars")
        if type(batch) is not int or batch < 1:
            raise ValueError("Bar batches must cover at least 1 symbol")
        self.client = client
        self.batch = batch
        self.feed = feed

    def minute_bars(
        self, symbols: Sequence[str], *, start: datetime, end: datetime
    ) -> tuple[Bar, ...]:
        for name, value in (("start", start), ("end", end)):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"Bar window {name} needs a timezone")
        if end <= start:
            raise ValueError("Bar window ends before it starts")
        names = tuple(sorted({symbol_name(symbol) for symbol in symbols}))
        bars: list[Bar] = []
        for index in range(0, len(names), self.batch):
            request = StockBarsRequest(
                symbol_or_symbols=list(names[index : index + self.batch]),
                timeframe=cast(TimeFrame, TimeFrame.Minute),
                start=start,
                end=end,
                feed=self.feed,
                adjustment=Adjustment.RAW,
                sort=Sort.ASC,
            )
            response = cast(BarSet, self.client.get_stock_bars(request))
            data: Mapping[str, Sequence[AlpacaBar]] | None = response.data
            if data is None:
                raise ValueError("Alpaca bar response has no data")
            bars += [_bar(str(symbol), row) for symbol, rows in data.items() for row in rows]
        return tuple(sorted(bars, key=lambda bar: (bar.close_ns, bar.symbol)))
