"""The ranking generation: immutable records, one canonical digest, a strict reader, its Arrow form.

Behaviour, wire text, digest bytes and the Arrow schema are the reference's
(`src/sextant/ranking/generation.py` at `06562bec0898a7203f4eaf69086ced83d2477e6b`). The class is
the hand-written custom data of nautechsystems/nautilus_trader v2.0.0rc8
`d53849b216e20f2c122ca01b4dd0da377638b186` (`docs/concepts/data/index.md:691-693`); its
`customdataclass` maps only str, bool, float, int, bytes, dict and InstrumentId fields
(`python/nautilus_trader/model/custom.py:195-216`), so it cannot carry Decimals or nested records.
A Decimal travels as its `str()` text, never normalised; RFC 8785 is rejected in the manifest.

The reader refuses duplicate keys, non-integer numbers, missing or unknown keys and wrong types.
A parsed mapping (the engine's form, `crates/model/src/python/data/mod.rs:465-475`) gets the same
type and range checks, but it cannot show duplicate keys, number spelling beyond int or float,
trailing text, key order, whitespace or escapes. On both paths a Decimal string the constructor
accepts in a non-canonical spelling is re-spelled by `str()`, as the reference does.

Engine lines read at that pin: registration takes the schema from `arrow_schema_py()` and checks
`type_name_static`, `from_json` and `decode_record_batch_py` (`mod.rs:648-720`); the engine calls
`encode_record_batch_py(items)` on the first item (`:485-535`) and the class's
`decode_record_batch_py(metadata, batch)` (`:548-603`); a catalog write refuses `ts_event` or
`ts_init` other than timestamp("ns", tz="UTC") (`crates/persistence/src/common/custom.rs:198-224`),
so only the engine view casts those two columns. Importing this module registers nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, ClassVar

import pyarrow as pa
from nautilus_trader.model import DataType, register_custom_data_class

SCHEMA_VERSION = 2
TYPE_NAME = "RankingGeneration"
_INT64_MAX = 2**63 - 1
_COUNTS = (
    "expected_symbols",
    "observed_symbols",
    "missing_symbols",
    "late_symbols",
    "invalid_symbols",
)


class GenerationStatus(StrEnum):
    """PROVISIONAL is decision evidence; SETTLED is a later audit observation, not finality."""

    PROVISIONAL = "PROVISIONAL"
    SETTLED = "SETTLED"


class CoverageState(StrEnum):
    UNKNOWN = "UNKNOWN"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"


def _check_text(value: Any, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{name} must be a nonempty string")


def _check_int64(value: Any, name: str, *, optional: bool = False, minimum: int = 0) -> None:
    if optional and value is None:
        return
    if type(value) is not int or value < minimum or value > _INT64_MAX:
        raise ValueError(f"{name} must be an int64 >= {minimum}")


def _check_bool(value: Any, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")


def _check_decimal(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("numeric values must be finite Decimal or null, never float")


def _as_tuple[T](value: Sequence[T], cls: type[T]) -> tuple[T, ...]:
    if not isinstance(value, tuple | list) or not all(isinstance(item, cls) for item in value):
        raise ValueError(f"expected a sequence of {cls.__name__}")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class GenerationId:
    """Sequence lineage within one producer epoch; a null predecessor means none."""

    producer_epoch: str
    sequence: int
    previous_sequence: int | None = None

    def __post_init__(self) -> None:
        _check_text(self.producer_epoch, "producer_epoch")
        _check_int64(self.sequence, "sequence")
        _check_int64(self.previous_sequence, "previous_sequence", optional=True)
        if self.previous_sequence is not None and self.previous_sequence >= self.sequence:
            raise ValueError("previous_sequence must precede sequence within the producer epoch")


@dataclass(frozen=True, slots=True)
class RankingReason:
    """A reason and its children, kept in the producer's order."""

    code: str
    detail: str | None = None
    children: tuple[RankingReason, ...] = ()

    def __post_init__(self) -> None:
        _check_text(self.code, "reason code")
        _check_text(self.detail, "reason detail", optional=True)
        object.__setattr__(self, "children", _as_tuple(self.children, RankingReason))


@dataclass(frozen=True, slots=True)
class Coverage:
    """Coverage evidence; an unknown count stays null, never zero."""

    state: CoverageState
    complete: bool
    expected_symbols: int | None = None
    observed_symbols: int | None = None
    missing_symbols: int | None = None
    late_symbols: int | None = None
    invalid_symbols: int | None = None
    reasons: tuple[RankingReason, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, CoverageState):
            raise ValueError("unknown coverage state")
        _check_bool(self.complete, "coverage.complete")
        for name in _COUNTS:
            _check_int64(getattr(self, name), name, optional=True)
        object.__setattr__(self, "reasons", _as_tuple(self.reasons, RankingReason))


@dataclass(frozen=True, slots=True)
class RankingFeature:
    """One numeric input; its Decimal keeps its exact spelling."""

    name: str
    value: Decimal | None = None
    ts_event: int | None = None
    known_at: int | None = None
    partial: bool = False
    completed: bool = True

    def __post_init__(self) -> None:
        _check_text(self.name, "feature name")
        _check_decimal(self.value)
        _check_int64(self.ts_event, "feature.ts_event", optional=True)
        _check_int64(self.known_at, "feature.known_at", optional=True)
        _check_bool(self.partial, "feature.partial")
        _check_bool(self.completed, "feature.completed")


@dataclass(frozen=True, slots=True)
class RankingRow:
    """One symbol's row; features are held sorted by name, reasons in producer order."""

    symbol: str
    instrument_id: str | None = None
    score: Decimal | None = None
    rank: int | None = None
    features: tuple[RankingFeature, ...] = ()
    partial: bool = False
    completed: bool = True
    focus_eligible: bool | None = None
    reasons: tuple[RankingReason, ...] = ()

    def __post_init__(self) -> None:
        _check_text(self.symbol, "symbol")
        _check_text(self.instrument_id, "instrument_id", optional=True)
        _check_decimal(self.score)
        _check_int64(self.rank, "rank", optional=True, minimum=1)
        _check_bool(self.partial, "row.partial")
        _check_bool(self.completed, "row.completed")
        _check_bool(self.focus_eligible, "focus_eligible", optional=True)
        features = _as_tuple(self.features, RankingFeature)
        if len({feature.name for feature in features}) != len(features):
            raise ValueError("duplicate feature name")
        ordered = tuple(sorted(features, key=lambda feature: feature.name))
        object.__setattr__(self, "features", ordered)
        object.__setattr__(self, "reasons", _as_tuple(self.reasons, RankingReason))


def _row_order(row: RankingRow) -> tuple[bool, int, str, str]:
    """Ranked rows first by rank, unranked after; then symbol and instrument id."""
    return row.rank is None, row.rank or 0, row.symbol, row.instrument_id or ""


@dataclass(frozen=True, slots=True, kw_only=True)
class RankingGeneration:
    """One published generation: identity, clocks, versions, coverage, status and ranked rows."""

    generation_id: GenerationId
    session_date: date
    input_cutoff: int
    ts_event: int
    ts_init: int
    known_at: int
    method_version: str
    profile_version: str
    ranker_version: str
    coverage: Coverage
    status: GenerationStatus
    rows: tuple[RankingRow, ...] = ()

    schema_version: ClassVar[int] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.generation_id, GenerationId) or not isinstance(
            self.coverage, Coverage
        ):
            raise ValueError("generation id and coverage must be immutable records")
        if type(self.session_date) is not date:
            raise ValueError("session_date must be a date")
        for name in ("input_cutoff", "ts_event", "ts_init", "known_at"):
            _check_int64(getattr(self, name), name)
        for name in ("method_version", "profile_version", "ranker_version"):
            _check_text(getattr(self, name), name)
        if not isinstance(self.status, GenerationStatus):
            raise ValueError("unknown generation status")
        rows = _as_tuple(self.rows, RankingRow)
        if len({row.symbol for row in rows}) != len(rows):
            raise ValueError("duplicate symbol identity")
        object.__setattr__(self, "rows", tuple(sorted(rows, key=_row_order)))

    def to_dict(self) -> dict[str, Any]:
        return {"type": TYPE_NAME, "schema_version": SCHEMA_VERSION, **_to_wire(self)}

    def to_json(self) -> str:
        return canonical_bytes(self).decode("utf-8")

    @classmethod
    def from_json(cls, data: bytes | str | Mapping[str, Any]) -> RankingGeneration:
        return _read_generation(_strict_loads(data) if isinstance(data, str | bytes) else data)

    @classmethod
    def type_name_static(cls) -> str:
        return TYPE_NAME

    @classmethod
    def arrow_schema_py(cls) -> pa.Schema:
        return _ENGINE_ARROW_SCHEMA

    def encode_record_batch_py(self, items: Sequence[RankingGeneration]) -> pa.RecordBatch:
        return encode_arrow(items).cast(_ENGINE_ARROW_SCHEMA)

    @classmethod
    def decode_record_batch_py(
        cls, metadata: Mapping[str, str], batch: pa.RecordBatch
    ) -> list[RankingGeneration]:
        return decode_arrow(_from_engine_batch(metadata, batch))


def _to_wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        _check_decimal(value)
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if type(value) is date:
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_to_wire(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    raise ValueError(f"unsupported wire value: {type(value).__name__}; floats are forbidden")


def _dumps(value: Any) -> bytes:
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return text.encode()


def canonical_bytes(record: RankingGeneration) -> bytes:
    """The one canonical payload: sorted keys, explicit nulls, ranked rows, ASCII only.

    It never holds a digest, a mutable clock or transport data.
    """
    if not isinstance(record, RankingGeneration):
        raise TypeError("expected RankingGeneration")
    return _dumps(record.to_dict())


def generation_digest(record: RankingGeneration) -> str:
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def _refuse_number(token: str) -> Any:
    raise ValueError(f"floating point/nonfinite JSON number forbidden: {token}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_loads(data: bytes | str) -> Any:
    return json.loads(
        data,
        parse_float=_refuse_number,
        parse_constant=_refuse_number,
        object_pairs_hook=_object_without_duplicates,
    )


def _fields(value: Any, required: set[str], optional: Set[str] = frozenset()) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError("missing or unknown record fields")
    keys = value.keys()
    if not required <= keys or keys - required - optional:
        raise ValueError("missing or unknown record fields")
    return dict(value)


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected JSON array")
    return value


def _read_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ValueError("Decimal wire values must be strings or null")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid Decimal string") from error
    _check_decimal(result)
    return result


def _read_reason(value: Any) -> RankingReason:
    item = _fields(value, {"code", "children"}, frozenset({"detail"}))
    children = tuple(_read_reason(child) for child in _array(item["children"]))
    return RankingReason(item["code"], item.get("detail"), children)


def _read_reasons(value: Any) -> tuple[RankingReason, ...]:
    return tuple(_read_reason(item) for item in _array(value))


_FEATURE_OPTIONAL = frozenset({"value", "ts_event", "known_at"})
_ROW_OPTIONAL = frozenset({"instrument_id", "score", "rank", "focus_eligible"})


def _read_feature(value: Any) -> RankingFeature:
    item = _fields(value, {"name", "partial", "completed"}, _FEATURE_OPTIONAL)
    return RankingFeature(
        name=item["name"],
        value=_read_decimal(item.get("value")),
        ts_event=item.get("ts_event"),
        known_at=item.get("known_at"),
        partial=item["partial"],
        completed=item["completed"],
    )


def _read_row(value: Any) -> RankingRow:
    required = {"symbol", "features", "partial", "completed", "reasons"}
    item = _fields(value, required, _ROW_OPTIONAL)
    return RankingRow(
        symbol=item["symbol"],
        instrument_id=item.get("instrument_id"),
        score=_read_decimal(item.get("score")),
        rank=item.get("rank"),
        features=tuple(_read_feature(feature) for feature in _array(item["features"])),
        partial=item["partial"],
        completed=item["completed"],
        focus_eligible=item.get("focus_eligible"),
        reasons=_read_reasons(item["reasons"]),
    )


def _read_session_date(value: Any) -> date:
    _check_text(value, "session_date")
    session = date.fromisoformat(value)
    if session.isoformat() != value:
        raise ValueError("session_date must be YYYY-MM-DD")
    return session


def _read_generation(value: Any) -> RankingGeneration:
    names = {field.name for field in fields(RankingGeneration)}
    item = _fields(value, {"type", "schema_version", *names})
    version = item["schema_version"]
    if item["type"] != TYPE_NAME or type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError("unknown ranking schema version or type")
    identity = _fields(item["generation_id"], {"producer_epoch", "sequence"}, {"previous_sequence"})
    coverage = _fields(item["coverage"], {"state", "complete", "reasons"}, frozenset(_COUNTS))
    session = _read_session_date(item["session_date"])
    return RankingGeneration(
        generation_id=GenerationId(**identity),
        session_date=session,
        input_cutoff=item["input_cutoff"],
        ts_event=item["ts_event"],
        ts_init=item["ts_init"],
        known_at=item["known_at"],
        method_version=item["method_version"],
        profile_version=item["profile_version"],
        ranker_version=item["ranker_version"],
        coverage=Coverage(
            **{
                **coverage,
                "state": CoverageState(coverage["state"]),
                "reasons": _read_reasons(coverage["reasons"]),
            }
        ),
        status=GenerationStatus(item["status"]),
        rows=tuple(_read_row(row) for row in _array(item["rows"])),
    )


# Recursive reasons stay one canonical JSON string column at each boundary: an Arrow struct
# cannot be recursive, and neither a depth cap nor a lossy flattening is taken.
_COUNT_FIELDS = [pa.field(name, pa.int64()) for name in _COUNTS]
_FEATURE = pa.struct(
    [
        pa.field("name", pa.string(), nullable=False),
        pa.field("value", pa.string()),
        pa.field("ts_event", pa.int64()),
        pa.field("known_at", pa.int64()),
        pa.field("partial", pa.bool_(), nullable=False),
        pa.field("completed", pa.bool_(), nullable=False),
    ]
)
_ROW = pa.struct(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("instrument_id", pa.string()),
        pa.field("score", pa.string()),
        pa.field("rank", pa.int64()),
        pa.field("features", pa.list_(_FEATURE), nullable=False),
        pa.field("partial", pa.bool_(), nullable=False),
        pa.field("completed", pa.bool_(), nullable=False),
        pa.field("focus_eligible", pa.bool_()),
        pa.field("reasons", pa.string(), nullable=False),
    ]
)
_IDENTITY = pa.struct(
    [
        pa.field("producer_epoch", pa.string(), nullable=False),
        pa.field("sequence", pa.int64(), nullable=False),
        pa.field("previous_sequence", pa.int64()),
    ]
)
_COVERAGE = pa.struct(
    [
        pa.field("state", pa.string(), nullable=False),
        pa.field("complete", pa.bool_(), nullable=False),
        *_COUNT_FIELDS,
        pa.field("reasons", pa.string(), nullable=False),
    ]
)
_TOP = (
    ("type", pa.string()),
    ("schema_version", pa.int64()),
    ("generation_id", _IDENTITY),
    ("session_date", pa.string()),
    ("input_cutoff", pa.int64()),
    ("ts_event", pa.int64()),
    ("ts_init", pa.int64()),
    ("known_at", pa.int64()),
    ("method_version", pa.string()),
    ("profile_version", pa.string()),
    ("ranker_version", pa.string()),
    ("coverage", _COVERAGE),
    ("status", pa.string()),
    ("rows", pa.list_(_ROW)),
)
ARROW_SCHEMA = pa.schema(
    [pa.field(name, kind, nullable=False) for name, kind in _TOP],
    metadata={b"type": TYPE_NAME.encode(), b"schema_version": str(SCHEMA_VERSION).encode()},
)

# The engine's catalog needs `ts_event` and `ts_init` as timestamp("ns", tz="UTC"); this view
# casts only those two columns from and to the int64 nanoseconds above.
_ENGINE_ARROW_SCHEMA = pa.schema(
    [
        pa.field(field.name, pa.timestamp("ns", tz="UTC"), nullable=False)
        if field.name in ("ts_event", "ts_init")
        else field
        for field in ARROW_SCHEMA
    ],
    metadata=ARROW_SCHEMA.metadata,
)


def encode_arrow(records: Sequence[RankingGeneration]) -> pa.RecordBatch:
    rows = []
    for record in records:
        row = record.to_dict()
        row["coverage"]["reasons"] = _dumps(row["coverage"]["reasons"]).decode()
        for item in row["rows"]:
            item["reasons"] = _dumps(item["reasons"]).decode()
        rows.append(row)
    return pa.RecordBatch.from_pylist(rows, schema=ARROW_SCHEMA)


def _load_reasons(holder: Any) -> None:
    if not isinstance(holder, dict) or not isinstance(holder.get("reasons"), str):
        raise ValueError("reasons must be JSON text")
    holder["reasons"] = _strict_loads(holder["reasons"])


def decode_arrow(batch: pa.RecordBatch) -> list[RankingGeneration]:
    """Arrow keeps a null in a not-null field. A top-level null is refused here; the strict reader
    refuses one nested in a struct or a list, as a child's nulls are not its column's. Reasons
    text is read strictly (no duplicate key or non-integer number) and must have the reasons
    shape; it is not required to be canonically spelled, as the reference does not require it."""
    if not batch.schema.equals(ARROW_SCHEMA, check_metadata=True):
        raise ValueError("unknown ranking Arrow schema")
    if any(column.null_count for column in batch.columns):
        raise ValueError("null in a required ranking column")
    records = []
    for row in batch.to_pylist():
        _load_reasons(row["coverage"])
        for item in row["rows"]:
            _load_reasons(item)
        records.append(RankingGeneration.from_json(row))
    return records


def _from_engine_batch(metadata: Mapping[str, str], batch: pa.RecordBatch) -> pa.RecordBatch:
    """The engine view as encoded, or as the rc8 catalog query hands it back: no `data_type`
    column, `type_name` in the metadata, top-level strings as string_view. Nothing wider."""
    if metadata.get("type_name") != TYPE_NAME:
        raise ValueError("unknown custom data type")
    normal = pa.schema(
        [
            field.with_type(pa.string()) if pa.types.is_string_view(field.type) else field
            for field in batch.schema
        ]
    )
    if not normal.equals(_ENGINE_ARROW_SCHEMA, check_metadata=False):
        raise ValueError("unknown engine Arrow schema")
    return batch.cast(_ENGINE_ARROW_SCHEMA).cast(ARROW_SCHEMA)


def register_ranking_generation() -> DataType:
    """The one explicit registration; importing this module registers nothing."""
    register_custom_data_class(RankingGeneration)
    return DataType(TYPE_NAME)
