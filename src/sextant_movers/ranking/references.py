"""The reference closes: the prior XNYS sessions and the daily official closes a universe gets.

Sources. gerrymanoim/exchange_calendars 4.13.2 `dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a`:
`get_calendar` and its cache (`calendar_utils.py:237,325`), the default bounds
(`exchange_calendar.py:59,62`), `date_to_session` and `sessions_window`
(`exchange_calendar.py:1281,2240`). alpacahq/alpaca-py v0.44.0
`cc4cb3b7ba50ae250e621983c2779047fb16bb28`: `get_stock_bars` (`alpaca/data/historical/stock.py:75`),
`StockBarsRequest` (`alpaca/data/requests.py:86`), `TimeFrame.Day` (`alpaca/data/timeframe.py:124`),
`Adjustment.SPLIT` (`alpaca/data/enums.py:76`), `Sort` (`alpaca/common/enums.py:36`), the SDK's
`Bar` and `BarSet` (`alpaca/data/models/bars.py:10,54`). The behaviour is the reference's
`src/sextant/tradingview/prestage.py` at `06562bec0898a7203f4eaf69086ced83d2477e6b`, except that
the prior sessions are the calendar's own window with a required count, `batch` and `feed` are the
caller's with no default and no upper bound, the client must be the paced one, and a refusal is a
plain `ValueError`.

The calendar is `get_calendar("XNYS")` with no bounds, the library's cached default, as the
reference uses it. Its bounds are taken from the clock when the module is imported, twenty years
back and one year ahead (`exchange_calendar.py:59,62`), and the cache keeps that one calendar
(`calendar_utils.py:237,325`). So the first and last sessions move with the date, and a day outside
them is refused by the calendar, not here.

The daily closes go through the one paced client, so each page and retry waits on its pacer.
Nothing here places an order or reads an account.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from decimal import Decimal
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from alpaca.common.enums import Sort
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.models.bars import BarSet
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from exchange_calendars import get_calendar

from sextant_movers.ranking.bars import AlpacaBarSource, Bar, PacedStockBars, decimal_value

ET = ZoneInfo("America/New_York")


class Source(Protocol):
    """The bar history one ranking pass may read."""

    def minute_bars(
        self, symbols: Sequence[str], *, start: datetime, end: datetime
    ) -> tuple[Bar, ...]: ...

    def daily_closes(self, symbols: Sequence[str], *, day: date) -> dict[str, Decimal]: ...


def prior_sessions(day: date, *, count: int) -> tuple[date, ...]:
    """The `count` XNYS sessions strictly before `day`, oldest first: the calendar's window.

    `get_calendar` returns its cached default calendar, twenty years back to one year ahead of
    the date it was built, so its first and last sessions move with the date; a window it cannot
    give is its own `ValueError`, passed on as it is.
    """
    if type(count) is not int or count < 1:
        raise ValueError("A prior-session count is at least 1")
    calendar = get_calendar("XNYS")
    last = calendar.date_to_session(day - timedelta(days=1), direction="previous")
    return tuple(session.date() for session in calendar.sessions_window(last, -count))


def collect_references(
    source: Source, *, day: date, symbols: Sequence[str]
) -> tuple[tuple[str, ...], dict[str, Decimal]]:
    """The official close of `day` for each name that has one, in the caller's order."""
    closes = source.daily_closes(symbols, day=day)
    names = tuple(name for name in symbols if name in closes)
    if not names:
        raise ValueError("no prior official close for the universe")
    return names, {name: closes[name] for name in names}


class AlpacaDailyBars:
    """Split-adjusted daily closes through the same paced client as the minute bars."""

    def __init__(self, client: PacedStockBars, *, batch: int, feed: DataFeed) -> None:
        if not isinstance(client, PacedStockBars):
            raise ValueError("historical bars need the paced transport, PacedStockBars")
        if type(batch) is not int or batch < 1:
            raise ValueError("A daily close batch covers at least 1 symbol")
        self._client, self._batch, self._feed = client, batch, feed

    def daily_closes(self, symbols: Sequence[str], *, day: date) -> dict[str, Decimal]:
        if not isinstance(day, date):
            raise ValueError("A daily close request needs a date")
        names = tuple(sorted(set(symbols)))
        if not names:
            return {}
        start = datetime.combine(day, clock_time(0, 0), tzinfo=ET)
        end = start + timedelta(days=1)
        closes: dict[str, Decimal] = {}
        for chunk in _chunks(names, self._batch):
            request = StockBarsRequest(
                symbol_or_symbols=list(chunk),
                timeframe=cast(TimeFrame, TimeFrame.Day),
                start=start,
                end=end,
                feed=self._feed,
                adjustment=Adjustment.SPLIT,
                sort=Sort.ASC,
            )
            response = cast(BarSet, self._client.get_stock_bars(request))
            for symbol, rows in response.data.items():
                for row in rows:
                    stamp = row.timestamp
                    zoned = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
                    if zoned.astimezone(ET).date() != day:
                        continue
                    closes[str(symbol).upper()] = decimal_value(row.close)
        return closes


class AlpacaSource:
    """The live source: the minute bars and the daily closes, each through its own part."""

    def __init__(self, bars: AlpacaBarSource, daily: AlpacaDailyBars) -> None:
        self._bars, self._daily = bars, daily

    def minute_bars(
        self, symbols: Sequence[str], *, start: datetime, end: datetime
    ) -> tuple[Bar, ...]:
        return self._bars.minute_bars(symbols, start=start, end=end)

    def daily_closes(self, symbols: Sequence[str], *, day: date) -> dict[str, Decimal]:
        return self._daily.daily_closes(symbols, day=day)


def _chunks(names: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(names), size):
        yield names[index : index + size]
