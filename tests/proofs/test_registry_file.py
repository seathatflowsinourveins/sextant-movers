"""Package data and loader parity with the reference at 06562bec0898a7203f4eaf69086ced83d2477e6b.

Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Lib/importlib/resources/_common.py:52,166; Lib/json/__init__.py:304;
Doc/library/decimal.rst:141; Doc/library/datetime.rst:555. The reference's
src/sextant/tradingview/prestage.py:107-137 supplies the loader composition.
Fault files and seeded inputs are made up, never operating parameter authority.
"""

import ast
import hashlib
import inspect
import json
import random
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import pytest
import reference_child

from sextant_movers.ranking import registry as module

COMMIT = "06562bec0898a7203f4eaf69086ced83d2477e6b"
EXPECTED = (
    ("mover_min_move", "fraction", "0.03"),
    ("mover_min_trades_accel", "ratio", "1.5"),
    ("mover_min_dvol_ratio", "ratio", "3"),
    ("move_1m_window_s", "seconds", 60),
    ("move_5m_window_s", "seconds", 300),
    ("trades_accel_window_s", "seconds", 60),
    ("trades_accel_baseline_min", "minutes", 5),
    ("publish_min_interval_s", "seconds", 1),
    ("generation_max_age_s", "seconds", 1200),
    ("heartbeat_max_age_s", "seconds", 1200),
    ("input_max_age_s", "seconds", 600),
    ("top_n", "symbols", 100),
)
SHIPPED_HASH = "691aa5d080b0f519b66db23e3b4a3327304153c48d99dfe5254f8d74aee95934"
REFUSALS = ("not a K2 ranking registry", "unknown registry row:", "invalid registry value:")
# Each fault changes one thing; Registry's existing refusals remain distinct from the loader's.
FAULTS = (
    ("schema", ValueError, "not a K2 ranking registry"),
    ("missing_schema", ValueError, "not a K2 ranking registry"),
    ("name", ValueError, "unknown registry row: made_up"),
    ("missing_name", ValueError, "unknown registry row: None"),
    ("unit", ValueError, "unknown registry row: mover_min_move"),
    ("missing_unit", ValueError, "unknown registry row: mover_min_move"),
    ("int_string", ValueError, "invalid registry value: top_n"),
    ("int_bool", ValueError, "invalid registry value: top_n"),
    ("int_float", ValueError, "invalid registry value: top_n"),
    ("decimal_float", ValueError, "invalid registry value: mover_min_move"),
    ("decimal_bool", ValueError, "invalid registry value: mover_min_move"),
    ("decimal_list", ValueError, "invalid registry value: mover_min_move"),
    ("missing_value", KeyError, "value"),
    ("missing_source", KeyError, "source"),
    ("missing_calibration_state", KeyError, "calibration_state"),
    ("missing_last_change", KeyError, "last_change"),
    ("date", ValueError, "Invalid isoformat string"),
    ("date_type", TypeError, "argument must be str"),
    ("decimal_text", InvalidOperation, "ConversionSyntax"),
    ("missing_rows", ValueError, "registry rows refused: missing"),
    ("null_rows", ValueError, "registry rows refused: missing"),
    ("duplicate", ValueError, "registry rows refused: duplicate"),
    ("missing_row", ValueError, "registry rows refused: missing"),
    ("row_object", AttributeError, "has no attribute 'get'"),
    ("root_object", AttributeError, "has no attribute 'get'"),
    ("damaged", json.JSONDecodeError, "Expecting"),
)


def shipped_text() -> str:
    return (files("sextant_movers") / "params" / "k2-ranking.params.json").read_text("utf-8")


def test_shipped_file_values_sources_date_and_hash() -> None:
    raw = json.loads(shipped_text())
    assert set(raw) == {"schema", "rows"}
    assert raw["schema"] == "k2-ranking.registry/1"
    assert len(raw["rows"]) == 12
    assert [(r["name"], r["unit"], r["value"]) for r in raw["rows"]] == list(EXPECTED)
    assert [type(r["value"]) for r in raw["rows"]] == [type(r[2]) for r in EXPECTED]
    assert {r["name"] for r in raw["rows"]} == module.ROWS.keys()
    for row in raw["rows"]:
        assert set(row) == {"name", "unit", "value", "source", "calibration_state", "last_change"}
        assert row["calibration_state"] == "UNCALIBRATED"
        assert row["source"] == (
            f"The reference, lanes/tradingview/k2-ranking.params.json at {COMMIT}, "
            f"row {row['name']}; a starting value, never measured success."
        )
    assert {date.fromisoformat(r["last_change"]) for r in raw["rows"]} == {date(2026, 11, 11)}
    resource = files("sextant_movers") / "params" / "k2-ranking.params.json"
    with as_file(resource) as path:
        built = module.load_registry(path)
    assert len(built.rows) == 12
    assert built.params_hash == SHIPPED_HASH
    expected = {name: Decimal(v) if type(v) is str else v for name, _, v in EXPECTED}
    assert {r.name: r.value for r in built.rows} == expected


def test_shipped_sources_are_public_text() -> None:
    for row in json.loads(shipped_text())["rows"]:
        for pattern in (r"/(?:home|Users)/", r"\b[A-Z][A-Z_]*-\d+", r"ADR-00"):
            assert not re.search(pattern, row["source"])


def made_up(rng: random.Random) -> dict[str, Any]:
    return {
        "schema": "k2-ranking.registry/1",
        "rows": [
            {
                "name": name,
                "unit": unit,
                "value": (
                    f"{rng.randint(1, 999999)}.{rng.randint(0, 999):03}00"
                    if type(value) is str
                    else rng.randint(1, 999999)
                ),
                "source": rng.choice(("made-up", "made-up café", 'made-up " \\ \n')),
                "calibration_state": "UNCALIBRATED",
                "last_change": date.fromordinal(rng.randint(730120, 740000)).isoformat(),
            }
            for name, unit, value in EXPECTED
        ],
    }


def file_text(raw: dict[str, Any], fault: str) -> str:
    if fault == "damaged":
        return "{"
    if fault == "root_object":
        return "[]"
    if fault in ("missing_schema", "missing_rows"):
        del raw[fault.removeprefix("missing_")]
    elif fault == "schema":
        raw["schema"] = "made-up.schema/0"
    elif fault == "null_rows":
        raw["rows"] = None
    elif fault == "duplicate":
        raw["rows"].append(raw["rows"][0].copy())
    elif fault == "missing_row":
        raw["rows"].pop()
    elif fault == "row_object":
        raw["rows"][0] = 17
    elif fault.startswith("missing_"):
        del raw["rows"][0][fault.removeprefix("missing_")]
    elif fault in ("name", "unit"):
        raw["rows"][0][fault] = "made_up"
    elif fault == "date":
        raw["rows"][0]["last_change"] = "made-up-date"
    elif fault == "date_type":
        raw["rows"][0]["last_change"] = None
    elif fault.startswith(("int_", "decimal_")):
        values = {"string": "7", "bool": True, "float": 7.5, "list": [7], "int": 7, "text": "bad"}
        family, kind = fault.split("_")
        raw["rows"][-1 if family == "int" else 0]["value"] = values[kind]
    return json.dumps(raw, ensure_ascii=False)


def write_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "made-up.json"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(("fault", "error", "message"), FAULTS)
def test_one_fault_at_a_time(
    tmp_path: Path, fault: str, error: type[Exception], message: str
) -> None:
    path = write_file(tmp_path, file_text(made_up(random.Random(7)), fault))  # noqa: S311
    with pytest.raises(error, match=re.escape(message)):
        module.load_registry(path)


@pytest.mark.parametrize("value", [7, "7"])
def test_decimal_int_and_string_are_accepted(tmp_path: Path, value: int | str) -> None:
    raw = made_up(random.Random(7))  # noqa: S311 - made-up test data
    raw["rows"][0]["value"] = value
    built = module.load_registry(write_file(tmp_path, json.dumps(raw)))
    assert type(built.mover_min_move) is Decimal
    assert built.mover_min_move == Decimal(7)


def test_missing_file_is_not_wrapped(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        module.load_registry(tmp_path / "missing.json")


def test_registry_module_has_no_reference_client_or_network_import() -> None:
    tree = ast.parse(Path(module.__file__).read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    forbidden = {"sextant", "socket", "urllib", "http", "requests", "mcp", "alpaca"}
    assert not any(name.split(".")[0] in forbidden for name in imports)
    assert not any("client" in name.split(".") for name in imports)


def seeded_files() -> list[str]:
    rng = random.Random(20011103)  # noqa: S311 - deterministic proof, not a secret
    choices = [fault for fault, _, _ in FAULTS] + ["valid", "decimal_int", "decimal_string"]
    cases = []
    selected = set()
    for _ in range(384):
        raw = made_up(rng)
        fault = rng.choice(choices)
        selected.add(fault)
        cases.append(file_text(raw, fault))
    assert selected == set(choices)
    return cases


def outcome(path: Path) -> list[str]:
    try:
        return ["accepted", module.load_registry(path).params_hash]
    except Exception as error:
        message = str(error)
        if message.startswith(REFUSALS):
            return ["refused", "loader", message]
        return ["refused", type(error).__name__]


def outcomes(tmp_path: Path, cases: list[str]) -> list[list[str]]:
    return [outcome(write_file(tmp_path, text)) for text in cases]


def test_seeded_outcomes_digest(tmp_path: Path) -> None:
    results = outcomes(tmp_path, seeded_files())
    digest = hashlib.sha256(json.dumps(results, separators=(",", ":")).encode()).hexdigest()
    assert digest == "18b8b16ad6148427cc68d23c286b4484f6e705de022cc2c09dd4dad6c51a19e8"


CHILD = """
import hashlib, json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace

src = Path(os.environ["MOVERS_REFERENCE_SRC"])
pins = {
    src / "sextant/tradingview/prestage.py": "e3345d330725a4ad12d6e517ae74c592d3633ffe",
    src / "sextant/ranking/registry.py": "14464b0aa5b97de42eeebf05a20a049da6ba58b6",
    src.parent / "lanes/tradingview/k2-ranking.params.json":
        "f1bf882a5f000cc308f81bd82f85d88ee12b08fa",
}
for path, expected in pins.items():
    data = path.read_bytes()
    assert hashlib.sha1(b"blob " + str(len(data)).encode() + b"\\0" + data).hexdigest() == expected
payload = json.load(sys.stdin)
version = list(sys.version_info[:3])
assert version == payload["version"], "Python versions differ; stop parity"
sys.path.insert(0, str(src))
from sextant.tradingview.prestage import load_registry
module = SimpleNamespace(load_registry=load_registry)
REFUSALS = tuple(payload["refusals"])
exec(payload["outcome"])
original = json.loads((src.parent / "lanes/tradingview/k2-ranking.params.json").read_text())
rows = [[r["name"], r["unit"], r["value"], type(r["value"]).__name__,
         r["calibration_state"]] for r in original["rows"]]
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "made-up.json"
    path.write_text(payload["shipped"], encoding="utf-8")
    built = load_registry(path)
    shipped = {"hash": built.params_hash,
               "values": [[r.name, r.unit, str(r.value), type(r.value).__name__]
                          for r in built.rows]}
    results = []
    for text in payload["cases"]:
        path.write_text(text, encoding="utf-8")
        results.append(outcome(path))
print("PARITY " + json.dumps({"version": version, "rows": rows, "shipped": shipped,
                             "results": results}))
"""


@pytest.mark.local
def test_seeded_loader_and_shipped_file_equal_reference(tmp_path: Path) -> None:
    src, python = reference_child.reference()
    cases = seeded_files()
    answer = reference_child.run(
        python,
        src,
        CHILD,
        stdin=json.dumps(
            {
                "version": list(sys.version_info[:3]),
                "refusals": REFUSALS,
                "outcome": inspect.getsource(outcome),
                "shipped": shipped_text(),
                "cases": cases,
            }
        ),
    )
    assert answer["version"] == list(sys.version_info[:3]), "Python versions differ; stop parity"
    raw = json.loads(shipped_text())
    assert answer["rows"] == [
        [r["name"], r["unit"], r["value"], type(r["value"]).__name__, r["calibration_state"]]
        for r in raw["rows"]
    ]
    built = module.load_registry(write_file(tmp_path, shipped_text()))
    assert answer["shipped"] == {
        "hash": built.params_hash,
        "values": [[r.name, r.unit, str(r.value), type(r.value).__name__] for r in built.rows],
    }
    results = outcomes(tmp_path, cases)
    assert answer["results"] == results
    accepted = sum(r[0] == "accepted" for r in results)
    version = ".".join(map(str, answer["version"]))
    print(
        f"files={len(cases)} accepted={accepted} refused={len(cases) - accepted} "
        f"Python={version} reference={version}; shipped rows and hash equal"
    )
