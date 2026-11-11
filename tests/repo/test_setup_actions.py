"""SETUP.md names the nested action of each composite review action, at the gate's own pin.

A repository that restricts which actions may run must also allow the `uses:` lines inside a
composite action's `action.yml`. GitHub's settings page gives the pattern syntax and is silent on
that (read 2026-11-11); measured on this repository: with only the parent allowed, the review job
ended at "Set up job" and the required `review` status failed with no finding. So a bump of a
review action has to re-read its `action.yml`, and this check stays red until SETUP.md's row
names the new pin.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
PIN = r"([\w.-]+/[\w.-]+)@([0-9a-f]{40})"
# The gate's composite actions from other owners; its JavaScript actions have no `uses:` lines.
COMPOSITE_REVIEW_ACTIONS = {"openai/codex-action", "anthropics/claude-code-action"}


def _gate_pins() -> dict[str, str]:
    gate = (ROOT / ".github" / "workflows" / "gate.yml").read_text(encoding="utf-8")
    return dict(re.findall(rf"uses: {PIN}", gate))


def _nested_rows() -> list[tuple[str, str, str, str]]:
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")
    assert "\n### Nested actions\n" in setup, "SETUP.md has no Nested actions section"
    section = setup.split("\n### Nested actions\n")[1].split("\n## ")[0]
    return re.findall(rf"^\| `{PIN}` \| `action\.yml:\d+` \| `{PIN}` \|", section, re.MULTILINE)


def test_each_composite_review_action_has_its_nested_row() -> None:
    assert {parent for parent, _, _, _ in _nested_rows()} == COMPOSITE_REVIEW_ACTIONS
    assert COMPOSITE_REVIEW_ACTIONS <= set(_gate_pins())


def test_each_nested_row_names_the_pin_the_gate_runs() -> None:
    gate = _gate_pins()
    for parent, commit, _, _ in _nested_rows():
        assert gate[parent] == commit, f"{parent}: re-read its action.yml and update SETUP.md"


def test_the_setup_step_points_at_the_nested_rows() -> None:
    setup = (ROOT / "SETUP.md").read_text(encoding="utf-8")
    step = next(line for line in setup.splitlines() if line.startswith("2. **Actions settings**"))
    assert "Nested actions" in step
