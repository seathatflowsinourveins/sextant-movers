"""Part 6: a script-free page from one synthetic published generation.

Reference acceptance: src/sextant/tradingview/ranked_view.py:1-245 at
06562bec0898a7203f4eaf69086ced83d2477e6b. CPython sources are named in the module.
"""

import ast
import html
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal as D
from decimal import InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from test_publish import COVER, NOW, NS, filled, full

from sextant_movers.ranking.engine import GATES
from sextant_movers.ranking.generation import RankingFeature, RankingGeneration, RankingReason
from sextant_movers.sessions import ET
from sextant_movers.tradingview import ranked_view as view


def generation() -> RankingGeneration:
    published = full(filled())
    first, second, third = published.rows
    values = ("0.123456789", "0.00005", "-0.0125", "3.445", None)
    features = tuple(
        RankingFeature(name, None if value is None else D(value), NOW, NOW)
        for name, value in zip(view.FEATURES, values, strict=True)
    )
    metadata = tuple(RankingReason(code, "zz-metadata") for code in view.METADATA)
    return replace(
        published,
        rows=(
            replace(
                second,
                symbol="ZZVAB",
                rank=2,
                features=features,
                focus_eligible=False,
                reasons=(
                    *metadata,
                    RankingReason("gate_failed", GATES[0]),
                    RankingReason("ZZ_NO"),
                    RankingReason("ZZ_DETAIL", "zz-detail"),
                ),
            ),
            replace(third, symbol="ZZVAC", rank=None, reasons=(RankingReason("ZZ_UNRANKED"),)),
            replace(
                first,
                symbol="ZZVAA",
                rank=1,
                score=D("0.123000"),
                features=features,
                focus_eligible=True,
                reasons=(*metadata, RankingReason("ZZ_FOCUS")),
            ),
        ),
    )


def document(**kw: Any) -> dict[str, Any]:
    gen = view.generation_view(generation(), segment="RTH", top_n=2, universe_source="zz-universe")
    args: dict[str, Any] = dict(reason=None, max_age_s=5, refresh_s=2, written_at_ns=NOW)
    return view.view_document(gen, **(args | kw))


def test_view_rows_has_each_recorded_field_rank_order_and_no_metadata() -> None:
    assert view.FEATURES == ("move", "move_1m", "move_5m", "dvol_ratio", "trades_accel")
    assert view.METADATA == frozenset({"SOURCE", "ASSET", "REFERENCE", "LABELS", "CLOCKS"})
    rows = view.view_rows(generation())
    assert rows == [
        {
            "symbol": "ZZVAA",
            "rank": 1,
            "score": "0.123000",
            "move": "0.123456789",
            "move_1m": "0.00005",
            "move_5m": "-0.0125",
            "dvol_ratio": "3.445",
            "trades_accel": None,
            "gates_passed": list(GATES),
            "focus_eligible": True,
            "reasons": ["ZZ_FOCUS"],
        },
        {
            "symbol": "ZZVAB",
            "rank": 2,
            "score": view._text(generation().rows[1].score),
            "move": "0.123456789",
            "move_1m": "0.00005",
            "move_5m": "-0.0125",
            "dvol_ratio": "3.445",
            "trades_accel": None,
            "gates_passed": list(GATES[1:]),
            "focus_eligible": False,
            "reasons": [f"gate_failed:{GATES[0]}", "ZZ_NO", "ZZ_DETAIL:zz-detail"],
        },
    ]
    gen = generation()
    changed = replace(gen, rows=(replace(gen.rows[0], score=None, focus_eligible=None),))
    assert view.view_rows(changed)[0]["score"] is None
    assert view.view_rows(changed)[0]["focus_eligible"] is False
    assert view.GATE_LABELS == dict(zip(GATES, ("move", "1m up", "accel", "dvol"), strict=True))


@pytest.mark.parametrize("gate", [*GATES, None])
def test_each_gate_failure_removes_only_its_named_gate(gate: str | None) -> None:
    gen = generation()
    row = replace(gen.rows[0], reasons=(RankingReason("gate_failed", gate),))
    actual = view.view_rows(replace(gen, rows=(row,)))[0]
    assert actual["gates_passed"] == [g for g in GATES if g != gate]
    assert actual["reasons"] == ["gate_failed" if gate is None else f"gate_failed:{gate}"]


def test_generation_view_has_every_key_and_all_rows_despite_top_n() -> None:
    gen = generation()
    assert view.generation_view(gen, segment="PRE", top_n=1, universe_source="zz-source") == {
        "generated_at_ns": NOW,
        "session_date": gen.session_date.isoformat(),
        "segment": "PRE",
        "coverage": {
            "state": COVER.state.value,
            "complete": True,
            "expected_symbols": 3,
            "observed_symbols": 3,
        },
        "rows_total": 3,
        "ranked": 2,
        "top_n": 1,
        "universe_source": "zz-source",
        "rows": view.view_rows(gen),
    }


def test_document_has_every_key_and_unavailable_carries_last_generation() -> None:
    doc = document()
    assert doc == {
        "schema": "sextant.tv-ranked-view/1",
        "display_only": True,
        "status": "fresh",
        "reason": None,
        "written_at_ns": NOW,
        "max_age_s": 5,
        "refresh_s": 2,
        "generation": doc["generation"],
    }
    refused = document(reason="zz-refusal")
    assert refused == doc | {"status": "unavailable", "reason": "zz-refusal"}
    assert refused["generation"] == doc["generation"]
    assert refused["generation"] is not doc["generation"]
    assert view.view_document(
        None, reason="zz-missing", max_age_s=5, refresh_s=None, written_at_ns=NOW
    ) == doc | {
        "status": "unavailable",
        "reason": "zz-missing",
        "refresh_s": None,
        "generation": None,
    }


@pytest.mark.parametrize(
    ("age", "reason", "banners"),
    [
        (0, None, ("FRESH",)),
        (5, None, ("FRESH",)),
        (6, None, ("STALE",)),
        (0, "zz-refusal", ("UNAVAILABLE",)),
        (6, "zz-refusal", ("UNAVAILABLE", "STALE")),
    ],
)
def test_kept_banners_age_at_render_and_unavailable_never_fresh(
    age: int, reason: str | None, banners: tuple[str, ...]
) -> None:
    page = view.render_html(document(reason=reason), now_ns=NOW + age * NS)
    for banner in ("FRESH", "STALE", "UNAVAILABLE"):
        assert (banner in page) == (banner in banners)
    assert "this page cannot age itself; compare Stale after with your clock" in page
    assert "ZZVAA" in page
    if "STALE" in banners:
        assert "STALE: generation 6 s old (bound 5 s)" in page


def test_facts_numbers_gates_focus_and_kept_reason_visibility() -> None:
    doc = document()
    page = view.render_html(doc, now_ns=NOW)

    def clock(ns):
        return datetime.fromtimestamp(ns // NS, ET).strftime("%Y-%m-%d %H:%M:%S %Z")

    facts = {
        "Page written": clock(NOW),
        "Generated": f"{clock(NOW)} (age 0 s at render)",
        "Stale after": clock(NOW + 5 * NS),
        "Session segment": f"{generation().session_date.isoformat()} RTH",
        "Coverage": "READY, complete True, 3 of 3 observed",
        "Ranked": "2 of 3 names (table shows the top 2)",
        "Universe": "zz-universe",
        "Reloads every": "2 s (J5 cadence floor)",
        "Note": "this page cannot age itself; compare Stale after with your clock",
    }
    for key, value in facts.items():
        assert f"<dt>{key}</dt><dd>{value}</dd>" in page
    for cell in (
        'title="0.123456789">+12.35',
        'title="0.00005">+0.00',
        'title="-0.0125">-1.25',
        'title="3.445">3.44',
        '<td class="n">n/a</td>',
    ):
        assert cell in page
    first = page.split("<td>ZZVAA</td>")[1].split("</tr>")[0]
    second = page.split("<td>ZZVAB</td>")[1].split("</tr>")[0]
    assert first.count('<td class="yes">yes</td>') == 5
    assert second.count('<td class="yes">yes</td>') == 3
    assert second.count('<td class="no">no</td>') == 2
    assert "ZZ_FOCUS" not in first
    assert f"gate_failed:{GATES[0]}, ZZ_NO, ZZ_DETAIL:zz-detail" in second
    assert "ZZ_UNRANKED" not in page and "ZZVAC" not in page
    assert view.ET is ET


def test_kept_top_n_cuts_table_but_json_rows_stay_complete() -> None:
    doc = document()
    doc["generation"]["top_n"] = 1
    assert len(doc["generation"]["rows"]) == 2
    page = view.render_html(doc, now_ns=NOW)
    assert "ZZVAA" in page and "ZZVAB" not in page


@pytest.mark.parametrize("refresh", [None, 0, -1, True, False, 2.0, "2", 2])
def test_kept_refresh_requires_a_positive_int(refresh: Any) -> None:
    page = view.render_html(document(refresh_s=refresh), now_ns=NOW)
    enabled = type(refresh) is int and refresh > 0
    assert ('<meta http-equiv="refresh" content="2">' in page) == enabled
    assert ("Reloads</dt><dd>never: outside the display window" in page) != enabled


class ParsedPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def test_special_characters_escape_in_text_and_numeric_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, escaped = "ZZESC<>&\"'", "ZZESC&lt;&gt;&amp;&quot;&#x27;"
    gen = generation()
    gen = replace(
        gen,
        rows=(replace(gen.rows[1], symbol=raw, reasons=(RankingReason("ZZ_DETAIL", raw),)),),
    )
    doc = document(reason=raw)
    doc["generation"] = view.generation_view(gen, segment="PRE", top_n=1, universe_source=raw)
    page = view.render_html(doc, now_ns=NOW)
    for expected in (f"<td>{escaped}</td>", f"ZZ_DETAIL:{escaped}", f"<dd>{escaped}</dd>"):
        assert expected in page
    assert f"UNAVAILABLE: {escaped}" in page and raw not in page
    # A valid Decimal cannot contain these characters. Isolate title escaping after conversion.
    monkeypatch.setattr(view, "Decimal", lambda _: D(1))
    assert view._number(raw, percent=False) == f'<td class="n" title="{escaped}">1.00</td>'
    assert html.unescape(escaped) == raw


def test_page_parser_has_no_active_element_or_external_reference() -> None:
    page = view.render_html(document(), now_ns=NOW)
    parser = ParsedPage()
    parser.feed(page)
    assert all(tag not in {"script", "link"} for tag, _ in parser.elements)
    assert all(not ({"src", "href"} & attrs.keys()) for _, attrs in parser.elements)
    assert "http-equiv" in page  # The only http text is the local reload pragma.
    for text in ("src=", "href=", "<script", "url(", "http://", "https://"):
        assert text not in page
    refused = view.view_document(
        None, reason="zz-missing", max_age_s=5, refresh_s=None, written_at_ns=NOW
    )
    page = view.render_html(refused, now_ns=NOW)
    assert "UNAVAILABLE: zz-missing" in page and "<table>" not in page
    assert "FRESH" not in page


@pytest.mark.parametrize(("top_n", "shown"), [(0, ()), (-1, ("ZZVAA",)), (-2, ())])
def test_observed_nonpositive_top_n_uses_python_slice(top_n: int, shown: tuple[str, ...]) -> None:
    doc = document()
    doc["generation"]["top_n"] = top_n
    page = view.render_html(doc, now_ns=NOW)
    assert tuple(name for name in ("ZZVAA", "ZZVAB") if name in page) == shown


def test_observed_missing_generation_key_raises_and_future_clock_clamps_age() -> None:
    doc = document()
    del doc["generation"]["coverage"]
    with pytest.raises(KeyError, match="coverage"):
        view.render_html(doc, now_ns=NOW)
    page = view.render_html(document(), now_ns=NOW - NS)
    assert "FRESH" in page and "age 0 s at render" in page


@pytest.mark.parametrize(("value", "error"), [("NaN", None), ("Infinity", InvalidOperation)])
def test_observed_nonfinite_feature_in_a_document(value: str, error: Any) -> None:
    doc = document()
    doc["generation"]["rows"][0]["move"] = value
    if error:
        with pytest.raises(error):
            view.render_html(doc, now_ns=NOW)
    else:
        assert 'title="NaN">+NaN</td>' in view.render_html(doc, now_ns=NOW)
    with pytest.raises(ValueError, match="finite Decimal"):
        RankingFeature("move", D(value))


def test_write_view_json_page_modes_path_and_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    json_write, text_write = view.write_atomic, view.write_atomic_text
    doc = document()

    def json_spy(path: Path, value: Any) -> None:
        events.append(path.name)
        json_write(path, value)

    def text_spy(path: Path, value: str) -> None:
        events.append(path.name)
        assert json.loads((path.parent / "ranked.json").read_text()) == doc
        text_write(path, value)

    monkeypatch.setattr(view, "write_atomic", json_spy)
    monkeypatch.setattr(view, "write_atomic_text", text_spy)
    directory = tmp_path / "view"
    page = view.write_view(directory, doc, now_ns=NOW)
    assert page == directory / "ranked.html"
    assert events == ["ranked.json", "ranked.html"]
    assert json.loads((directory / "ranked.json").read_text()) == doc
    assert page.read_bytes() == view.render_html(doc, now_ns=NOW).encode("utf-8")
    assert {p.name for p in directory.iterdir()} == {"ranked.json", "ranked.html"}
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())


def test_second_write_failure_leaves_new_json_and_old_page_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    old = document()
    page = view.write_view(tmp_path, old, now_ns=NOW)
    old_bytes = page.read_bytes()
    real_replace = os.replace

    def fail_text(source: str, target: Path) -> None:
        if target.suffix == ".html":
            raise OSError("zz-page-replace")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_text)
    new = document(reason="zz-refused")
    with pytest.raises(OSError, match="zz-page-replace"):
        view.write_view(tmp_path, new, now_ns=NOW)
    assert json.loads((tmp_path / "ranked.json").read_text()) == new
    assert page.read_bytes() == old_bytes
    assert {p.name for p in tmp_path.iterdir()} == {"ranked.json", "ranked.html"}


def test_source_has_only_display_imports_and_delegates_all_file_writing() -> None:
    package = Path(view.__file__).parent
    allowed = {
        "__future__",
        "html",
        "collections.abc",
        "datetime",
        "decimal",
        "pathlib",
        "typing",
        "sextant_movers.atomic",
        "sextant_movers.ranking.engine",
        "sextant_movers.ranking.generation",
        "sextant_movers.sessions",
    }
    for path in (package / "__init__.py", package / "ranked_view.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name in allowed for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.level == 0 and node.module in allowed
                if node.module == "sextant_movers.ranking.engine":
                    assert [alias.name for alias in node.names] == ["GATES"]
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in {"open", "exec", "eval", "__import__"}
                if isinstance(node.func, ast.Attribute):
                    assert node.func.attr not in {
                        "open",
                        "write",
                        "write_text",
                        "write_bytes",
                        "replace",
                        "mkdir",
                        "mkstemp",
                        "fsync",
                        "unlink",
                        "fdopen",
                    }
        if path.name == "__init__.py":
            assert len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr)
