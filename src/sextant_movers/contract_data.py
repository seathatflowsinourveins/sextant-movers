"""The data contract: the listing and paced-bars names Sextant's ranking-inputs module uses.

The second of the two modules Sextant imports from this package; `contract.py` is the first.
It holds what that module takes from the listings, the paced bars and the reference closes:
`HEADERS`, `RECEIPT`, `validate`, `creation_time` and `parse_directory` (build part 5a),
`Pacer` and `PacedStockBars` (part 5b-1), `AlpacaDailyBars`, `collect_references` and
`prior_sessions` (part 5b-2). `__all__` is the whole public namespace, and each name is its
builder module's own object, never a copy.

Importing this module loads the vendor's SDK, which `contract.py` never does; that is why the
two are separate. Not exported, because this package does not build them: a batch bound, the
operating-company filter, the asset and reference source labels and the close-time helper.
Display only, never a signal; no orders.

Source: docs/design.md, "The contract"; ADR-0002, decision 3; `__all__` as the public names of a
module, python/cpython@v3.13.18 `Doc/reference/simple_stmts.rst:833-846`.
"""

from sextant_movers.ranking.bars import PacedStockBars, Pacer
from sextant_movers.ranking.listings import (
    HEADERS,
    RECEIPT,
    creation_time,
    parse_directory,
    validate,
)
from sextant_movers.ranking.references import AlpacaDailyBars, collect_references, prior_sessions

__all__ = [
    "HEADERS",
    "RECEIPT",
    "AlpacaDailyBars",
    "PacedStockBars",
    "Pacer",
    "collect_references",
    "creation_time",
    "parse_directory",
    "prior_sessions",
    "validate",
]
