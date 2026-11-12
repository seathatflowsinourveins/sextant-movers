"""Manifest Proof cells name files under tests/ and definitions in those files.

Build on pytest's file::test syntax (runtime pinned to 9.2.0):
https://docs.pytest.org/en/stable/how-to/usage.html#specifying-which-tests-to-run,
read 2026-11-11. The page documents selection and --collect-only, not a Markdown checker.
The thin table reader follows tests/proofs/test_manifest_union.py; AST reads follow
tests/repo/test_landscape.py. This checks definitions, not test success or claim coverage.
Only the last cell is evidence here: upstream paths in source cells are not local proofs.
Bare filenames must resolve uniquely; ::test spans inherit the preceding file in that cell.
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
REFERENCE = re.compile(
    r"(?P<file>(?:[\w.-]+/)*(?:test_[\w-]+\.py|[\w.-]+\.test\.cjs))"
    r"|(?P<node>::[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)"
)


def _defines(path: Path, name: str) -> bool:
    if path.suffix == ".cjs":
        return name in [
            match[1] for match in re.findall(r"\btest\(\s*(['\"])(.*?)\1", path.read_text())
        ]
    scope = ast.parse(path.read_text()).body
    for part in name.split("::"):
        found = next(
            (
                node
                for node in scope
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == part
            ),
            None,
        )
        if found is None:
            return False
        scope = found.body
    return True


def check_proofs(text: str, root: Path) -> None:
    files = [p for p in (root / "tests").rglob("*") if p.is_file()]
    problems: list[str] = []
    for row in text.splitlines():
        if not row.startswith("| "):
            continue
        cells = row.split("|")
        title, proof = cells[1].strip(), cells[-2].strip()
        current: Path | None = None
        for match in REFERENCE.finditer(proof):
            if filename := match.group("file"):
                matches = [
                    p
                    for p in files
                    if (
                        p.relative_to(root).as_posix() == filename
                        if "/" in filename
                        else p.name == filename
                    )
                ]
                current = matches[0] if len(matches) == 1 else None
                if current is None:
                    problems.append(
                        f"{title}: {filename} resolves to {len(matches)} files under tests/"
                    )
            elif node := match.group("node"):
                if current is None or not _defines(current, node[2:]):
                    problems.append(f"{title}: {node} is not defined by the preceding proof file")
    assert not problems, "\n".join(problems)


def test_every_manifest_proof_names_a_local_file_and_definition() -> None:
    check_proofs((ROOT / "docs/manifest.md").read_text(), ROOT)


def _table(proof: str, title: str = "Example practice") -> str:
    return (
        "| Practice or pick | Source, pinned | How it is applied | Proof |\n"
        "|---|---|---|---|\n"
        f"| {title} | upstream/test_not_local.py | applied | {proof} |\n"
    )


@pytest.fixture
def proof_tree(tmp_path: Path) -> Path:
    folder = tmp_path / "tests/proofs"
    folder.mkdir(parents=True)
    (folder / "test_real.py").write_text("def test_present(): pass\ndef test_second(): pass\n")
    (folder / "real.test.cjs").write_text("test('test_present', () => {});\n")
    return tmp_path


@pytest.mark.parametrize("filename", ["test_missing.py", "missing.test.cjs"])
def test_a_missing_file_is_refused_with_the_row_title_and_filename(
    proof_tree: Path, filename: str
) -> None:
    with pytest.raises(AssertionError) as error:
        check_proofs(_table(f"`{filename}`"), proof_tree)
    assert "Example practice" in str(error.value) and filename in str(error.value)


@pytest.mark.parametrize("filename", ["test_real.py", "real.test.cjs"])
def test_a_missing_definition_is_refused_with_the_row_title_and_name(
    proof_tree: Path, filename: str
) -> None:
    with pytest.raises(AssertionError) as error:
        check_proofs(_table(f"`{filename}::test_missing`"), proof_tree)
    assert "Example practice" in str(error.value) and "test_missing" in str(error.value)


def test_qualified_bare_and_inherited_references_and_none_yet_pass(proof_tree: Path) -> None:
    check_proofs(
        _table("`tests/proofs/test_real.py::test_present`, `::test_second`; `real.test.cjs`")
        + _table("`test_real.py::test_present`", "Bare filename")
        + _table("None yet: no integration proof", "Unavailable"),
        proof_tree,
    )


def test_an_inherited_name_is_checked_and_never_inherits_from_another_cell(
    proof_tree: Path,
) -> None:
    for text in (
        _table("`test_real.py::test_present`, `::test_missing`"),
        _table("`test_real.py::test_present`") + _table("`::test_present`", "Another row"),
    ):
        with pytest.raises(AssertionError, match="preceding proof file"):
            check_proofs(text, proof_tree)


def test_a_qualified_path_cannot_fall_back_to_a_matching_basename(proof_tree: Path) -> None:
    with pytest.raises(AssertionError, match=r"tests/repo/test_real\.py"):
        check_proofs(_table("tests/repo/test_real.py"), proof_tree)


def test_an_ambiguous_basename_is_refused(proof_tree: Path) -> None:
    folder = proof_tree / "tests/repo"
    folder.mkdir()
    (folder / "test_real.py").write_text("")
    with pytest.raises(AssertionError, match="resolves to 2 files"):
        check_proofs(_table("`test_real.py`"), proof_tree)
