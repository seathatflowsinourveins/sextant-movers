"""Read the display cadence policy; no admission, state, lease or scheduling.

Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Lib/json/__init__.py:185,304; Lib/hashlib.py:14; Doc/library/types.rst:357;
Doc/library/datetime.rst:1352. Package data uses Lib/importlib/resources/_common.py:52,166.
Build on gerrymanoim/exchange_calendars@4.13.2
dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a:exchange_calendars/exchange_calendar.py:1263,
through inputs._session; RegistryRow and sessions.ET are reused from this package.
Acceptance: the reference at 06562bec0898a7203f4eaf69086ced83d2477e6b,
src/sextant/tradingview/cadence.py:27-47,88-137,198-217 and prestage.py:164-173.
CadencePolicy is deliberately distinct from the reference's admission class Cadence;
later admission will hold this policy. The wrapper requires registry and usage, with no state.
"""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from sextant_movers.ranking.registry import RegistryRow
from sextant_movers.sessions import ET
from sextant_movers.tradingview.inputs import _ns, _session

ROWS = {
    "enabled": "flag",
    "changed_only": "flag",
    "max_concurrent_runs": "runs",
    "max_retries": "retries",
    "overnight_enabled": "flag",
    **dict.fromkeys(("onset_floor_s", "regular_floor_s", "usage_floor_s"), "seconds"),
    "weekly_usage_backoff_pct": "percent",
    **dict.fromkeys(
        (
            "display_start_minute_et",
            "display_end_minute_et",
            "am_start_minute_et",
            "am_end_minute_et",
            "pm_start_minute_et",
            "pm_end_minute_et",
        ),
        "minute ET",
    ),
}


class CadencePolicy:
    """A sourced registry and its floor; every shipped value remains UNCALIBRATED."""

    def __init__(self, registry: Path) -> None:
        raw = json.loads(registry.read_text())
        rows = raw["rows"]
        if raw["schema"] != "j5-cadence.registry/1" or len(rows) != len(ROWS):
            raise ValueError("missing or duplicate cadence registry row")
        values = {}
        for item in rows:
            row = RegistryRow(**{**item, "last_change": date.fromisoformat(item["last_change"])})
            if (
                row.name in values
                or row.name not in ROWS
                or row.unit != ROWS[row.name]
                or type(row.value) is not int
                or row.value < 0
                or not row.source.strip()
                or row.calibration_state != "UNCALIBRATED"
            ):
                raise ValueError("sourced UNCALIBRATED integer cadence rows required")
            values[row.name] = row.value
        if (
            values["enabled"] not in {0, 1}
            or any(
                values[k] != v
                for k, v in {
                    "changed_only": 1,
                    "max_concurrent_runs": 1,
                    "max_retries": 0,
                    "overnight_enabled": 0,
                }.items()
            )
            or not 0
            < values["onset_floor_s"]
            <= values["regular_floor_s"]
            <= values["usage_floor_s"]
            or not 0 <= values["weekly_usage_backoff_pct"] <= 100
            or not 0
            <= values["display_start_minute_et"]
            <= values["am_start_minute_et"]
            < values["am_end_minute_et"]
            <= values["pm_start_minute_et"]
            < values["pm_end_minute_et"]
            <= values["display_end_minute_et"]
            <= 1440
        ):
            raise ValueError("invalid cadence policy bounds")
        self.params = MappingProxyType(values)
        self.params_hash = hashlib.sha256(
            json.dumps(sorted(rows, key=lambda r: r["name"]), sort_keys=True).encode()
        ).hexdigest()

    def floor(self, now: datetime, weekly_usage_pct: Decimal) -> int | None:
        _ns(now)
        local, p = now.astimezone(ET), self.params
        minute = local.hour * 60 + local.minute
        if not p["display_start_minute_et"] <= minute < p["display_end_minute_et"]:
            return None
        try:
            _session(now)
        except ValueError:
            return None
        onset = (
            p["am_start_minute_et"] <= minute < p["am_end_minute_et"]
            or p["pm_start_minute_et"] <= minute < p["pm_end_minute_et"]
        )
        normal = p["onset_floor_s"] if onset else p["regular_floor_s"]
        return (
            max(normal, p["usage_floor_s"])
            if weekly_usage_pct > p["weekly_usage_backoff_pct"]
            else normal
        )


def cadence_floor(now: datetime, *, registry: Path, weekly_usage_pct: Decimal) -> int | None:
    """The display floor from an explicit registry and usage reading; never a timer."""
    cadence = CadencePolicy(Path(registry))
    return cadence.floor(now, weekly_usage_pct)
