"""Directory selection from Nasdaq's published flags, exchanges and symbol forms.

Only invented rows, built from HEADERS here; no fixture, recorded row or external input.
The one-letter case is Z; all longer roots start ZZ. Sources and limits: ranking/universe.py.
"""

import ast
import string
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from sextant_movers.ranking import universe as uni
from sextant_movers.ranking.listings import HEADERS

NASDAQ, OTHER = "nasdaqlisted.txt", "otherlisted.txt"
N_REASONS = ("test", "etf", "symbol_form", "fifth_letter", "selected")
O_REASONS = ("test", "etf", "exchange", "symbol_form", "symbology", "selected")


def row(file: str, symbol: str = "ZZAA", **changes: str) -> str:
    values = {
        "Symbol": symbol,
        "ACT Symbol": symbol,
        "NASDAQ Symbol": symbol,
        "CQS Symbol": symbol,
        "Security Name": "Zz Invented Issuance",
        "Market Category": "Q",
        "Financial Status": "N",
        "Round Lot Size": "100",
        "Test Issue": "N",
        "ETF": "N",
        "NextShares": "N",
        "Exchange": "N",
    }
    return "|".join((values | changes)[key] for key in HEADERS[file].split("|"))


def text(file: str, *rows: str) -> str:
    return "\n".join([HEADERS[file], *rows])


def counted(file: str, reason: str, *rows: str) -> uni.Selection:
    result = uni.select(text(file, *rows))
    reasons = N_REASONS if file == NASDAQ else O_REASONS
    assert dict(result.counts) == {key: len(rows) if key == reason else 0 for key in reasons}
    if reason != "selected":
        assert result.symbols == ()
    return result


def test_nasdaq_lengths_and_all_fifth_letters() -> None:
    short = ["Z", "ZZ", "ZZA", "ZZAA"]
    fifths = ["ZZAA" + letter for letter in string.ascii_uppercase]
    result = uni.select(text(NASDAQ, *(row(NASDAQ, s) for s in short + fifths)))
    assert result.symbols == tuple(sorted(short + ["ZZAA" + c for c in "ABFJKSY"]))
    assert dict(result.counts) == dict.fromkeys(N_REASONS, 0) | {
        "selected": 11,
        "fifth_letter": 19,
    }
    assert set(uni.NASDAQ_SHARE_FIFTH_LETTERS) == set("ABFJKSY")


def test_only_a_fifth_letter_is_read_as_a_suffix() -> None:
    # Up to four letters are a common stock's symbol whatever its last letter is.
    symbols = ["ZZ" + c for c in string.ascii_uppercase]
    symbols += ["ZZA" + c for c in string.ascii_uppercase]
    result = counted(NASDAQ, "selected", *(row(NASDAQ, s) for s in symbols))
    assert result.symbols == tuple(sorted(symbols))


@pytest.mark.parametrize("symbol", ["ZZAAAA", "ZZa", "ZZ1", "ZZ.A", "", "ZZ\N{KELVIN SIGN}"])
def test_nasdaq_bad_forms(symbol: str) -> None:
    counted(NASDAQ, "symbol_form", row(NASDAQ, symbol))


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
@pytest.mark.parametrize("flag,reason", [("Test Issue", "test"), ("ETF", "etf")])
def test_flags_exclude_rows(file: str, flag: str, reason: str) -> None:
    counted(file, reason, row(file, **{flag: "Y"}))


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
@pytest.mark.parametrize("flag", ["Test Issue", "ETF"])
@pytest.mark.parametrize("value", ["", "y", "X"])
@pytest.mark.parametrize("placement", ["alone", "otherwise_skipped", "last_after_selected"])
def test_bad_flags_refuse_the_whole_text(file: str, flag: str, value: str, placement: str) -> None:
    changes = {flag: value}
    if placement == "otherwise_skipped":
        changes["ETF" if flag == "Test Issue" else "Test Issue"] = "Y"
        changes["Exchange"] = ""
    damaged = row(file, "ZZ!" if placement == "otherwise_skipped" else "ZZAA", **changes)
    rows = [row(file), damaged] if placement == "last_after_selected" else [damaged]
    with pytest.raises(ValueError):
        uni.select(text(file, *rows))


@pytest.mark.parametrize("exchange", list("ANPZV"))
def test_each_published_exchange_is_selected(exchange: str) -> None:
    assert counted(OTHER, "selected", row(OTHER, Exchange=exchange)).symbols == ("ZZAA",)


@pytest.mark.parametrize("exchange", ["Q", ""])
def test_other_exchanges_are_counted(exchange: str) -> None:
    counted(OTHER, "exchange", row(OTHER, Exchange=exchange))


def test_other_roots_and_every_class_letter() -> None:
    symbols = ["Z", "ZZ", "ZZA", "ZZAA", "ZZAAA"]
    symbols += ["ZZ." + c for c in string.ascii_uppercase]
    result = counted(OTHER, "selected", *(row(OTHER, s) for s in symbols))
    assert result.symbols == tuple(sorted(symbols))


@pytest.mark.parametrize(
    "symbol",
    ["ZZAAAA", "ZZ.AA", "ZZ.a", "ZZ.1", "ZZ.", "ZZa", "ZZ1", ""]
    + ["ZZ" + mark + "A" for mark in "-+^=#*%$!@~"],
)
def test_other_non_class_forms_are_counted(symbol: str) -> None:
    counted(OTHER, "symbol_form", row(OTHER, symbol))


def test_act_must_equal_nasdaq_symbol() -> None:
    for act in ("ZZAB", "ZZ/A", ""):
        counted(OTHER, "symbology", row(OTHER, act, **{"NASDAQ Symbol": "ZZ.A"}))


# Each entry triggers two adjacent exclusions; selected is the final fallback.
@pytest.mark.parametrize(
    "file,reason,changes",
    [
        (NASDAQ, "test", {"Test Issue": "Y", "ETF": "Y"}),
        (NASDAQ, "etf", {"ETF": "Y", "Symbol": "ZZ!"}),
        (NASDAQ, "symbol_form", {"Symbol": "ZZ!AW"}),
        (NASDAQ, "fifth_letter", {"Symbol": "ZZAAW"}),
        (OTHER, "test", {"Test Issue": "Y", "ETF": "Y"}),
        (OTHER, "etf", {"ETF": "Y", "Exchange": ""}),
        (OTHER, "exchange", {"Exchange": "", "NASDAQ Symbol": "ZZ!"}),
        (OTHER, "symbol_form", {"NASDAQ Symbol": "ZZ!"}),
        (OTHER, "symbology", {"NASDAQ Symbol": "ZZ.A"}),
    ],
)
def test_adjacent_reasons_use_the_first(file: str, reason: str, changes: dict[str, str]) -> None:
    counted(file, reason, row(file, **changes))


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
def test_counts_cover_each_row_and_symbols_are_sorted_unique(file: str) -> None:
    rows = [row(file, "ZZAB"), row(file), row(file), row(file, **{"Test Issue": "Y"})]
    rows += [row(file, ETF="Y"), row(file, "ZZ!")]
    expected = {"test": 1, "etf": 1, "symbol_form": 1, "selected": 3}
    if file == NASDAQ:
        rows += [row(file, "ZZAAW")]
        expected["fifth_letter"] = 1
    else:
        rows += [row(file, Exchange="Q"), row(file, **{"NASDAQ Symbol": "ZZ.A"})]
        expected |= {"exchange": 1, "symbology": 1}
    result = uni.select(text(file, *rows))
    assert result.symbols == ("ZZAA", "ZZAB")
    assert dict(result.counts) == expected
    assert sum(result.counts.values()) == len(rows)


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
def test_header_alone_and_selection_shape(file: str) -> None:
    result = uni.select(HEADERS[file])
    assert isinstance(result, uni.Selection)
    assert result.symbols == ()
    assert dict(result.counts) == dict.fromkeys(N_REASONS if file == NASDAQ else O_REASONS, 0)
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.symbols = ("ZZ",)  # type: ignore[misc]


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
def test_blanks_trailer_and_stripped_fields(file: str) -> None:
    padded = " | ".join(row(file).split("|"))
    body = "\r\n".join(
        ["", "   ", HEADERS[file], "", padded, "", " Zz File Creation Time ignored|", ""]
    )
    assert uni.select(body) == uni.select(text(file, row(file)))


@pytest.mark.parametrize(
    "body",
    [
        "",
        "\n \n",
        "Zz preamble",
        "Symbol|Security Name",
        " " + HEADERS[NASDAQ],
        "Zz preamble\n" + HEADERS[NASDAQ],
        HEADERS[OTHER].lower(),
        HEADERS[NASDAQ] + " ",
        HEADERS[OTHER] + " ",
    ],
)
def test_any_other_first_nonempty_line_is_refused(body: str) -> None:
    with pytest.raises(ValueError):
        uni.select(body)


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
@pytest.mark.parametrize("extra", [False, True])
def test_wrong_field_count_refuses_text(file: str, extra: bool) -> None:
    good = row(file)
    damaged = good + "|N" if extra else good.rsplit("|", 1)[0]
    with pytest.raises(ValueError):
        uni.select(text(file, good, damaged))


def test_universe_requires_text_and_unions_then_intersects() -> None:
    with pytest.raises(ValueError):
        uni.build_universe()
    nasdaq = text(NASDAQ, row(NASDAQ, "ZZAB"), row(NASDAQ))
    other = text(OTHER, row(OTHER, "ZZ.A"), row(OTHER))
    expected = ("ZZ.A", "ZZAA", "ZZAB")
    assert uni.build_universe(nasdaq, other) == expected
    assert uni.build_universe(other, nasdaq, active=None) == expected
    assert uni.build_universe(nasdaq, other, active={"ZZAA", "ZZAC"}) == ("ZZAA",)
    assert uni.build_universe(nasdaq, other, active=set()) == ()


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
def test_security_name_never_changes_selection(file: str) -> None:
    symbols = ["ZZAA", "ZZAB", "ZZAAW", "ZZ+", "ZZ.A"]
    original = text(file, *(row(file, s) for s in symbols))
    for name in ("", "Zz Synthetic Warrant", "Zz Synthetic Preferred Share", "Zz Units"):
        changed = text(file, *(row(file, s, **{"Security Name": name}) for s in symbols))
        assert uni.select(changed) == uni.select(original)
        assert uni.build_universe(changed) == uni.build_universe(original)


def test_imports_are_stdlib_and_listings_constants_only() -> None:
    tree = ast.parse(Path(uni.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    outside = {m for m in imported if m.split(".")[0] not in sys.stdlib_module_names}
    assert outside == {"sextant_movers.ranking.listings"}
    names = {
        a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "sextant_movers.ranking.listings"
        for a in n.names
    }
    assert names == {"HEADERS", "OTHER_LISTED_EXCHANGES"}


def test_seeded_selection_is_a_subset_of_parsed_non_test_non_etf_rows() -> None:
    import random

    from sextant_movers.ranking.listings import parse_directory

    rng = random.Random(20261112)  # noqa: S311 - invented directory texts
    accepted = 0
    for index in range(360):
        file = (NASDAQ, OTHER)[index % 2]
        rows = [
            row(
                file,
                rng.choice(("ZZAA", "ZZBB", "ZZCC.A", "ZZZZF", "ZZQQW")),
                **{
                    "Test Issue": rng.choice(("Y", "N")),
                    "ETF": rng.choice(("Y", "N")),
                    "Exchange": rng.choice(("N", "P", "?")),
                },
            )
            for _ in range(rng.randint(1, 5))
        ]
        fault = index % 6
        if fault == 1:
            rows[0] += "|extra"
        elif fault == 2:
            rows[0] = rows[0].rsplit("|", 1)[0]
        elif fault == 3:
            rows[0] = row(file, **{"Test Issue": "?"})
        value = text(file, *rows)
        if fault == 4:
            value = "zz-preface\n" + value
        elif fault == 5:
            value += "\nFile Creation Time: 1112202612:00|||||||"
        try:
            selection = uni.select(value)
        except ValueError:
            continue
        parsed = parse_directory(value)
        accepted += 1
        eligible = {r.symbol for r in parsed if not r.test_issue and not r.etf}
        assert set(selection.symbols) <= eligible, value
    assert accepted == 120


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
def test_preface_is_accepted_by_parser_but_refused_by_selection(file: str) -> None:
    from sextant_movers.ranking.listings import parse_directory

    value = "zz-preface\n" + text(file, row(file))
    assert len(parse_directory(value)) == 1
    with pytest.raises(
        ValueError, match=r"^NASDAQ Trader selection requires an exact directory header$"
    ):
        uni.select(value)


@pytest.mark.parametrize("file", [NASDAQ, OTHER])
def test_extra_field_is_accepted_by_parser_but_refused_by_selection(file: str) -> None:
    from sextant_movers.ranking.listings import parse_directory

    value = text(file, row(file) + "|extra")
    assert len(parse_directory(value)) == 1
    with pytest.raises(ValueError, match=r"^Malformed NASDAQ Trader selection row$"):
        uni.select(value)
