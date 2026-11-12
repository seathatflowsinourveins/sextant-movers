"""Read the mapped display universe and two caller-named parameter files.

Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Lib/json/__init__.py:304, Doc/library/decimal.rst:141, Doc/library/datetime.rst:555.
Acceptance: the reference 06562bec0898a7203f4eaf69086ced83d2477e6b,
src/sextant/tradingview/prestage.py:89-90,140-162,311-371.
Departures: ValueError refusals, required paths with no default or path constant,
and the budget's unavailable text names the caller's path. load_maps composes this
package's build_universe, load_symbol_map and creation_time; its docstring is ours.
The literal ceiling 200 validates a file row, not an operating default: Alpaca,
About Market Data API, https://docs.alpaca.markets/docs/about-market-data-api,
read by the lead 2026-11-12, Historical API calls on Basic: "200 / min".
Four UNCALIBRATED starting values ship as data, never measured success.
No request, environment read, clock read or write occurs here; display only.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sextant_movers.ranking.listings import creation_time
from sextant_movers.ranking.universe import build_universe
from sextant_movers.tradingview.bridge import DisplayBounds
from sextant_movers.tradingview.host import load_symbol_map

BUDGET_NAME = "prestage_request_budget"
BOUNDS = {"gap_bound": "fraction", "rvol_bound": "ratio", "max_age_s": "seconds"}


def load_maps(
    nasdaq_listed: Path, other_listed: Path, symbol_map: Path
) -> tuple[tuple[str, ...], dict[str, str], str]:
    """Mapped names in the directory universe's order, exact routes and file provenance.

    Missing and null routes are omitted and counted; each trailer time is displayed.
    """
    texts = [Path(path).read_text(encoding="utf-8") for path in (nasdaq_listed, other_listed)]
    directory = build_universe(*texts)
    raw = load_symbol_map(Path(symbol_map))
    universe = tuple(name for name in directory if raw.get(name))
    if not universe:
        raise ValueError(f"The symbol map maps none of {len(directory)} directory symbols")
    created = " / ".join(creation_time(text) for text in texts)
    provenance = (
        f"NASDAQ Trader nasdaqlisted.txt + otherlisted.txt, File Creation Time {created}; "
        f"{len(universe)} of {len(directory)} directory symbols mapped, "
        f"{len(directory) - len(universe)} unmapped omitted"
    )
    return universe, {name: str(raw[name]) for name in universe}, provenance


def load_request_budget(path: Path) -> int:
    """Refuse until a measured request budget and its authority are on record (Rule 2)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("rows") or ()
    matches = tuple(item for item in rows if item.get("name") == BUDGET_NAME)
    if not matches:
        raise ValueError(f"the prestage request budget row is missing from {path}")
    item = matches[0]
    value = item.get("value")
    source = item.get("source")
    if (
        type(value) is not int
        or not 1 <= value <= 200
        or item.get("scope") != "REVIEWED"
        or item.get("calibration") not in {"UNCALIBRATED", "CALIBRATED"}
        or not isinstance(source, str)
        or not source.strip()
    ):
        raise ValueError(
            "the prestage request budget is unavailable: no measured value and authority "
            f"are on record; record both in {path} (Rule 2)"
        )
    return value


def load_display_bounds(path: Path) -> DisplayBounds:
    """The three display bounds from their sourced registry rows; never a code literal."""
    refusal = ValueError(
        f"the display bounds in {path} need exactly one sourced row each for "
        f"{', '.join(BOUNDS)} (Rule 2)"
    )
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = raw.get("rows") if raw.get("schema") == "tv-display-bounds.registry/1" else None
    if (
        not isinstance(rows, list)
        or not all(isinstance(row, dict) and isinstance(row.get("name"), str) for row in rows)
        or sorted(row["name"] for row in rows) != sorted(BOUNDS)
    ):
        raise refusal
    values: dict[str, Any] = {}
    for row in rows:
        name, value, source = row["name"], row.get("value"), row.get("source")
        try:
            date.fromisoformat(row.get("last_change"))
        except (TypeError, ValueError):
            raise refusal from None
        if (
            row.get("unit") != BOUNDS[name]
            or row.get("calibration_state") not in {"UNCALIBRATED", "CALIBRATED"}
            or not isinstance(source, str)
            or not source.strip()
            or type(value) is not (int if name == "max_age_s" else str)
        ):
            raise refusal
        values[name] = value
    try:
        return DisplayBounds(
            Decimal(values["gap_bound"]), Decimal(values["rvol_bound"]), values["max_age_s"]
        )
    except (ArithmeticError, ValueError):
        raise refusal from None
