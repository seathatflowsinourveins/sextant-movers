"""The reference-child helper: the skip, the child's environment, the run and the answer.

`reference_child.py` is the one owner of how a `local` parity proof starts the reference in a
child interpreter. This proof never starts the reference: each child here is this side's own
interpreter, `sys.executable`, running a one-line script, and every path and value is made up.
"""

import re
import sys

import pytest
from reference_child import NEEDS, reference, run

SRC, PYTHON = "MOVERS_REFERENCE_SRC", "MOVERS_REFERENCE_PYTHON"


@pytest.mark.parametrize("missing", [SRC, PYTHON])
def test_a_missing_variable_skips_with_one_message(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.setenv(SRC, "/made-up/src")
    monkeypatch.setenv(PYTHON, "/made-up/python")
    assert reference() == ("/made-up/src", "/made-up/python")
    monkeypatch.delenv(missing)
    with pytest.raises(pytest.skip.Exception, match=re.escape(NEEDS)):
        reference()


def test_a_child_that_exits_non_zero_fails_with_its_stderr() -> None:
    script = "import sys; sys.stderr.write('made-up failure text'); sys.exit(3)"
    with pytest.raises(AssertionError, match=r"exited 3(.|\n)*made-up failure text"):
        run(sys.executable, "/made-up/src", script)


def test_a_child_without_an_answer_line_fails_with_its_stdout() -> None:
    with pytest.raises(AssertionError, match=r"no PARITY line(.|\n)*made-up output"):
        run(sys.executable, "/made-up/src", "print('made-up output')")


def test_the_answer_line_comes_back_parsed_with_the_arguments_and_input() -> None:
    script = (
        "import json, sys; print('noise'); "
        "print('PARITY ' + json.dumps({'args': sys.argv[1:], 'input': sys.stdin.read()}))"
    )
    answer = run(sys.executable, "/made-up/src", script, "a", "b", stdin="made-up input")
    assert answer == {"args": ["a", "b"], "input": "made-up input"}


def test_the_child_sees_the_source_and_path_and_nothing_else_of_this_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/made-up/bin")
    monkeypatch.setenv(PYTHON, "/made-up/python")
    monkeypatch.setenv("MOVERS_PROOF_OTHER", "made-up")
    names = [SRC, "PATH", PYTHON, "MOVERS_PROOF_OTHER"]
    script = f"import json, os; print('PARITY ' + json.dumps([os.environ.get(n) for n in {names}]))"
    answer = run(sys.executable, "/made-up/src", script)
    assert answer == ["/made-up/src", "/made-up/bin", None, None]
