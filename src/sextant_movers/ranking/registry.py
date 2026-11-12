"""The parameter registry: every display tunable is one explicit row, and outputs carry its hash.

A row has a name, unit, value, source, calibration state and last change (MANIFEST-TEMPLATE.md,
"Registry rows for tunable values"; AGENTS.md Rule 2). `Registry` builds only from all twelve
display rows, each a sourced UNCALIBRATED row with a set, positive, finite value of its kind:
an UNAVAILABLE or missing row means there is no registry, and a calibrated row is refused until
this repository has a calibration record format. Values live in package data; `load_registry`
reads only the caller-named file.

Source: the reference's `src/sextant/ranking/registry.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b` (`RegistryRow`, `ROWS`, `Registry`), whose
`params_hash` bytes are kept: the rows' `asdict` sorted by name, `json.dumps(default=str,
sort_keys=True, separators=(",", ":"))`, SHA-256 (python/cpython@v3.13.18
`Lib/json/__init__.py:185`, `Lib/dataclasses.py:1387`). The field name `calibration_state` is
covered by that hash, so it stays.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RegistryRow:
    name: str
    unit: str
    value: Decimal | int
    source: str
    calibration_state: str
    last_change: date


# The display rows: name -> (value kind, unit).
ROWS: dict[str, tuple[type[Decimal] | type[int], str]] = {
    "mover_min_move": (Decimal, "fraction"),
    "mover_min_trades_accel": (Decimal, "ratio"),
    "mover_min_dvol_ratio": (Decimal, "ratio"),
    "move_1m_window_s": (int, "seconds"),
    "move_5m_window_s": (int, "seconds"),
    "trades_accel_window_s": (int, "seconds"),
    "trades_accel_baseline_min": (int, "minutes"),
    "publish_min_interval_s": (int, "seconds"),
    "generation_max_age_s": (int, "seconds"),
    "heartbeat_max_age_s": (int, "seconds"),
    "input_max_age_s": (int, "seconds"),
    "top_n": (int, "symbols"),
}


def _check_names(rows: tuple[RegistryRow, ...]) -> None:
    names = [row.name for row in rows]
    cases = {
        "duplicate": sorted({name for name in names if names.count(name) > 1}),
        "missing": sorted(ROWS.keys() - set(names)),
        "unknown": sorted(set(names) - ROWS.keys()),
    }
    found = [f"{case} {listed}" for case, listed in cases.items() if listed]
    if found:
        raise ValueError(f"registry rows refused: {'; '.join(found)}")


def _check_state(row: RegistryRow) -> None:
    state = row.calibration_state
    if state == "UNAVAILABLE":
        raise ValueError(f"{row.name} is UNAVAILABLE: no sourced value, so no registry")
    if isinstance(state, str) and state.upper() == "CALIBRATED":
        raise ValueError(f"{row.name} is calibrated, but no calibration record format exists yet")
    if state != "UNCALIBRATED":
        raise ValueError(f"{row.name}: unknown calibration state {state!r}")


def _check_row(row: RegistryRow) -> None:
    _check_state(row)
    kind, unit = ROWS[row.name]
    if type(row.value) is not kind:
        raise TypeError(f"{row.name} must be {kind.__name__}, never float or bool")
    if not Decimal(row.value).is_finite():
        raise ValueError(f"{row.name}: non-finite value")
    if row.value <= 0:
        raise ValueError(f"{row.name}: non-positive value")
    if row.unit != unit:
        raise ValueError(f"{row.name}: unit {row.unit!r} is not {unit!r}")
    source: object = row.source
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"{row.name}: registry source required (non-empty text)")
    if type(row.last_change) is not date:
        raise ValueError(f"{row.name}: last change must be a date, not {type(row.last_change)}")


@dataclass(frozen=True, slots=True)
class Registry:
    rows: tuple[RegistryRow, ...]
    params_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _check_names(self.rows)
        for row in self.rows:
            _check_row(row)
        ordered = tuple(sorted(self.rows, key=lambda r: r.name))
        object.__setattr__(self, "rows", ordered)
        payload = json.dumps(
            [asdict(row) for row in ordered], default=str, sort_keys=True, separators=(",", ":")
        )
        object.__setattr__(self, "params_hash", hashlib.sha256(payload.encode()).hexdigest())

    def __getattr__(self, name: str) -> Decimal | int:
        for row in self.rows:
            if row.name == name:
                return row.value
        raise AttributeError(name)

    def seconds(self, name: str) -> int:
        return int(getattr(self, name)) * 1_000_000_000


def load_registry(path: Path) -> Registry:
    """Read the operational K2 registry file into the validated registry."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") != "k2-ranking.registry/1":
        raise ValueError("not a K2 ranking registry")
    rows = []
    for item in raw.get("rows") or ():
        name, unit = item.get("name"), item.get("unit")
        kind = ROWS.get(name)
        if kind is None or kind[1] != unit:
            raise ValueError(f"unknown registry row: {name}")
        raw_value = item["value"]
        if kind[0] is int:
            if type(raw_value) is not int:
                raise ValueError(f"invalid registry value: {name}")
            value = raw_value
        else:
            if type(raw_value) not in (int, str):
                raise ValueError(f"invalid registry value: {name}")
            value = Decimal(raw_value)
        rows.append(
            RegistryRow(
                name,
                unit,
                value,
                item["source"],
                item["calibration_state"],
                date.fromisoformat(item["last_change"]),
            )
        )
    return Registry(tuple(rows))
