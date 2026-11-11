"""Part 3c: the generation's Arrow form, its refusals and its parity with the reference.

Synthetic values only, no network. The reference is `src/sextant/ranking/generation.py` at
`06562bec0898a7203f4eaf69086ced83d2477e6b`; this process never imports it, and the `local` proof
runs it in a child interpreter as part 3b does. The engine view and registration are part 3d.
"""

import hashlib
import inspect
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from test_generation import GENERATION, MAX, PRELUDE, ROW, build_generations, digest_all

from sextant_movers.ranking import generation as gen
from sextant_movers.ranking.generation import (
    ARROW_SCHEMA,
    Coverage,
    CoverageState,
    GenerationId,
    RankingFeature,
    RankingGeneration,
    RankingReason,
    RankingRow,
    decode_arrow,
    encode_arrow,
    generation_digest,
)

SEEDED = build_generations(gen, 4096)  # its first 512 are part 3b's records


def encodes(record: RankingGeneration) -> bool:
    try:
        encode_arrow([record])
    except UnicodeEncodeError:
        return False
    return True


# Arrow UTF-8 cannot hold a lone surrogate the JSON form escapes, so a record with one in a
# string column is refused; inside reasons it travels as canonical JSON text.
CLEAN = [r for r in SEEDED if encodes(r)]
REFUSED = [r for r in SEEDED if not encodes(r)]
GOOD = encode_arrow([GENERATION])
UTC_NS = pa.timestamp("ns", tz="UTC")


def test_the_seeded_generations_round_trip_through_arrow() -> None:
    assert (len(CLEAN), len(REFUSED)) == (1290, 4096 - 1290)
    assert all("\\ud800" in r.to_json() for r in REFUSED)
    assert sum("\\ud800" in r.to_json() for r in CLEAN) == 739
    batch = encode_arrow(CLEAN)
    assert batch.schema.equals(ARROW_SCHEMA, check_metadata=True) and batch.num_rows == 1290
    back = decode_arrow(batch)
    assert back == CLEAN
    assert list(map(generation_digest, back)) == list(map(generation_digest, CLEAN))
    rows = [row for r in CLEAN for row in r.rows]
    assert {len(r.rows) for r in CLEAN} == {0, 1, 2, 5, 9}
    assert {r.generation_id.previous_sequence is None for r in CLEAN} == {True, False}
    assert {row.score is None for row in rows} == {row.focus_eligible is None for row in rows}
    assert {f.value is None for row in rows for f in row.features} == {True, False}


def test_zero_rows_optionals_int64_extremes_and_nested_reasons() -> None:
    deep = (RankingReason("a", None, (RankingReason("b", "x", (RankingReason("c"),)),)),)
    edge = replace(
        GENERATION,
        generation_id=GenerationId("ZZZT-EPOCH", MAX, MAX - 1),
        input_cutoff=0,
        ts_event=MAX,
        ts_init=0,
        known_at=MAX,
        coverage=Coverage(CoverageState.UNKNOWN, False, MAX, 0, None, MAX, 0, deep),
        rows=(RankingRow("ZZZT1.TEST", rank=MAX, features=(RankingFeature("f", None, 0, MAX),)),),
    )
    records = [edge, replace(GENERATION, rows=()), GENERATION]
    assert decode_arrow(encode_arrow(records)) == records
    assert encode_arrow([]).num_rows == 0 and decode_arrow(encode_arrow([])) == []
    row = encode_arrow([edge]).to_pylist()[0]
    assert (row["ts_event"], row["ts_init"], row["generation_id"]["sequence"]) == (MAX, 0, MAX)
    assert row["coverage"]["reasons"] == (
        '[{"children":[{"children":[{"children":[],"code":"c","detail":null}],'
        '"code":"b","detail":"x"}],"code":"a","detail":null}]'
    )
    assert row["rows"][0]["reasons"] == "[]" and row["rows"][0]["score"] is None


@pytest.mark.parametrize(
    "spelling", ["1.500", "1E+3", "-0", "0E-7", "1.2300E-10", "-12345678901234567890.123456789"]
)
def test_decimals_travel_as_their_text(spelling: str) -> None:
    feature = RankingFeature("f", D(spelling))
    record = replace(GENERATION, rows=(replace(ROW, score=D(spelling), features=(feature,)),))
    row = encode_arrow([record]).to_pylist()[0]["rows"][0]
    assert row["score"] == row["features"][0]["value"] == spelling
    back = decode_arrow(encode_arrow([record]))[0].rows[0]
    assert str(back.score) == str(back.features[0].value) == spelling


Edit = Callable[[pa.Field], list[pa.Field]]


def change(path: str, new: Edit, base: pa.Schema = ARROW_SCHEMA, metadata: Any = ...) -> pa.Schema:
    """`base` with the field at a dotted path (through structs and list items) edited."""

    def walk(items: list[pa.Field], names: list[str]) -> list[pa.Field]:
        out: list[pa.Field] = []
        for field in items:
            if field.name != names[0]:
                out.append(field)
            elif len(names) == 1:
                out.extend(new(field))
            else:
                listed = pa.types.is_list(field.type)
                struct = field.type.value_type if listed else field.type
                inner = walk([struct.field(i) for i in range(struct.num_fields)], names[1:])
                kind = pa.struct(inner)
                out.append(field.with_type(pa.list_(kind) if listed else kind))
        return out

    meta = base.metadata if metadata is ... else metadata
    return pa.schema(walk(list(base), path.split(".")), metadata=meta)


def to(kind: pa.DataType) -> Edit:
    return lambda field: [field.with_type(kind)]


def flip(field: pa.Field) -> list[pa.Field]:
    return [field.with_nullable(not field.nullable)]


def pylist(schema: pa.Schema, edit: Callable[[dict], None] = lambda row: None) -> pa.RecordBatch:
    rows = GOOD.to_pylist()
    for row in rows:
        edit(row)
    return pa.RecordBatch.from_pylist(rows, schema=schema)


def rename(row: dict) -> None:
    row["state"] = row.pop("status")


def rename_symbol(row: dict) -> None:
    for item in row["rows"]:
        item["ticker"] = item.pop("symbol")


EXTRA = pa.field("zzz_extra", pa.int64())
CASTS = {
    "status large_string": change("status", to(pa.large_string())),
    "status string_view": change("status", to(pa.string_view())),
    "ts_event float64": change("ts_event", to(pa.float64())),
    "ts_event timestamp": change("ts_event", to(UTC_NS)),
    "ts_init timestamp": change("ts_init", to(UTC_NS)),
    "input_cutoff naive timestamp": change("input_cutoff", to(pa.timestamp("ns"))),
    "session_date date32": change("session_date", to(pa.date32())),
    "session_date timestamp": change("session_date", to(pa.timestamp("ns"))),
    "sequence float64": change("generation_id.sequence", to(pa.float64())),
    "rank int32": change("rows.rank", to(pa.int32())),
    "score float64": change("rows.score", to(pa.float64())),
    "feature value float64": change("rows.features.value", to(pa.float64())),
    "row reasons large_string": change("rows.reasons", to(pa.large_string())),
    "status nullable": change("status", flip),
    "sequence nullable": change("generation_id.sequence", flip),
    "complete nullable": change("coverage.complete", flip),
    "feature partial nullable": change("rows.features.partial", flip),
    "no metadata": change("status", lambda f: [f], metadata=None),
    "other type": change("status", lambda f: [f], metadata={"type": "ZZZT-Other"}),
    "version 1": change(
        "status", lambda f: [f], metadata={"type": gen.TYPE_NAME, "schema_version": "1"}
    ),
    "extra metadata": change(
        "status", lambda f: [f], metadata={**ARROW_SCHEMA.metadata, b"zzz": b"1"}
    ),
}
BUILT = {
    "dropped status": lambda: pylist(change("status", lambda f: [])),
    "dropped row score": lambda: pylist(change("rows.score", lambda f: [])),
    "added column": lambda: pylist(change("status", lambda f: [f, EXTRA])),
    "added coverage field": lambda: pylist(change("coverage.state", lambda f: [f, EXTRA])),
    "added feature field": lambda: pylist(change("rows.features.name", lambda f: [f, EXTRA])),
    "renamed status": lambda: pylist(change("status", lambda f: [f.with_name("state")]), rename),
    "renamed symbol": lambda: pylist(
        change("rows.symbol", lambda f: [f.with_name("ticker")]), rename_symbol
    ),
}


@pytest.mark.parametrize("case", CASTS)
def test_decode_refuses_a_retyped_field_nullability_or_metadata(case: str) -> None:
    assert decode_arrow(GOOD) == [GENERATION]
    with pytest.raises(ValueError, match="unknown ranking Arrow schema"):
        decode_arrow(GOOD.cast(CASTS[case]))


@pytest.mark.parametrize("case", BUILT)
def test_decode_refuses_a_dropped_added_or_renamed_field(case: str) -> None:
    with pytest.raises(ValueError, match="unknown ranking Arrow schema"):
        decode_arrow(BUILT[case]())


def set_reasons(text: str, where: str) -> Callable[[dict], None]:
    def edit(row: dict) -> None:
        (row["coverage"] if where == "coverage" else row["rows"][0])["reasons"] = text

    return edit


TAMPERED = [
    '[{"children":[],"code":"a","code":"b","detail":null}]',
    '[{"children":[],"code":"a","detail":1.0}]',
    '[{"children":[],"code":"a","detail":NaN}]',
    '[{"children":[],"code":"a"}] x',
    '{"children":[],"code":"a"}',
    "",
]


@pytest.mark.parametrize("where", ["coverage", "row"])
@pytest.mark.parametrize("text", TAMPERED)
def test_decode_refuses_tampered_reasons(text: str, where: str) -> None:
    with pytest.raises(ValueError):
        decode_arrow(pylist(ARROW_SCHEMA, set_reasons(text, where)))


NULLS = {
    "method_version": lambda row: row.update(method_version=None),
    "ts_event": lambda row: row.update(ts_event=None),
    "coverage": lambda row: row.update(coverage=None),
    "rows": lambda row: row.update(rows=None),
    "coverage reasons": lambda row: row["coverage"].update(reasons=None),
    "a row": lambda row: row["rows"].append(None),
    "row reasons": lambda row: row["rows"][0].update(reasons=None),
    "row symbol": lambda row: row["rows"][0].update(symbol=None),
}


@pytest.mark.parametrize("case", NULLS)
def test_decode_refuses_a_null_where_the_schema_says_not_null(case: str) -> None:
    """Arrow keeps a null the schema marks not-null; it is refused, never a TypeError."""
    with pytest.raises(ValueError):
        decode_arrow(pylist(ARROW_SCHEMA, NULLS[case]))


def row_digest(batch: pa.RecordBatch) -> str:
    rows = batch.to_pylist()
    return digest_all(json.dumps(r, sort_keys=True, separators=(",", ":")).encode() for r in rows)


def write_stream(batch: pa.RecordBatch, path: Path) -> None:
    with pa.ipc.new_stream(str(path), batch.schema) as writer:
        writer.write_batch(batch)


def read_stream(path: Path) -> pa.RecordBatch:
    with pa.ipc.open_stream(str(path)) as reader:
        return reader.read_next_batch()


ARROW_PRELUDE = PRELUDE + "import json\nfrom pathlib import Path\n\nimport pyarrow as pa\n"
CHILD = """
import os, sys
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.ranking import generation as ref
assert os.path.samefile(ref.__file__, os.path.join(SRC, "sextant", "ranking", "generation.py"))
seeded = build_generations(ref, 4096)
clean = []
for r in seeded:
    try:
        ref.encode_arrow([r])
        clean.append(r)
    except UnicodeEncodeError:
        pass
refused = len(seeded) - len(clean)
batch = ref.encode_arrow(clean)
tmp = Path(sys.argv[1])
write_stream(batch, tmp / "reference.arrows")
print("PARITY " + json.dumps({
    "pyarrow": pa.__version__,
    "schema": str(ref.ARROW_SCHEMA),
    "metadata": {k.decode(): v.decode() for k, v in ref.ARROW_SCHEMA.metadata.items()},
    "count": len(clean),
    "refused": refused,
    "rows": row_digest(batch),
    "ours": [ref.generation_digest(r) for r in ref.decode_arrow(read_stream(tmp / "ours.arrows"))],
}))
"""
PARITY_NEEDS = (
    "set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof; the second "
    "names the reference's own interpreter because its module imports the engine and pyarrow "
    "at its top, at the reference's versions"
)


@pytest.mark.local
def test_arrow_parity_with_the_reference_in_a_child_interpreter(tmp_path: Path) -> None:
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip(PARITY_NEEDS)
    batch = encode_arrow(CLEAN)
    write_stream(batch, tmp_path / "ours.arrows")
    parts = (build_generations, digest_all, row_digest, write_stream, read_stream)
    script = ARROW_PRELUDE + "".join(map(inspect.getsource, parts)) + CHILD
    done = subprocess.run(
        [python, "-I", "-c", script, str(tmp_path)],
        env={"MOVERS_REFERENCE_SRC": src, "PATH": os.environ.get("PATH", os.defpath)},
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    line = next(x for x in done.stdout.splitlines() if x.startswith("PARITY "))
    theirs = json.loads(line.removeprefix("PARITY "))
    expected = list(map(generation_digest, CLEAN))
    metadata = {k.decode(): v.decode() for k, v in ARROW_SCHEMA.metadata.items()}
    results = {
        "(a) schema": theirs["schema"] == str(ARROW_SCHEMA) and theirs["metadata"] == metadata,
        "(b) rows": theirs["rows"] == row_digest(batch),
        "(c) reference stream read here": list(
            map(generation_digest, decode_arrow(read_stream(tmp_path / "reference.arrows")))
        )
        == expected,
        "(c) this stream read by the reference": theirs["ours"] == expected,
        "surrogates refused": theirs["refused"] == len(REFUSED),
    }
    print(f"pyarrow: here {pa.__version__}, reference {theirs['pyarrow']}")
    print(f"arrow parity over {theirs['count']} generations: {results}")
    assert theirs["count"] == len(CLEAN) == 1290
    assert all(results.values()), results


def witness(directory: Path) -> int:
    """Each `*.json` is one generation text: it reads back to itself and keeps its digest."""
    paths = sorted(directory.glob("*.json"))
    for path in paths:
        text = path.read_text(encoding="utf-8")
        record = RankingGeneration.from_json(text)
        assert record.to_json() == text
        back = decode_arrow(encode_arrow([record]))
        assert list(map(generation_digest, back)) == [hashlib.sha256(text.encode()).hexdigest()]
    return len(paths)


def test_the_parity_witness_over_synthetic_files(tmp_path: Path) -> None:
    for i, record in enumerate(CLEAN[:32]):
        (tmp_path / f"{i:03}.json").write_text(record.to_json(), encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("x")
    assert witness(tmp_path) == 32


@pytest.mark.local
def test_the_parity_witness_over_the_private_directory() -> None:
    directory = os.environ.get("MOVERS_PARITY_DIR")
    if not directory:
        pytest.skip("set MOVERS_PARITY_DIR to run the parity witness")
    print(f"parity witness: {witness(Path(directory))} generations")
