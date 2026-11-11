"""Part 3b: the ranking generation record, its strict JSON reader and its canonical digest.

Synthetic identifiers and values only. The reference is `src/sextant/ranking/generation.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never imports it. The `local` proof
runs it in a child interpreter named by `MOVERS_REFERENCE_PYTHON`, with the export's `src`
directory named by `MOVERS_REFERENCE_SRC`, and compares digests over the same seeded records.
"""

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from sextant_movers.ranking.generation import (
    Coverage,
    CoverageState,
    GenerationId,
    GenerationStatus,
    RankingFeature,
    RankingGeneration,
    RankingReason,
    RankingRow,
    canonical_bytes,
    generation_digest,
)

from sextant_movers.ranking import generation as gen

MAX = 2**63 - 1
REFERENCE_BLOB = "35e946323d784ec6c8c0166e2145ed7747f57728"  # git blob of the reference file
REASON = RankingReason("ZZZT-code", "ZZZT-detail", (RankingReason("ZZZT-child"),))
FEATURE = RankingFeature("ZZZT-f2", D("1.50"), 3, 4, True, False)
COUNTS = (
    "expected_symbols",
    "observed_symbols",
    "missing_symbols",
    "late_symbols",
    "invalid_symbols",
)
BASE: dict[type, dict[str, Any]] = {
    GenerationId: {"producer_epoch": "ZZZT-EPOCH", "sequence": 7, "previous_sequence": 6},
    RankingReason: {"code": "ZZZT-code", "detail": "ZZZT-detail", "children": (REASON,)},
    Coverage: {
        "state": CoverageState.READY,
        "complete": True,
        **dict.fromkeys(COUNTS, 2),
        "reasons": (REASON,),
    },
    RankingFeature: {
        "name": "ZZZT-f2",
        "value": D("1.50"),
        "ts_event": 3,
        "known_at": 4,
        "partial": True,
        "completed": False,
    },
    RankingRow: {
        "symbol": "ZZZT1.TEST",
        "instrument_id": "ZZZT1.TEST",
        "score": D("2.5E+1"),
        "rank": 1,
        "features": (FEATURE, RankingFeature("ZZZT-f1")),
        "partial": False,
        "completed": True,
        "focus_eligible": True,
        "reasons": (REASON,),
    },
}
ROW = RankingRow(**BASE[RankingRow])
BASE[RankingGeneration] = {
    "generation_id": GenerationId(**BASE[GenerationId]),
    "session_date": date(2030, 1, 2),
    "input_cutoff": 10,
    "ts_event": 11,
    "ts_init": 12,
    "known_at": 13,
    "method_version": "ZZZT-m",
    "profile_version": "ZZZT-p",
    "ranker_version": "ZZZT-r",
    "coverage": Coverage(**BASE[Coverage]),
    "status": GenerationStatus.PROVISIONAL,
    "rows": (ROW, RankingRow("ZZZT2.TEST", rank=None)),
}
GENERATION = RankingGeneration(**BASE[RankingGeneration])

INT64_BAD = (-1, 2**63, True, 1.0, "1", D(1))
TEXT_BAD = ("", None, 1, b"x")
BOOL_BAD = (None, 0, 1, "true")
DEC_BAD = (1.5, 1, "1", D("NaN"), D("-Infinity"), D("sNaN"))
REFUSALS = [
    *((GenerationId, "producer_epoch", v) for v in TEXT_BAD),
    *((GenerationId, f, v) for f in ("sequence", "previous_sequence") for v in INT64_BAD),
    (GenerationId, "previous_sequence", 7),
    (GenerationId, "previous_sequence", 8),
    *((RankingReason, "code", v) for v in TEXT_BAD),
    *((RankingReason, "detail", v) for v in ("", 1)),
    *((RankingReason, "children", v) for v in ("x", (None,), [REASON, 1], {"a": REASON})),
    *((Coverage, "state", v) for v in ("READY", None, GenerationStatus.SETTLED)),
    *((Coverage, "complete", v) for v in BOOL_BAD),
    *((Coverage, f, v) for f in COUNTS for v in INT64_BAD),
    *((Coverage, "reasons", v) for v in ((None,), "x", (FEATURE,))),
    *((RankingFeature, "name", v) for v in TEXT_BAD),
    *((RankingFeature, "value", v) for v in DEC_BAD),
    *((RankingFeature, f, v) for f in ("ts_event", "known_at") for v in INT64_BAD),
    *((RankingFeature, f, v) for f in ("partial", "completed") for v in BOOL_BAD),
    *((RankingRow, "symbol", v) for v in TEXT_BAD),
    *((RankingRow, "instrument_id", v) for v in ("", 1)),
    *((RankingRow, "score", v) for v in DEC_BAD),
    *((RankingRow, "rank", v) for v in (0, *INT64_BAD)),
    *((RankingRow, f, v) for f in ("partial", "completed") for v in BOOL_BAD),
    *((RankingRow, "focus_eligible", v) for v in (0, 1, "true")),
    *((RankingRow, "features", v) for v in ((ROW,), (FEATURE, FEATURE), "x")),
    *((RankingRow, "reasons", v) for v in ((FEATURE,), None)),
    *((RankingGeneration, f, v) for f in ("generation_id", "coverage") for v in (None, {}, ())),
    *((RankingGeneration, "session_date", v) for v in (datetime(2030, 1, 2), "2030-01-02", None)),
    *(
        (RankingGeneration, f, v)
        for f in ("input_cutoff", "ts_event", "ts_init", "known_at")
        for v in INT64_BAD
    ),
    *(
        (RankingGeneration, f, v)
        for f in ("method_version", "profile_version", "ranker_version")
        for v in TEXT_BAD
    ),
    *((RankingGeneration, "status", v) for v in ("PROVISIONAL", CoverageState.READY, None)),
    *((RankingGeneration, "rows", v) for v in ((FEATURE,), (ROW, ROW), "x")),
]


@pytest.mark.parametrize(("cls", "name", "value"), REFUSALS, ids=lambda v: repr(v)[:24])
def test_every_record_refuses_each_invalid_field(cls: type, name: str, value: Any) -> None:
    cls(**BASE[cls])
    with pytest.raises(ValueError):
        cls(**{**BASE[cls], name: value})


def test_int64_bounds_and_optional_nulls_are_accepted() -> None:
    edge = replace(
        GENERATION,
        generation_id=GenerationId("ZZZT-EPOCH", MAX, MAX - 1),
        input_cutoff=0,
        ts_event=MAX,
        ts_init=0,
        known_at=MAX,
        coverage=Coverage(CoverageState.UNKNOWN, False, *(MAX, 0, None, MAX, 0)),
        rows=(RankingRow("ZZZT1.TEST", rank=MAX, features=(RankingFeature("f", None, 0, MAX),)),),
    )
    assert RankingGeneration.from_json(edge.to_json()).to_json() == edge.to_json()
    assert RankingRow("ZZZT1.TEST", rank=1).rank == 1
    assert GenerationId("ZZZT-EPOCH", 0).previous_sequence is None


def test_records_are_frozen_hashable_and_hold_tuples() -> None:
    feature = GENERATION.rows[0].features[0]
    records = (GENERATION, GENERATION.generation_id, GENERATION.coverage, ROW, feature, REASON)
    for record in records:
        for field in fields(record):
            with pytest.raises(FrozenInstanceError):
                setattr(record, field.name, None)
        assert hash(record) == hash(record)
    assert RankingReason("c", None, [REASON]).children == (REASON,)
    assert isinstance(RankingGeneration(**{**BASE[RankingGeneration], "rows": [ROW]}).rows, tuple)


def test_rows_rank_then_symbol_features_by_name_reasons_keep_order() -> None:
    rows = [
        RankingRow("ZZZT9.TEST"),
        RankingRow("ZZZT8.TEST", rank=2),
        RankingRow("ZZZT7.TEST", "ZZZT7.TEST", rank=1),
        RankingRow("ZZZT6.TEST", rank=1),
        RankingRow("ZZZT5.TEST"),
    ]
    record = replace(GENERATION, rows=rows)
    assert [r.symbol for r in record.rows] == [f"ZZZT{n}.TEST" for n in (6, 7, 8, 5, 9)]
    assert generation_digest(replace(GENERATION, rows=rows[::-1])) == generation_digest(record)
    assert [f.name for f in ROW.features] == ["ZZZT-f1", "ZZZT-f2"]
    reasons = (RankingReason("z"), RankingReason("a"))
    assert replace(ROW, reasons=reasons).reasons == reasons
    assert Coverage(CoverageState.READY, True, reasons=reasons).reasons == reasons


def test_text_bytes_and_dict_round_trip() -> None:
    text = GENERATION.to_json()
    for data in (text, text.encode(), GENERATION.to_dict(), json.loads(text)):
        restored = RankingGeneration.from_json(data)
        assert restored == GENERATION and restored.to_json() == text
    assert GENERATION.to_dict()["type"] == gen.TYPE_NAME == "RankingGeneration"
    assert GENERATION.to_dict()["schema_version"] == gen.SCHEMA_VERSION == 2
    assert RankingGeneration.type_name_static() == "RankingGeneration"
    with pytest.raises(TypeError):
        canonical_bytes(ROW)  # type: ignore[arg-type]


LITERAL = RankingGeneration(
    generation_id=GenerationId("ZZZT-\u00e9poch", 1, 0),
    session_date=date(2030, 1, 2),
    input_cutoff=0,
    ts_event=MAX,
    ts_init=MAX,
    known_at=1,
    method_version="m",
    profile_version="p",
    ranker_version="r",
    coverage=Coverage(
        CoverageState.UNKNOWN, False, reasons=(RankingReason("c", '\u4e2d\n"\U0001f600'),)
    ),
    status=GenerationStatus.SETTLED,
    rows=(
        RankingRow(
            "ZZZT1.TEST",
            score=D("1.50E+3"),
            rank=1,
            features=(RankingFeature("f", D("0.0100"), 2, 3, True, False),),
        ),
    ),
)
EXPECTED = (
    rb'{"coverage":{"complete":false,"expected_symbols":null,"invalid_symbols":null,'
    rb'"late_symbols":null,"missing_symbols":null,"observed_symbols":null,"reasons":[{"children":[],'
    rb'"code":"c","detail":"\u4e2d\n\"\ud83d\ude00"}],"state":"UNKNOWN"},"generation_id":'
    rb'{"previous_sequence":0,"producer_epoch":"ZZZT-\u00e9poch","sequence":1},"input_cutoff":0,'
    rb'"known_at":1,"method_version":"m","profile_version":"p","ranker_version":"r","rows":[{'
    rb'"completed":true,"features":[{"completed":false,"known_at":3,"name":"f","partial":true,'
    rb'"ts_event":2,"value":"0.0100"}],"focus_eligible":null,"instrument_id":null,"partial":false,'
    rb'"rank":1,"reasons":[],"score":"1.50E+3","symbol":"ZZZT1.TEST"}],"schema_version":2,'
    rb'"session_date":"2030-01-02","status":"SETTLED","ts_event":9223372036854775807,'
    rb'"ts_init":9223372036854775807,"type":"RankingGeneration"}'
)


def test_the_canonical_form_is_pinned_literally() -> None:
    assert canonical_bytes(LITERAL) == EXPECTED
    assert LITERAL.to_json() == EXPECTED.decode("ascii")
    assert generation_digest(LITERAL) == hashlib.sha256(EXPECTED).hexdigest()
    assert RankingGeneration.from_json(EXPECTED).coverage.reasons[0].detail == '\u4e2d\n"\U0001f600'


@pytest.mark.parametrize(
    "spelling", ["1.500", "1E+3", "-0", "0E-7", "1.2300E-10", "-12345678901234567890.123456789"]
)
def test_decimals_keep_their_spelling(spelling: str) -> None:
    record = replace(GENERATION, rows=(replace(ROW, score=D(spelling)),))
    assert f'"score":"{spelling}"' in record.to_json()
    assert str(RankingGeneration.from_json(record.to_json()).rows[0].score) == spelling


def test_canonical_bytes_are_ascii_and_escape_non_ascii_and_controls() -> None:
    text = "\u00e9\u4e2d\U0001f600\x00\x1f\u2028\ud800"
    record = replace(GENERATION, method_version=text)
    data = canonical_bytes(record)
    assert max(data) < 128
    assert rb'"method_version":"\u00e9\u4e2d\ud83d\ude00\u0000\u001f\u2028\ud800"' in data
    assert RankingGeneration.from_json(data).method_version == text


LEVELS: dict[str, tuple[Any, ...]] = {
    "top": (),
    "generation_id": ("generation_id",),
    "coverage": ("coverage",),
    "coverage.reason": ("coverage", "reasons", 0),
    "reason.child": ("coverage", "reasons", 0, "children", 0),
    "row": ("rows", 0),
    "feature": ("rows", 0, "features", 1),
    "row.reason": ("rows", 0, "reasons", 0),
}
OPTIONAL = {
    "generation_id": {"previous_sequence"},
    "coverage": set(COUNTS),
    "coverage.reason": {"detail"},
    "reason.child": {"detail"},
    "row": {"instrument_id", "score", "rank", "focus_eligible"},
    "feature": {"value", "ts_event", "known_at"},
}


def tree() -> Any:
    return json.loads(GENERATION.to_json())


def at(value: Any, path: Iterable[Any]) -> Any:
    for step in path:
        value = value[step]
    return value


def refused_both_ways(value: Any) -> None:
    with pytest.raises(ValueError):
        RankingGeneration.from_json(json.dumps(value))
    with pytest.raises(ValueError):
        RankingGeneration.from_json(value)


def _keys() -> list[tuple[str, str]]:
    return [(level, key) for level, path in LEVELS.items() for key in at(tree(), path)]


@pytest.mark.parametrize(("level", "key"), _keys())
def test_a_missing_required_key_is_refused_and_a_missing_optional_reads_null(
    level: str, key: str
) -> None:
    value = tree()
    del at(value, LEVELS[level])[key]
    if key not in OPTIONAL.get(level, ()):
        refused_both_ways(value)
        return
    for data in (json.dumps(value), value):
        assert at(RankingGeneration.from_json(data).to_dict(), LEVELS[level])[key] is None


@pytest.mark.parametrize("level", LEVELS)
def test_an_unknown_key_is_refused_at_each_level(level: str) -> None:
    value = tree()
    at(value, LEVELS[level])["zzz_unknown"] = None
    refused_both_ways(value)


def dumps_with_duplicate(
    value: Any, target: tuple[Any, ...] | None, path: tuple[Any, ...] = ()
) -> str:
    if isinstance(value, dict):
        items = list(value.items())
        if path == target:
            items.insert(0, items[0])
        parts = (
            json.dumps(k) + ":" + dumps_with_duplicate(v, target, (*path, k)) for k, v in items
        )
        return "{" + ",".join(parts) + "}"
    if isinstance(value, list):
        parts = (dumps_with_duplicate(v, target, (*path, i)) for i, v in enumerate(value))
        return "[" + ",".join(parts) + "]"
    return json.dumps(value)


@pytest.mark.parametrize("level", LEVELS)
def test_a_duplicate_key_is_refused_at_each_level(level: str) -> None:
    assert RankingGeneration.from_json(dumps_with_duplicate(tree(), None)) == GENERATION
    with pytest.raises(ValueError, match="duplicate"):
        RankingGeneration.from_json(dumps_with_duplicate(tree(), LEVELS[level]))


NUMBERS = ['"input_cutoff":10', '"sequence":7', '"rank":1', '"ts_event":3', '"schema_version":2']
TOKENS = ["10.0", "1e1", "1E+1", "-0.0", "NaN", "Infinity", "-Infinity"]


@pytest.mark.parametrize("token", TOKENS)
@pytest.mark.parametrize("where", NUMBERS)
def test_a_bare_number_that_is_not_an_integer_is_refused(where: str, token: str) -> None:
    text = GENERATION.to_json()
    assert text.count(where) == 1
    key = where.split(":")[0]
    with pytest.raises(ValueError):
        RankingGeneration.from_json(text.replace(where, f"{key}:{token}"))
    value = json.loads(text.replace(where, f"{key}:{token}"))  # the engine's dict path
    with pytest.raises(ValueError):
        RankingGeneration.from_json(value)


WRONG = [
    ((), "type", "ZZZT-Other"),
    *(((), "schema_version", v) for v in (1, "2", True)),
    *(((), "session_date", v) for v in (20300102, "2030-1-2", "20300102", "2030-01-02T00:00", "")),
    ((), "status", "BAD"),
    (("coverage",), "state", "BAD"),
    (("coverage",), "complete", "true"),
    *(((), "rows", v) for v in ({}, None, "x")),
    (("rows", 0), "features", "x"),
    (("rows", 0), "reasons", {}),
    (("coverage", "reasons", 0), "children", None),
    *((("rows", 0), "score", v) for v in (25, "", "ZZZT", "NaN", "Infinity", "sNaN")),
    (("rows", 0, "features", 1), "value", "-Infinity"),
    (("generation_id",), "sequence", "7"),
    (("generation_id",), "previous_sequence", 7),
    (("rows", 0), "rank", 0),
    ((), "generation_id", []),
]


@pytest.mark.parametrize(("path", "key", "bad"), WRONG, ids=lambda v: repr(v)[:24])
def test_a_wrong_type_or_value_is_refused(path: tuple[Any, ...], key: str, bad: Any) -> None:
    value = tree()
    at(value, path)[key] = bad
    refused_both_ways(value)


@pytest.mark.parametrize("data", ["", "[]", "null", '"x"', "ZZZT", b"\xff"])
def test_a_non_object_text_is_refused(data: str | bytes) -> None:
    with pytest.raises(ValueError):
        RankingGeneration.from_json(data)


@pytest.mark.parametrize("tail", [" x", "{}", ",", " 1", "]"])
def test_trailing_text_is_refused(tail: str) -> None:
    with pytest.raises(ValueError):
        RankingGeneration.from_json(GENERATION.to_json() + tail)
    assert RankingGeneration.from_json(GENERATION.to_json() + " \n") == GENERATION


PYTHON_ONLY = [
    *(((), "input_cutoff", v) for v in (10.0, D("10"), True)),
    ((), "rows", tuple(json.loads(GENERATION.to_json())["rows"])),
    ((), "session_date", date(2030, 1, 2)),
    (("rows", 0), "score", D("25")),
    (("rows", 0), "rank", 1.0),
    (("generation_id",), "sequence", float("nan")),
]


@pytest.mark.parametrize(("path", "key", "bad"), PYTHON_ONLY, ids=lambda v: repr(v)[:24])
def test_the_dict_path_refuses_python_values_json_cannot_carry(
    path: tuple[Any, ...], key: str, bad: Any
) -> None:
    value = tree()
    at(value, path)[key] = bad
    with pytest.raises(ValueError):
        RankingGeneration.from_json(value)


def reorder(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: reorder(v) for k, v in reversed(value.items())}
    if isinstance(value, list):
        return [reorder(v) for v in value]
    return value


def test_the_digest_ignores_input_key_order_and_whitespace() -> None:
    digest = generation_digest(GENERATION)
    for text in (json.dumps(reorder(tree())), json.dumps(tree(), indent=2)):
        assert generation_digest(RankingGeneration.from_json(text)) == digest
    assert generation_digest(RankingGeneration.from_json(reorder(tree()))) == digest


def single_field_changes() -> list[RankingGeneration]:
    g, row = GENERATION, ROW
    feature, reason = row.features[1], g.coverage.reasons[0]

    def with_row(**kw: Any) -> RankingGeneration:
        return replace(g, rows=(replace(row, **kw), *g.rows[1:]))

    def with_feature(**kw: Any) -> RankingGeneration:
        return with_row(features=(row.features[0], replace(feature, **kw)))

    def with_reason(**kw: Any) -> RankingGeneration:
        return replace(g, coverage=replace(g.coverage, reasons=(replace(reason, **kw),)))

    def ident(**kw: Any) -> RankingGeneration:
        return replace(g, generation_id=replace(g.generation_id, **kw))

    def cover(**kw: Any) -> RankingGeneration:
        return replace(g, coverage=replace(g.coverage, **kw))

    return [
        ident(producer_epoch="ZZZT-EPOCH2"),
        ident(sequence=8),
        ident(previous_sequence=None),
        replace(g, session_date=date(2030, 1, 3)),
        *(replace(g, **{f: 99}) for f in ("input_cutoff", "ts_event", "ts_init", "known_at")),
        *(
            replace(g, **{f: "ZZZT-x"})
            for f in ("method_version", "profile_version", "ranker_version")
        ),
        replace(g, status=GenerationStatus.SETTLED),
        replace(g, rows=g.rows[:1]),
        cover(state=CoverageState.DEGRADED),
        cover(complete=False),
        *(cover(**{f: None}) for f in COUNTS),
        cover(reasons=()),
        with_row(symbol="ZZZT3.TEST"),
        with_row(instrument_id=None),
        with_row(score=D("25")),
        with_row(rank=3),
        with_row(features=row.features[:1]),
        with_row(partial=True),
        with_row(completed=False),
        with_row(focus_eligible=None),
        with_row(reasons=()),
        with_feature(name="ZZZT-f3"),
        with_feature(value=D("1.5")),
        with_feature(ts_event=None),
        with_feature(known_at=5),
        with_feature(partial=False),
        with_feature(completed=True),
        with_reason(code="ZZZT-code2"),
        with_reason(detail=None),
        with_reason(children=()),
    ]


def test_the_digest_changes_with_any_single_field() -> None:
    digests = [generation_digest(GENERATION), *map(generation_digest, single_field_changes())]
    assert len(set(digests)) == len(digests) == 40


def test_the_module_imports_the_standard_library_only() -> None:
    source = Path(gen.__file__).read_text()
    names = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert names <= set(sys.stdlib_module_names) | {"__future__"}
    assert (Path(gen.__file__).parent / "__init__.py").read_bytes() == b""


def build_generations(m: Any, count: int = 512) -> list[Any]:
    """Seeded synthetic generations; the parity child runs this function's own source."""
    import random
    from datetime import date
    from decimal import Decimal

    texts = ("ZZZT", "\u00e9", "\u4e2d", "\U0001f600", "\x00\x1f", '\n\t"\\', "\u2028", "\ud800")
    decimals = ("0", "-0", "0.000", "1.2300", "1E+3", "-4.5E-7", "0E-9", "1e-30", "123456789.0123")
    edges = (0, 1, 2**53 + 1, 2**63 - 2, 2**63 - 1)
    rng = random.Random(20261111)  # noqa: S311 - a seeded fixture, not a secret

    def text() -> str:
        return rng.choice(texts) + str(rng.randrange(100))

    def maybe(make: Any) -> Any:
        return make() if rng.random() < 0.5 else None

    def int64(low: int = 0) -> int:
        if rng.random() < 0.5:
            return rng.choice([v for v in edges if v >= low])
        return rng.randrange(low, 2**63)

    def flag() -> bool:
        return rng.random() < 0.5

    def reasons(depth: int = 0) -> tuple[Any, ...]:
        return tuple(
            m.RankingReason(text(), maybe(text), reasons(depth + 1) if depth < 2 else ())
            for _ in range(rng.randrange(3))
        )

    states, statuses, out = list(m.CoverageState), list(m.GenerationStatus), []
    for i in range(count):
        rows = []
        for j in range(rng.choice((0, 1, 2, 5, 9))):
            features = tuple(
                m.RankingFeature(
                    n,
                    maybe(lambda: Decimal(rng.choice(decimals))),
                    maybe(int64),
                    maybe(int64),
                    flag(),
                    flag(),
                )
                for n in rng.sample(texts, rng.randrange(4))
            )
            rows.append(
                m.RankingRow(
                    f"ZZZT{j}.TEST" + rng.choice(("", "\u00e9")),
                    maybe(text),
                    maybe(lambda: Decimal(rng.choice(decimals))),
                    maybe(lambda: int64(1)),
                    features,
                    flag(),
                    flag(),
                    rng.choice((None, True, False)),
                    reasons(),
                )
            )
        sequence = int64(1)
        out.append(
            m.RankingGeneration(
                generation_id=m.GenerationId(
                    text(), sequence, maybe(lambda s=sequence: rng.randrange(s))
                ),
                session_date=date.fromordinal(rng.randrange(1, date.max.toordinal() + 1)),
                input_cutoff=int64(),
                ts_event=int64(),
                ts_init=int64(),
                known_at=int64(),
                method_version=text(),
                profile_version=text(),
                ranker_version=text(),
                coverage=m.Coverage(
                    states[i % len(states)], flag(), *(maybe(int64) for _ in range(5)), reasons()
                ),
                status=statuses[i % len(statuses)],
                rows=rows,
            )
        )
    return out


def digest_all(chunks: Iterable[bytes]) -> str:
    import hashlib

    total = hashlib.sha256()
    for chunk in chunks:
        total.update(hashlib.sha256(chunk).digest())
    return total.hexdigest()


def test_seeded_generations_round_trip_through_every_input() -> None:
    records = build_generations(gen)
    assert len(records) == 512
    assert {r.status for r in records} == set(GenerationStatus)
    assert {r.coverage.state for r in records} == set(CoverageState)
    assert {len(r.rows) for r in records} >= {0, 9}
    for record in records:
        text = record.to_json()
        for data in (text, text.encode(), json.loads(text)):
            assert RankingGeneration.from_json(data).to_json() == text


PRELUDE = "from collections.abc import Iterable\nfrom typing import Any\n"
CHILD = """
import json, os, sys
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.ranking import generation as ref
assert os.path.samefile(ref.__file__, os.path.join(SRC, "sextant", "ranking", "generation.py"))
records = build_generations(ref)
texts = [r.to_json() for r in records]
theirs = json.load(sys.stdin)
print("PARITY " + json.dumps({
    "count": len(records),
    "canonical": digest_all(ref.canonical_bytes(r) for r in records),
    "json": digest_all(t.encode() for t in texts),
    "reread": digest_all(ref.canonical_bytes(ref.RankingGeneration.from_json(t)) for t in theirs),
    "texts": texts,
}))
"""


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter() -> None:
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip("set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof")
    blob = (Path(src) / "sextant" / "ranking" / "generation.py").read_bytes()
    git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
    assert git_blob.hexdigest() == REFERENCE_BLOB
    records = build_generations(gen)
    ours = [r.to_json() for r in records]
    script = PRELUDE + inspect.getsource(build_generations) + inspect.getsource(digest_all) + CHILD
    done = subprocess.run(
        [python, "-I", "-c", script],
        input=json.dumps(ours),
        env={"MOVERS_REFERENCE_SRC": src},
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    line = next(x for x in done.stdout.splitlines() if x.startswith("PARITY "))
    theirs = json.loads(line.removeprefix("PARITY "))
    reread = digest_all(canonical_bytes(RankingGeneration.from_json(t)) for t in theirs["texts"])
    results = {
        "canonical_bytes": digest_all(map(canonical_bytes, records)) == theirs["canonical"],
        "to_json": digest_all(t.encode() for t in ours) == theirs["json"],
        "reference texts read here": reread == theirs["canonical"],
        "new texts read by the reference": theirs["reread"] == theirs["canonical"],
    }
    print(f"parity over {theirs['count']} generations: {results}")
    assert theirs["count"] == len(records) == 512
    assert all(results.values()), results
