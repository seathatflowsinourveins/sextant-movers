"""Public gate invariants; GitHub workflow_run/REST and upstream composite contracts.

Sources: .github/gate/upstreams.json and google/eng-practices@3bb3ec25:
review/reviewer/standard.md:31-34,72,88-90; review/developer/small-cls.md:175.
"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def _gate() -> dict:
    return yaml.safe_load((WORKFLOWS / "gate.yml").read_text())


def test_no_workflow_requests_a_local_runner_or_privileged_pr_trigger() -> None:
    for path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get("on", workflow.get(True))
        assert "pull_request_target" not in events, path
        for job in workflow["jobs"].values():
            assert job["runs-on"] == "ubuntu-24.04", path


def test_gate_runs_only_after_ci_from_the_default_branch() -> None:
    gate = _gate()
    assert gate.get("on", gate.get(True)) == {
        "workflow_run": {"workflows": ["ci"], "types": ["completed"]}
    }
    for job in gate["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"] == {
                    "ref": "${{ github.sha }}",
                    "persist-credentials": False,
                }


def test_danger_reads_api_data_and_never_checks_out_the_head() -> None:
    source = _gate()["jobs"]["source"]
    danger = next(step for step in source["steps"] if step.get("id") == "danger")
    assert "--dangerfile dangerfile.js --failOnErrors" in danger["run"]
    assert danger["env"]["DANGER_EVENT_PATH"] == "${{ steps.prepare.outputs.danger_event }}"
    assert danger["run"].startswith('GITHUB_EVENT_PATH="$DANGER_EVENT_PATH" ')
    assert not any("run" in step and "checkout" in step["run"] for step in source["steps"])


def test_dangers_native_provider_accepts_the_api_only_event(tmp_path: Path) -> None:
    """Danger 14.1.0 GitHubActions constructor/useEventDSL, without network calls."""
    executable = shutil.which("danger")
    assert executable is not None
    event = tmp_path / "danger-event.json"
    event.write_text(
        json.dumps({"pull_request": {"number": 7, "base": {"repo": {"full_name": "owner/repo"}}}})
    )
    done = subprocess.run(
        [
            "node",
            "-e",
            "const {createRequire}=require('node:module');"
            "const load=createRequire(process.argv[1]);"
            "const {GitHubActions}=load('danger/distribution/ci_source/providers/GitHubActions');"
            "const source=new GitHubActions({GITHUB_EVENT_PATH:process.argv[2],"
            "GITHUB_WORKFLOW:'gate'});"
            "process.stdout.write(JSON.stringify([source.useEventDSL,"
            "source.pullRequestID,source.repoSlug]));",
            str(Path(executable).resolve()),
            str(event),
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [False, 7, "owner/repo"]


def test_every_action_in_workflows_and_composites_has_a_full_pin() -> None:
    paths = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    paths += list((ROOT / ".github/actions").rglob("action.y*ml"))
    for path in paths:
        data = yaml.safe_load(path.read_text())
        groups = [data["runs"]] if "runs" in data else data["jobs"].values()
        for group in groups:
            for step in group.get("steps", []):
                action = step.get("uses", "")
                if action and not action.startswith("./"):
                    assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), (path, action)


def test_ci_runs_the_complete_check_without_repository_secrets() -> None:
    text = (WORKFLOWS / "ci.yml").read_text()
    ci = yaml.safe_load(text)
    assert set(ci.get("on", ci.get(True))) == {"pull_request"}
    assert ci["permissions"] == {"contents": "read"}
    runs = " ".join(
        str(step.get("run", "")) for job in ci["jobs"].values() for step in job["steps"]
    )
    for part in ("just lint", "just test tests/proofs", "just test tests/repo", "just secrets"):
        assert part in runs, part
    assert "secrets." not in text
    assert "enable-cache: true" in text and "package-manager-cache: false" in text


def _setup_uv_steps() -> dict[str, list[tuple[str, dict]]]:
    found: dict[str, list[tuple[str, dict]]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            for step in job["steps"]:
                if step.get("uses", "").startswith("astral-sh/setup-uv@"):
                    found.setdefault(path.name, []).append((name, step))
    return found


def test_setup_uv_runs_at_the_recorded_pin_in_every_workflow() -> None:
    """The pin record is .github/gate/upstreams.json; each `uses:` line names its version."""
    record = json.loads((ROOT / ".github/gate/upstreams.json").read_text())
    pin = record["components"]["setup-uv"]
    steps = _setup_uv_steps()
    assert set(steps) == {"ci.yml", "consolidation.yml", "gate.yml"}
    for name, found in steps.items():
        for job, step in found:
            assert step["uses"] == "astral-sh/setup-uv@" + pin["sha"], (name, job)
        lines = (WORKFLOWS / name).read_text().splitlines()
        for line in (line for line in lines if "astral-sh/setup-uv@" in line):
            assert re.search(rf"# {re.escape(pin['version'])}(,|$)", line), (name, line)


def test_the_uv_cache_is_on_in_ci_saved_by_one_job_and_off_elsewhere() -> None:
    """astral-sh/setup-uv `docs/caching.md`: `enable-cache` and `save-cache` at the pin."""
    steps = _setup_uv_steps()
    saving = []
    for job, step in steps["ci.yml"]:
        assert step["with"]["enable-cache"] is True, job
        if step["with"].get("save-cache") is not False:
            saving.append(job)
    assert saving == ["tests"]
    for name in ("consolidation.yml", "gate.yml"):
        for job, step in steps[name]:
            assert step["with"] == {"enable-cache": False}, (name, job)


def test_fork_pr_workflows_never_bind_repository_secrets() -> None:
    """GitHub events-that-trigger-workflows#pull_request, fork PR secrets, read 2026-11-09.

    Owner ruling 00:58 replaces the live fork run with this static proof and the
    all_external_contributors API receipt in the handoff scratch/receipts directory.
    """
    found = []
    for path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        text = path.read_text()
        workflow = yaml.safe_load(text)
        events = workflow.get("on", workflow.get(True))
        if "pull_request" in events:
            found.append(path.name)
            assert "secrets." not in text, path
    assert "ci.yml" in found


def test_parity_set_has_twenty_four_bundles_and_no_memory_plugins() -> None:
    profile = json.loads((ROOT / ".github/gate/dsh-profile.json").read_text())
    assert len(profile["bundles"]) == 24
    names = {row["package"] for row in profile["bundles"]}
    assert "@deepseek-ai/dsh-headless" in names
    assert not any(re.search(r"memento|engram|openviking", name, re.I) for name in names)
    assert profile["engine_authority"] == "reuses main's dsh pin (lead ruling 2026-11-09)"


def test_dsh_github_scripts_are_unchanged_at_the_qualifying_upstream_pin() -> None:
    expected = {
        "action-patch.mjs": "a897b80da6c1a42f32261e54a92e2bd1163dd075e0574edc4c336b96ceb3e797",
        "action-post.mjs": "f8df0a576aefe1ff68e2c45caae276bf06ecae6eeba5648346a7ff0cf0981875",
    }
    for filename, digest in expected.items():
        script = ROOT / ".github/actions/dsh-github/scripts" / filename
        assert hashlib.sha256(script.read_bytes()).hexdigest() == digest


def test_landing_standard_and_fail_on_error_are_wired_into_the_verdict() -> None:
    rules = (WORKFLOWS / "shared/review-rules.md").read_text().split("## Report", 1)[1]
    for required in (
        "3bb3ec25b3b0199f4940b1aa75f0ac5c5753301c",
        "standard.md:31-34,72,88-90",
        "small-cls.md:175",
        "concrete failing witness at the exact head",
        "input or state",
        "wrong output, crash or failing test",
        "speculative or unwitnessed",
        "From the third reviewed commit, only a P0 blocks",
        "ONE follow-up issue per PR",
        "fail-on: error",
    ):
        assert required in rules
    dsh = _gate()["jobs"]["dsh"]
    review = next(step for step in dsh["steps"] if step.get("id") == "review")
    assert review["with"]["blocking"] == "true"
    assert review["with"]["fail-on"] == "error"
    adapter = (ROOT / ".github/gate/gate.cjs").read_text()
    assert ".github/workflows/shared/review-rules.md" in adapter


def test_every_blocking_finding_carries_an_executed_witness_or_the_static_label() -> None:
    """The program lead's 2026-11-11 gate ruling: eight static findings, four did not reproduce."""
    prompt = (WORKFLOWS / "review.md").read_text()
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    for surface in (prompt, rules):
        assert "executed witness" in surface
        assert "static: no witness run" in surface
    report = rules.split("## Report", 1)[1].split("## Two author families", 1)[0]
    assert "states its evidence status" in report
    assert "does not replace the witness" in report
    assert "non-blocking" in report and "follow-up issue" in report


def test_quality_verdict_runs_even_when_the_review_action_fails() -> None:
    """A failed action leaves no verdict; the verdict step still runs to name it."""
    for family in ("codex", "claude"):
        verdict = next(
            step for step in _gate()["jobs"][family]["steps"] if step.get("id") == "verdict"
        )
        assert verdict.get("if") == "always()", family


def test_always_p0_holds_the_generic_lines_and_this_repositorys_hard_lines() -> None:
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    always = rules.split("## Always P0 in this repo", 1)[1].split("\n\n", 1)[0]
    for required in (
        # Kept from the harness.
        "**Self-invention, the repo's first rule:**",
        "A secret, key or token in a file, log, command line or URL.",
        "Weakening a gate without saying so and why",
        "An unrequested destructive action on data or on the repo.",
        # This repository's own hard lines.
        "Any code that places an order, or any import of a trading client.",
        "TradingView used as a signal or as an input to the rank",
        "reached by anything other than its official interface",
        "Recorded market data, a symbol, price, quantity, order id, account id or private path",
        "in the repository or in PR text",
        "A broker stream opened without holding the account lease.",
    ):
        assert required in always, required
    for retired in ("live order", "Floats for money"):
        assert retired not in always, retired


def test_no_carveout_relaxes_the_review_verdict() -> None:
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    for retired in ("carve-out", "paper lane", "paper and e2e", "lanes/", "src/sextant/", "#128"):
        assert retired not in rules, retired


def test_upstream_first_rule_names_the_sdk_call_and_its_severity() -> None:
    rules = (WORKFLOWS / "shared/review-rules.md").read_text()
    owner = (
        "with clean upstrean integrated line should not be talking too much lines "
        "in the first place ,we need sota automation"
    )
    assert owner in rules
    upstream_first = rules.split("## Upstream-first", 1)[1].split("## Report", 1)[0]
    # What a pinned upstream already provides must not be re-implemented in new src code.
    for required in (
        "re-implements what a pinned upstream already provides",
        "HTTP transports",
        "retry or pagination loops",
        "rate pacers",
        "calculators",
        "data clients",
        "OAuth flows",
        # The finding names the SDK call to use instead: this repository's pinned SDKs.
        "`ScreenerClient`",
        "`StockHistoricalDataClient`",
        "`StockDataStream`",
        "NautilusTrader's own components",
        "exchange_calendars' `get_calendar`",
        "the MCP Python SDK's `MCPServer`",
        "Lightweight Charts",
        # Severity: P1 on normal paths with its witness, otherwise never blocking.
        "[P1]",
        "never blocks",
        # The example witness, from Sextant.
        "hand-built HTTP transport",
        # Using the upstream, or carrying it verbatim, is not re-implementing it.
        "not re-implementing it",
        "not an Always P0",
    ):
        assert required in upstream_first, required
    for retired in ("TradingClient", "edgartools", "src/sextant/"):
        assert retired not in upstream_first, retired
    # The Report verdict admits the witnessed P1 on normal paths, and only then.
    report = rules.split("## Report", 1)[1]
    assert "upstream-first duplication on a normal path" in report
    assert (
        "Only a finding at the blocking threshold" in (ROOT / ".github/gate/gate.cjs").read_text()
    )


def test_key_policy_and_failure_paths_execute_against_the_actual_adapter() -> None:
    done = subprocess.run(
        ["node", "--test", "tests/proofs/public-gate.test.cjs"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_review_spill_root_is_the_gates_known_output_dir() -> None:
    overlay = (ROOT / ".github/gate/headless-services.cordis.yml").read_text()
    assert "- id: spill-local" in overlay
    assert "root: !!js process.env.DSH_GITHUB_CI_OUTPUT_DIR + '/spill'" in overlay
    action = (ROOT / ".github/actions/dsh-github/action.yml").read_text()
    assert "DSH_GITHUB_CI_OUTPUT_DIR: ${{ inputs.output-dir }}" in action
    bundle = (ROOT / ".github/gate/bundles/api-only/index.mjs").read_text()
    assert "DSH_GITHUB_CI_OUTPUT_DIR" in bundle
    assert "const spillPathFields = new Map([['read', 'file_path'], ['grep', 'path']]);" in bundle
