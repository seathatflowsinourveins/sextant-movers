"""The contract exports what is built so far, and nothing else.

`contract.py` is the one module Sextant imports (docs/design.md, "The contract"; ADR-0002,
decision 3). Each name it exports is the builder module's own object, the public namespace
equals `__all__` (python/cpython@v3.13.18 `Doc/reference/simple_stmts.rst:833-846`), every
exported name is one the design lists, and importing the contract never loads Sextant.
"""

import re
import subprocess
import sys
from pathlib import Path

import sextant_movers.contract as contract
from sextant_movers.ranking import registry

ROOT = Path(__file__).resolve().parents[2]
BUILT = {"Registry": registry.Registry, "RegistryRow": registry.RegistryRow}


def _designed() -> set[str]:
    section = (ROOT / "docs" / "design.md").read_text(encoding="utf-8").split("## The contract")[1]
    sentence = section.split("`contract_data.py`")[0]
    return set(re.findall(r"`([A-Za-z_]+)`", sentence))


def test_the_contract_exports_exactly_the_built_names() -> None:
    assert contract.__all__ == sorted(BUILT)


def test_each_exported_name_is_the_builders_own_object() -> None:
    for name, built in BUILT.items():
        assert getattr(contract, name) is built


def test_the_public_namespace_is_all() -> None:
    public = {name for name in vars(contract) if not name.startswith("_")}
    assert public == set(contract.__all__)


def test_every_exported_name_is_one_the_design_lists() -> None:
    designed = _designed()
    assert len(designed) == 12
    assert set(contract.__all__) <= designed


def test_a_star_import_binds_the_exported_names_only() -> None:
    namespace: dict[str, object] = {}
    exec("from sextant_movers.contract import *", namespace)  # noqa: S102
    namespace.pop("__builtins__")
    assert namespace == BUILT


def test_importing_the_contract_never_loads_sextant() -> None:
    script = (
        "import sys\n"
        "import sextant_movers.contract\n"
        "tops = {name.partition('.')[0] for name in sys.modules}\n"
        "print('sextant' in tops)\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert done.stdout.strip() == "False"
