"""Synthetic cadence policy acceptance, the reference at
06562bec0898a7203f4eaf69086ced83d2477e6b, cadence.py:27-47,88-137,198-217.
Package resources: python/cpython@v3.13.18
0d4bd512fc429e3d4eacdd113c975e161f21644d:Lib/importlib/resources/_common.py:52,166.
"""

import ast
import copy
import hashlib
import inspect
import json
import random
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from sextant_movers.sessions import ET
from sextant_movers.tradingview import cadence as policy
from sextant_movers.tradingview.inputs import InputError

PIN = "06562bec0898a7203f4eaf69086ced83d2477e6b"
RESOURCE = files("sextant_movers") / "params" / "j5-cadence.params.json"
EXPECTED = [
    ("enabled", "flag", 1),
    ("changed_only", "flag", 1),
    ("max_concurrent_runs", "runs", 1),
    ("max_retries", "retries", 0),
    ("overnight_enabled", "flag", 0),
    ("onset_floor_s", "seconds", 150),
    ("regular_floor_s", "seconds", 300),
    ("usage_floor_s", "seconds", 900),
    ("weekly_usage_backoff_pct", "percent", 80),
    ("display_start_minute_et", "minute ET", 240),
    ("display_end_minute_et", "minute ET", 1200),
    ("am_start_minute_et", "minute ET", 420),
    ("am_end_minute_et", "minute ET", 630),
    ("pm_start_minute_et", "minute ET", 900),
    ("pm_end_minute_et", "minute ET", 960),
]
MEASUREMENTS = ["first_session_j5_run_time_ns", "first_session_date", "run_receipt_source"]
MISSING = "missing or duplicate cadence registry row"
ROW = "sourced UNCALIBRATED integer cadence rows required"
BOUNDS = "invalid cadence policy bounds"
SHIPPED_HASH = "1ed831ae16fff5baacd9d81052b601fb1d85eaf05b1dda40539d0f31fc841a33"


def document(**values: Any) -> dict[str, Any]:
    """Made-up sourced rows; no authority supplied by fixtures."""
    return {
        "schema": "j5-cadence.registry/1",
        "rows": [
            {
                "name": name,
                "unit": unit,
                "value": values.get(name, value),
                "source": "zz-synthetic policy test, no rollout authority",
                "calibration_state": "UNCALIBRATED",
                "last_change": "2026-11-11",
            }
            for name, unit, value in EXPECTED
        ],
        "measurements": dict.fromkeys(MEASUREMENTS),
    }


def write(tmp_path: Path, doc: Any) -> Path:
    path = tmp_path / "zz-policy.json"
    path.write_text(json.dumps(doc))
    return path


def build(tmp_path: Path, **values: Any):
    return policy.CadencePolicy(write(tmp_path, document(**values)))


def test_source_reads_text_as_utf8() -> None:
    calls = [
        node
        for node in ast.walk(ast.parse(Path(policy.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_text"
    ]
    assert calls
    assert all(
        any(k.arg == "encoding" and ast.literal_eval(k.value) == "utf-8" for k in c.keywords)
        for c in calls
    )


def test_utf8_source_is_accepted_and_hashed(tmp_path: Path) -> None:
    doc = document()
    doc["rows"][0]["source"] = "zz-caf\u00e9-\u96ea"
    path = tmp_path / "zz-utf8.json"
    path.write_bytes(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    rows = sorted(doc["rows"], key=lambda r: r["name"])
    assert (
        policy.CadencePolicy(path).params_hash
        == hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()
    )


def at(minute: int, day: str = "2026-11-11") -> datetime:
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), ET).replace(
        hour=minute // 60, minute=minute % 60
    )


def test_shipped_rows_values_sources_date_measurements_and_hash() -> None:
    raw = json.loads(RESOURCE.read_text())
    assert list(raw) == ["schema", "rows", "measurements"]
    rows = raw["rows"]
    assert len(rows) == 15
    assert [(r["name"], r["unit"], r["value"]) for r in rows] == EXPECTED
    assert {r["name"]: r["unit"] for r in rows} == policy.ROWS
    assert {r["calibration_state"] for r in rows} == {"UNCALIBRATED"}
    assert {r["last_change"] for r in rows} == {"2026-11-11"}
    for row in rows:
        assert set(row) == {"name", "unit", "value", "source", "calibration_state", "last_change"}
        assert type(row["value"]) is int
        assert row["source"] == (
            f"The reference, lanes/tradingview/j5-cadence.params.json at {PIN}, "
            f"row {row['name']}; a starting value, never measured success."
        )
        date.fromisoformat(row["last_change"])
    assert list(raw["measurements"]) == MEASUREMENTS
    assert raw["measurements"] == dict.fromkeys(MEASUREMENTS)
    with as_file(RESOURCE) as path:
        result = policy.CadencePolicy(path)
    assert dict(result.params) == {name: value for name, _, value in EXPECTED}
    assert result.params_hash == SHIPPED_HASH
    with pytest.raises(TypeError):
        params: Any = result.params
        params["enabled"] = 0


def test_shipped_sources_have_no_private_markers() -> None:
    sources = "\n".join(row["source"] for row in json.loads(RESOURCE.read_text())["rows"])
    for pattern in (r"/(?:home|Users)/", r"\b[A-Z]{2,}-\d+\b", r"\bADR[-_]\d+\b"):
        assert not re.search(pattern, sources)


@pytest.mark.parametrize("damage", ["schema", "count", "duplicate_added"])
def test_schema_count_and_added_duplicate_first_refusal(tmp_path, damage) -> None:
    doc = document()
    if damage == "schema":
        doc["schema"] = "zz-wrong"
    elif damage == "count":
        doc["rows"].pop()
    else:
        doc["rows"].append(copy.deepcopy(doc["rows"][0]))
    with pytest.raises(ValueError, match=f"^{MISSING}$"):
        policy.CadencePolicy(write(tmp_path, doc))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "zz-unknown"),
        ("unit", "zz-wrong"),
        ("value", True),
        ("value", 1.0),
        ("value", "1"),
        ("value", None),
        ("value", -1),
        ("source", "  \t"),
        ("calibration_state", "CALIBRATED"),
        ("calibration_state", "UNAVAILABLE"),
    ],
)
def test_each_row_refusal_alone(tmp_path, field, value) -> None:
    doc = document()
    doc["rows"][0][field] = value
    with pytest.raises(ValueError, match=f"^{ROW}$"):
        policy.CadencePolicy(write(tmp_path, doc))


def test_kept_same_count_duplicate_uses_row_refusal(tmp_path) -> None:
    doc = document()
    doc["rows"][1] = copy.deepcopy(doc["rows"][0])
    with pytest.raises(ValueError, match=f"^{ROW}$"):
        policy.CadencePolicy(write(tmp_path, doc))


@pytest.mark.parametrize("field", ["name", "unit", "value", "source", "calibration_state"])
def test_kept_missing_row_key_constructor_typeerror(tmp_path, field) -> None:
    doc = document()
    del doc["rows"][0][field]
    with pytest.raises(TypeError):
        policy.CadencePolicy(write(tmp_path, doc))


def test_kept_extra_row_key_constructor_typeerror(tmp_path) -> None:
    doc = document()
    doc["rows"][0]["zz-extra"] = 1
    with pytest.raises(TypeError):
        policy.CadencePolicy(write(tmp_path, doc))


@pytest.mark.parametrize(
    ("text", "exception"),
    [
        (None, FileNotFoundError),
        ("{", json.JSONDecodeError),
        ("[]", TypeError),
        ("null", TypeError),
        ('{"rows": []}', KeyError),
        ('{"schema": "j5-cadence.registry/1"}', KeyError),
    ],
)
def test_kept_raw_document_exceptions(tmp_path, text, exception) -> None:
    path = tmp_path / "zz-raw.json"
    if text is not None:
        path.write_text(text)
    with pytest.raises(exception):
        policy.CadencePolicy(path)


def test_non_utf8_source_raises_unicode_decode_error(tmp_path: Path) -> None:
    path = write(tmp_path, document())
    path.write_bytes(path.read_bytes().replace(b"zz-synthetic", b"zz-\xff", 1))
    with pytest.raises(UnicodeDecodeError) as error:
        policy.CadencePolicy(path)
    assert isinstance(error.value, ValueError)


@pytest.mark.parametrize("value", ["zz-not-date", None, 1])
def test_kept_invalid_last_change(tmp_path, value) -> None:
    doc = document()
    doc["rows"][0]["last_change"] = value
    with pytest.raises(ValueError if isinstance(value, str) else TypeError):
        policy.CadencePolicy(write(tmp_path, doc))


def test_kept_missing_last_change_is_keyerror(tmp_path) -> None:
    doc = document()
    del doc["rows"][0]["last_change"]
    with pytest.raises(KeyError):
        policy.CadencePolicy(write(tmp_path, doc))


@pytest.mark.parametrize("value", [None, [], {"zz": 42}, "zz-ignored"])
def test_kept_measurements_unread_and_unhashed(tmp_path, value) -> None:
    doc = document()
    original = policy.CadencePolicy(write(tmp_path, doc)).params_hash
    doc["measurements"] = value
    assert policy.CadencePolicy(write(tmp_path, doc)).params_hash == original
    del doc["measurements"]
    assert policy.CadencePolicy(write(tmp_path, doc)).params_hash == original


@pytest.mark.parametrize(
    ("values", "accepted"),
    [
        ({"enabled": 0}, True),
        ({"enabled": 1}, True),
        ({"enabled": 2}, False),
        ({"changed_only": 0}, False),
        ({"max_concurrent_runs": 2}, False),
        ({"max_retries": 1}, False),
        ({"overnight_enabled": 1}, False),
        ({"onset_floor_s": 0}, False),
        ({"onset_floor_s": 1}, True),
        ({"onset_floor_s": 300}, True),
        ({"onset_floor_s": 301}, False),
        ({"regular_floor_s": 900}, True),
        ({"regular_floor_s": 901}, False),
        ({"usage_floor_s": 300}, True),
        ({"usage_floor_s": 299}, False),
        ({"weekly_usage_backoff_pct": 0}, True),
        ({"weekly_usage_backoff_pct": 100}, True),
        ({"weekly_usage_backoff_pct": 101}, False),
        ({"display_start_minute_et": 0}, True),
        ({"display_start_minute_et": 420}, True),
        ({"display_start_minute_et": 421}, False),
        ({"am_start_minute_et": 629}, True),
        ({"am_start_minute_et": 630}, False),
        ({"am_end_minute_et": 900}, True),
        ({"am_end_minute_et": 901}, False),
        ({"pm_start_minute_et": 959}, True),
        ({"pm_start_minute_et": 960}, False),
        ({"pm_end_minute_et": 1200}, True),
        ({"pm_end_minute_et": 1201}, False),
        ({"display_end_minute_et": 1440}, True),
        ({"display_end_minute_et": 1441}, False),
    ],
)
def test_each_policy_bound_equal_and_one_past(tmp_path, values, accepted) -> None:
    if accepted:
        assert build(tmp_path, **values).params.items() >= values.items()
    else:
        with pytest.raises(ValueError, match=f"^{BOUNDS}$"):
            build(tmp_path, **values)


@pytest.mark.parametrize(
    ("minute", "expected"),
    [
        (0, None),
        (239, None),
        (240, 300),
        (419, 300),
        (420, 150),
        (629, 150),
        (630, 300),
        (899, 300),
        (900, 150),
        (959, 150),
        (960, 300),
        (1199, 300),
        (1200, None),
        (1201, None),
        (1439, None),
    ],
)
def test_floor_half_open_display_and_onset_minutes(tmp_path, minute, expected) -> None:
    assert build(tmp_path).floor(at(minute), Decimal(30)) == expected


@pytest.mark.parametrize("minute,normal", [(420, 150), (800, 300)])
@pytest.mark.parametrize("usage", [Decimal(-1), Decimal(79), Decimal(80), Decimal("80.0001")])
def test_kept_usage_strict_backoff_and_negative(tmp_path, minute, normal, usage) -> None:
    assert build(tmp_path).floor(at(minute), usage) == (900 if usage > 80 else normal)


@pytest.mark.parametrize(
    ("day", "minute", "expected"),
    [
        ("2026-11-14", 420, None),
        ("2026-12-25", 420, None),
        ("2026-11-27", 900, 150),
        ("2026-11-27", 1080, 300),
        ("2026-03-08", 420, None),
        ("2026-11-01", 420, None),
        ("2026-03-06", 420, 150),
        ("2026-03-09", 420, 150),
        ("1600-06-15", 800, None),
        ("2500-06-15", 800, None),
    ],
)
def test_kept_calendar_early_close_dst_and_unsupported_year(tmp_path, day, minute, expected):
    assert build(tmp_path).floor(at(minute, day), Decimal(0)) == expected


@pytest.mark.parametrize("zone", [UTC, ZoneInfo("Asia/Tokyo"), ZoneInfo("Europe/London")])
def test_kept_other_zone_converted_to_new_york(tmp_path, zone) -> None:
    assert build(tmp_path).floor(at(420).astimezone(zone), Decimal(0)) == 150


def test_kept_naive_now_raises_before_window_check(tmp_path) -> None:
    with pytest.raises(InputError, match="explicit timezone-aware"):
        build(tmp_path).floor(datetime(2026, 11, 11), Decimal(0))


@pytest.mark.parametrize(
    "usage,exception", [(None, TypeError), ("80", TypeError), (Decimal("NaN"), InvalidOperation)]
)
def test_kept_noncomparable_usage_raises(tmp_path, usage, exception) -> None:
    with pytest.raises(exception):
        build(tmp_path).floor(at(420), usage)


def test_kept_window_precedes_session_and_usage_checks(tmp_path, monkeypatch) -> None:
    def unexpected(_):
        pytest.fail("outside-window minute consulted the calendar")

    monkeypatch.setattr(policy, "_session", unexpected)
    gate = build(tmp_path)
    floor: Any = gate.floor
    assert floor(at(239), None) is None
    assert floor(at(1200), None) is None


def test_kept_session_tuple_and_overnight_flag_unused(tmp_path, monkeypatch) -> None:
    gate = build(tmp_path)
    for answer in [("zz-ignored", False), ("zz-ignored", True)]:
        monkeypatch.setattr(policy, "_session", lambda _, result=answer: result)
        assert gate.floor(at(420), Decimal(0)) == 150


def test_kept_enabled_is_admission_setting_not_floor_setting(tmp_path) -> None:
    assert build(tmp_path, enabled=0).floor(at(420), Decimal(0)) == 150


@pytest.mark.parametrize("name", ["display_start_minute_et", "weekly_usage_backoff_pct"])
def test_negative_lower_bound_refused_by_row_before_policy_bounds(tmp_path, name) -> None:
    with pytest.raises(ValueError, match=f"^{ROW}$"):
        build(tmp_path, **{name: -1})


@pytest.mark.parametrize("source", [None, 1])
def test_kept_nontext_source_attributeerror(tmp_path, source) -> None:
    doc = document()
    doc["rows"][0]["source"] = source
    with pytest.raises(AttributeError):
        policy.CadencePolicy(write(tmp_path, doc))


def test_kept_custom_window_observes_session_date_rollover(tmp_path) -> None:
    gate = build(tmp_path, display_start_minute_et=0, display_end_minute_et=1440)
    # _session's overnight flag is ignored, but its date rollover determines session validity.
    assert gate.floor(at(1200, "2026-11-13"), Decimal(0)) is None
    assert gate.floor(at(1200, "2026-11-15"), Decimal(0)) == 300
    assert gate.floor(at(120, "2026-11-16"), Decimal(0)) == 300


def test_wrapper_equal_and_both_keywords_required(tmp_path) -> None:
    registry = write(tmp_path, document())
    for minute in (239, 240, 420, 630, 900, 960, 1200):
        assert policy.cadence_floor(
            at(minute), registry=registry, weekly_usage_pct=Decimal(81)
        ) == (policy.CadencePolicy(registry).floor(at(minute), Decimal(81)))
    wrapper: Any = policy.cadence_floor
    for kwargs in ({}, {"registry": registry}, {"weekly_usage_pct": Decimal(0)}):
        with pytest.raises(TypeError):
            wrapper(at(420), **kwargs)


def test_source_only_policy_dependencies_no_writes_network_or_scheduling() -> None:
    tree = ast.parse(Path(policy.__file__).read_text())
    allowed = {
        "__future__",
        "hashlib",
        "json",
        "datetime",
        "decimal",
        "pathlib",
        "types",
        "sextant_movers.ranking.registry",
        "sextant_movers.sessions",
        "sextant_movers.tradingview.inputs",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "exec", "eval", "__import__", "sleep"}
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "open",
                    "write",
                    "write_text",
                    "write_bytes",
                    "mkdir",
                    "unlink",
                    "rename",
                    "touch",
                    "sleep",
                    "start",
                    "replace_file",
                }


def seeded_cases() -> list[dict[str, Any]]:
    rng = random.Random(60211)  # noqa: S311 - deterministic synthetic registry cases
    cases = []
    zones = [ET, UTC, ZoneInfo("Asia/Tokyo"), ZoneInfo("Europe/London")]
    days = [
        "2026-11-14",
        "2026-12-25",
        "2026-11-27",
        "2026-03-08",
        "2026-11-01",
        "2026-03-06",
        "2026-03-09",
    ]
    bounds = [
        "display_start_minute_et",
        "am_start_minute_et",
        "am_end_minute_et",
        "pm_start_minute_et",
        "pm_end_minute_et",
        "display_end_minute_et",
    ]
    for index in range(240):
        edges = sorted(rng.sample(range(1441), 6))
        values = dict(zip(bounds, edges, strict=True))
        onset = rng.randint(1, 500)
        regular = onset + rng.randint(0, 500)
        threshold = rng.randint(0, 100)
        values.update(
            enabled=rng.randrange(2),
            onset_floor_s=onset,
            regular_floor_s=regular,
            usage_floor_s=regular + rng.randint(0, 1000),
            weekly_usage_backoff_pct=threshold,
        )
        doc = document(**values)
        fault = rng.randrange(28)
        # Each case has at most one fault; remaining choices keep a valid policy.
        if fault == 0:
            doc["schema"] = "zz-wrong"
        elif fault == 1:
            doc["rows"].pop()
        elif fault == 2:
            doc["rows"].append(copy.deepcopy(doc["rows"][0]))
        elif fault == 3:
            doc["rows"][1] = copy.deepcopy(doc["rows"][0])
        elif fault == 4:
            doc["rows"][0]["name"] = "zz-unknown"
        elif fault == 5:
            doc["rows"][0]["unit"] = "zz-wrong"
        elif fault == 6:
            doc["rows"][0]["value"] = True
        elif fault == 7:
            doc["rows"][0]["source"] = " "
        elif fault == 8:
            doc["rows"][0]["calibration_state"] = "UNAVAILABLE"
        elif fault == 9:
            doc["rows"][0]["zz-extra"] = 1
        elif fault == 10:
            del doc["rows"][0]["value"]
        elif fault == 11:
            doc["rows"][0]["last_change"] = "zz-not-date"
        elif fault == 12:
            doc["rows"][0]["value"] = 2
        elif fault == 13:
            doc["rows"][7]["value"] = regular - 1
        elif fault == 14:
            del doc["schema"]
        elif fault == 15:
            del doc["rows"]
        text = json.dumps(doc)
        if fault == 16:
            text = "{"
        elif fault == 17:
            text = "[]"
        elif fault == 18:
            text = None
        nows = set()
        for edge in edges:
            for offset in (-1, 0, 1):
                instant = at(0) + timedelta(minutes=edge + offset)
                nows.add(instant.astimezone(rng.choice(zones)).isoformat())
        for day in days:
            for minute in (90, 930):
                instant = at(minute, day)
                nows.add(instant.astimezone(rng.choice(zones)).isoformat())
        # Both folds on the fall clock-change day and the spring transition gap in UTC.
        nows.update([at(90, "2026-11-01").replace(fold=fold).isoformat() for fold in (0, 1)])
        nows.add("2026-03-08T07:00:00+00:00")
        while len(nows) < 48:
            instant = at(rng.randrange(1440), rng.choice([*days, "2026-11-11"]))
            nows.add(instant.astimezone(rng.choice(zones)).isoformat())
        cases.append(
            {
                "text": text,
                "nows": sorted(nows),
                "index": index,
                "fault": fault,
                "usages": [str(threshold - 1), str(threshold), str(threshold + 1)],
            }
        )
    return cases


def cadence_answers(factory: Any, cases: list[dict[str, Any]]) -> list[Any]:
    """Same file text and instants on both sides; no reference import in this process."""
    import tempfile

    def error(exc):
        if str(exc) in {
            "missing or duplicate cadence registry row",
            "sourced UNCALIBRATED integer cadence rows required",
            "invalid cadence policy bounds",
        }:
            return ["refused", str(exc)]
        return ["exception", type(exc).__name__]

    answers = []
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "zz-policy.json"
        for case in cases:
            if case["text"] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(case["text"])
            try:
                gate = factory(path)
            except Exception as exc:
                answers.append(error(exc))
                continue
            floors = []
            for clock in case["nows"]:
                for usage in case["usages"]:
                    try:
                        floors.append(gate.floor(datetime.fromisoformat(clock), Decimal(usage)))
                    except Exception as exc:
                        floors.append(error(exc))
            answers.append(["accepted", gate.params_hash, floors])
    return answers


def parity_counts(answers: list[Any]) -> tuple[int, int, int, int]:
    accepted = [answer for answer in answers if answer[0] == "accepted"]
    return (
        len(answers),
        len(accepted),
        len(answers) - len(accepted),
        sum(len(answer[2]) for answer in accepted),
    )


SEEDED_DIGEST = "4a3ff6ff260c63cf55894c5c63dc211be6cad6547c2d63e1699c7dce5daf8149"


def test_seeded_cadence_answers_digest() -> None:
    cases = seeded_cases()
    answers = cadence_answers(policy.CadencePolicy, cases)
    count, accepted, refused, floors = parity_counts(answers)
    assert count >= 200 and accepted > 0 and refused > 0 and floors >= 4000
    assert all(len(case["nows"]) >= 40 for case in cases)
    assert {answer[0] for answer in answers} == {"accepted", "refused", "exception"}
    payload = json.dumps(answers, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(payload.encode()).hexdigest() == SEEDED_DIGEST


CADENCE_PARITY = """
import hashlib, os, sys, tempfile
payload = json.loads(sys.stdin.read())
assert list(sys.version_info[:3]) == payload["versions"], "STOP: Python versions differ"
src = Path(os.environ["MOVERS_REFERENCE_SRC"])
for relative, expected in [
    ("sextant/tradingview/cadence.py", "22fee1114ebc9f48177dd0e9743bfcf900424eea"),
    ("sextant/tradingview/inputs.py", "83bb85dfb9e2833430b6d95b010dced8f6e47a49"),
    ("../lanes/tradingview/j5-cadence.params.json", "41627ac0640740c3fcf9a67d971c7f30a9beb5db"),
]:
    blob = (src / relative).read_bytes()
    assert hashlib.sha1(b"blob %d\\0" % len(blob) + blob).hexdigest() == expected
sys.path.insert(0, str(src))
from sextant.tradingview.cadence import Cadence
assert os.path.samefile(sys.modules[Cadence.__module__].__file__,
                        src / "sextant/tradingview/cadence.py")
guarding = [False]
def no_constructor_side_effect(event, args):
    if not guarding[0]:
        return
    if event == "fcntl.flock" or event in {"os.mkdir", "os.remove", "os.rename"}:
        raise AssertionError("STOP: reference constructor attempted a write or lock")
    if event == "open":
        mode, flags = args[1:3]
        assert not (isinstance(mode, str) and any(k in mode for k in "wax+")), (
            "STOP: reference constructor attempted a write"
        )
        assert not (isinstance(flags, int) and flags & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        )), "STOP: reference constructor attempted a write"
sys.addaudithook(no_constructor_side_effect)
def factory(path):
    guarding[0] = True
    try:
        return Cadence(path.parent / "zz-state", path, page=lambda _: None)
    finally:
        guarding[0] = False
answers = cadence_answers(factory, payload["cases"])
with tempfile.TemporaryDirectory() as folder:
    registry = Path(folder) / "zz-shipped.json"
    registry.write_text(payload["shipped"])
    gate = factory(registry)
    shipped = {"params": dict(gate.params), "hash": gate.params_hash}
    assert list(Path(folder).iterdir()) == [registry], "STOP: constructor wrote state"
reference = json.loads((src.parent / "lanes/tradingview/j5-cadence.params.json").read_text())
fields = ("name", "unit", "value", "calibration_state")
rows = [[row[key] for key in fields] for row in reference["rows"]]
reference_hash = hashlib.sha256(json.dumps(
    sorted(reference["rows"], key=lambda row: row["name"]), sort_keys=True
).encode()).hexdigest()
print("PARITY " + json.dumps({"versions": list(sys.version_info[:3]), "answers": answers,
                             "shipped": shipped, "rows": rows, "reference_hash": reference_hash}))
"""


@pytest.mark.local
def test_seeded_cadence_reference_parity_and_shipped_file() -> None:
    import sys

    import reference_child as child

    src, python = child.reference()
    cases = seeded_cases()
    versions = list(sys.version_info[:3])
    payload = {"cases": cases, "shipped": RESOURCE.read_text(), "versions": versions}
    script = (
        "import json\nfrom datetime import datetime\nfrom decimal import Decimal\n"
        "from pathlib import Path\nfrom typing import Any\n"
        + inspect.getsource(cadence_answers)
        + CADENCE_PARITY
    )
    theirs = child.run(python, src, script, stdin=json.dumps(payload))
    assert versions == theirs["versions"], "STOP: Python versions differ"
    ours = cadence_answers(policy.CadencePolicy, cases)
    for case, actual, expected in zip(cases, ours, theirs["answers"], strict=True):
        assert actual == expected, f"cadence case differs at {case['index']}"
    with as_file(RESOURCE) as registry:
        gate = policy.CadencePolicy(registry)
    assert theirs["shipped"] == {"params": dict(gate.params), "hash": gate.params_hash}
    assert gate.params_hash != theirs["reference_hash"]
    fields = ("name", "unit", "value", "calibration_state")
    assert theirs["rows"] == [
        [row[key] for key in fields] for row in json.loads(RESOURCE.read_text())["rows"]
    ]
    count, accepted, refused, floors = parity_counts(ours)
    print(f"\nPython: here {versions}, reference {theirs['versions']}")
    print(
        f"cadence parity: {count} files, {accepted} accepted, {refused} refused, "
        f"{floors} floor answers; shipped params and hash equal, reference row order equal"
    )
