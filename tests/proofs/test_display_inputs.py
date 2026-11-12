"""Synthetic display-unit acceptance; reference inputs.py:1-252 at
06562bec0898a7203f4eaf69086ced83d2477e6b. No recorded rows or host contact.
"""

import ast
import copy
import inspect
import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from nautilus_trader.core.datetime import dt_to_unix_nanos
from pandas import Timestamp

from sextant_movers.sessions import ET, OVERNIGHT_START
from sextant_movers.tradingview import inputs as reader

FILES = ("top100.json", "focus.json", "display-lists.json")
CLOCK = "2026-11-11T15:00:00.123456789Z"
NOW = cast(datetime, Timestamp("2026-11-11T15:00:30.123456789Z"))
NS = 1_000_000_000
UNAVAILABLE = "Research generation is unavailable; lists NOT synced"
EXPIRED = "Research generation expired; lists NOT synced"
MIXED = "Research files must share one exact producer generation"
CONTRACT = "Invalid producer display contract"
CALENDAR = "No supported XNYS trading date for this input"
PARENT = "Parent ranking empty or more than five unmapped names"
MOVERS = "Display movers empty or more than five unmapped names"
MAPPING = "Research needs exact TradingView exchange mappings"
IDENTITY = "Invalid snapshot generation identity"
AWARE = "Expected an explicit timezone-aware timestamp"


def symbol(i: int) -> str:
    return "ZZ" + "".join(chr(65 + i // power % 26) for power in (676, 26, 1))


def member(i: int, venue: str | None = "ZZX") -> dict[str, Any]:
    name = symbol(i)
    return {
        "symbol": name,
        "tradingview_symbol": f"{venue}:{name}" if venue else None,
        "labels": ["zz-synthetic"],
    }


def documents(*, clocks=None, movers=None, focus=None, bounds=None) -> dict[str, Any]:
    """A complete unit, parameterized by clocks, members/mappings and bounds."""
    movers = [member(0), member(1)] if movers is None else movers
    focus = [member(1)] if focus is None else focus
    clocks = dict.fromkeys(("as_of_snapshot", "as_of", "generated_at"), CLOCK) | (clocks or {})
    generation = {"as_of": clocks["as_of_snapshot"], "sha256": "b" * 64}
    common = {
        **clocks,
        "session_date": "2026-11-11",
        "status": "fresh",
        "snapshot_status": "observed",
        "generation": generation,
        "ranking_context": {"session": "rth"},
        "research_inputs": {"zz": "synthetic"},
    }
    return copy.deepcopy(
        dict(
            zip(
                FILES,
                (
                    {**common, "schema": "morning-pack.candidates/1", "candidates": movers},
                    {**common, "schema": "top100.focus/1", "candidates": focus},
                    {
                        **common,
                        "schema": "ns-display-lists/1",
                        "ns_movers": movers,
                        "ns_focus": focus,
                        "bounds": {"gap_bound": "0.05", "rvol_bound": "2", "max_age_s": 600}
                        | (bounds or {}),
                        "sources": {"snapshot": generation},
                        "armed": "UNVERIFIED",
                        "excluded_unknown": 0,
                        "counts": {"ns_movers": len(movers), "ns_focus": len(focus)},
                    },
                ),
                strict=True,
            )
        )
    )


def write_unit(path: Path, docs: dict[str, Any]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, doc in docs.items():
        (path / name).write_text(json.dumps(doc))
    return path


def read(path: Path, docs=None, *, now: Any = NOW, max_age=1200):
    return reader.read_research_inputs(
        write_unit(path, docs or documents()), now=now, max_age=max_age
    )


def refusal(path: Path, docs, reason: str, **kw) -> None:
    with pytest.raises(reader.InputError) as error:
        read(path, docs, **kw)
    assert str(error.value) == reason


def test_good_memberships_generation_unmapped_and_microsecond_expiry(tmp_path: Path) -> None:
    result = read(tmp_path)
    assert result.memberships == {
        "NS movers": frozenset({"ZZX:ZZAAA", "ZZX:ZZAAB"}),
        "NS focus": frozenset({"ZZX:ZZAAB"}),
    }
    assert result.expires_at == datetime(2026, 11, 11, 15, 10, 0, 123456, tzinfo=UTC)
    assert result.unmapped == frozenset()
    assert result.generation == documents()[FILES[2]]["generation"]
    assert reader.ET is ET
    assert (
        inspect.signature(reader.read_research_inputs).parameters["max_age"].default
        is inspect._empty
    )
    with pytest.raises(TypeError):
        reader.read_research_inputs(tmp_path, now=NOW)  # type: ignore[call-arg]


def test_source_uses_named_session_bounds_without_time_calls() -> None:
    tree = ast.parse(Path(reader.__file__).read_text())
    assert not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "time")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and (node.args or node.keywords)
            )
        )
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(20, 30, ("2026-11-11", False)), (4, 30, ("2026-11-11", True))],
)
def test_session_follows_imported_bounds(monkeypatch, hour, minute, expected) -> None:
    monkeypatch.setattr(reader, "OVERNIGHT_START", time(21), raising=False)
    monkeypatch.setattr(reader, "PRE_START", time(5), raising=False)
    assert reader._session(datetime(2026, 11, 11, hour, minute, tzinfo=ET)) == expected


def test_source_reads_text_as_utf8() -> None:
    tree = ast.parse(Path(reader.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read_text"
    ]
    assert calls
    assert all(
        any(
            keyword.arg == "encoding"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == "utf-8"
            for keyword in call.keywords
        )
        for call in calls
    )


def test_utf8_display_labels_are_accepted_and_returned_unchanged(tmp_path: Path) -> None:
    docs = documents()
    display = docs[FILES[2]]
    display["ns_movers"][0]["labels"] = ["zz-caf\u00e9-\u96ea"]
    write_unit(tmp_path, docs)
    target = tmp_path / FILES[2]
    target.write_bytes(json.dumps(display, ensure_ascii=False).encode("utf-8"))
    assert reader._read(target) == display
    assert reader.read_research_inputs(tmp_path, now=NOW, max_age=1200).memberships[
        "NS movers"
    ] == {"ZZX:ZZAAA", "ZZX:ZZAAB"}


def test_non_utf8_display_label_refuses_as_invalid_unit(tmp_path: Path) -> None:
    write_unit(tmp_path, documents())
    target = tmp_path / FILES[2]
    target.write_bytes(target.read_bytes().replace(b"zz-synthetic", b"zz-\xff", 1))
    with pytest.raises(
        reader.InputError, match="Missing or invalid research unit; lists NOT synced"
    ):
        reader.read_research_inputs(tmp_path, now=NOW, max_age=1200)


@pytest.mark.parametrize("damage", ["missing", "broken", [], None, 2, "zz-json"])
def test_missing_damaged_and_nonobject_json_refuse(tmp_path: Path, damage: Any) -> None:
    write_unit(tmp_path, documents())
    target = tmp_path / FILES[2]
    if damage == "missing":
        target.unlink()
    else:
        target.write_text("{" if damage == "broken" else json.dumps(damage))
    with pytest.raises(
        reader.InputError, match="Missing or invalid research unit; lists NOT synced"
    ):
        reader.read_research_inputs(tmp_path, now=NOW, max_age=1200)


@pytest.mark.parametrize("file", FILES)
@pytest.mark.parametrize(
    ("field", "value"),
    [("schema", "zz-schema"), ("status", "stale"), ("snapshot_status", "unknown")],
)
def test_each_envelope_availability_refusal(tmp_path, file, field, value) -> None:
    docs = documents()
    docs[file][field] = value
    refusal(tmp_path, docs, UNAVAILABLE)


@pytest.mark.parametrize(
    "clocks",
    [
        {"as_of_snapshot": "2026-11-11T15:00:01Z", "as_of": "2026-11-11T15:00:00Z"},
        {"as_of": "2026-11-11T15:00:02Z", "generated_at": "2026-11-11T15:00:01Z"},
        {"generated_at": "2026-11-11T15:00:30.123456790Z"},
    ],
)
def test_each_clock_order_and_future_nanosecond(tmp_path, clocks) -> None:
    refusal(tmp_path, documents(clocks=clocks), "Inconsistent or future research clocks")


@pytest.mark.parametrize(("delta", "expired"), [(NS - 1, False), (NS, True)])
def test_age_exact_bound_and_one_nanosecond_inside(tmp_path, delta, expired) -> None:
    at = (
        Timestamp(CLOCK) + timedelta(seconds=1)
        if expired
        else Timestamp(reader._ns(CLOCK) + delta, unit="ns", tz=UTC)
    )
    if expired:
        refusal(tmp_path, documents(), EXPIRED, now=at, max_age=1)
    else:
        assert read(tmp_path, now=at, max_age=1).expires_at == reader.timestamp(CLOCK) + timedelta(
            seconds=1
        )


@pytest.mark.parametrize(("delta", "expired"), [(600 * NS, False), (600 * NS + 1, True)])
def test_snapshot_lag_at_bound_and_one_nanosecond_past(tmp_path, delta, expired) -> None:
    at = Timestamp(reader._ns(CLOCK) + delta, unit="ns", tz=UTC)
    docs = documents(
        clocks={"as_of": at.isoformat(), "generated_at": at.isoformat()}, bounds={"max_age_s": 1200}
    )
    if expired:
        refusal(tmp_path, docs, EXPIRED, now=at)
    else:
        assert read(tmp_path, docs, now=at).generation == docs[FILES[2]]["generation"]


@pytest.mark.parametrize(
    ("age", "ok"), [(0, False), (1, True), (3600, True), (3601, False), (True, False)]
)
def test_required_max_age_range(tmp_path, age, ok) -> None:
    if ok:
        read(tmp_path, now=Timestamp(CLOCK), max_age=age)
    else:
        refusal(
            tmp_path, documents(), "Need an aware clock and max age 1..3600 seconds", max_age=age
        )
    refusal(
        tmp_path,
        documents(),
        "Need an aware clock and max age 1..3600 seconds",
        now=datetime(2026, 11, 11, 15),
    )


@pytest.mark.parametrize(
    ("caller", "own", "age", "expired"),
    [(1200, 30, 30, True), (30, 1200, 30, True), (1200, 31, 30, False), (31, 1200, 30, False)],
)
def test_display_age_narrows_never_widens(tmp_path, caller, own, age, expired) -> None:
    docs = documents(bounds={"max_age_s": own})
    if expired:
        refusal(tmp_path, docs, EXPIRED, max_age=caller)
    else:
        result = read(tmp_path, docs, max_age=caller)
        assert result.expires_at == reader.timestamp(CLOCK) + timedelta(seconds=min(caller, own))


@pytest.mark.parametrize("file", FILES)
@pytest.mark.parametrize(
    "field", ["generation", "ranking_context", "research_inputs", "generated_at"]
)
def test_three_files_must_share_each_exact_field(tmp_path, file, field) -> None:
    docs = documents()
    docs[file][field] = (
        "2026-11-11T15:00:00.123456790Z" if field == "generated_at" else {"zz": "changed"}
    )
    refusal(tmp_path, docs, MIXED)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"as_of": CLOCK, "sha256": "B" * 64},
        {"as_of": CLOCK, "sha256": 2},
        {"as_of": CLOCK, "sha256": "a" * 63},
        {"as_of": CLOCK, "sha256": "b" * 64, "zz": True},
        {"as_of": "2026-11-11T15:00:00Z", "sha256": "b" * 64},
    ],
)
def test_generation_identity_refusals(tmp_path, bad) -> None:
    docs = documents()
    for doc in docs.values():
        doc["generation"] = bad
    refusal(tmp_path, docs, IDENTITY)


def set_field(doc, path, value) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        doc = doc[int(key)] if isinstance(doc, list) else doc[key]
    key = int(keys[-1]) if isinstance(doc, list) else keys[-1]
    doc[key] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("sources", None),
        ("sources.snapshot", {}),
        ("bounds", None),
        ("bounds.gap_bound", "0"),
        ("bounds.rvol_bound", "-1"),
        ("bounds.gap_bound", "NaN"),
        ("bounds.rvol_bound", "Infinity"),
        ("bounds.gap_bound", "zz"),
        ("bounds.max_age_s", True),
        ("bounds.max_age_s", 0),
        ("bounds.max_age_s", 1.0),
        ("armed", "zz"),
        ("armed", []),
        ("excluded_unknown", True),
        ("excluded_unknown", -1),
        ("ns_movers", None),
        ("ns_focus", {}),
        ("counts.ns_movers", True),
        ("counts.ns_focus", 2),
        ("ns_movers.0.labels", []),
        ("ns_focus.0.labels", [""]),
        ("ns_focus.0.labels", [1]),
        ("ns_movers.0.labels", "zz"),
        ("ns_movers.0", 42),
    ],
)
def test_each_display_contract_refusal_including_nonobject_row(tmp_path, path, value) -> None:
    docs = documents()
    set_field(docs[FILES[2]], path, value)
    refusal(tmp_path, docs, CONTRACT)


@pytest.mark.parametrize(
    "path", ["sources", "bounds", "counts", "armed", "excluded_unknown", "ns_movers", "ns_focus"]
)
def test_missing_contract_fields_refuse(tmp_path, path) -> None:
    docs = documents()
    del docs[FILES[2]][path]
    refusal(tmp_path, docs, CONTRACT)


@pytest.mark.parametrize("count", [100, 101])
@pytest.mark.parametrize("which", ["parent", "movers", "focus"])
def test_membership_size_edges_and_kept_unbounded_focus(tmp_path, count, which) -> None:
    rows = [member(i) for i in range(count)]
    docs = documents(
        movers=rows if which == "movers" else None, focus=rows if which == "focus" else None
    )
    if which == "parent":
        docs[FILES[0]]["candidates"] = rows
    if count == 101 and which != "focus":
        refusal(tmp_path, docs, CONTRACT if which == "movers" else PARENT)
    else:
        result = read(tmp_path, docs)
        if which == "focus":
            assert len(result.memberships["NS focus"]) == count


@pytest.mark.parametrize("count", [5, 6])
@pytest.mark.parametrize("which", ["parent", "movers", "focus"])
def test_unmapped_edges_return_five_refuse_six(tmp_path, count, which) -> None:
    rows = [member(0), *[member(i, None) for i in range(2, 2 + count)]]
    docs = documents(
        movers=rows if which == "movers" else None, focus=rows if which == "focus" else None
    )
    if which == "parent":
        docs[FILES[0]]["candidates"] = rows
    if count == 6:
        refusal(tmp_path, docs, PARENT if which in {"parent", "movers"} else MOVERS)
    else:
        result = read(tmp_path, docs)
        assert result.unmapped == (
            frozenset() if which == "parent" else frozenset(symbol(i) for i in range(2, 7))
        )


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        (None, "Research members must be an array"),
        ([42], "Invalid research member"),
        ([{}], "Research tickers must be explicit and unique"),
        ([member(0), member(0)], "Research tickers must be explicit and unique"),
        ([member(0) | {"symbol": "zzbad"}], "Research tickers must be explicit and unique"),
        ([member(0) | {"tradingview_symbol": 3}], MAPPING),
        ([member(0) | {"tradingview_symbol": "ZZX:ZZAAA\n"}], MAPPING),
        (
            [member(0), member(1) | {"tradingview_symbol": "ZZX:ZZAAA"}],
            "Research mappings alias different tickers",
        ),
    ],
)
def test_each_member_refusal(tmp_path, rows, reason) -> None:
    with pytest.raises(reader.InputError) as error:
        reader._members(rows, {}, {}, set())
    assert str(error.value) == reason


@pytest.mark.parametrize("prefix", ["SMART", "IB", "IBKR", "ALPACA"])
def test_each_refused_prefix_refuses_entire_unit(tmp_path, prefix) -> None:
    refusal(tmp_path, documents(focus=[member(3, prefix)]), MAPPING)


@pytest.mark.parametrize("venue", ["ZZX_ALT", None])
def test_cross_list_symbol_mappings_must_match(tmp_path, venue) -> None:
    refusal(
        tmp_path, documents(focus=[member(0, venue)]), "Cross-list TradingView mappings disagree"
    )


def test_cross_list_alias_and_empty_members_never_clear(tmp_path) -> None:
    refusal(
        tmp_path,
        documents(focus=[member(2) | {"tradingview_symbol": "ZZX:ZZAAA"}]),
        "Research mappings alias different tickers",
    )
    refusal(tmp_path, documents(movers=[]), PARENT)
    docs = documents()
    docs[FILES[2]]["ns_movers"] = []
    docs[FILES[2]]["counts"]["ns_movers"] = 0
    refusal(tmp_path, docs, MOVERS)
    docs[FILES[0]]["candidates"] = None
    refusal(tmp_path, docs, PARENT)
    assert read(tmp_path, documents(focus=[member(3)])).memberships["NS focus"] == {"ZZX:ZZAAD"}


@pytest.mark.parametrize(
    ("value", "good"),
    [
        ("2026-11-11 15:00:00.123456789Z", True),
        (CLOCK, True),
        ("2026-11-11T15:00:00.123456789+0000", True),
        ("2026-11-11T10:00:00.123456789-05:00", True),
        ("2026-11-11T15:00:00.1234567890Z", False),
        ("2026-11-11T15:00:00", False),
        (datetime(2026, 11, 11, 15), False),
        (Timestamp("2026-11-11T15:00:00"), False),
        (None, False),
        ("2026-02-30T15:00:00Z", False),
        ("2026-11-11t15:00:00z", False),
    ],
)
def test_kept_timestamp_forms(value, good) -> None:
    if good:
        assert reader._ns(value) == reader._ns(CLOCK)
        assert reader.timestamp(value).microsecond == 123456
    else:
        with pytest.raises(reader.InputError, match=AWARE):
            reader.timestamp(value)


@pytest.mark.parametrize("year", [1, 1600, 1970, 2026, 2500, 9999])
def test_full_range_private_bridge_and_public_overflow_witness(year) -> None:
    at = datetime(year, 6, 15, 14, 30, 0, 123456, tzinfo=UTC)
    delta = at - datetime(1970, 1, 1, tzinfo=UTC)
    expected = (delta.days * 86400 + delta.seconds) * NS + 123456000
    assert reader._ns(at) == expected
    assert reader._ns(at.isoformat()) == expected
    assert reader.timestamp(at) == at
    if year in {1, 1600, 2500, 9999}:
        with pytest.raises(OverflowError):
            dt_to_unix_nanos(at)
    else:
        assert dt_to_unix_nanos(at) == expected
    assert reader._ns(Timestamp(CLOCK)) == reader._ns(CLOCK)


@pytest.mark.parametrize("year", [1600, 2500])
def test_unsupported_calendar_year_refuses(tmp_path, year) -> None:
    at = datetime(year, 6, 15, 14, 30, tzinfo=UTC)
    docs = documents(
        clocks=dict.fromkeys(("as_of_snapshot", "as_of", "generated_at"), at.isoformat())
    )
    refusal(tmp_path, docs, CALENDAR, now=at)


def test_now_on_non_session_refuses(tmp_path) -> None:
    clock = "2026-11-14T15:00:00Z"
    docs = documents(clocks=dict.fromkeys(("as_of_snapshot", "as_of", "generated_at"), clock))
    refusal(tmp_path, docs, CALENDAR, now=Timestamp(clock))


@pytest.mark.parametrize("file", FILES)
def test_each_envelope_session_date_refuses(tmp_path, file) -> None:
    docs = documents()
    docs[file]["session_date"] = "2026-11-10"
    refusal(tmp_path, docs, "Research belongs to a different Eastern trading session")


def test_each_clock_session_at_overnight_boundary() -> None:
    now = cast(datetime, Timestamp("2026-11-12T01:00:00Z"))
    old, new = "2026-11-12T00:59:59Z", now.isoformat()
    for clocks in ((old, new, new), (old, old, new), (old, old, old)):
        doc = documents()[FILES[0]]
        doc.update(dict(zip(("as_of_snapshot", "as_of", "generated_at"), clocks, strict=True)))
        doc["session_date"] = "2026-11-12"
        with pytest.raises(reader.InputError, match="different Eastern trading session"):
            reader._envelope(doc, "morning-pack.candidates/1", now, 1200)


@pytest.mark.parametrize("damage", [None, "missing", "word", "inputs"])
def test_exact_overnight_context_required(tmp_path, damage) -> None:
    clock = "2026-11-12T01:01:00Z"
    docs = documents(clocks=dict.fromkeys(("as_of_snapshot", "as_of", "generated_at"), clock))
    context = {
        "session": "overnight",
        "session_date": "2026-11-12",
        "intraday_feed": "boats",
        "daily_history_feed": "sip",
        "daily_history_scope": "prior completed XNYS sessions; not BOATS intraday evidence",
    }
    if damage == "word":
        context["daily_history_scope"] += " changed"
    for doc in docs.values():
        doc.update(session_date="2026-11-12", ranking_context=context, research_inputs={})
        if damage == "missing":
            del doc["ranking_context"]
        if damage == "inputs":
            doc["research_inputs"] = []
    if damage:
        refusal(
            tmp_path,
            docs,
            "Overnight input requires explicit BOATS provenance",
            now=Timestamp(clock),
        )
    else:
        assert read(tmp_path, docs, now=Timestamp(clock)).generation == docs[FILES[0]]["generation"]


def test_bounds_json_floats_parse_as_decimal_and_are_accepted(tmp_path) -> None:
    docs = documents(bounds={"gap_bound": 0.05, "rvol_bound": 2.5})
    path = write_unit(tmp_path, docs)
    assert reader._read(path / FILES[2])["bounds"]["gap_bound"] == Decimal("0.05")
    read(tmp_path, docs)


def test_symlink_resolved_once_cannot_mix_units_when_swapped(tmp_path, monkeypatch) -> None:
    first = write_unit(tmp_path / "first", documents())
    changed = documents()
    for doc in changed.values():
        doc["generation"]["sha256"] = "c" * 64
    write_unit(tmp_path / "second", changed)
    link = tmp_path / "current"
    link.symlink_to(first, target_is_directory=True)
    original = reader._read
    seen = []

    def swapped(path):
        seen.append(path)
        result = original(path)
        if len(seen) == 1:
            link.unlink()
            link.symlink_to(tmp_path / "second", target_is_directory=True)
        return result

    monkeypatch.setattr(reader, "_read", swapped)
    result = reader.read_research_inputs(link, now=NOW, max_age=1200)
    assert seen == [first / name for name in FILES]
    assert result.generation["sha256"] == "b" * 64
    assert link.resolve() == tmp_path / "second"


def test_kept_unchecked_focus_candidates_and_no_parent_subset_check(tmp_path) -> None:
    docs = documents(focus=[member(3)])
    docs[FILES[1]]["candidates"] = "zz-ignored"
    result = read(tmp_path, docs)
    assert result.memberships["NS focus"] == {"ZZX:ZZAAD"}
    docs[FILES[0]]["candidates"] = [member(4)]
    assert read(tmp_path, docs).memberships == result.memberships


def test_kept_bad_generation_timestamp_uses_timestamp_refusal(tmp_path) -> None:
    docs = documents()
    for doc in docs.values():
        doc["generation"]["as_of"] = "zz-invalid"
    refusal(tmp_path, docs, AWARE)


def test_source_only_pinned_dependencies_no_writes_or_network() -> None:
    tree = ast.parse(Path(reader.__file__).read_text())
    allowed = {
        "__future__",
        "json",
        "re",
        "dataclasses",
        "datetime",
        "decimal",
        "functools",
        "pathlib",
        "typing",
        "exchange_calendars",
        "nautilus_trader.core.datetime",
        "pandas",
        "sextant_movers.sessions",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in allowed for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in allowed
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "exec", "eval", "__import__"}
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
                    "replace_file",
                }


def seeded_units() -> list[dict[str, Any]]:
    import random

    rng = random.Random(6011)  # noqa: S311 - deterministic synthetic units
    units = []
    clock_keys = ("as_of_snapshot", "as_of", "generated_at")
    for index in range(320):
        clock = rng.choice(
            [
                CLOCK,
                "2026-11-11 15:00:00.000000001+0000",
                "2026-11-12T01:01:00.999999999Z",
                "2026-11-14T15:00:00Z",
                "1600-06-15T14:30:00Z",
                "2500-06-15T14:30:00Z",
                "0001-06-15T14:30:00Z",
                "9999-06-15T14:30:00Z",
            ]
        )
        at = datetime.fromisoformat(clock)
        local = at.astimezone(ET)
        day = (
            local.date() + timedelta(days=1) if local.time() >= OVERNIGHT_START else local.date()
        ).isoformat()
        rows = [member(i) for i in range(rng.randint(1, 12))]
        focus = rows[: rng.randint(0, len(rows))]
        docs = documents(
            clocks=dict.fromkeys(clock_keys, clock),
            movers=rows,
            focus=focus,
            bounds={"max_age_s": rng.choice([1, 30, 600, 3600])},
        )
        for doc in docs.values():
            doc["session_date"] = day
            if "01:01" in clock:
                doc["ranking_context"] = {
                    "session": "overnight",
                    "session_date": day,
                    "intraday_feed": "boats",
                    "daily_history_feed": "sip",
                    "daily_history_scope": (
                        "prior completed XNYS sessions; not BOATS intraday evidence"
                    ),
                }
        docs = json.loads(json.dumps(docs))
        target = docs[rng.choice(FILES)]
        damage = rng.randrange(28)
        if damage < 3:
            target[rng.choice(["status", "schema", "snapshot_status"])] = "zz-damaged"
        elif damage == 3:
            target["generation"] = {"as_of": clock, "sha256": "c" * 64}
        elif damage == 4:
            target["ranking_context"] = {"zz": "different"}
        elif damage == 5:
            target["research_inputs"] = {"zz": "different"}
        elif damage == 6:
            target["session_date"] = "zz-wrong"
        elif damage == 7:
            target[rng.choice(clock_keys)] = "zz-clock"
        elif damage == 8:
            docs[FILES[2]]["bounds"]["gap_bound"] = rng.choice([0, -1, "NaN", 0.05])
        elif damage == 9:
            docs[FILES[2]]["bounds"]["max_age_s"] = rng.choice([0, True, 1.0])
        elif damage == 10:
            docs[FILES[2]]["counts"]["ns_movers"] += 1
        elif damage == 11:
            docs[FILES[2]]["ns_movers"][0]["labels"] = rng.choice([[], [""], [True]])
        elif damage == 12:
            docs[FILES[2]]["ns_movers"][0]["tradingview_symbol"] = "SMART:" + symbol(0)
        elif damage == 13:
            docs[FILES[2]]["ns_focus"] = [member(0, "ZZX_ALT")]
            docs[FILES[2]]["counts"]["ns_focus"] = 1
        elif damage == 14:
            docs[FILES[0]]["candidates"] = []
        elif damage == 15:
            docs[FILES[2]]["ns_movers"] = []
            docs[FILES[2]]["counts"]["ns_movers"] = 0
        elif damage == 16:
            docs[FILES[2]]["armed"] = "zz-unknown"
        elif damage == 17:
            docs[FILES[2]]["excluded_unknown"] = -1
        elif damage == 18:
            docs[FILES[2]]["ns_movers"][0] = 42
        elif damage == 19:
            docs[FILES[0]]["candidates"] = [member(i) for i in range(101)]
        elif damage == 20:
            extra = [member(i, None) for i in range(20, 20 + rng.choice([5, 6]))]
            docs[FILES[2]]["ns_movers"] += extra
            docs[FILES[2]]["counts"]["ns_movers"] += len(extra)
        elif damage == 21:
            docs[FILES[2]]["sources"]["snapshot"]["sha256"] = "c" * 64
        # The remaining choices keep a unit undamaged, including supported and refused dates.
        units.append(
            {
                "documents": docs,
                "nows": [clock, (at + timedelta(seconds=rng.choice([1, 30, 601]))).isoformat()],
                "ages": rng.choice([[1, 3600], [30, 1200], [0, 3601], [True, 3600]]),
                "damage": damage,
                "index": index,
            }
        )
    return units


def unit_answers(m: Any, units: list[dict[str, Any]]) -> list[Any]:
    """Same wire JSON and explicit caller values, fresh temporary folder on either side."""
    import tempfile

    answers = []
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for unit in units:
            for name, document in unit["documents"].items():
                (root / name).write_text(json.dumps(document))
            for now_text in unit["nows"]:
                for age in unit["ages"]:
                    # Pandas preserves wire ns in its range; datetime keeps the full year range.
                    now = datetime.fromisoformat(now_text)
                    if 1700 <= now.year <= 2200:
                        now = Timestamp(now_text)
                    try:
                        result = m.read_research_inputs(root, now=now, max_age=age)
                    except m.InputError as exc:
                        answer = ["refused", str(exc)]
                    else:
                        answer = [
                            "accepted",
                            {k: sorted(v) for k, v in result.memberships.items()},
                            result.expires_at.isoformat(),
                            sorted(result.unmapped),
                            result.generation,
                        ]
                    answers.append(answer)
    return answers


def clock_answers(m: Any) -> list[int]:
    # Full-range integer bridge plus all nine fractional digits, on both sides.
    return [
        m._ns(f"{year:04}-06-15T14:30:00.123456789Z") for year in (1, 1600, 1970, 2026, 2500, 9999)
    ]


UNIT_DIGEST = "e25baa71e9b2b24ea095088c76979910eccc00f5e0ecfc88f35076a50586b59d"


def test_seeded_unit_answers_are_pinned() -> None:
    import hashlib

    units = seeded_units()
    answers = unit_answers(reader, units)
    assert len(units) == 320 and len(answers) == 1280
    assert {answer[0] for answer in answers} == {"accepted", "refused"}
    payload = json.dumps([answers, clock_answers(reader)], sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(payload.encode()).hexdigest() == UNIT_DIGEST


def test_seeded_overnight_units_are_accepted_and_refused() -> None:
    units = [
        unit
        for unit in seeded_units()
        if datetime.fromisoformat(unit["nows"][0]).astimezone(ET).time() >= OVERNIGHT_START
    ]
    assert units
    answers = unit_answers(reader, units)
    assert {answer[0] for answer in answers} == {"accepted", "refused"}


UNIT_PARITY = """
import hashlib, os, sys
src = os.environ["MOVERS_REFERENCE_SRC"]
path = os.path.join(src, "sextant", "tradingview", "inputs.py")
with open(path, "rb") as handle:
    blob = handle.read()
assert hashlib.sha1(b"blob %d\\0" % len(blob) + blob).hexdigest() == (
    "83bb85dfb9e2833430b6d95b010dced8f6e47a49"
)
sys.path.insert(0, src)
from sextant.tradingview import inputs as reader
assert os.path.samefile(reader.__file__, path)
print("PARITY " + json.dumps({
    "versions": list(sys.version_info[:3]),
    "answers": unit_answers(reader, json.loads(sys.stdin.read())),
    "clocks": clock_answers(reader),
}))
"""


@pytest.mark.local
def test_seeded_unit_parity_with_reference_in_child() -> None:
    import sys

    import reference_child as child

    src, python = child.reference()
    units = seeded_units()
    script = (
        "import json\nfrom datetime import datetime\nfrom pathlib import Path\n"
        "from typing import Any\nfrom pandas import Timestamp\n"
        + inspect.getsource(unit_answers)
        + inspect.getsource(clock_answers)
        + UNIT_PARITY
    )
    theirs = child.run(python, src, script, stdin=json.dumps(units))
    here = list(sys.version_info[:3])
    assert here == theirs["versions"], f"STOP: Python mismatch {here} != {theirs['versions']}"
    ours = unit_answers(reader, units)
    assert len(ours) == len(theirs["answers"]) == 1280
    for index, (actual, expected) in enumerate(zip(ours, theirs["answers"], strict=True)):
        assert actual == expected, f"display input answer differs at {index}"
    assert clock_answers(reader) == theirs["clocks"]
    accepted = sum(answer[0] == "accepted" for answer in ours)
    print(f"\nPython: here {here}, reference {theirs['versions']}")
    print(
        f"unit parity: {len(units)} units, {len(ours)} answers ({accepted} accepted, "
        f"{len(ours) - accepted} refused), all fields and six full-range clocks equal"
    )
