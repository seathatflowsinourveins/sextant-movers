"""Part 4d: the score engine, a pure per-trade transition, and the ranking order.

Synthetic symbols, prices, sizes and dates only. The reference is `src/sextant/ranking/engine.py`
at `06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never imports it. Seven of its eight
engine cases (`tests/proofs/test_ranking.py` at that commit) drive a publisher this repository has
not built; they are rewritten here as direct calls, each named `test_ref_*`. The eighth, the
time-matched baseline median, is already a direct call and is ported in `test_baseline.py`. The
`local` proof runs the reference in a child interpreter, as `tests/proofs/test_baseline.py` does.
"""

import ast
import hashlib
import inspect
import json
import os
import random
import subprocess
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import nautilus_trader.model as nt
import pytest
from nautilus_trader.model import (
    AggressorSide,
    InstrumentId,
    Price,
    Quantity,
    QuoteTick,
    TradeId,
    TradeTick,
)

from sextant_movers.ranking import baseline, engine
from sextant_movers.ranking.baseline import BaselineSample, BaselineTable, Segment
from sextant_movers.ranking.engine import (
    GATES,
    MoverState,
    ReferencePrices,
    advance_mover,
    qualifies,
    rank_movers,
)
from sextant_movers.ranking.registry import ROWS, Registry, RegistryRow

D = Decimal
NS = 1_000_000_000
DAY = date(2030, 11, 29)
START = (1_922_140_800 + 9 * 3600) * NS  # 04:00 ET on DAY, an explicit fixture boundary
SCOPE = Segment("PRE", DAY, START, START + 19_800 * NS, "fixture boundary")
POST = replace(SCOPE, name="POST")
REFERENCE_BLOB = "15e1907ac6c3e24218e6ae945001ea3ab0c1492f"  # git blob of the reference file
FUTURE = "regressed receipt or future reference/baseline"
BASELINE_SESSION = "current/future baseline session"
VALUES: dict[str, Decimal | int] = {
    "mover_min_move": D("0.05"),
    "mover_min_trades_accel": D("2"),
    "mover_min_dvol_ratio": D("0.5"),
    "move_1m_window_s": 60,
    "move_5m_window_s": 300,
    "trades_accel_window_s": 60,
    "trades_accel_baseline_min": 5,
    "publish_min_interval_s": 1,
    "generation_max_age_s": 2,
    "heartbeat_max_age_s": 2,
    "input_max_age_s": 2,
    "top_n": 2,
}


def registry(**over: Decimal | int) -> Registry:
    values = {**VALUES, **over}
    return Registry(
        tuple(
            RegistryRow(n, ROWS[n][1], v, "fixture only", "UNCALIBRATED", DAY)
            for n, v in values.items()
        )
    )


def table(
    *,
    dv: str = "500",
    feed: str = "sip",
    session: str = "2030-11-27",
    symbols: tuple[str, ...] = ("ZZZT", "ZZZU"),
    known_at: int = START - NS,
) -> BaselineTable:
    curve = ((0, "0", "0"), (600, dv, "100"))
    samples = tuple(
        BaselineSample(s, "PRE", session, off, D(v), D(c)) for s in symbols for off, v, c in curve
    )
    return BaselineTable(samples, "fixture curves", known_at, feed)


REG, BASE = registry(), table()
REFS = ReferencePrices(D("2"), START - NS, "fixture close")


def tick(second: int, price: str = "2.00", name: str = "ZZZT", *, ns: int = 0) -> TradeTick:
    at = START + second * NS + ns
    iid = InstrumentId.from_str(name + ".SIM")
    return TradeTick(
        iid, Price.from_str(price), Quantity.from_int(10), AggressorSide.BUY, TradeId("t"), at, at
    )


def quote(at: int, name: str = "ZZZT", *, bid: str = "1.99", ask: str = "2.01") -> QuoteTick:
    iid, size = InstrumentId.from_str(name + ".SIM"), Quantity.from_int(1)
    return QuoteTick(iid, Price.from_str(bid), Price.from_str(ask), size, size, at, at)


def step(state: MoverState, t: TradeTick, **kw: Any) -> tuple[MoverState, Any]:
    refs, base = kw.pop("refs", REFS), kw.pop("base", BASE)
    kw = {"segment": SCOPE, "feed": "sip", "registry": REG, **kw}
    return advance_mover(state, t, refs, base, **kw)


def run(*ticks: TradeTick, **kw: Any) -> tuple[MoverState, Any]:
    state, row = MoverState(), None
    for t in ticks:
        state, row = step(state, t, **kw)
    return state, row


def warm(name: str = "ZZZT") -> tuple[TradeTick, ...]:
    flat = tuple(tick(s, name=name) for s in range(0, 301, 60))
    return flat + tuple(tick(s, "2.20", name) for s in range(310, 361, 10))


WARM, WARM_ROW = run(*warm())


def test_ref_per_trade_score_uses_its_own_normal() -> None:
    # $120 + $132 against a $300 time-matched normal; move 10%; 6 trades a minute against 1.
    row = WARM_ROW
    assert (row.move, row.dvol_ratio, row.score, row.trades_accel) == (
        D("0.1"),
        D("0.84"),
        D("0.084"),
        D(6),
    )
    assert row.move_1m == row.move_5m == D("0.1") and row.observations["price"] == D("2.20")
    assert row.gates_passed == GATES and qualifies(row) and row.observations["uncalibrated"]
    assert row.observations["unknown_reasons"] == () and row.params_hash == REG.params_hash
    _, later = step(WARM, tick(360, "2.40", ns=1))
    assert later.score != row.score


def test_ref_rank_is_deterministic_and_leaves_out_null_scores() -> None:
    rows = rank_movers(
        (
            replace(WARM_ROW, symbol="ZZZZ", trades_accel=D(2)),
            replace(WARM_ROW, symbol="ZZZB", trades_accel=D(3)),
            replace(WARM_ROW, symbol="ZZZA", trades_accel=D(3)),
            replace(WARM_ROW, symbol="ZZZN", score=None),
            replace(WARM_ROW, symbol="ZZZC", trades_accel=None),
            replace(WARM_ROW, symbol="ZZZY", score=D("0.09")),
        )
    )
    assert [(r.symbol, r.rank) for r in rows] == [
        ("ZZZY", 1),
        ("ZZZA", 2),
        ("ZZZB", 3),
        ("ZZZZ", 4),
    ]
    failing = replace(WARM_ROW, gates_passed=GATES[:3])
    assert rank_movers([failing])[0].rank == 1 and not qualifies(failing)


@pytest.mark.parametrize("missing", ["reference", "prefix", "baseline", "zero_normal"])
def test_ref_missing_inputs_never_manufacture_a_score(missing: str) -> None:
    kw: dict[str, Any] = {
        "reference": {"refs": ReferencePrices(None, START - NS, "fixture close")},
        "baseline": {"base": replace(BASE, samples=())},
        "zero_normal": {"base": table(dv="0")},
    }.get(missing, {})
    _, row = run(tick(1 if missing == "prefix" else 0), tick(360, "2.40"), **kw)
    assert row.score is None and row.observations["unknown_reasons"]


def test_ref_price_and_volume_flags_and_a_future_reference() -> None:
    initial, _ = run(tick(0))
    assert step(initial, tick(10, "9.00"), price_eligible=False, volume_eligible=False) == (
        initial,
        None,
    )
    state, row = step(initial, tick(20, "9.00"), price_eligible=False)
    assert row.observations["price"] == D("2.00") and row.observations["dollar_volume"] == D(110)
    _, row = step(state, tick(30, "2.40"), volume_eligible=False)
    assert row.observations["price"] == D("2.40") and row.observations["dollar_volume"] == D(110)
    with pytest.raises(ValueError, match=FUTURE):
        step(initial, tick(40), refs=ReferencePrices(D(2), START + 50 * NS, "fixture"))


def test_ref_feed_and_session_change_refuse_spliced_baselines() -> None:
    with pytest.raises(ValueError, match="feed"):
        step(WARM, tick(361), base=table(feed="boats"))
    with pytest.raises(ValueError, match="session"):
        step(WARM, tick(361), segment=replace(SCOPE, start_ns=START + 300 * NS))


def test_ref_a_trade_at_the_post_start_is_no_post_trade() -> None:
    state = MoverState()
    assert step(state, tick(0, "3.00"), segment=POST) == (state, None)


def test_ref_changed_parameters_and_current_baseline_cannot_reuse_history() -> None:
    with pytest.raises(ValueError, match="parameters changed"):
        step(WARM, tick(361), registry=registry(publish_min_interval_s=2))
    with pytest.raises(ValueError, match="baseline session"):
        step(WARM, tick(361), base=table(session=DAY.isoformat()))


REFUSALS: dict[str, tuple[Any, str]] = {
    "baseline feed": (
        lambda: step(WARM, tick(361), base=table(feed="boats")),
        "baseline feed mismatch",
    ),
    "feed name": (lambda: step(WARM, tick(361), feed="iex"), "baseline feed mismatch"),
    "feed change": (
        lambda: step(WARM, tick(361), feed="boats", base=table(feed="boats")),
        "source/session changed; create a fresh publisher",
    ),
    "segment": (
        lambda: step(WARM, tick(361), segment=replace(SCOPE, source="other")),
        "source/session changed; create a fresh publisher",
    ),
    "symbol": (lambda: step(WARM, tick(361, name="ZZZU")), "one MoverState per symbol"),
    "parameters": (
        lambda: step(WARM, tick(361), registry=registry(top_n=3)),
        "parameters changed; create fresh history",
    ),
    "before": (lambda: run(tick(0, ns=-1)), "trade outside supplied session"),
    "at end": (lambda: run(tick(19_800)), "trade outside supplied session"),
    "regressed": (lambda: step(WARM, tick(300)), FUTURE),
    "known_at": (
        lambda: step(step(WARM, tick(361), known_at=START + 900 * NS)[0], tick(362)),
        FUTURE,
    ),
    "reference": (lambda: run(tick(0), refs=ReferencePrices(D(2), START + 1, "x")), FUTURE),
    "baseline": (lambda: run(tick(0), base=table(known_at=START + 1)), FUTURE),
    "current": (lambda: run(tick(0), base=table(session=DAY.isoformat())), BASELINE_SESSION),
    "future": (lambda: run(tick(0), base=table(session=str(DAY + timedelta(1)))), BASELINE_SESSION),
    "quote symbol": (
        lambda: step(WARM, tick(361), quote=quote(START, "ZZZU")),
        "quote symbol mismatch",
    ),
    "quote event": (
        lambda: step(WARM, tick(361), quote=quote(START + 361 * NS + 1)),
        "future quote",
    ),
    "quote init": (
        lambda: step(WARM, tick(361), quote=replace_init(quote(START), START + 362 * NS)),
        "future quote",
    ),
}


def replace_init(q: QuoteTick, ts_init: int) -> QuoteTick:
    return QuoteTick(
        q.instrument_id, q.bid_price, q.ask_price, q.bid_size, q.ask_size, q.ts_event, ts_init
    )


@pytest.mark.parametrize("case", REFUSALS)
def test_each_refusal_is_a_value_error_with_its_message(case: str) -> None:
    call, message = REFUSALS[case]
    with pytest.raises(ValueError) as caught:
        call()
    assert type(caught.value) is ValueError and str(caught.value) == message


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((D(2), 0, ""), "official split-adjusted completed-close source required"),
        ((2.0, 0, "x"), "reference price must be positive finite Decimal"),
        ((D(0), 0, "x"), "reference price must be positive finite Decimal"),
        ((D("NaN"), 0, "x"), "reference price must be positive finite Decimal"),
        ((D(2), 0, "x", D(-1), 0), "reference price must be positive finite Decimal"),
        ((D(2), 0, "x", D(2)), "regular open requires its event timestamp"),
        ((D(2), 0, "x", None, 0), "regular open requires its event timestamp"),
    ],
)
def test_reference_prices_refuse(args: tuple[Any, ...], message: str) -> None:
    with pytest.raises(ValueError) as caught:
        ReferencePrices(*args)
    assert str(caught.value) == message


def test_quiet_returns_change_nothing() -> None:
    for state in (MoverState(), WARM):
        assert step(state, tick(0), segment=POST) == (state, None)
        later = tick(361, "9.00")
        assert step(state, later, price_eligible=False, volume_eligible=False) == (state, None)


def test_a_quiet_post_start_runs_before_the_receipt_checks() -> None:
    """Kept from the reference: a trade at a POST start is quiet even with a future reference."""
    future = ReferencePrices(D(2), START + 900 * NS, "fixture")
    assert step(MoverState(), tick(0), segment=POST, refs=future) == (MoverState(), None)


def test_a_trade_at_the_start_covers_the_prefix_and_a_late_one_never_does() -> None:
    assert WARM.segment_covered and WARM_ROW.observations["segment_total_covered"]
    # Kept from the reference: only a trade at exactly `start_ns` covers the prefix, so a state that
    # was attached before the first trade still leaves the ratio unknown for the whole segment.
    late, row = run(tick(0, ns=1), tick(360, "2.40"))
    assert row.dvol_ratio is None and row.score is None and not late.segment_covered
    assert row.observations["dollar_volume"] is None
    assert row.observations["observed_dollar_volume_lower_bound"] == D(44)
    assert "segment_prefix_unknown" in row.observations["unknown_reasons"]


def test_quotes_fresh_stale_and_kept() -> None:
    state, row = step(MoverState(), tick(0), quote=quote(START - NS))
    assert row.observations["spread"] is None and state.quote is None
    state, row = step(state, tick(10), quote=quote(START + 5 * NS))
    assert row.observations["spread"] == D("0.02")
    state, row = step(state, tick(20))
    assert row.observations["spread"] == D("0.02") and state.quote is not None
    # Kept from the reference: no order check on quotes, so an older quote replaces a newer one.
    _, row = step(state, tick(30), quote=quote(START + 2 * NS, bid="1.90", ask="2.10"))
    assert row.observations["spread"] == D("0.20")


def test_move_from_open_needs_the_open_time() -> None:
    refs = ReferencePrices(D(2), START - NS, "x", D("2.20"), START + 100 * NS)
    _, before = run(tick(0, "2.42"), refs=refs)
    _, after = run(tick(100, "2.42"), refs=refs)
    assert before.observations["move_from_open"] is None
    assert after.observations["move_from_open"] == D("0.1")


EARLY = (
    "price_1m_unknown",
    "price_5m_unknown",
    "prior_window_uncovered",
    "baseline_zero_dollar_volume",
)


@pytest.mark.parametrize(
    ("ticks", "kw", "reasons"),
    [
        (
            (tick(0),),
            {},
            EARLY,
        ),
        (warm(), {"refs": ReferencePrices(None, START - NS, "x")}, ("reference_unknown",)),
        (
            (tick(0),),
            {"price_eligible": False},
            ("price_unknown", *EARLY),
        ),
        ((tick(0), tick(400)), {}, ("prior_window_zero_trades",)),
        (warm(), {"base": table(symbols=("ZZZU",))}, ("baseline_unknown",)),
        ((tick(0), tick(601)), {}, ("prior_window_zero_trades", "baseline_unknown")),
        (warm(), {"base": table(dv="0")}, ("baseline_zero_dollar_volume",)),
    ],
)
def test_each_unknown_reason(ticks: tuple[TradeTick, ...], kw: Any, reasons: tuple[str]) -> None:
    _, row = run(*ticks, **kw)
    assert row.observations["unknown_reasons"] == reasons


@pytest.mark.parametrize(
    ("name", "exact", "above"),
    [
        ("mover_min_move", D("0.1"), D("0.1000001")),
        ("mover_min_trades_accel", D(6), D("6.000001")),
        ("mover_min_dvol_ratio", D("0.84"), D("0.8400001")),
    ],
)
def test_each_gate_on_its_exact_threshold(name: str, exact: Decimal, above: Decimal) -> None:
    gate = name  # the three threshold gates are named after their rows
    assert (
        gate in GATES and gate in run(*warm(), registry=registry(**{name: exact}))[1].gates_passed
    )
    assert gate not in run(*warm(), registry=registry(**{name: above}))[1].gates_passed


def test_the_one_minute_gate_needs_a_rise() -> None:
    _, flat = run(*(tick(s) for s in range(0, 361, 10)))
    assert flat.move_1m == 0 and GATES[1] not in flat.gates_passed and not qualifies(flat)


def test_a_transition_leaves_the_old_state_as_it_was() -> None:
    before = replace(WARM)
    new, row = step(WARM, tick(361, "2.50"))
    assert WARM == before and new != WARM and WARM.latest == WARM_ROW and new.latest is row
    with pytest.raises(TypeError):
        row.observations["price"] = D(1)  # type: ignore[index]


def test_no_sale_condition_literal_and_no_conditions_parameter() -> None:
    source = inspect.getsource(engine)
    texts = {n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant)}
    assert not texts & {"G", "U", "Z", "6", "M", "9"}
    assert "conditions" not in inspect.signature(advance_mover).parameters
    assert not hasattr(engine, "EXCLUDED_CONDITIONS") and not hasattr(engine, "price_counts")
    assert engine.Segment is baseline.Segment
    imports = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in n.names
    }
    allowed = {"collections.abc", "dataclasses", "decimal", "types", "typing"}
    allowed |= {"nautilus_trader.model"} | {
        f"sextant_movers.ranking.{m}" for m in ("baseline", "registry", "window")
    }
    assert imports <= allowed, imports - allowed


# The parity proof. Every helper below runs on both sides from this source (`inspect.getsource`);
# each side reaches its modules only through `m` and `nt`.
PRELUDE = (
    "import hashlib, json, random\nfrom dataclasses import replace\n"
    "from datetime import date, timedelta\nfrom decimal import Decimal\nfrom typing import Any\n"
)


def shown(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, tuple | list):
        return [shown(x) for x in v]
    return v


def row_shown(r: Any) -> Any:
    fields = (r.score, r.rank, r.move, r.move_1m, r.move_5m, r.dvol_ratio, r.trades_accel)
    obs = [[k, shown(v)] for k, v in r.observations.items()]
    return [r.symbol, r.known_at, *map(shown, fields), list(r.gates_passed), obs, r.params_hash]


def tape_answers(m: dict[str, Any], nt: Any) -> dict[str, Any]:
    """240 seeded multi-symbol tapes, five segments each in turn (the fourth a POST after an
    early close), with every quote kind, flag pair, refusal and quiet return."""
    rng = random.Random(20301129)  # noqa: S311 - a seeded fixture, not a secret
    ns, day = 1_000_000_000, date(2030, 11, 29)
    midnight, hour = 1_922_140_800 * ns, 3600 * ns  # UTC; ET is UTC-5 on the day
    spans = (("PRE", 9, 14.5), ("RTH", 14.5, 21), ("POST", 21, 25), ("POST", 18, 22))
    spans += (("OVERNIGHT", 1, 9),)
    units = dict(zip(m["names"], m["units"], strict=True))
    codes = ("6", "M", "9", "G", "U", "Z", "@", "F", "T", "I")

    def price(n: int) -> Any:
        return nt.Price.from_str(f"{n // 100}.{n % 100:02d}")

    def registry(top: int) -> Any:
        v = {n: rng.randint(1, 5) for n in units}
        v["mover_min_move"] = Decimal(rng.randint(1, 20)).scaleb(-2)
        v["mover_min_trades_accel"] = Decimal(rng.randint(5, 40)).scaleb(-1)
        v["mover_min_dvol_ratio"] = Decimal(rng.randint(1, 30)).scaleb(-1)
        v["move_1m_window_s"], v["move_5m_window_s"] = rng.choice((30, 60)), rng.choice((120, 300))
        v["trades_accel_window_s"], v["top_n"] = rng.choice((20, 60)), top
        v["trades_accel_baseline_min"] = rng.choice((1, 2, 5))
        rows = (m["RegistryRow"](n, units[n], v[n], "fixture", "UNCALIBRATED", day) for n in v)
        return m["Registry"](tuple(rows))

    out: list[Any] = []
    for i in range(240):
        name, a, b = spans[i % 5]
        seg = m["Segment"](name, day, midnight + int(a * hour), midnight + int(b * hour), "fixture")
        reg, alt = registry(2), registry(3)
        syms = rng.sample(("ZZZA", "ZZZB", "ZZZC", "ZZZD"), rng.randint(1, 3))
        ids = {s: nt.InstrumentId.from_str(s + ".SIM") for s in (*syms, "ZZZQ")}
        feed, samples = rng.choice(("sip", "boats")), []
        for s in syms:
            for k in rng.sample(range(1, 11), rng.randint(0, 3)):
                when = day + timedelta(days=-k if rng.random() > 0.03 else rng.choice((0, 1)))
                offset, dv, tc = 0, Decimal(0), Decimal(0)
                for _ in range(rng.randint(1, 5)):
                    sample = (s, rng.choice((name, name, "RTH")), when.isoformat(), offset, dv, tc)
                    samples.append(m["BaselineSample"](*sample))
                    offset += rng.choice((60, 600, 3600))
                    dv += Decimal(rng.randint(0, 10 ** rng.randint(1, 7))).scaleb(-2)
                    tc += rng.randint(0, 40)
        known = seg.start_ns - rng.randint(1, 9) * ns
        if rng.random() < 0.05:
            known = seg.start_ns + rng.randint(1, 600) * ns
        try:
            table = m["BaselineTable"](tuple(samples), "fixture curves", known, feed)
        except ValueError as exc:  # a repeated sample offset, from two draws of the same day
            out.append([type(exc).__name__, str(exc)])
            continue
        refs: dict[str, Any] = {}
        for s in syms:
            close = None if rng.random() < 0.1 else Decimal(rng.randint(50, 6000)).scaleb(-2)
            at = (
                seg.start_ns - ns if rng.random() > 0.05 else seg.start_ns + rng.randint(1, 99) * ns
            )
            opened = rng.random() < 0.5
            open_price = Decimal(rng.randint(50, 6000)).scaleb(-2) if opened else None
            open_ts = seg.start_ns + rng.randint(0, 3600) * ns if opened else None
            refs[s] = m["ReferencePrices"](close, at, "fixture close", open_price, open_ts)
        bad = rng.choice(((Decimal(2), 0, ""), (2.0, 0, "x"), (Decimal(0), 0, "x")))
        bad = rng.choice((bad, (Decimal("NaN"), 0, "x"), (Decimal(2), 0, "x", Decimal(2), None)))
        try:
            m["ReferencePrices"](*bad)
        except ValueError as exc:
            out.append([type(exc).__name__, str(exc)])
        states = {s: m["MoverState"]() for s in syms}
        t = seg.start_ns if rng.random() < 0.5 else seg.start_ns + rng.randint(1, 120) * ns
        for k in range(rng.randint(10, 60)):
            t += rng.choice((0, 1, ns, 5 * ns, 10 * ns, 30 * ns, 60 * ns, 61 * ns))
            r, when, sym = rng.random(), t, rng.choice(syms)
            if r < 0.03:
                when = seg.start_ns - rng.randint(1, 5)
            elif r < 0.06:
                when = seg.end_ns + rng.randint(0, 5)
            elif r < 0.09:
                when = t - rng.randint(1, 30) * ns
            elif r < 0.12:
                when = seg.start_ns
            size = nt.Quantity.from_int(rng.randint(1, 500))
            init = when + rng.choice((0, 0, 1, 1000))
            buy = nt.AggressorSide.BUY
            tick = nt.TradeTick(
                ids[sym], price(rng.randint(50, 6000)), size, buy, nt.TradeId(str(k)), when, init
            )
            kind, quote = (
                rng.choice(("none", "none", "fresh", "stale", "other", "future", "init")),
                None,
            )
            if kind != "none":
                bid = rng.randint(50, 6000)
                q_at = {
                    "fresh": max(seg.start_ns, when - rng.randint(0, 5) * ns),
                    "future": when + 1,
                }
                q_at["stale"] = seg.start_ns - rng.randint(1, 100) * ns
                q_ts = q_at.get(kind, when)
                q_init = q_ts + 10**12 if kind == "init" else q_ts
                q_id = ids["ZZZQ" if kind == "other" else sym]
                bp, ap = price(bid), price(bid + rng.randint(0, 10))
                one = nt.Quantity.from_int(1)
                quote = nt.QuoteTick(q_id, bp, ap, one, one, q_ts, q_init)
            key = sym
            if rng.random() < 0.02:
                key = rng.choice(syms)
            kw = {
                "segment": seg if rng.random() > 0.02 else replace(seg, start_ns=seg.start_ns + ns),
                "feed": feed if rng.random() > 0.02 else rng.choice(("sip", "boats", "iex")),
                "registry": reg if rng.random() > 0.02 else alt,
                "quote": quote,
                "price_eligible": rng.random() < 0.85,
                "volume_eligible": rng.random() < 0.85,
                "known_at": None if rng.random() > 0.1 else when + rng.randint(0, 3) * ns,
            }
            conditions = tuple(rng.sample(codes, rng.choice((0, 0, 0, 1, 2))))
            state = states[key]
            try:
                new, row = m["advance"](state, tick, refs[sym], table, conditions, **kw)
            except Exception as exc:
                out.append([type(exc).__name__, str(exc)])
                continue
            states[key] = new
            summary = [new.known_at, new.segment_covered, str(new.segment_dollar_volume)]
            summary.append(new.quote is not None)
            out.append(["quiet", new == state] if row is None else ["row", row_shown(row), summary])
            ranked = m["rank_movers"](s.latest for s in states.values() if s.latest is not None)
            out.append([[x.symbol, x.rank, str(x.score)] for x in ranked])
        tied = [replace(s.latest, score=Decimal(1)) for s in states.values() if s.latest]
        out.append([[x.symbol, x.rank] for x in m["rank_movers"](tied)])
    kinds: dict[str, int] = {}
    for entry in out:
        label = entry[0] if entry and isinstance(entry[0], str) else "rank"
        kinds[label] = kinds.get(label, 0) + 1
    messages = sorted(
        {e[1] for e in out if e and e[0] not in ("row", "quiet") and isinstance(e[0], str)}
    )
    text = json.dumps(out, separators=(",", ":"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    return {"count": len(out), "kinds": kinds, "messages": messages, "digest": digest}


def ours(state: Any, tick: Any, refs: Any, table: Any, conditions: Any, **kw: Any) -> Any:
    # "6", "M" and "9" are the reference's closing-print codes, its test input, never a value of
    # this package: a trade the reference skips for them is, here, a call with both flags false.
    if {"6", "M", "9"} & set(conditions):
        kw.update(price_eligible=False, volume_eligible=False)
    return advance_mover(state, tick, refs, table, **kw)


OURS = {
    "Segment": Segment,
    "BaselineSample": BaselineSample,
    "BaselineTable": BaselineTable,
    "Registry": Registry,
    "RegistryRow": RegistryRow,
    "names": list(ROWS),
    "units": [u for _, u in ROWS.values()],
    "ReferencePrices": ReferencePrices,
    "MoverState": MoverState,
    "rank_movers": rank_movers,
    "advance": ours,
}
PARITY_DIGEST = "pending"


def test_the_seeded_tapes_give_the_pinned_digest() -> None:
    """The digest is the reference's answer over the same tapes, pinned so CI checks it too."""
    result = tape_answers(OURS, nt)
    assert result["digest"] == PARITY_DIGEST, result
    expected = {message for _, message in REFUSALS.values()} | {"out-of-order event"}
    expected |= {"source required", "positive finite Decimal", "requires its event timestamp"}
    assert all(any(e in m for m in result["messages"]) for e in expected), result["messages"]
    assert result["kinds"]["row"] > 1000 and result["kinds"]["quiet"] > 100


CHILD = """
import os, sys
from importlib.metadata import version
import nautilus_trader.model as nt
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.ranking import baseline as bl, engine as ref, registry as rg
assert os.path.samefile(ref.__file__, os.path.join(SRC, "sextant", "ranking", "engine.py"))

def advance(state, tick, refs, table, conditions, **kw):
    return ref.advance_mover(state, tick, refs, table, conditions=conditions, **kw)

m = {"Segment": ref.Segment, "BaselineSample": bl.BaselineSample,
     "BaselineTable": bl.BaselineTable, "Registry": rg.Registry, "RegistryRow": rg.RegistryRow,
     "names": list(rg.ROWS), "units": [u for _, u in rg.ROWS.values()],
     "ReferencePrices": ref.ReferencePrices, "MoverState": ref.MoverState,
     "rank_movers": ref.rank_movers, "advance": advance}
print("PARITY " + json.dumps({"engine": version("nautilus_trader"), **tape_answers(m, nt)}))
"""


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter() -> None:
    from importlib.metadata import version

    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip("set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof")
    blob = (Path(src) / "sextant" / "ranking" / "engine.py").read_bytes()
    git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
    assert git_blob.hexdigest() == REFERENCE_BLOB
    script = (
        PRELUDE + "".join(inspect.getsource(f) for f in (shown, row_shown, tape_answers)) + CHILD
    )
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
    mine = tape_answers(OURS, nt)
    print(f"\nengine: here {version('nautilus_trader')}, reference {theirs.pop('engine')}")
    print(f"answers {mine['count']}, kinds {mine['kinds']}; equal: {mine == theirs}")
    assert mine == theirs and theirs["digest"] == PARITY_DIGEST
