"""The trade window: an immutable history of trades in one-second buckets, bounded by age.

The behaviour is the reference's (`src/sextant/ranking/window.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`), value for value and refusal for refusal, so the score
engine computes the same rows. The ticks are nautechsystems/nautilus_trader v2.0.0rc8
`TradeTick` and `Quantity` (`python/nautilus_trader/model/__init__.pyi:7035-7044`, `:6346`,
`:6357`); the engine ships no time-bounded trade window Python can call. A `deque` with
`maxlen` bounds by count, not by age, and keeps no anchor for the trade before the window.

Each append evicts every bucket older than `keep_s` seconds before the new tick's second, except
the newest bucket, and keeps the last evicted tick as the anchor that `price_at` falls back to.
`keep_s` is the caller's and is not checked: it is read only when an older bucket exists.
"""

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal
from typing import Self

from nautilus_trader.model import Quantity, TradeTick

NS = 1_000_000_000

ZERO = Decimal(0)

_ORDER = "out-of-order event; canonicalize equal-time inputs through M1 first"


def _twin(tick: TradeTick) -> TradeTick:
    return TradeTick(
        tick.instrument_id,
        tick.price,
        Quantity.from_int(1),
        tick.aggressor_side,
        tick.trade_id,
        tick.ts_event,
        tick.ts_init,
    )


@dataclass(frozen=True, slots=True)
class Bucket:
    """The ticks of one epoch second in arrival order, and their price times size."""

    second: int
    ticks: tuple[TradeTick, ...]
    dollar_volume: Decimal


@dataclass(frozen=True, slots=True)
class TradeWindow:
    """Buckets in ascending second; `anchor` is the newest evicted tick, or None."""

    buckets: tuple[Bucket, ...] = ()
    anchor: TradeTick | None = None

    def _check_order(self, tick: TradeTick) -> None:
        if self.buckets and tick.ts_event < self.buckets[-1].ticks[-1].ts_event:
            raise ValueError(_ORDER)

    def _with(
        self, tick: TradeTick, ticks: tuple[TradeTick, ...], value: Decimal | None, keep_s: int
    ) -> Self:
        """`value` None adds no dollar volume: the sum is left as it is, never `+ ZERO`."""
        second = tick.ts_event // NS
        buckets = self.buckets
        if buckets and buckets[-1].second == second:
            last = buckets[-1]
            dv = last.dollar_volume if value is None else last.dollar_volume + value
            buckets = (*buckets[:-1], Bucket(second, last.ticks + ticks, dv))
        else:
            buckets = (*buckets, Bucket(second, ticks, ZERO if value is None else value))
        anchor, i = self.anchor, 0
        while i < len(buckets) - 1 and buckets[i].second < second - keep_s:
            anchor = buckets[i].ticks[-1]
            i += 1
        return type(self)(buckets[i:], anchor)

    def append(self, tick: TradeTick, keep_s: int) -> Self:
        """A new window with `tick`; an equal event time is accepted, an earlier one refused."""
        self._check_order(tick)
        return self._with(tick, (tick,), tick.price.as_decimal() * tick.size.as_decimal(), keep_s)

    def append_count(self, tick: TradeTick, count: int, keep_s: int) -> Self:
        """Add `count` unit-size twins of `tick` that add trade count and no dollar volume.

        A bar carries a trade count, not each print: the caller appends `count - 1` twins, then
        the bar's own tick through `append`, which adds its dollar volume once. The count is
        checked before the order; a count of 0 returns this window itself, without eviction.
        """
        if type(count) is not int or count < 0:
            raise ValueError("A counted bar needs a non-negative integer trade count")
        self._check_order(tick)
        if count == 0:
            return self
        return self._with(tick, tuple(_twin(tick) for _ in range(count)), None, keep_s)

    def price_at(self, ts: int) -> TradeTick | None:
        """The last tick at or before `ts`, else the anchor if it is at or before `ts`."""
        i = bisect_right(self.buckets, ts // NS, key=lambda b: b.second) - 1
        while i >= 0:
            ticks = self.buckets[i].ticks
            j = bisect_right(ticks, ts, key=lambda t: t.ts_event) - 1
            if j >= 0:
                return ticks[j]
            i -= 1
        return self.anchor if self.anchor and self.anchor.ts_event <= ts else None

    def before(self, ts: int) -> TradeTick | None:
        """The last tick strictly before `ts`."""
        return self.price_at(ts - 1)

    def last(self, n: int) -> tuple[TradeTick, ...]:
        """The newest `n` ticks in order; the anchor is never one.

        `n` below 1 is not refused: `ticks[-0:]` is the whole tuple, so `last(0)` returns the
        newest bucket's ticks, and a negative `n` slices from the front of that bucket.
        """
        result: tuple[TradeTick, ...] = ()
        for bucket in reversed(self.buckets):
            result = bucket.ticks[-n:] + result
            if len(result) >= n:
                break
        return result[-n:]

    def first_moved(self, start: int, price: Decimal, move: Decimal) -> TradeTick | None:
        """The first tick after `start` at or above `price * (1 + move)`; never the anchor."""
        for bucket in self.buckets:
            if (bucket.second + 1) * NS <= start:
                continue
            for tick in bucket.ticks:
                if tick.ts_event > start and tick.price.as_decimal() >= price * (1 + move):
                    return tick
        return None

    def totals(self, start: int, end: int) -> tuple[Decimal, int]:
        """Dollar volume and trade count over `(start, end]`, open at start, closed at end.

        A bucket wholly inside adds its stored dollar volume, which leaves counted twins out; a
        bucket cut by a bound adds price times size per tick, so each twin there adds its price.
        """
        dv, count = ZERO, 0
        for bucket in self.buckets:
            if bucket.second * NS > end or (bucket.second + 1) * NS <= start:
                continue
            if bucket.ticks[0].ts_event > start and bucket.ticks[-1].ts_event <= end:
                dv += bucket.dollar_volume
                count += len(bucket.ticks)
            else:
                for tick in bucket.ticks:
                    if start < tick.ts_event <= end:
                        dv += tick.price.as_decimal() * tick.size.as_decimal()
                        count += 1
        return dv, count
