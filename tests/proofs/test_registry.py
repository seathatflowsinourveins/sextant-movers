"""The parameter registry: every display row explicit, sourced and UNCALIBRATED, and its
params_hash byte-identical to the reference's.

Sources: MANIFEST-TEMPLATE.md, "Registry rows for tunable values" (the three states); ADR-0002
decision 3 (`RegistryRow` and `Registry` belong to the contract); the reference's
`src/sextant/ranking/registry.py` at `06562bec0898a7203f4eaf69086ced83d2477e6b` and its proofs
`tests/proofs/test_k2_registry.py` and `tests/proofs/test_ranking.py:394-402`, rebuilt here with
made-up rows. Every value, source and date below is made up for the test; none is a parameter.

The parity proof is `local`: it needs the reference's exported source tree, named by
MOVERS_REFERENCE_SRC, and builds the reference's Registry only in a child process.
"""

import hashlib
import json
import os
import random
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest

from sextant_movers.ranking.registry import ROWS, Registry, RegistryRow

DAY = date(2001, 2, 3)
SOURCE = "made-up test source, not a parameter"
# The twelve display rows, their kind and unit, written out without the implementation.
EXPECTED_ROWS: dict[str, tuple[type, str]] = {
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


def made_up(index: int, kind: type) -> Decimal | int:
    return Decimal(f"{index + 4}.25") if kind is Decimal else 17 * (index + 1)


def rows() -> tuple[RegistryRow, ...]:
    return tuple(
        RegistryRow(name, unit, made_up(i, kind), SOURCE, "UNCALIBRATED", DAY)
        for i, (name, (kind, unit)) in enumerate(EXPECTED_ROWS.items())
    )


def swap(name: str, **changes: object) -> tuple[RegistryRow, ...]:
    return tuple(replace(row, **changes) if row.name == name else row for row in rows())


def test_the_rows_are_the_twelve_display_rows() -> None:
    assert dict(ROWS) == EXPECTED_ROWS


def test_a_complete_registry_builds_sorted_with_its_values() -> None:
    registry = Registry(tuple(reversed(rows())))
    assert [row.name for row in registry.rows] == sorted(EXPECTED_ROWS)
    assert registry.top_n == 17 * 12
    assert registry.mover_min_move == Decimal("4.25")
    assert registry.seconds("move_1m_window_s") == 17 * 4 * 1_000_000_000
    with pytest.raises(AttributeError, match="no_such_row"):
        _ = registry.no_such_row


def test_missing_duplicate_and_unknown_rows_are_refused_by_case() -> None:
    full = rows()
    with pytest.raises(ValueError, match=r"missing \['top_n'\]"):
        Registry(full[:-1])
    with pytest.raises(ValueError, match=r"duplicate \['mover_min_move'\]"):
        Registry((*full, full[0]))
    extra = replace(full[0], name="entry_heartbeat_max_age_s")
    with pytest.raises(ValueError, match=r"unknown \['entry_heartbeat_max_age_s'\]"):
        Registry((*full, extra))
    with pytest.raises(ValueError, match=r"missing \['top_n'\]; unknown \['top_k'\]"):
        Registry((*full[:-1], replace(full[-1], name="top_k")))
    with pytest.raises(ValueError, match="missing"):
        Registry(())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("mover_min_move", 0.03),
        ("top_n", 7.0),
        ("top_n", True),
        ("top_n", Decimal(7)),
        ("mover_min_move", 4),
        ("mover_min_move", None),
        ("top_n", "7"),
    ],
)
def test_a_value_of_the_wrong_kind_is_refused(name: str, value: object) -> None:
    with pytest.raises(TypeError, match=f"{name} must be .*, never float or bool"):
        Registry(swap(name, value=value))


@pytest.mark.parametrize("value", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_non_finite_value_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="mover_min_move: non-finite value"):
        Registry(swap("mover_min_move", value=Decimal(value)))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("top_n", 0),
        ("top_n", -3),
        ("mover_min_move", Decimal("0")),
        ("mover_min_move", Decimal("-0.5")),
    ],
)
def test_a_non_positive_value_is_refused(name: str, value: object) -> None:
    with pytest.raises(ValueError, match=f"{name}: non-positive value"):
        Registry(swap(name, value=value))


def test_a_wrong_unit_is_refused() -> None:
    with pytest.raises(ValueError, match="top_n: unit 'seconds' is not 'symbols'"):
        Registry(swap("top_n", unit="seconds"))


@pytest.mark.parametrize("source", ["", "   ", None])
def test_an_empty_source_is_refused(source: object) -> None:
    with pytest.raises(ValueError, match="top_n: registry source required"):
        Registry(swap("top_n", source=source))


@pytest.mark.parametrize("last_change", [datetime(2001, 2, 3), "2001-02-03", None])
def test_a_last_change_that_is_not_a_date_is_refused(last_change: object) -> None:
    with pytest.raises(ValueError, match="top_n: last change must be a date"):
        Registry(swap("top_n", last_change=last_change))


def test_an_unavailable_row_means_no_registry() -> None:
    with pytest.raises(ValueError, match="top_n is UNAVAILABLE"):
        Registry(swap("top_n", value=None, calibration_state="UNAVAILABLE"))


@pytest.mark.parametrize("state", ["calibrated", "CALIBRATED"])
def test_a_calibrated_row_is_refused_until_a_record_format_exists(state: str) -> None:
    with pytest.raises(ValueError, match="top_n is calibrated, but no calibration record"):
        Registry(swap("top_n", calibration_state=state))


@pytest.mark.parametrize("state", ["uncalibrated", "", "MEASURED"])
def test_an_unknown_calibration_state_is_refused(state: str) -> None:
    with pytest.raises(ValueError, match="top_n: unknown calibration state"):
        Registry(swap("top_n", calibration_state=state))


def test_row_order_does_not_change_params_hash_and_any_field_change_does() -> None:
    base = Registry(rows())
    shuffled = list(rows())
    random.Random(7).shuffle(shuffled)  # noqa: S311 - a test permutation, not a secret
    assert Registry(tuple(shuffled)).params_hash == base.params_hash
    seen = {base.params_hash}
    for row in rows():
        bumped = row.value + 1 if type(row.value) is int else row.value * 2
        for change in (
            {"value": bumped},
            {"source": row.source + "."},
            {"last_change": date(2001, 2, 4)},
        ):
            seen.add(Registry(swap(row.name, **change)).params_hash)
    trailing = Registry(swap("mover_min_move", value=Decimal("4.250"))).params_hash
    seen.add(trailing)
    assert len(seen) == 1 + 3 * len(EXPECTED_ROWS) + 1


def documented_hash(dicts: list[dict[str, object]]) -> str:
    """The documented formula: the row dicts sorted by name, compact sorted-key JSON with every
    non-JSON value (Decimal, date) as its str, then SHA-256 of the UTF-8 bytes."""
    ordered = sorted(dicts, key=lambda d: str(d["name"]))
    text = json.dumps(ordered, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def test_params_hash_is_the_documented_formula() -> None:
    dicts: list[dict[str, object]] = [
        {
            "name": row.name,
            "unit": row.unit,
            "value": str(row.value) if type(row.value) is Decimal else row.value,
            "source": row.source,
            "calibration_state": row.calibration_state,
            "last_change": DAY.isoformat(),
        }
        for row in rows()
    ]
    assert Registry(rows()).params_hash == documented_hash(dicts)


# The parity proof. Each set is the twelve rows with seeded values (int and Decimal across their
# range, exponents, trailing zeros), sources (non-ASCII, quotes, escapes) and dates, in a seeded
# order. The child receives them as JSON and rebuilds them with the reference's own types.
SETS = 256
CHILD = """
import hashlib, json, os, sys
from datetime import date
from decimal import Decimal
sys.path.insert(0, os.environ["MOVERS_REFERENCE_SRC"])
from sextant.ranking.registry import Registry, RegistryRow
digest = hashlib.sha256()
for rows in json.load(sys.stdin):
    built = Registry(tuple(
        RegistryRow(r["name"], r["unit"], Decimal(r["value"]) if r["decimal"] else r["value"],
                    r["source"], "UNCALIBRATED", date.fromisoformat(r["last_change"]))
        for r in rows))
    digest.update(built.params_hash.encode() + b"\\n")
print(digest.hexdigest())
"""
TEXTS = ("plain", "café ≤ 5", "日本語の出典", 'quote " and \\ slash', "tab\tline\nend", "😀 emoji")


def seeded_sets() -> list[list[dict[str, object]]]:
    rng = random.Random(20011103)  # noqa: S311 - seeded test inputs, not a secret
    sets = []
    for _ in range(SETS):
        rows = []
        for name, (kind, unit) in EXPECTED_ROWS.items():
            if kind is Decimal:
                digits = str(rng.randint(1, 10 ** rng.randint(1, 28)))
                digits += "0" * rng.choice((0, 0, 1, 3))
                value: object = f"{digits}E{rng.randint(-30, 30)}"
                if rng.random() < 0.3:
                    value = f"{digits[:-1] or '1'}.{digits[-1]}0"
            else:
                value = rng.randint(1, 10 ** rng.randint(1, 30))
            source = " ".join(rng.sample(TEXTS, rng.randint(1, len(TEXTS))))
            day = date(1990, 1, 1).toordinal() + rng.randint(0, 40_000)
            rows.append(
                {
                    "name": name,
                    "unit": unit,
                    "value": value,
                    "decimal": kind is Decimal,
                    "source": source,
                    "last_change": date.fromordinal(day).isoformat(),
                }
            )
        rng.shuffle(rows)
        sets.append(rows)
    return sets


def new_digest(sets: list[list[dict[str, object]]]) -> str:
    digest = hashlib.sha256()
    for rows in sets:
        built = Registry(
            tuple(
                RegistryRow(
                    str(r["name"]),
                    str(r["unit"]),
                    Decimal(str(r["value"])) if r["decimal"] else int(str(r["value"])),
                    str(r["source"]),
                    "UNCALIBRATED",
                    date.fromisoformat(str(r["last_change"])),
                )
                for r in rows
            )
        )
        digest.update(built.params_hash.encode() + b"\n")
    return digest.hexdigest()


@pytest.mark.local
def test_params_hash_equals_the_reference_over_seeded_row_sets() -> None:
    reference = os.environ.get("MOVERS_REFERENCE_SRC")
    if not reference:
        pytest.skip("MOVERS_REFERENCE_SRC is not set: it names the reference's exported src tree")
    sets = seeded_sets()
    assert len(sets) >= 200
    child = subprocess.run(
        [sys.executable, "-I", "-c", CHILD],
        input=json.dumps(sets),
        env={"MOVERS_REFERENCE_SRC": reference, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert child.stdout.strip() == new_digest(sets)
