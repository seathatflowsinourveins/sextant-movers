"""dangerfile.js, run with only a mocked Danger API: the first rule, as `review` applies it.

The source rule for `src/sextant_movers/` and its cases are Sextant's (see ORIGINS.md), with
Sextant's package path read as this one's.
"""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MANIFEST = (
    "| Practice or pick | Source, pinned | How it is applied | Proof |\n"
    "|---|---|---|---|\n"
    "| Real row | source | applied | proof |\n"
)
PLUGIN_MANIFEST = (
    MANIFEST + "\n## Third-party plugins\n\n"
    "| Practice or pick | Source, pinned | How it is applied | Proof |\n"
    "|---|---|---|---|\n"
    "| Plugin X | owner/repo@v1 " + "d" * 40 + " | one lane | proof |\n"
)
HARNESS = """
const fs = require("fs");
const vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const fails = [];
const pending = [];
const danger = {
  github: {
    pr: { body: input.body, user: { login: input.login } },
    utils: { fileContents: async () => input.manifest },
  },
  git: {
    created_files: input.created, modified_files: input.modified, deleted_files: input.deleted,
    structuredDiffForFile: async (filename) => input.structured_diffs[filename] ?? null,
  },
};
const api = { danger, fail: (m) => fails.push(m), schedule: (p) => pending.push(p()) };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { require: () => api });
Promise.all(pending).then(() => console.log(JSON.stringify(fails)));
"""
PIN = "a/b@v1 " + "c" * 40
OTHER_PIN = "d/e@v2 " + "d" * 40
EVIDENCE = " src/client.py:12 BUILD ON"
RANKING = "src/sextant_movers/ranking/client.py"
TRADINGVIEW = "src/sextant_movers/tradingview/display.py"
SOURCE = "src/sextant_movers/contract.py"  # in the package, on no AUTHORITY surface


def _body(*, row: str = '"Real row"', sources: str = PIN) -> str:
    return (
        "## What changed\nx\n"
        f"## Manifest row\n{row}\n"
        f"## Upstream references\n{sources}\n"
        "## How verified\ny\n"
        "## How to revert\nz\n"
    )


def _failures(
    body: str,
    login: str = "someone",
    created: tuple[str, ...] = (),
    manifest: str = MANIFEST,
    *,
    modified: tuple[str, ...] = (),
    deleted: tuple[str, ...] = (),
    structured_diffs: dict[str, dict[str, object]] | None = None,
) -> list[str]:
    done = subprocess.run(
        ["node", "-e", HARNESS, str(ROOT / "dangerfile.js")],
        input=json.dumps(
            {
                "body": body,
                "login": login,
                "created": list(created),
                "modified": list(modified),
                "deleted": list(deleted),
                "manifest": manifest,
                "structured_diffs": structured_diffs or {},
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout)


def test_an_honest_body_passes_even_when_its_last_section_ends_the_body() -> None:
    assert _failures(_body()) == []


def test_a_docs_page_with_a_read_date_is_a_source() -> None:
    assert _failures(_body(sources="https://x.dev/page, read 2026-11-05")) == []


def test_a_paper_is_a_source() -> None:
    assert _failures(_body(sources="doi:10.1234/abc")) == []


def test_no_source_is_refused_as_self_invention() -> None:
    assert any("self-invent" in f for f in _failures(_body(sources="my own idea")))


def test_a_url_without_a_read_date_is_refused() -> None:
    assert any("self-invent" in f for f in _failures(_body(sources="https://x.dev/page")))


def test_a_commit_shorter_than_full_is_refused() -> None:
    assert any("self-invent" in f for f in _failures(_body(sources="a/b@v1 abc1234")))


def test_a_markdown_linked_repo_at_a_tag_with_its_full_sha_is_a_source() -> None:
    # The form docs/manifest.md itself uses: the link's ")" sits between the repo and its "@".
    sources = "[a/b](https://github.com/a/b)@v1 `" + "c" * 40 + "`:`docs/x.md`"
    assert _failures(_body(sources=sources)) == []


def test_a_markdown_linked_repo_still_needs_its_full_sha() -> None:
    sources = "[a/b](https://github.com/a/b)@v1 `abc1234`"
    assert any("self-invent" in f for f in _failures(_body(sources=sources)))


def test_an_empty_section_is_refused() -> None:
    assert any("How verified" in f for f in _failures(_body().replace("\ny\n", "\n\n")))


def test_a_template_comment_is_not_content() -> None:
    body = _body().replace("\ny\n", "\n<!-- fill me in -->\n")
    assert any("How verified" in f for f in _failures(body))


def test_a_manifest_row_that_does_not_exist_is_refused() -> None:
    assert any("Manifest row" in f for f in _failures(_body(row='"Not a row"')))


def test_the_table_header_is_not_a_row() -> None:
    assert any("Manifest row" in f for f in _failures(_body(row='"Practice or pick"')))


def test_a_quoted_word_from_another_cell_is_not_a_row() -> None:
    assert any("Manifest row" in f for f in _failures(_body(row='"source"')))


def test_a_manifest_row_must_be_named_in_quotes() -> None:
    assert any("Manifest row" in f for f in _failures(_body(row="Real row")))


def test_dependabot_generated_bodies_pass() -> None:
    assert _failures("Bumps a from 1 to 2.", login="dependabot[bot]") == []


@pytest.mark.parametrize("path", [RANKING, TRADINGVIEW])
def test_dependabot_on_a_governed_path_keeps_its_exemption(path: str) -> None:
    assert _failures("Bumps a from 1 to 2.", login="dependabot[bot]", created=(path,)) == []


def test_a_sourced_gate_edit_needs_no_owner_label() -> None:
    assert _failures(_body(), created=(".github/workflows/gate.yml",)) == []


def test_a_third_party_plugin_row_must_record_its_disclosure() -> None:
    found = _failures(_body(row='"Plugin X"'), manifest=PLUGIN_MANIFEST)
    assert any("capability" in failure for failure in found)


def test_a_third_party_plugin_row_with_its_disclosure_passes() -> None:
    manifest = PLUGIN_MANIFEST.replace(
        "| one lane | proof |", "| capabilities: fs-read; red lines: none; one lane | proof |"
    )
    assert _failures(_body(row='"Plugin X"'), manifest=manifest) == []


# AUTHORITY: one upstream set owns each surface of this package.
OWNERS = {
    "ranking": (
        RANKING,
        (
            "nautechsystems/nautilus_trader",
            "alpacahq/alpaca-py",
            "docs.alpaca.markets",
            "gerrymanoim/exchange_calendars",
        ),
    ),
    "tradingview": (TRADINGVIEW, ("tradingview.com", "tradingview/lightweight-charts")),
}


@pytest.mark.parametrize(
    ("surface", "owner"),
    [(surface, owner) for surface, (_, owners) in OWNERS.items() for owner in owners],
)
def test_every_owner_on_its_own_path_passes(surface: str, owner: str) -> None:
    path = OWNERS[surface][0]
    sources = f"{owner}@v1 {'c' * 40}{EVIDENCE}"
    if "/" not in owner:
        sources = f"https://{owner}/docs, read 2026-11-11{EVIDENCE}"
    assert _failures(_body(sources=sources), created=(path,)) == []


@pytest.mark.parametrize("surface", list(OWNERS))
def test_a_governed_path_must_name_its_authority(surface: str) -> None:
    found = _failures(
        _body(sources=f"example/fork@v1 {'c' * 40}{EVIDENCE}"), created=(OWNERS[surface][0],)
    )
    assert any(surface in failure and "authority" in failure.lower() for failure in found)


@pytest.mark.parametrize(
    "owner",
    [
        "nautechsystems/nautilus_trader",
        "alpacahq/alpaca-py",
        "modelcontextprotocol/python-sdk",
        "ferroxlabs/tvcontrol",
    ],
)
def test_tradingview_authority_stands_alone(owner: str) -> None:
    # Only TradingView's own sources govern the display; the MCP SDK is cited as the transport,
    # in addition, and a community controller or a ranking owner never stands in for TradingView.
    found = _failures(_body(sources=f"{owner}@v1 {'c' * 40}{EVIDENCE}"), created=(TRADINGVIEW,))
    assert any("tradingview" in failure and "authority" in failure.lower() for failure in found)


def test_the_mcp_sdk_cited_beside_tradingview_passes() -> None:
    sources = (
        f"- Display: tradingview/lightweight-charts@v1 {'c' * 40}{EVIDENCE}\n"
        f"- Transport: modelcontextprotocol/python-sdk@v1 {'d' * 40} src/server.py:3 USE AS IS"
    )
    assert _failures(_body(sources=sources), created=(TRADINGVIEW,)) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tradingview.com/lightweight-charts/",
        "https://tradingview.github.io/lightweight-charts/",
    ],
)
def test_tradingviews_own_site_and_docs_hosts_govern_the_display(url: str) -> None:
    # tradingview/lightweight-charts@v5.2.1 b2ce010e4ad59f6556c9ed60e2b2b30feccdd2d1:README.md
    # links its own site and its documentation at these two hosts.
    sources = f"{url}, read 2026-11-11{EVIDENCE}"
    assert _failures(_body(sources=sources), created=(TRADINGVIEW,)) == []


def test_no_sextant_path_stays_in_the_authority_table() -> None:
    table = (ROOT / "dangerfile.js").read_text().split("const AUTHORITY", 1)[1]
    for retired in ("src/sextant/", "lanes/", "deploy/wsl/", "ferroxlabs/tvcontrol", "strategies"):
        assert retired not in table, retired


def test_a_docs_only_change_needs_no_authority() -> None:
    body = _body(sources="example/fork@v1 " + "c" * 40)
    assert _failures(body, created=("docs/design.md",)) == []


def test_a_lookalike_owner_does_not_pass_for_its_prefix() -> None:
    body = _body(sources=f"not-nautechsystems/nautilus_trader@v1 {'c' * 40}{EVIDENCE}")
    found = _failures(body, created=(RANKING,))
    assert any("ranking" in failure and "authority" in failure.lower() for failure in found)


# The source rule: a PR touching src/sextant_movers/ names, for each mechanism, a pinned source,
# its path:line at that pin and one verdict.
@pytest.mark.parametrize(
    ("sources", "missing"),
    [
        (PIN + " BUILD ON", "path:line"),
        (PIN + " src/client.py:12", "verdict"),
        ("doi:10.1234/abc src/client.py:12 BUILD ON", "pinned source"),
        ("a/b@v1 abc1234 src/client.py:12 BUILD ON", "pinned source"),
        ("https://x.dev/page src/client.py:12 BUILD ON", "pinned source"),
        (PIN + " https://x.dev:443 BUILD ON", "path:line"),
        (PIN + " src/client.py:0 BUILD ON", "path:line"),
        (PIN + " src/client.py:12 REBUILD", "verdict"),
        (PIN + " src/client.py:12 KEEP OURS", "Where searched"),
        (PIN + " src/client.py:12 KEEP OURS\n- Where searched: ", "Where searched"),
        (PIN + " <!-- src/client.py:12 BUILD ON -->", "path:line"),
    ],
)
def test_a_src_pr_names_missing_upstream_evidence(sources: str, missing: str) -> None:
    found = _failures(_body(sources=sources), created=(SOURCE,))
    assert any("Never self-invent" in failure and missing in failure for failure in found)


@pytest.mark.parametrize(
    "path",
    [
        "docs/design.md",
        "tests/proofs/test_client.py",
        "dangerfile.js",
        "src/other.py",
        "src/sextant/ranking/client.py",
    ],
)
def test_a_pr_without_package_src_keeps_the_body_rules_only(path: str) -> None:
    assert _failures(_body(), created=(path,)) == []


@pytest.mark.parametrize(
    "sources",
    [
        PIN + " src/client.py:12 USE AS IS",
        "[a/b](https://github.com/a/b)@v1 `" + "c" * 40 + "` `src/client.py:12` BUILD ON",
        "https://x.dev/page, read 2026-11-11 `src/client.py:12` build on",
        PIN + " README.md:1 KEEP OURS\n- Where searched: src/; client",
    ],
)
def test_a_conforming_src_pr_passes(sources: str) -> None:
    assert _failures(_body(sources=sources), created=(SOURCE,)) == []


@pytest.mark.parametrize(
    ("modified", "deleted"),
    [((SOURCE,), ()), ((), (SOURCE,))],
    ids=["modified", "deleted"],
)
def test_the_source_rule_also_reads_modified_and_deleted_src(
    modified: tuple[str, ...], deleted: tuple[str, ...]
) -> None:
    found = _failures(_body(), modified=modified, deleted=deleted)
    assert any("path:line" in failure for failure in found)


@pytest.mark.parametrize("separator", ["\n\n", "\n- ", "\n* ", "\n1. ", "\n| "])
@pytest.mark.parametrize(
    ("second", "missing"),
    [
        (OTHER_PIN + " BUILD ON", "path:line"),
        (OTHER_PIN + " src/clock.py:45", "verdict"),
        ("doi:10.1234/abc src/clock.py:45 BUILD ON", "pinned source"),
        (OTHER_PIN + " src/clock.py:45 KEEP OURS", "Where searched"),
    ],
)
def test_each_mechanism_entry_needs_its_own_evidence(
    separator: str, second: str, missing: str
) -> None:
    first = PIN + " src/client.py:12 BUILD ON"
    found = _failures(_body(sources=first + separator + second), created=(SOURCE,))
    assert any(missing in failure for failure in found)


@pytest.mark.parametrize(
    "sources",
    [
        f"- Client: {PIN} src/client.py:12 BUILD ON\n"
        f"- Clock: {OTHER_PIN} src/clock.py:45 USE AS IS",
        "| Mechanism | Upstream | Verdict |\n|---|---|---|\n"
        f"| Client | {PIN} src/client.py:12 | BUILD ON |\n"
        f"| Clock | {OTHER_PIN} src/clock.py:45 | USE AS IS |",
        f"- Shipped piece: {PIN} src/client.py:12\n"
        "- Verdict: KEEP OURS\n- Where searched: src/ and docs/; client",
    ],
)
def test_complete_mechanism_entries_pass(sources: str) -> None:
    assert _failures(_body(sources=sources), created=(SOURCE,)) == []


@pytest.mark.parametrize(
    "markers", [("-", "-"), ("+", "+"), ("*", "*"), ("1.", "2."), ("1)", "2)")]
)
@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
@pytest.mark.parametrize(
    ("clock", "missing"),
    [
        (OTHER_PIN + " BUILD ON", "path:line"),
        ("src/clock.py:45 USE AS IS", "pinned source"),
        (OTHER_PIN + " src/clock.py:45", "verdict"),
    ],
)
def test_all_commonmark_markers_separate_mechanism_evidence(
    markers: tuple[str, str], indent: str, clock: str, missing: str
) -> None:
    sources = (
        f"{indent}{markers[0]} Client: {PIN} src/client.py:12 BUILD ON\n"
        f"{indent}{markers[1]} Clock: {clock}"
    )
    found = _failures(_body(sources=sources), created=(SOURCE,))
    assert any(missing in failure for failure in found)


@pytest.mark.parametrize(
    ("original", "destination", "sources", "blocked"),
    [
        (SOURCE, "tools/renamed.py", PIN, True),
        ("tools/client.py", "src/sextant_movers/renamed.py", PIN, True),
        ("docs/old.md", "docs/renamed.md", PIN, False),
        (SOURCE, "tools/renamed.py", PIN + " src/client.py:12 BUILD ON", False),
    ],
)
def test_rename_scope_reads_the_native_from_path(
    original: str, destination: str, sources: str, blocked: bool
) -> None:
    # Danger 14.1.0 diffToGitJSONDSL.ts:22 retains only the destination.
    # GitDSL.ts:24-27 / gitJSONToGitDSL.ts:194-213 expose the original in fromPath.
    found = _failures(
        _body(sources=sources),
        modified=(destination,),
        structured_diffs={destination: {"fromPath": original, "chunks": []}},
    )
    assert bool(found) is blocked
    if blocked:
        assert any("path:line" in failure for failure in found)


@pytest.mark.parametrize("marker", ["-", "+", "*", "1.", "1)"])
@pytest.mark.parametrize("gap", ["\n", "\n\n"])
def test_nested_citation_fields_belong_to_the_parent_mechanism(marker: str, gap: str) -> None:
    indent = " " * (len(marker) + 1)
    sources = (
        f"{marker} Client: {PIN}{gap}"
        f"{indent}- Shipped piece: `src/client.py:12-40`\n"
        f"{indent}- Verdict: BUILD ON"
    )
    assert _failures(_body(sources=sources), created=(SOURCE,)) == []


@pytest.mark.parametrize(
    "location",
    ["`src/client.py`:12", "`src/vendor:client.py:12`", "`src/client.py:12-40`"],
)
def test_source_locations_accept_code_spans_and_colons_in_filenames(location: str) -> None:
    assert _failures(_body(sources=f"{PIN} {location} BUILD ON"), created=(SOURCE,)) == []


@pytest.mark.parametrize("location", ["https://x.dev:443", "src/client.py:0", "src/vendor:12:0"])
def test_source_locations_require_a_positive_last_line_group(location: str) -> None:
    found = _failures(_body(sources=f"{PIN} {location} BUILD ON"), created=(SOURCE,))
    assert any("path:line" in failure for failure in found)


# Nested source blocks in the shape of two Sextant PR bodies: the upstream pins, paths and nesting
# are theirs; Sextant's own policy lines are replaced by placeholder context lines.
NESTED_A = """\
- Native cancel/resubmit: BUILD ON nautechsystems/nautilus_trader@v2.0.0rc8
  `d53849b216e20f2c122ca01b4dd0da377638b186`:
  - `crates/model/src/orders/mod.rs:1313-1320`;
  - `crates/trading/src/examples/strategies/grid_mm/strategy.rs:249-258`
    (the official example cancels and resubmits).
  - ADR-0002:17, :25; docs/design.md for context.
- Broker docs: USE AS IS https://docs.alpaca.markets/docs/orders, read 2026-11-11:
  - `docs-saved/alpaca-orders.md:165`.
"""
NESTED_B = """\
- Native cancel policy: BUILD ON nautechsystems/nautilus_trader@v2.0.0rc8
  `d53849b216e20f2c122ca01b4dd0da377638b186`:
  - `crates/model/src/events/order/denied.rs:36-39`, `rejected.rs:36`;
  - `crates/adapters/interactive_brokers/src/execution/core.rs:1032-1043`, `:2191-2196`;
  - `crates/adapters/interactive_brokers/src/execution/transform/policy.rs:91-126`;
  - `crates/adapters/interactive_brokers/src/execution/transform/tags.rs:30-51`;
  - `docs/integrations/interactive_brokers.md:418-428`.
  - ADR-0002:17, :27; docs/design.md for context.
  - d/e@v2 dddddddddddddddddddddddddddddddddddddddd
    `src/clock.py:45`.
"""


@pytest.mark.parametrize("sources", [NESTED_A, NESTED_B], ids=["nested-a", "nested-b"])
def test_nested_citation_blocks_pass(sources: str) -> None:
    assert _failures(_body(sources=sources), modified=(RANKING,)) == []
