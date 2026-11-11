"""Parts 3c and 3d: the generation's Arrow form, its engine view and registration, the bus trip.

Synthetic values only, no network. The engine is nautechsystems/nautilus_trader v2.0.0rc8
`d53849b216e20f2c122ca01b4dd0da377638b186`; line numbers below are at that pin. The reference is
`src/sextant/ranking/generation.py` at `06562bec0898a7203f4eaf69086ced83d2477e6b`; this process
never imports it, and the `local` proof runs it in a child interpreter as part 3b does.
"""

import hashlib
import inspect
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from nautilus_trader.backtest import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common import DataActor
from nautilus_trader.model import CustomData, DataType
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
    register_ranking_generation,
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
    both = {(row.score is None, row.focus_eligible is None) for row in rows}
    assert both == {(True, True), (True, False), (False, True), (False, False)}
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


@pytest.mark.parametrize("where", ["coverage", "row"])
def test_decode_reads_reasons_strictly_not_by_their_spelling(where: str) -> None:
    """As the reference: reasons text is refused for a duplicate key, a non-integer number or a
    wrong shape, not for key order or whitespace, and the next encode writes it canonically."""
    text = '[ {"detail": null, "code": "a", "children": []} ]'
    back = decode_arrow(pylist(ARROW_SCHEMA, set_reasons(text, where)))[0]
    holder = back.coverage if where == "coverage" else back.rows[0]
    assert holder.reasons == (RankingReason("a"),)
    row = encode_arrow([back]).to_pylist()[0]
    spelled = row["coverage"] if where == "coverage" else row["rows"][0]
    assert spelled["reasons"] == '[{"children":[],"code":"a","detail":null}]'


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


def not_null_paths(kind: pa.DataType, prefix: str) -> list[str]:
    """Every not-null field below a column, through structs and list items."""
    if pa.types.is_list(kind):
        return not_null_paths(kind.value_type, prefix)
    if not pa.types.is_struct(kind):
        return []
    paths = []
    for field in (kind.field(i) for i in range(kind.num_fields)):
        path = f"{prefix}.{field.name}"
        paths += ([path] if not field.nullable else []) + not_null_paths(field.type, path)
    return paths


NESTED = [p for column in ARROW_SCHEMA for p in not_null_paths(column.type, column.name)]


def set_null(path: str) -> Callable[[dict], None]:
    def edit(row: dict) -> None:
        *parents, leaf = path.split(".")
        node: Any = row
        for name in parents:
            node = node[name][0] if isinstance(node[name], list) else node[name]
        node[leaf] = None

    return edit


@pytest.mark.parametrize("path", NESTED)
def test_a_null_in_a_nested_not_null_field_is_refused(path: str) -> None:
    """The top-level guard cannot see these (a child array's nulls are not its column's); the
    strict reader refuses each, so every nested not-null field of the schema is listed here."""
    batch = pylist(ARROW_SCHEMA, set_null(path))
    assert not any(column.null_count for column in batch.columns)
    with pytest.raises(ValueError):
        decode_arrow(batch)


def test_the_nested_not_null_fields_are_all_listed() -> None:
    assert len(NESTED) == 13 and "rows.features.name" in NESTED and "coverage.reasons" in NESTED


# The engine's own view and the shapes rc8 hands the callbacks. Encode is called on the first
# item with the list (`crates/model/src/python/data/mod.rs:497-509`); decode is called on the
# class with a str->str dict and a batch imported from a struct array, so the schema metadata is
# gone (`:561-588`). A catalog query (`crates/persistence/src/common/custom.rs`) was measured
# handing back top-level strings as string_view and the data type's metadata beside `type_name`.
ENGINE = RankingGeneration.arrow_schema_py()
TN = {"type_name": gen.TYPE_NAME}


def handed(batch: pa.RecordBatch, views: bool = False) -> pa.RecordBatch:
    out = pa.RecordBatch.from_struct_array(batch.to_struct_array())
    if views:
        view = [f.with_type(pa.string_view()) if f.type == pa.string() else f for f in out.schema]
        out = out.cast(pa.schema(view))
    return out


def test_the_callbacks_as_rc8_calls_them() -> None:
    records = CLEAN[:64]
    batch = records[0].encode_record_batch_py(records)
    assert batch.schema.equals(ENGINE, check_metadata=True)
    view = change("ts_event", to(UTC_NS), change("ts_init", to(UTC_NS)))
    assert ENGINE.equals(view, check_metadata=True)
    assert handed(batch).schema.metadata is None
    for meta, views in ((TN, False), ({**TN, "zzz": "1"}, True)):
        assert RankingGeneration.decode_record_batch_py(meta, handed(batch, views)) == records


NESTED_VIEW = change("rows.symbol", to(pa.string_view()), ENGINE)
ENGINE_REFUSALS = {
    "no type_name": ({}, lambda b: b),
    "other type_name": ({"type_name": "ZZZT-Other"}, lambda b: b),
    "data_type column": (TN, lambda b: b.append_column("data_type", pa.array(["x"] * len(b)))),
    "large_string": (TN, lambda b: b.cast(change("status", to(pa.large_string()), ENGINE))),
    "nested string_view": (TN, lambda b: b.cast(NESTED_VIEW)),
    "int64 clocks": (TN, lambda b: handed(encode_arrow([GENERATION]))),
    "dropped column": (TN, lambda b: b.drop_columns(["status"])),
}


@pytest.mark.parametrize("case", ENGINE_REFUSALS)
def test_the_decode_callback_accepts_nothing_wider(case: str) -> None:
    meta, edit = ENGINE_REFUSALS[case]
    batch = handed(GENERATION.encode_record_batch_py([GENERATION]))
    with pytest.raises(ValueError):
        RankingGeneration.decode_record_batch_py(meta, edit(batch))


# The JSON envelope the engine's own tests read back (`python/tests/unit/model/test_data.py`).
REGISTER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from nautilus_trader.model import deserialize_custom_from_json
from sextant_movers.ranking import generation as g
envelope = sys.stdin.read().encode()
def read():
    try:
        return g.generation_digest(deserialize_custom_from_json(g.TYPE_NAME, envelope).data)
    except ValueError as error:
        return str(error)
before = read()
dt = g.register_ranking_generation()
print(json.dumps([before, read(), dt.type_name, dt.topic, dt.metadata, dt.identifier]))
"""


def test_importing_registers_nothing_and_the_call_registers_it() -> None:
    data_type = {"type_name": gen.TYPE_NAME, "metadata": {}, "identifier": None}
    payload = json.loads(GENERATION.to_json())
    envelope = json.dumps({"type": gen.TYPE_NAME, "data_type": data_type, "payload": payload})
    src = str(Path(gen.__file__).parents[2])
    done = subprocess.run(
        [sys.executable, "-I", "-c", REGISTER, src],
        input=envelope,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    before, after, *names = json.loads(done.stdout.splitlines()[-1])
    assert "is not registered" in before
    assert after == generation_digest(GENERATION)
    assert names == [gen.TYPE_NAME, gen.TYPE_NAME, None, None]


@pytest.fixture(scope="module")
def data_type() -> DataType:
    return register_ranking_generation()


def test_the_engine_json_envelope_round_trips(data_type: DataType) -> None:
    wrapped = CustomData(data_type, GENERATION)
    back = CustomData.from_json_bytes(bytes(wrapped.to_json_bytes()))
    assert back.data == GENERATION and back.data_type == data_type
    assert generation_digest(back.data) == generation_digest(GENERATION)


def test_the_bus_carries_a_generation_between_two_actors(data_type: DataType) -> None:
    """The engine's own shape: subscribe in `on_start`, receive in `on_data`
    (`python/tests/unit/backtest/test_backtest_engine_custom_data.py:56-66`); publish in
    `on_start`, then `run` and `dispose` (`python/tests/unit/common/test_actor.py:1509-1532`)."""
    sent, received = CLEAN[:3], []

    class Consumer(DataActor):
        def on_start(self) -> None:
            self.subscribe_data(data_type)

        def on_data(self, data: Any) -> None:
            received.append(data)

    class Publisher(DataActor):
        def on_start(self) -> None:
            other = DataType("ZZZT-Other")
            self.publish_data(other, CustomData(other, GENERATION))
            for record in sent:
                self.publish_data(data_type, CustomData(data_type, record))

    engine = BacktestEngine(BacktestEngineConfig(bypass_logging=True, run_analysis=False))
    try:
        engine.add_actor(Consumer())
        engine.add_actor(Publisher())
        engine.run()
    finally:
        engine.dispose()
    assert [type(d) for d in received] == [CustomData] * 3
    assert all(d.data_type == data_type for d in received)
    assert all(d.data is r for d, r in zip(received, sent, strict=True))
    assert [generation_digest(d.data) for d in received] == list(map(generation_digest, sent))


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
import nautilus_trader
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
    "versions": [pa.__version__, nautilus_trader.__version__],
    "schema": str(ref.ARROW_SCHEMA),
    "metadata": {k.decode(): v.decode() for k, v in ref.ARROW_SCHEMA.metadata.items()},
    "engine": str(ref._RC7_ARROW_SCHEMA),
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
        "(a) engine view": theirs["engine"] == str(ENGINE),
        "(b) rows": theirs["rows"] == row_digest(batch),
        "(c) reference stream read here": list(
            map(generation_digest, decode_arrow(read_stream(tmp_path / "reference.arrows")))
        )
        == expected,
        "(c) this stream read by the reference": theirs["ours"] == expected,
        "surrogates refused": theirs["refused"] == len(REFUSED),
    }
    here = [pa.__version__, sys.modules["nautilus_trader"].__version__]
    print(f"pyarrow, engine: here {here}, reference {theirs['versions']}")
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
