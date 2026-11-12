"""Part 6 service files, synthetic inputs only.

Build on python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:
Lib/json/__init__.py:304; Lib/importlib/resources/_common.py:52,166;
Doc/library/decimal.rst:141; Doc/library/datetime.rst:555.
Acceptance: the reference 06562bec0898a7203f4eaf69086ced83d2477e6b,
src/sextant/tradingview/prestage.py:89-90,140-162,311-371.
Fixtures carry no operating authority. load_maps has no reference parity: our universe
uses published flags, exchange and symbol forms, not the reference's name patterns.
"""

import ast
import inspect
import json
import re
from datetime import date
from decimal import Decimal
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

import pytest

from sextant_movers.ranking.listings import HEADERS
from sextant_movers.tradingview import loaders
from sextant_movers.tradingview.bridge import DisplayBounds

PIN = "06562bec0898a7203f4eaf69086ced83d2477e6b"
BUDGET_SOURCE = (
    "Alpaca, About Market Data API, https://docs.alpaca.markets/docs/about-market-data-api, "
    'read by the lead 2026-11-12: Historical API calls on the Basic plan, "200 / min". '
    f"The reference, lanes/tradingview/prestage-parameters.json at {PIN}, "
    "row prestage_request_budget. A starting value, never measured success."
)


def budget(**changes: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": "prestage_request_budget",
            "unit": "requests/minute",
            "value": 17,
            "source": "zz-invented, no operating authority",
            "calibration": "UNCALIBRATED",
            "last_change": "2026-11-12",
            "scope": "REVIEWED",
            **changes,
        }
    ]


def bounds() -> dict[str, Any]:
    return {
        "schema": "tv-display-bounds.registry/1",
        "rows": [
            {
                "name": name,
                "unit": unit,
                "value": value,
                "source": "zz-invented",
                "calibration_state": "UNCALIBRATED",
                "last_change": "2026-11-12",
            }
            for name, unit, value in (
                ("gap_bound", "fraction", "0.05"),
                ("rvol_bound", "ratio", "2"),
                ("max_age_s", "seconds", 600),
            )
        ],
    }


def write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "zz-file.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def budget_refusal(path: Path) -> str:
    return (
        "the prestage request budget is unavailable: no measured value and authority "
        f"are on record; record both in {path} (Rule 2)"
    )


def bounds_refusal(path: Path) -> str:
    return (
        f"the display bounds in {path} need exactly one sourced row each for "
        "gap_bound, rvol_bound, max_age_s (Rule 2)"
    )


def assert_refused(call: Any, path: Path, message: str) -> None:
    with pytest.raises(ValueError) as error:
        call(path)
    assert type(error.value) is ValueError
    assert str(error.value) == message


@pytest.mark.parametrize("value,accepted", [(0, False), (1, True), (200, True), (201, False)])
def test_budget_inclusive_edges(tmp_path, value, accepted) -> None:
    path = write(tmp_path, budget(value=value))
    if accepted:
        assert loaders.load_request_budget(path) == value
    else:
        assert_refused(loaders.load_request_budget, path, budget_refusal(path))


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", True),
        ("value", 1.0),
        ("value", "1"),
        ("value", None),
        ("value", -1),
        ("scope", "UNAVAILABLE"),
        ("scope", None),
        ("calibration", "UNAVAILABLE"),
        ("calibration", "uncalibrated"),
        ("source", None),
        ("source", " \t"),
        ("source", 1),
    ],
)
def test_budget_each_refusal_alone(tmp_path, field, value) -> None:
    path = write(tmp_path, budget(**{field: value}))
    assert_refused(loaders.load_request_budget, path, budget_refusal(path))


@pytest.mark.parametrize("field", ["value", "scope", "calibration", "source"])
def test_budget_missing_authority_field(tmp_path, field) -> None:
    document = budget()
    del document[0][field]
    path = write(tmp_path, document)
    assert_refused(loaders.load_request_budget, path, budget_refusal(path))


@pytest.mark.parametrize("document", [[], {}, {"rows": None}, [{"name": "zz-other"}]])
def test_budget_missing_named_row(tmp_path, document) -> None:
    path = write(tmp_path, document)
    assert_refused(
        loaders.load_request_budget, path, f"the prestage request budget row is missing from {path}"
    )


@pytest.mark.parametrize("state", ["UNCALIBRATED", "CALIBRATED"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_budget_list_or_rows_and_both_calibration_words(tmp_path, state, wrapped) -> None:
    rows = budget(calibration=state)
    assert loaders.load_request_budget(write(tmp_path, {"rows": rows} if wrapped else rows)) == 17


def test_budget_first_matching_row_decides_later_values_are_unread(tmp_path) -> None:
    rows = [{"name": "zz-other"}, *budget(), *budget(value=None)]
    assert loaders.load_request_budget(write(tmp_path, rows)) == 17
    rows[1], rows[2] = rows[2], rows[1]
    path = write(tmp_path, rows)
    assert_refused(loaders.load_request_budget, path, budget_refusal(path))


def test_budget_still_inspects_later_row_names_and_ignores_unit_and_date(tmp_path) -> None:
    assert loaders.load_request_budget(write(tmp_path, budget(unit=None, last_change=None))) == 17
    with pytest.raises(AttributeError):
        loaders.load_request_budget(write(tmp_path, [*budget(), 1]))


@pytest.mark.parametrize(
    "fault",
    [
        "schema",
        "missing_schema",
        "missing_rows",
        "null_rows",
        "nonlist",
        "missing_row",
        "duplicate",
        "row_scalar",
        "name_scalar",
        "unknown_name",
    ],
)
def test_bounds_document_refusals_alone(tmp_path, fault) -> None:
    doc = bounds()
    if fault == "schema":
        doc["schema"] = "zz-wrong"
    elif fault.startswith("missing_") and fault != "missing_row":
        del doc[fault.removeprefix("missing_")]
    elif fault in {"null_rows", "nonlist"}:
        doc["rows"] = None if fault == "null_rows" else {}
    elif fault == "missing_row":
        doc["rows"].pop()
    elif fault == "duplicate":
        doc["rows"].append(doc["rows"][0].copy())
    elif fault == "row_scalar":
        doc["rows"][0] = 1
    else:
        doc["rows"][0]["name"] = 1 if fault == "name_scalar" else "zz-unknown"
    path = write(tmp_path, doc)
    assert_refused(loaders.load_display_bounds, path, bounds_refusal(path))


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize(
    "field,value",
    [
        ("unit", "zz-wrong"),
        ("calibration_state", "UNAVAILABLE"),
        ("source", " \t"),
        ("source", None),
        ("source", 1),
        ("last_change", "zz-not-date"),
        ("last_change", None),
        ("last_change", 1),
        ("value", True),
        ("value", 1.5),
        ("value", None),
        ("value", []),
        ("value", {}),
    ],
)
def test_bounds_each_row_refusal_alone(tmp_path, index, field, value) -> None:
    doc = bounds()
    doc["rows"][index][field] = value
    path = write(tmp_path, doc)
    assert_refused(loaders.load_display_bounds, path, bounds_refusal(path))


@pytest.mark.parametrize(
    "field", ["name", "unit", "value", "source", "calibration_state", "last_change"]
)
def test_bounds_missing_row_field(tmp_path, field) -> None:
    doc = bounds()
    del doc["rows"][0][field]
    path = write(tmp_path, doc)
    assert_refused(loaders.load_display_bounds, path, bounds_refusal(path))


@pytest.mark.parametrize("index,value", [(0, 1), (1, 2), (2, "600")])
def test_bounds_decimal_requires_string_and_age_requires_int(tmp_path, index, value) -> None:
    doc = bounds()
    doc["rows"][index]["value"] = value
    path = write(tmp_path, doc)
    assert_refused(loaders.load_display_bounds, path, bounds_refusal(path))


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("value", [-1, 0, 1])
def test_bounds_positive_edges_delegate_to_display_bounds(tmp_path, index, value) -> None:
    doc = bounds()
    doc["rows"][index]["value"] = value if index == 2 else str(value)
    path = write(tmp_path, doc)
    if value > 0:
        assert getattr(loaders.load_display_bounds(path), doc["rows"][index]["name"]) == value
    else:
        assert_refused(loaders.load_display_bounds, path, bounds_refusal(path))


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize(
    "text,expected",
    [
        ("NaN", None),
        ("Infinity", None),
        ("zz-invalid", None),
        ("1e3", Decimal(1000)),
        (" 2 ", Decimal(2)),
    ],
)
def test_bounds_decimal_nonfinite_exponent_and_spaces(tmp_path, index, text, expected) -> None:
    doc = bounds()
    doc["rows"][index]["value"] = text
    path = write(tmp_path, doc)
    if expected is None:
        assert_refused(loaders.load_display_bounds, path, bounds_refusal(path))
    else:
        assert getattr(loaders.load_display_bounds(path), doc["rows"][index]["name"]) == expected


@pytest.mark.parametrize("state", ["UNCALIBRATED", "CALIBRATED"])
def test_bounds_both_calibration_words_and_row_order(tmp_path, state) -> None:
    doc = bounds()
    doc["rows"].reverse()
    for row in doc["rows"]:
        row["calibration_state"] = state
    assert loaders.load_display_bounds(write(tmp_path, doc)) == DisplayBounds(
        Decimal("0.05"), Decimal(2), 600
    )


@pytest.mark.parametrize("loader", [loaders.load_request_budget, loaders.load_display_bounds])
@pytest.mark.parametrize(
    "text,error", [("{", json.JSONDecodeError), ("1", AttributeError), (None, FileNotFoundError)]
)
def test_raw_file_exceptions_are_not_wrapped(tmp_path, loader, text, error) -> None:
    path = tmp_path / "zz-raw.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    with pytest.raises(error) as caught:
        loader(path)
    assert type(caught.value) is error


def test_nonobject_budget_row_is_attribute_error(tmp_path) -> None:
    with pytest.raises(AttributeError):
        loaders.load_request_budget(write(tmp_path, [1]))


def directories(tmp_path: Path, trailers: bool = True) -> tuple[Path, Path, Path]:
    paths = []
    for filename, symbols, created in (
        ("nasdaqlisted.txt", ("ZZCC", "ZZAA", "ZZBB"), "1112202612:00"),
        ("otherlisted.txt", ("ZZDD.A",), "1112202613:00"),
    ):
        rows = [HEADERS[filename]]
        for symbol in symbols:
            values = {
                "Symbol": symbol,
                "ACT Symbol": symbol,
                "NASDAQ Symbol": symbol,
                "CQS Symbol": symbol,
                "Security Name": "Zz Invented",
                "Test Issue": "N",
                "ETF": "N",
                "Exchange": "N",
            }
            rows.append("|".join(values.get(key, "N") for key in HEADERS[filename].split("|")))
        if trailers:
            rows.append(f"File Creation Time: {created}|||||||")
        path = tmp_path / filename
        path.write_text("\n".join(rows), encoding="utf-8")
        paths.append(path)
    mapping = tmp_path / "zz-map.json"
    mapping.write_text(
        json.dumps({"ZZCC": "NASDAQ:ZZCC", "ZZBB": None, "ZZDD.A": "NYSE:ZZDD.A"}), encoding="utf-8"
    )
    return paths[0], paths[1], mapping


@pytest.mark.parametrize("trailers", [True, False])
def test_maps_keeps_universe_order_omits_null_and_absent_counts_both_times(tmp_path, trailers):
    result = loaders.load_maps(*directories(tmp_path, trailers))
    created = "1112202612:00 / 1112202613:00" if trailers else "unknown / unknown"
    assert result == (
        ("ZZCC", "ZZDD.A"),
        {"ZZCC": "NASDAQ:ZZCC", "ZZDD.A": "NYSE:ZZDD.A"},
        f"NASDAQ Trader nasdaqlisted.txt + otherlisted.txt, File Creation Time {created}; "
        "2 of 4 directory symbols mapped, 2 unmapped omitted",
    )
    assert all(type(v) is str for v in result[1].values())


def test_maps_none_mapped_exact_refusal(tmp_path) -> None:
    n, o, m = directories(tmp_path)
    m.write_text('{"ZZBB": null}', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        loaders.load_maps(n, o, m)
    assert type(error.value) is ValueError
    assert str(error.value) == "The symbol map maps none of 4 directory symbols"


@pytest.mark.parametrize(
    "text,message",
    [
        ("{", "the symbol map must be readable JSON"),
        ("1", "the symbol map must be a non-empty JSON object"),
        ('{"ZZAA": []}', "symbol map values must be exchange-prefixed mappings"),
        (None, "the symbol map must be readable JSON"),
    ],
)
def test_maps_delegated_file_refusals(tmp_path, text, message) -> None:
    n, o, m = directories(tmp_path)
    if text is None:
        m.unlink()
    else:
        m.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as error:
        loaders.load_maps(n, o, m)
    assert str(error.value) == message


def test_maps_missing_directory_is_file_not_found(tmp_path) -> None:
    n, o, m = directories(tmp_path)
    n.unlink()
    with pytest.raises(FileNotFoundError):
        loaders.load_maps(n, o, m)


def test_departures_required_paths_no_module_path_defaults_and_no_network_or_writes() -> None:
    for call in (loaders.load_request_budget, loaders.load_display_bounds):
        assert inspect.signature(call).parameters["path"].default is inspect.Parameter.empty
        with pytest.raises(TypeError):
            call()  # type: ignore[call-arg]
    tree = ast.parse(Path(loaders.__file__).read_text(encoding="utf-8"))
    assert [n.targets[0].id for n in tree.body if isinstance(n, ast.Assign)] == [
        "BUDGET_NAME",
        "BOUNDS",
    ]
    forbidden = {"sextant", "socket", "urllib", "http", "requests", "alpaca", "mcp", "os", "time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(n.name.split(".")[0] not in forbidden for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden
            assert "client" not in (node.module or "")
        elif isinstance(node, ast.Attribute):
            assert node.attr not in {
                "home",
                "now",
                "today",
                "environ",
                "getenv",
                "write_text",
                "write_bytes",
                "open",
                "unlink",
                "mkdir",
            }
        elif isinstance(node, ast.Name):
            assert node.id not in {"__file__", "open"}


def test_shipped_files_through_loaders_values_sources_dates_and_key_order() -> None:
    root = files("sextant_movers") / "params"
    resource = root / "prestage-parameters.json"
    rows = json.loads(resource.read_text("utf-8"))
    assert len(rows) == 1
    row = rows[0]
    assert list(row) == ["name", "unit", "value", "source", "calibration", "last_change", "scope"]
    assert row == {
        "name": "prestage_request_budget",
        "unit": "requests/minute",
        "value": 200,
        "source": BUDGET_SOURCE,
        "calibration": "UNCALIBRATED",
        "last_change": "2026-11-12",
        "scope": "REVIEWED",
    }
    with as_file(resource) as path:
        assert loaders.load_request_budget(path) == 200
    resource = root / "display-bounds.params.json"
    raw = json.loads(resource.read_text("utf-8"))
    assert list(raw) == ["schema", "rows"]
    assert raw["schema"] == "tv-display-bounds.registry/1"
    assert [(r["name"], r["unit"], r["value"]) for r in raw["rows"]] == [
        ("gap_bound", "fraction", "0.05"),
        ("rvol_bound", "ratio", "2"),
        ("max_age_s", "seconds", 600),
    ]
    for row in raw["rows"]:
        assert list(row) == ["name", "unit", "value", "source", "calibration_state", "last_change"]
        assert row["calibration_state"] == "UNCALIBRATED"
        assert row["source"] == (
            f"The reference, lanes/tradingview/display-bounds.params.json at "
            f"{PIN}, row {row['name']}; a starting value, never measured success."
        )
        assert date.fromisoformat(row["last_change"]) == date(2026, 11, 12)
    with as_file(resource) as path:
        assert loaders.load_display_bounds(path) == DisplayBounds(Decimal("0.05"), Decimal(2), 600)
    for source in [BUDGET_SOURCE, *(r["source"] for r in raw["rows"])]:
        assert not re.search(r"/(?:home|Users)/|ADR-\d+", source)


# A complete passing prefix ends above this seeded file-loader extension.
def loader_cases() -> list[list[str]]:
    import random

    rng = random.Random(20261114)  # noqa: S311 - made-up documents
    cases = []
    budget_faults = [
        None,
        ("value", 0),
        ("value", 201),
        ("value", True),
        ("value", "2"),
        ("scope", "UNAVAILABLE"),
        ("calibration", "UNAVAILABLE"),
        ("source", " "),
        ("source", 1),
        ("calibration", []),
    ]
    bounds_faults = [
        None,
        ("unit", "zz"),
        ("value", 1.5),
        ("source", " "),
        ("last_change", "zz"),
        ("calibration_state", "UNAVAILABLE"),
        ("value", "NaN"),
        ("value", "Infinity"),
        ("value", "0"),
        ("value", "1e3"),
        ("value", " 2 "),
        ("calibration_state", []),
    ]
    for kind in ("budget", "bounds"):
        for index in range(384):
            doc: Any
            if kind == "budget":
                doc = budget(
                    value=rng.randint(1, 200),
                    source=f"zz-made-up {rng.randint(0, 99999)}",
                    calibration=rng.choice(("CALIBRATED", "UNCALIBRATED")),
                )
                faults = budget_faults
                row = doc[0]
            else:
                doc = bounds()
                for item in doc["rows"]:
                    item["value"] = (
                        rng.randint(1, 9999)
                        if item["name"] == "max_age_s"
                        else f"{rng.randint(1, 9999)}.{rng.randint(0, 999):03}"
                    )
                    item["calibration_state"] = rng.choice(("CALIBRATED", "UNCALIBRATED"))
                row = doc["rows"][rng.randrange(3)]
                faults = bounds_faults
            # Valid, single fault, and seeded combinations; raw-shape faults are separate.
            if index % 4 in (1, 2):
                for _ in range(1 if index % 4 == 1 else rng.randint(2, 4)):
                    fault = rng.choice(faults)
                    if fault is not None:
                        row[fault[0]] = fault[1]
            if index % 12 == 3:
                text = rng.choice(("{", "null", "1", "[]", '{"rows": [1]}'))
            elif index % 12 == 7:
                if kind == "budget":
                    doc = [*doc, *budget(value=None)]
                else:
                    doc["rows"].append(doc["rows"][0].copy())
                text = json.dumps(doc)
            elif index % 12 == 11:
                if kind == "budget":
                    doc = {"rows": doc}
                else:
                    del row[rng.choice(tuple(row))]
                text = json.dumps(doc)
            else:
                text = json.dumps(doc)
            cases.append([kind, text])
    return cases


def loader_answer(module: Any, kind: str, path: Path) -> list[Any]:
    try:
        if kind == "budget":
            return ["accepted", module.load_request_budget(path)]
        result = module.load_display_bounds(path)
        return ["accepted", str(result.gap_bound), str(result.rvol_bound), result.max_age_s]
    except Exception as error:
        name, message = type(error).__name__, str(error)
        # Only the reference's named refusal class is mapped; raw exceptions stay exact.
        if name == "PrestageError":
            name = "ValueError"
            if kind == "budget" and message.startswith(
                "the prestage request budget is unavailable:"
            ):
                message = budget_refusal(path)
        return ["refused", name, message.replace(str(path), "<file>")]


def loader_answers(tmp_path: Path, cases: list[list[str]]) -> list[list[Any]]:
    path = tmp_path / "zz-seeded.json"
    answers = []
    for kind, text in cases:
        path.write_text(text, encoding="utf-8")
        answers.append(loader_answer(loaders, kind, path))
    return answers


def loader_digest(answers: Any) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(answers, separators=(",", ":")).encode()).hexdigest()


def test_seeded_loader_answers_are_pinned(tmp_path: Path) -> None:
    cases = loader_cases()
    assert sum(kind == "budget" for kind, _ in cases) >= 300
    assert sum(kind == "bounds" for kind, _ in cases) >= 300
    assert (
        loader_digest(loader_answers(tmp_path, cases))
        == "c2b9c79ab5011e15c0fb88dde298e060ba39d3eead832bee4a646f494e129125"
    )


LOADER_CHILD = r"""
import hashlib, json, os, platform, sys, tempfile
from pathlib import Path
from typing import Any
src = Path(os.environ["MOVERS_REFERENCE_SRC"])
pins = {
    "tradingview/prestage.py": "e3345d330725a4ad12d6e517ae74c592d3633ffe",
    "tradingview/bridge.py": "29f8c37cd021082ae43f1d5c70c834456b6c0a8c",
}
for name, expected in pins.items():
    blob = (src / "sextant" / name).read_bytes()
    assert hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest() == expected
payload = json.load(sys.stdin)
version = platform.python_version()
assert version == payload["version"], "Python versions differ; stop parity"
sys.path.insert(0, str(src))
from sextant.tradingview import prestage as module
with tempfile.TemporaryDirectory() as folder:
    path = Path(folder) / "zz-seeded.json"
    answers = []
    for kind, text in payload["cases"]:
        path.write_text(text, encoding="utf-8")
        answers.append(loader_answer(module, kind, path))
print("PARITY " + json.dumps({"version": version, "answers": answers}))
"""


@pytest.mark.local
def test_seeded_loaders_equal_reference(tmp_path: Path) -> None:
    import platform

    import reference_child

    src, python = reference_child.reference()
    cases = loader_cases()
    script = (
        "from __future__ import annotations\n"
        + inspect.getsource(budget_refusal)
        + inspect.getsource(loader_answer)
        + LOADER_CHILD
    )
    theirs = reference_child.run(
        python,
        src,
        script,
        stdin=json.dumps({"version": platform.python_version(), "cases": cases}),
    )
    assert theirs["version"] == platform.python_version(), "Python versions differ; stop parity"
    ours = loader_answers(tmp_path, cases)
    assert theirs["answers"] == ours
    counts = []
    for kind in ("budget", "bounds"):
        answers = [a for c, a in zip(cases, ours, strict=True) if c[0] == kind]
        accepted = sum(a[0] == "accepted" for a in answers)
        counts.append(
            f"{kind} files={len(answers)} accepted={accepted} refused={len(answers) - accepted}"
        )
    print(
        "; ".join(counts) + f"; Python={theirs['version']} both sides; digest={loader_digest(ours)}"
    )
