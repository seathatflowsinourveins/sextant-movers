"""Part 4a: the trade window, a bounded per-second history of trades.

Synthetic instruments, prices and sizes only. The reference is `src/sextant/ranking/window.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never imports it. The `local` proof
runs it in a child interpreter named by `MOVERS_REFERENCE_PYTHON`, with the export's `src`
directory named by `MOVERS_REFERENCE_SRC`, over the same seeded tick streams and queries.
"""

import ast
import hashlib
import inspect
import json
import os
import subprocess
from dataclasses import FrozenInstanceError
from decimal import Decimal as D
from importlib.metadata import version
from pathlib import Path
from typing import Any

import nautilus_trader.model as model
import pytest
from nautilus_trader.model import AggressorSide, InstrumentId, Price, Quantity, TradeId, TradeTick

from sextant_movers.ranking import window as win
from sextant_movers.ranking.window import NS, ZERO, Bucket, TradeWindow

REFERENCE_BLOB = "4180e5aa8466eb4c548488218d3826fac10fa0ef"  # git blob of the reference file
# Measured by the `local` proof from the reference in its child interpreter, then held here.
ANSWERS = 202_469
REFERENCE_DIGEST = "c91eb8deb02c94b53b81ba956ece842467d66680ab9af30ed027eb996561a746"
ORDER = "out-of-order event; canonicalize equal-time inputs through M1 first"
COUNT = "A counted bar needs a non-negative integer trade count"
DAY = 20_000 * 86_400 * NS  # a synthetic session start, on an exact second
ID = InstrumentId.from_str("ZZZT1.TEST")


def tick(ts: int, price: str = "1.00", size: int | str = 1, name: str = "") -> TradeTick:
    quantity = Quantity.from_int(size) if isinstance(size, int) else Quantity.from_str(size)
    trade = TradeId(name or f"T{ts}")
    return TradeTick(ID, Price.from_str(price), quantity, AggressorSide.BUY, trade, ts, ts)


def fill(*ticks: TradeTick, keep_s: int = 400) -> TradeWindow:
    window = TradeWindow()
    for item in ticks:
        window = window.append(item, keep_s)
    return window


def test_the_public_names_and_constants() -> None:
    assert NS == 1_000_000_000
    assert ZERO == D(0) and isinstance(ZERO, D)
    assert TradeWindow() == TradeWindow((), None)


def test_an_empty_window_answers_nothing() -> None:
    window = TradeWindow()
    assert window.buckets == () and window.anchor is None
    for ts in (0, DAY, DAY + NS - 1):
        assert window.price_at(ts) is None
        assert window.before(ts) is None
        assert window.first_moved(ts, D(1), D(0)) is None
        assert window.totals(ts - NS, ts) == (ZERO, 0)
    assert window.last(1) == window.last(3) == ()


def test_one_tick() -> None:
    t = tick(DAY + 7, "1.25", 3)
    window = fill(t)
    assert window.buckets == (Bucket(DAY // NS, (t,), D("3.75")),)
    assert window.price_at(DAY + 7) == t and window.price_at(DAY + 10 * NS) == t
    assert window.price_at(DAY + 6) is None
    assert window.before(DAY + 7) is None and window.before(DAY + 8) == t
    assert window.last(1) == window.last(5) == (t,)
    assert window.totals(DAY + 6, DAY + 7) == (D("3.75"), 1)
    assert window.totals(DAY + 7, DAY + 9 * NS) == (ZERO, 0)  # start is exclusive
    assert window.totals(DAY, DAY + 6) == (ZERO, 0)  # end is inclusive, and before the tick


def test_equal_timestamps_share_a_bucket_and_the_last_one_answers() -> None:
    a, b = tick(DAY + 5, "1.00", 2, "a"), tick(DAY + 5, "2.00", 3, "b")
    window = fill(a, b)
    assert window.buckets == (Bucket(DAY // NS, (a, b), D("8.00")),)
    assert window.price_at(DAY + 5) == b
    assert window.last(1) == (b,) and window.last(2) == (a, b)
    assert window.totals(DAY + 4, DAY + 5) == (D("8.00"), 2)


def test_exact_second_boundaries_and_both_edges_of_totals() -> None:
    a, b, c = (
        tick(DAY + NS, "1.00", 1),
        tick(DAY + 2 * NS - 1, "2.00", 1),
        tick(DAY + 2 * NS, "4.00"),
    )
    window = fill(a, b, c)
    assert [bucket.second for bucket in window.buckets] == [DAY // NS + 1, DAY // NS + 2]
    assert window.price_at(DAY + 2 * NS - 1) == b and window.price_at(DAY + 2 * NS) == c
    assert window.before(DAY + 2 * NS) == b and window.before(DAY + NS) is None
    assert window.totals(DAY + NS - 1, DAY + 2 * NS - 1) == (D("3.00"), 2)  # whole bucket
    assert window.totals(DAY + NS, DAY + 2 * NS) == (D("6.00"), 2)  # a excluded, c included
    assert window.totals(DAY + 2 * NS - 1, DAY + 2 * NS) == (D("4.00"), 1)
    assert window.totals(DAY, DAY + 3 * NS) == (D("7.00"), 3)
    assert window.totals(DAY + 2 * NS, DAY + NS) == (ZERO, 0)  # an empty range
    assert window.last(2) == (b, c) and window.last(3) == (a, b, c) and window.last(9) == (a, b, c)


def test_decimal_dollar_volume_is_exact() -> None:
    window = fill(tick(DAY, "0.0001", "2.5"), tick(DAY + 1, "12.50", 7))
    assert window.buckets[0].dollar_volume == D("87.50025")
    assert str(window.totals(DAY - 1, DAY + 1)[0]) == "87.50025"


def test_eviction_keeps_the_newest_bucket_and_sets_the_anchor() -> None:
    old, edge, new = tick(DAY + 9 * NS), tick(DAY + 10 * NS), tick(DAY + 20 * NS)
    first = tick(DAY + 1 * NS, "3.00")
    window = fill(first, old, edge, new, keep_s=10)
    assert [b.second for b in window.buckets] == [
        DAY // NS + 10,
        DAY // NS + 20,
    ]  # 10 < 10 is false
    assert window.anchor == old
    assert window.price_at(DAY + 9 * NS) == old and window.price_at(DAY + 10 * NS - 1) == old
    assert window.price_at(DAY + 9 * NS - 1) is None  # the anchor is newer than the query
    assert window.before(DAY + 10 * NS) == old and window.before(DAY + 9 * NS) is None
    assert window.totals(DAY, DAY + 20 * NS) == (D("2.00"), 2)  # evicted ticks count nothing
    assert window.last(5) == (edge, new)


def test_a_gap_longer_than_keep_s_evicts_all_but_the_newest() -> None:
    a, b = tick(DAY, "2.00"), tick(DAY + 31 * NS + 3)
    window = fill(a, b, keep_s=30)
    assert window.buckets == (Bucket(DAY // NS + 31, (b,), D("1.00")),) and window.anchor == a
    assert window.price_at(DAY + 31 * NS + 2) == a and window.before(DAY + 31 * NS + 3) == a
    c = tick(DAY + 70 * NS)
    later = window.append(c, 30)
    assert later.anchor == b and later.buckets == (Bucket(DAY // NS + 70, (c,), D("1.00")),)
    assert later.price_at(DAY + 31 * NS + 2) is None  # the first anchor is gone


def test_a_negative_keep_s_keeps_only_the_newest_bucket() -> None:
    a, b, c = tick(DAY), tick(DAY + NS), tick(DAY + NS + 1)
    window = fill(a, b, c, keep_s=-1)
    assert window.buckets == (Bucket(DAY // NS + 1, (b, c), D("2.00")),) and window.anchor == a


def test_keep_s_is_read_only_when_an_older_bucket_exists() -> None:
    window = TradeWindow().append(tick(DAY), "not a number")  # type: ignore[arg-type]
    assert len(window.buckets) == 1
    with pytest.raises(TypeError):
        window.append(tick(DAY + NS), "not a number")  # type: ignore[arg-type]


def test_append_count_zero_one_and_n() -> None:
    t = tick(DAY + 5, "1.25", 9, "bar")
    empty = TradeWindow()
    assert empty.append_count(t, 0, 400) is empty
    one = empty.append_count(t, 1, 400)
    (twin,) = one.buckets[0].ticks
    assert one.buckets[0].dollar_volume == ZERO and twin.size == Quantity.from_int(1)
    assert (twin.instrument_id, twin.price, twin.aggressor_side, twin.trade_id) == (
        t.instrument_id,
        t.price,
        t.aggressor_side,
        t.trade_id,
    )
    assert (twin.ts_event, twin.ts_init) == (t.ts_event, t.ts_init)
    many = empty.append_count(t, 6, 400).append(t, 400)
    assert len(many.buckets[0].ticks) == 7 and many.buckets[0].dollar_volume == D("11.25")
    assert many.totals(DAY, DAY + 5) == (D("11.25"), 7)
    assert many.append_count(t, 0, 400) is many


def test_append_count_mixed_with_append_and_eviction() -> None:
    a, b = tick(DAY, "2.00", 4), tick(DAY + 50 * NS, "3.00", 2)
    window = fill(a).append_count(tick(DAY + 1, "2.00", 1), 3, 400)
    assert window.buckets[0].dollar_volume == D("8.00") and len(window.buckets[0].ticks) == 4
    later = window.append_count(b, 2, 10)
    assert later.anchor is not None and later.anchor == window.buckets[0].ticks[-1]
    assert later.anchor.size == Quantity.from_int(1)
    assert later.buckets == (Bucket(DAY // NS + 50, later.buckets[0].ticks, ZERO),)
    assert later.totals(DAY, DAY + 50 * NS) == (ZERO, 2)


def test_counted_twins_match_independent_appends() -> None:
    """The reference's one direct witness, the window part only, on synthetic values."""
    for count in (1, 4, 50):
        volume = 10 + count
        sizes = [1] * (count - 1) + [volume - (count - 1)]
        partition = [tick(DAY, "1.2500", size, str(index)) for index, size in enumerate(sizes)]
        real = tick(DAY, "1.2500", volume, "real")
        independent = fill(*partition)
        seeded = TradeWindow().append_count(real, count - 1, 400).append(real, 400)
        assert len(seeded.buckets) == len(independent.buckets) == 1
        assert seeded.buckets[0].dollar_volume == independent.buckets[0].dollar_volume
        assert seeded.totals(DAY - 60 * NS, DAY) == (D("1.2500") * volume, count)
        assert seeded.totals(DAY - 60 * NS, DAY) == independent.totals(DAY - 60 * NS, DAY)
        seeded_price, independent_price = seeded.price_at(DAY), independent.price_at(DAY)
        assert seeded_price is not None and independent_price is not None
        assert seeded_price.price.as_decimal() == independent_price.price.as_decimal()
        with pytest.raises(ValueError, match="out-of-order"):
            seeded.append(tick(DAY - NS, "1.0000", 1, "early"), 400)


def test_a_partial_bucket_counts_each_twin_at_unit_size() -> None:
    """Kept for parity and reported: only the whole-bucket path leaves the twins' size out."""
    t, u = tick(DAY + 5, "2.00", 3), tick(DAY + 6, "1.00", 1)
    window = TradeWindow().append_count(t, 2, 400).append(t, 400).append(u, 400)
    assert window.totals(DAY, DAY + 6) == (D("7.00"), 4)
    assert window.totals(DAY, DAY + 5) == (D("10.00"), 3)


@pytest.mark.parametrize(
    ("call", "error", "message"),
    [
        (lambda w: w.append(tick(DAY + 9), 400), ValueError, ORDER),
        (lambda w: w.append(tick(DAY + 9 - NS), 400), ValueError, ORDER),
        (lambda w: w.append_count(tick(DAY + 9), 1, 400), ValueError, ORDER),
        (lambda w: w.append_count(tick(DAY + 9), 0, 400), ValueError, ORDER),
        (lambda w: w.append_count(tick(DAY + 20), -1, 400), ValueError, COUNT),
        (lambda w: w.append_count(tick(DAY + 9), -1, 400), ValueError, COUNT),
        (lambda w: w.append_count(tick(DAY + 20), True, 400), ValueError, COUNT),
        (lambda w: w.append_count(tick(DAY + 20), 1.0, 400), ValueError, COUNT),
        (lambda w: w.append_count(tick(DAY + 20), "1", 400), ValueError, COUNT),
    ],
)
def test_each_refusal_has_the_reference_type_and_message(
    call: Any, error: type, message: str
) -> None:
    window = fill(tick(DAY + 10))
    with pytest.raises(error) as raised:
        call(window)
    assert type(raised.value) is error and str(raised.value) == message
    assert window == fill(tick(DAY + 10))


def test_a_size_zero_trade_tick_is_refused_by_the_engine() -> None:
    with pytest.raises(ValueError, match="invalid `Quantity` for 'size' not positive, was 0"):
        tick(DAY, "1.00", 0)


def test_append_returns_a_new_window_and_leaves_the_old_one() -> None:
    a, b = tick(DAY), tick(DAY + 1)
    old = fill(a)
    snapshot = (old.buckets, old.anchor)
    new = old.append(b, 400)
    counted = old.append_count(b, 2, 400)
    assert new is not old and counted is not old and (old.buckets, old.anchor) == snapshot
    assert old == fill(a) and new == fill(a, b)
    for record, name in ((old, "buckets"), (old, "anchor"), (old.buckets[0], "dollar_volume")):
        with pytest.raises(FrozenInstanceError):
            setattr(record, name, None)
    assert not hasattr(old, "__dict__") and not hasattr(old.buckets[0], "__dict__")


def test_last_below_one_is_not_refused() -> None:
    """Kept for parity and reported: `[-0:]` is the whole tuple, so `last(0)` is not empty."""
    a, b, c = tick(DAY), tick(DAY + NS), tick(DAY + NS + 1)
    window = fill(a, b, c)
    assert window.last(0) == (b, c)
    assert window.last(-1) == ()


def test_first_moved() -> None:
    a, b, c, d = (
        tick(DAY + 1, "1.00"),
        tick(DAY + 2, "1.10"),
        tick(DAY + NS + 3, "1.09"),
        tick(DAY + NS + 4, "1.20"),
    )
    window = fill(a, b, c, d)
    assert window.first_moved(DAY, D("1.00"), D("0.10")) == b  # >= the threshold
    assert window.first_moved(DAY + 2, D("1.00"), D("0.10")) == d  # start is exclusive
    assert window.first_moved(DAY + NS, D("1.00"), D("0.05")) == c  # an earlier bucket is skipped
    assert window.first_moved(DAY, D("1.00"), D("-0.5")) == a
    assert window.first_moved(DAY + NS + 4, D("1.00"), D("0")) is None
    evicted = fill(a, tick(DAY + 50 * NS, "0.50"), keep_s=10)
    assert evicted.first_moved(0, D("1.00"), D("0")) is None  # the anchor is never a candidate


def test_the_module_imports_the_standard_library_and_the_engine_model_only() -> None:
    tree = ast.parse(Path(win.__file__).read_text())
    imports = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imports == {"bisect", "dataclasses", "decimal", "typing", "nautilus_trader.model"}


def build_streams(count: int = 320) -> list[tuple[int, list[tuple]]]:
    """Seeded synthetic streams: (keep_s, operations). Shared with the child by its source."""
    import random

    ns = 1_000_000_000
    rng = random.Random(20261111)  # noqa: S311 - a seeded synthetic stream, not a secret
    prices = ("1.00", "1.25", "0.0001", "12.50", "99.99", "100", "3.1415")
    sizes = (1, 2, 7, 100, 999, "0.5", "2.25")
    streams: list[tuple[int, list[tuple]]] = []
    for index in range(count):
        keep = (0, 1, 2, 5, 30, -1)[index % 6]
        ts = 20_000 * 86_400 * ns + rng.randrange(3) * ns + rng.choice((0, 1, ns - 1))
        operations: list[tuple] = []
        for _ in range(rng.choice((0, 1, 3, 8, 20, 40))):
            step = rng.randrange(8)
            if step == 0:
                ts += 0
            elif step == 1:
                ts += 1
            elif step == 2:
                ts = (ts // ns + 1) * ns
            elif step == 3:
                ts = (ts // ns + 1) * ns - 1 if ts % ns != ns - 1 else ts
            elif step == 4:
                ts += rng.randrange(1, 3) * ns + rng.randrange(ns)
            elif step == 5:
                ts += (abs(keep) + rng.randrange(1, 4)) * ns + rng.choice((0, 1))
            elif step == 6:
                operations.append(("tick", ts - rng.choice((1, ns)), rng.choice(prices), 1))
                continue
            else:
                counted = rng.choice((0, 1, 2, 9, 50, -1, True))
                operations.append(("count", ts, rng.choice(prices), rng.choice(sizes), counted))
                continue
            operations.append(("tick", ts, rng.choice(prices), rng.choice(sizes)))
        streams.append((keep, operations))
    return streams


def run_streams(m: Any, nt: Any, streams: list[tuple[int, list[tuple]]]) -> tuple[int, str]:
    """Every answer of every query, as canonical JSON, hashed into one SHA-256."""
    import hashlib
    import itertools
    import json
    from decimal import Decimal

    ns, total, answers = m.NS, hashlib.sha256(), 0
    sides = (nt.AggressorSide.BUY, nt.AggressorSide.SELL, nt.AggressorSide.NO_AGGRESSOR)

    def put(value: Any) -> None:
        nonlocal answers
        answers += 1
        total.update(json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n")

    def fields(t: Any) -> Any:
        if t is None:
            return None
        names = (t.instrument_id, t.price, t.size, t.aggressor_side, t.trade_id)
        return [*map(str, names), t.ts_event, t.ts_init]

    def state(w: Any) -> Any:
        buckets = [
            [b.second, [fields(t) for t in b.ticks], str(b.dollar_volume)] for b in w.buckets
        ]
        return {"buckets": buckets, "anchor": fields(w.anchor)}

    def queries(w: Any, at: int, keep: int) -> None:
        second = at // ns
        anchor = w.anchor.ts_event if w.anchor is not None else at
        oldest = w.buckets[0].second * ns if w.buckets else at
        points = sorted(
            {at, at - 1, at + 1, second * ns, second * ns - 1, (second + 1) * ns}
            | {(second + 1) * ns - 1, at - keep * ns, anchor, anchor - 1, oldest, oldest - 1}
        )
        for p in points:
            put(["price_at", p, fields(w.price_at(p))])
            put(["before", p, fields(w.before(p))])
        pairs = [*itertools.pairwise(points), (points[0], points[-1]), (at, at)]
        pairs += [(at - 1, at), (at, at - 1), (oldest - 1, at), (anchor, at), (at - ns, at)]
        for start, end in pairs:
            dv, count = w.totals(start, end)
            put(["totals", start, end, str(dv), count])
        for n in (-1, 0, 1, 2, 7, 1000):
            put(["last", n, [fields(t) for t in w.last(n)]])
        for start in (oldest - 1, at - 1, at - ns):
            for price, move in (("1.00", "0"), ("1.25", "0.01"), ("50", "-0.5"), ("1.00", "0.1")):
                found = w.first_moved(start, Decimal(price), Decimal(move))
                put(["first_moved", start, price, move, fields(found)])
        put(["state", state(w)])

    for index, (keep, operations) in enumerate(streams):
        window = m.TradeWindow()
        queries(window, 20_000 * 86_400 * ns, keep)
        for number, (kind, ts, price, size, *rest) in enumerate(operations):
            q = nt.Quantity.from_int(size) if isinstance(size, int) else nt.Quantity.from_str(size)
            t = nt.TradeTick(
                nt.InstrumentId.from_str(f"ZZZT{index % 7}.TEST"),
                nt.Price.from_str(price),
                q,
                sides[number % 3],
                nt.TradeId(f"S{index}N{number}"),
                ts,
                ts,
            )
            before = state(window)
            try:
                if kind == "tick":
                    window = window.append(t, keep)
                else:
                    window = window.append_count(t, rest[0], keep)
                put(["ok", kind, number])
            except Exception as error:
                put(["refused", kind, number, type(error).__name__, str(error)])
            queries(window, ts, keep)
            put(["unchanged", before == state(window)])
    return answers, total.hexdigest()


def test_the_seeded_streams_cover_every_case() -> None:
    streams = build_streams()
    ops = [op for _, operations in streams for op in operations]
    assert len(streams) == 320 and {keep for keep, _ in streams} == {0, 1, 2, 5, 30, -1}
    counts = [op[4] for op in ops if op[0] == "count"]
    assert {c for c in counts if type(c) is int} == {0, 1, 2, 9, 50, -1}
    assert any(c is True for c in counts)
    assert any(not operations for _, operations in streams)
    seen = {"out of order": 0, "gap over keep_s": 0, "equal time": 0, "second edge": 0}
    for keep, operations in streams:
        last = None
        for kind, ts, *_ in operations:
            if kind != "tick":
                continue
            if last is not None and ts < last:
                seen["out of order"] += 1
                continue
            seen["gap over keep_s"] += last is not None and ts // NS - last // NS > abs(keep)
            seen["equal time"] += ts == last
            seen["second edge"] += ts % NS in (0, NS - 1)
            last = ts
    assert min(seen.values()) > 10, seen
    assert run_streams(win, model, streams) == (ANSWERS, REFERENCE_DIGEST)


CHILD = """
import json, os, sys
from importlib.metadata import version
import nautilus_trader.model as nt
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.ranking import window as ref
assert os.path.samefile(ref.__file__, os.path.join(SRC, "sextant", "ranking", "window.py"))
streams = build_streams()
answers, digest = run_streams(ref, nt, streams)
print("PARITY " + json.dumps({
    "streams": len(streams), "answers": answers, "digest": digest,
    "engine": version("nautilus-trader"),
}))
"""


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter() -> None:
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip("set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof")
    blob = (Path(src) / "sextant" / "ranking" / "window.py").read_bytes()
    git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
    assert git_blob.hexdigest() == REFERENCE_BLOB
    prelude = "from typing import Any\n"
    script = prelude + inspect.getsource(build_streams) + inspect.getsource(run_streams) + CHILD
    done = subprocess.run(
        [python, "-I", "-c", script],
        env={"MOVERS_REFERENCE_SRC": src},
        capture_output=True,
        text=True,
        timeout=900,
        check=True,
    )
    line = next(x for x in done.stdout.splitlines() if x.startswith("PARITY "))
    theirs = json.loads(line.removeprefix("PARITY "))
    streams = build_streams()
    answers, digest = run_streams(win, model, streams)
    ours = {"streams": len(streams), "answers": answers, "digest": digest}
    ours["engine"] = version("nautilus-trader")
    print(f"parity: ours {ours}, reference {theirs}, equal {digest == theirs['digest']}")
    assert theirs["streams"] == len(streams) == 320 and theirs["answers"] == answers
    assert theirs["digest"] == digest
