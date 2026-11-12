"""An uncalibrated starting selection from Nasdaq Trader directory text.

Select by Test Issue and ETF flags, listing exchange and Nasdaq's published symbol forms.
Neither file carries a security-type column: an instrument with a share-form symbol is
selected whatever its name says, and a share with any other fifth letter is skipped, never
guessed. A rule on the names' wording has no official source and is not built. No name is read.

Sources, read by the lead 2026-11-11: Nasdaq Trader, Symbol Directory Definitions,
https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs (flags, exchange codes and fields);
Ticker Symbol Convention, https://www.nasdaqtrader.com/trader.aspx?id=CQSsymbolconvention
(NASDAQ Integrated Platform: a class is a dot and one capital letter; other categories have
other punctuation). ACT/CTCI and CQS suffixes overlap classes, so an other-listed row is
selected only when ACT Symbol equals NASDAQ Symbol, without translation.
Nasdaq, List of Fifth Character Symbol Suffixes,
https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/
nasdaqfifthcharactersuffixlist.pdf (one to four letters for common stock; five for subordinate
issues; the share forms below). Nasdaq reserves the right to change this methodology.
python/cpython@v3.13.18 0d4bd512fc429e3d4eacdd113c975e161f21644d:Doc/library/re.rst:952
(`fullmatch`: the whole string). The reference, at 06562bec0898a7203f4eaf69086ced83d2477e6b,
src/sextant/tradingview/bars.py:162: only the universe signature, empty-call refusal, sorted
union and active intersection; its name and dash-suffix patterns are not carried.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from sextant_movers.ranking.listings import HEADERS, OTHER_LISTED_EXCHANGES

# Fifth Character Symbol Suffixes, cited above: class A/B, foreign, voting/non-voting, SBI, ADR.
NASDAQ_SHARE_FIFTH_LETTERS = frozenset("ABFJKSY")
_NASDAQ_FORM = re.compile(r"[A-Z]{1,5}")
_OTHER_FORM = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?")
_NASDAQ_REASONS = ("test", "etf", "symbol_form", "fifth_letter", "selected")
_OTHER_REASONS = ("test", "etf", "exchange", "symbol_form", "symbology", "selected")


@dataclass(frozen=True, slots=True)
class Selection:
    symbols: tuple[str, ...]
    counts: Mapping[str, int]


def select(text: str) -> Selection:
    """Select one exact-header directory; count each row under its first applicable reason."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or lines[0] not in HEADERS.values():
        raise ValueError("NASDAQ Trader selection requires an exact directory header")
    header = lines[0].split("|")
    nasdaq = lines[0] == HEADERS["nasdaqlisted.txt"]
    test_col, etf_col = header.index("Test Issue"), header.index("ETF")
    symbol_col = header.index("Symbol" if nasdaq else "ACT Symbol")
    rows: list[list[str]] = []
    # Refuse invalid flags anywhere in the text before selecting or counting any row.
    for line in lines[1:]:
        fields = [field.strip() for field in line.split("|")]
        if "File Creation Time" in fields[0]:
            continue
        if len(fields) != len(header):
            raise ValueError("Malformed NASDAQ Trader selection row")
        if fields[test_col] not in {"Y", "N"} or fields[etf_col] not in {"Y", "N"}:
            raise ValueError("NASDAQ Trader selection flags must be Y or N")
        rows.append(fields)

    counts = dict.fromkeys(_NASDAQ_REASONS if nasdaq else _OTHER_REASONS, 0)
    symbols: set[str] = set()
    for fields in rows:
        symbol = fields[symbol_col]
        reason = "selected"
        if fields[test_col] == "Y":
            reason = "test"
        elif fields[etf_col] == "Y":
            reason = "etf"
        elif nasdaq:
            if _NASDAQ_FORM.fullmatch(symbol) is None:
                reason = "symbol_form"
            elif len(symbol) == 5 and symbol[-1] not in NASDAQ_SHARE_FIFTH_LETTERS:
                reason = "fifth_letter"
        elif fields[header.index("Exchange")] not in OTHER_LISTED_EXCHANGES:
            reason = "exchange"
        elif _OTHER_FORM.fullmatch(fields[header.index("NASDAQ Symbol")]) is None:
            reason = "symbol_form"
        elif symbol != fields[header.index("NASDAQ Symbol")]:
            reason = "symbology"
        counts[reason] += 1
        if reason == "selected":
            symbols.add(symbol)
    return Selection(tuple(sorted(symbols)), counts)


def build_universe(*directories: str, active: set[str] | None = None) -> tuple[str, ...]:
    """Sorted union of directory selections, optionally intersected with caller activity."""
    if not directories:
        raise ValueError("At least one directory text is required")
    symbols = {symbol for directory in directories for symbol in select(directory).symbols}
    if active is not None:
        symbols.intersection_update(active)
    return tuple(sorted(symbols))
