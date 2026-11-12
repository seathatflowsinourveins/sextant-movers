"""A cross-check report beside the rank, never its input.

Build on alpacahq/alpaca-py v0.44.0 cc4cb3b7ba50ae250e621983c2779047fb16bb28:
`alpaca/data/historical/screener.py:10-72` supplies the client, paths and reads;
`alpaca/data/requests.py:489-521` the requests; `alpaca/data/enums.py:117-142` the
metrics and markets; `alpaca/data/models/screener.py:8-68` the entries and answers;
`alpaca/common/rest.py:85-135,176-210` the retry loop and one-request hook.
The wait follows `PacedStockBars`; pairing uses `RankingGeneration` and `RankingRow`.
Bounds: https://docs.alpaca.markets/us/reference/movers-1.md (1 to 50 per side) and
https://docs.alpaca.markets/us/reference/mostactives-1.md (1 to 100), read 2026-11-11
as recorded in docs/landscape.md. Neither page specifies a plan or numeric rate limit.

Nothing here builds a client, reads a key or process settings, starts a stream,
concurrent work or a node, or places an order. No similarity measure is defined.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alpaca.data.enums import MarketType, MostActivesBy
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.models.screener import ActiveStock, MostActives, Mover, Movers
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest

from sextant_movers.ranking.bars import Waitable
from sextant_movers.ranking.generation import GenerationId, RankingGeneration, RankingRow


class PacedScreener(ScreenerClient):
    """The SDK client with one wait on the caller's pacer before every wire request."""

    def __init__(self, *, pacer: Waitable, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pacer = pacer

    def _one_request(self, method: str, url: str, opts: dict, retry: int) -> dict:
        self._pacer.wait()
        return super()._one_request(method, url, opts, retry)


def read(
    client: ScreenerClient, *, movers_top: int, actives_top: int, by: MostActivesBy
) -> tuple[Movers, MostActives]:
    """Read the SDK's stock movers, then most actives, with caller-selected sizes and metric."""
    if type(movers_top) is not int or not 1 <= movers_top <= 50:
        raise ValueError("movers_top must be an integer from 1 to 50")
    if type(actives_top) is not int or not 1 <= actives_top <= 100:
        raise ValueError("actives_top must be an integer from 1 to 100")
    if not isinstance(by, MostActivesBy):
        raise ValueError("by must be a MostActivesBy member")
    movers = client.get_market_movers(
        MarketMoversRequest(top=movers_top, market_type=MarketType.STOCKS)
    )
    if not isinstance(movers, Movers):
        raise TypeError("expected the SDK's Movers model")
    if movers.market_type != MarketType.STOCKS:
        raise ValueError("movers must describe stocks")
    most_actives = client.get_most_actives(MostActivesRequest(top=actives_top, by=by))
    if not isinstance(most_actives, MostActives):
        raise TypeError("expected the SDK's MostActives model")
    return movers, most_actives


@dataclass(frozen=True, slots=True)
class Pair[T: (Mover, ActiveStock)]:
    """One vendor entry and the same-symbol generation row, if present."""

    position: int
    entry: T
    row: RankingRow | None


@dataclass(frozen=True, slots=True)
class Report:
    """Generation identity, vendor timestamps and pairs, without a similarity verdict."""

    generation_id: GenerationId
    ts_event: int
    movers_updated: datetime
    actives_updated: datetime
    gainers: tuple[Pair[Mover], ...]
    losers: tuple[Pair[Mover], ...]
    actives: tuple[Pair[ActiveStock], ...]
    unlisted: tuple[RankingRow, ...]


def cross_check(generation: RankingGeneration, movers: Movers, most_actives: MostActives) -> Report:
    """Pair by exact symbol equality, preserving vendor order and generation row identity."""
    if not isinstance(generation, RankingGeneration):
        raise TypeError("expected RankingGeneration")
    if not isinstance(movers, Movers):
        raise TypeError("expected the SDK's Movers model")
    if not isinstance(most_actives, MostActives):
        raise TypeError("expected the SDK's MostActives model")
    rows = {row.symbol: row for row in generation.rows}

    def pairs[T: (Mover, ActiveStock)](entries: list[T]) -> tuple[Pair[T], ...]:
        return tuple(Pair(i, entry, rows.get(entry.symbol)) for i, entry in enumerate(entries, 1))

    listed = {
        entry.symbol for entry in (*movers.gainers, *movers.losers, *most_actives.most_actives)
    }
    return Report(
        generation_id=generation.generation_id,
        ts_event=generation.ts_event,
        movers_updated=movers.last_updated,
        actives_updated=most_actives.last_updated,
        gainers=pairs(movers.gainers),
        losers=pairs(movers.losers),
        actives=pairs(most_actives.most_actives),
        unlisted=tuple(
            row for row in generation.rows if row.rank is not None and row.symbol not in listed
        ),
    )
