"""The data contract exports the listing and paced-bars names built so far, and nothing else.

`contract_data.py` is the second of the two modules Sextant imports (docs/design.md, "The
contract"; ADR-0002, decision 3): the names its ranking-inputs module takes from the listings,
the paced bars and the reference closes. Each exported name is the builder module's own object,
the public namespace equals `__all__` (python/cpython@v3.13.18
`Doc/reference/simple_stmts.rst:833-846`), every exported name is one the reference's module
uses, the names it uses that this package does not build are named here, and importing the
module never loads Sextant.
"""

import subprocess
import sys
from pathlib import Path

import sextant_movers.contract as contract
import sextant_movers.contract_data as contract_data
from sextant_movers.ranking import bars, listings, references

ROOT = Path(__file__).resolve().parents[2]
BUILT = {
    "HEADERS": listings.HEADERS,
    "RECEIPT": listings.RECEIPT,
    "AlpacaDailyBars": references.AlpacaDailyBars,
    "PacedStockBars": bars.PacedStockBars,
    "Pacer": bars.Pacer,
    "collect_references": references.collect_references,
    "creation_time": listings.creation_time,
    "parse_directory": listings.parse_directory,
    "prior_sessions": references.prior_sessions,
    "validate": listings.validate,
}
# What the reference's ranking-inputs module takes from its listings, bars and prestage modules:
# `src/sextant/data/ranking_inputs.py:26-39,53-63,123` at
# 06562bec0898a7203f4eaf69086ced83d2477e6b.
REFERENCE_USES = {
    "ASSET_SOURCE",
    "HEADERS",
    "MAX_BATCH",
    "RECEIPT",
    "REFERENCE_SOURCE",
    "AlpacaDailyBars",
    "PacedStockBars",
    "Pacer",
    "_at",
    "_keeps",
    "collect_references",
    "creation_time",
    "parse_directory",
    "prior_sessions",
    "validate",
}
# Used there, not built here: the batch bound, the operating-company filter, the two source
# labels and the close-time helper. None exists in this package, so none is exported.
NOT_BUILT = {"ASSET_SOURCE", "MAX_BATCH", "REFERENCE_SOURCE", "_at", "_keeps"}


def test_the_data_contract_exports_exactly_the_built_names() -> None:
    assert sorted(contract_data.__all__) == sorted(BUILT)  # the linter orders `__all__` (RUF022)


def test_each_exported_name_is_the_builders_own_object() -> None:
    for name, built in BUILT.items():
        assert getattr(contract_data, name) is built


def test_the_public_namespace_is_all() -> None:
    public = {name for name in vars(contract_data) if not name.startswith("_")}
    assert public == set(contract_data.__all__)


def test_the_exported_names_are_the_references_minus_the_unbuilt() -> None:
    assert NOT_BUILT <= REFERENCE_USES
    assert set(contract_data.__all__) == REFERENCE_USES - NOT_BUILT


def test_no_unbuilt_name_exists_in_a_builder_module() -> None:
    for name in NOT_BUILT:
        for module in (bars, listings, references):
            assert not hasattr(module, name), (module.__name__, name)


def test_the_two_contracts_share_no_name() -> None:
    assert not set(contract_data.__all__) & set(contract.__all__)


def test_a_star_import_binds_the_exported_names_only() -> None:
    namespace: dict[str, object] = {}
    exec("from sextant_movers.contract_data import *", namespace)  # noqa: S102
    namespace.pop("__builtins__")
    assert namespace == BUILT


def _child(script: str) -> str:
    # `-I` drops PYTHONPATH and the working directory, so the child is handed the source root.
    cmd = [sys.executable, "-I", "-c", script, str(ROOT / "src")]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def test_importing_the_data_contract_never_loads_sextant() -> None:
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import sextant_movers.contract_data\n"
        "tops = {name.partition('.')[0] for name in sys.modules}\n"
        "print('sextant' in tops)\n"
    )
    assert _child(script) == "False"


def test_the_first_contract_stays_free_of_the_vendor_sdk() -> None:
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import sextant_movers.contract\n"
        "tops = {name.partition('.')[0] for name in sys.modules}\n"
        "print('alpaca' in tops, 'sextant_movers.contract_data' in sys.modules)\n"
    )
    assert _child(script) == "False False"
