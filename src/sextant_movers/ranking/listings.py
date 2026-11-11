"""The Nasdaq Trader symbol directory files: parse, validate and stage them.

`parse_directory` returns every row with its flags and its exchange code; nothing here decides
which rows are a universe, and the filter that would is not built until it has a source.
`stage` downloads both files, validates each (the exact header, a field count per row equal to
the header's, the File Creation Time trailer as the last line) and only then replaces them; a
truncated download or an HTML page fails a check and the previous files stay. No key is used
and nothing here calls TradingView or a broker.

Sources. Nasdaq Trader, "Symbol Directory Definitions",
https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs, read 2026-11-11: the fields of both
files, the `Exchange` codes, the Test Issue and ETF flags, "The symbol directory files are
updated periodically through-out each day" and "The last row of each Symbol Directory text file
contains a timestamp that reports the File Creation Time", with the example
`File Creation Time: 1217200717:03|||||`. The page names FTP addresses only; `BASE` and the two
header lines are the reference's, which observed the files on 2026-11-11, and a changed header
is a changed schema, refused until reviewed. The CPython 3.13 documentation, read 2026-11-11 in
python/cpython@v3.13.18 `0d4bd512fc429e3d4eacdd113c975e161f21644d`:
`Doc/library/urllib.request.rst:37-74` (`urlopen`: the timeout, the `status` and `headers`
properties, `URLError` on protocol errors), `Doc/library/tempfile.rst:230-243` (`mkstemp`: no
race in the creation; the caller closes and deletes the file) and `Doc/library/os.rst:2754-2760`
(`os.replace`: atomic when it succeeds; it may fail across filesystems, hence the same
directory). Behaviour: the reference, `src/sextant/tradingview/listings.py` and
`src/sextant/tradingview/bars.py:42-51,110-148` at 06562bec0898a7203f4eaf69086ced83d2477e6b.
"""

import hashlib
import os
import re
import tempfile
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sextant_movers.atomic import write_atomic

# otherlisted.txt "Exchange" codes as the definitions page lists them.
OTHER_LISTED_EXCHANGES = {
    "A": "NYSE MKT",
    "N": "New York Stock Exchange (NYSE)",
    "P": "NYSE ARCA",
    "Z": "BATS Global Markets (BATS)",
    "V": "Investors' Exchange, LLC (IEXG)",
}
BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"
HEADERS = {
    "nasdaqlisted.txt": (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares"
    ),
    "otherlisted.txt": (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol"
    ),
}
# "The row contains the words File Creation Time followed by mmddyyyyhhmm as the first field,
# followed by all delimiters to round out the row"; the page's example is `1217200717:03`.
TRAILER = re.compile(r"File Creation Time: (\d{10}:\d{2})\|*\Z")
RECEIPT = "listings-receipt.json"

Fetch = Callable[[str, float], bytes]


class ListingError(ValueError):
    """A refused download; the previous good files are left in place."""


@dataclass(frozen=True, slots=True)
class Listing:
    symbol: str
    name: str
    test_issue: bool
    etf: bool
    exchange: str | None = None


def parse_directory(text: str) -> tuple[Listing, ...]:
    """Every row of one directory file, after its header and without its trailer."""
    header: list[str] | None = None
    symbol_key = "Symbol"
    rows: list[Listing] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split("|")]
        if header is None:
            # Lines before the header are skipped, as in the reference.
            for key in ("ACT Symbol", "Symbol"):
                if key in fields:
                    header, symbol_key = fields, key
                    break
            continue
        if "File Creation Time" in fields[0]:
            continue
        if len(fields) < len(header):
            raise ValueError("Malformed NASDAQ Trader directory row")
        # Looked up per row in this order, so a missing column raises as the reference's does.
        symbol = fields[header.index(symbol_key)]
        name = fields[header.index("Security Name")]
        test_issue = fields[header.index("Test Issue")] == "Y"
        etf = fields[header.index("ETF")] == "Y"
        exchange = fields[header.index("Exchange")] if "Exchange" in header else None
        rows.append(Listing(symbol, name, test_issue, etf, exchange))
    if header is None:
        raise ValueError("NASDAQ Trader directory has no header")
    return tuple(rows)


def fetch(url: str, timeout: float) -> bytes:
    """One GET with the timeout; anything but a 200 `text/plain` answer is refused."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (stage's fixed https BASE)
        kind = response.headers.get_content_type()
        if response.status != 200 or kind != "text/plain":
            raise ListingError(f"{url} answered {response.status} {kind}")
        return response.read()


def validate(name: str, data: bytes) -> dict[str, Any]:
    """One directory file's receipt; a file of any other shape is refused."""
    header = HEADERS[name]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ListingError(f"{name} is not UTF-8 text") from exc
    lines = text.splitlines()
    if not lines or lines[0] != header:
        raise ListingError(f"{name} does not start with the directory header")
    match = TRAILER.fullmatch(lines[-1])
    if match is None:
        raise ListingError(f"{name} does not end with the File Creation Time row (truncated?)")
    created = match.group(1)
    try:
        datetime.strptime(created, "%m%d%Y%H:%M")
    except ValueError as exc:
        raise ListingError(f"{name} has an invalid File Creation Time") from exc
    rows = lines[1:-1]
    width = header.count("|")
    if not rows or any(row.count("|") != width for row in rows):
        raise ListingError(f"{name} has no rows or a row with the wrong field count")
    columns = header.split("|")
    unknown: Counter[str] = Counter()
    if "Exchange" in columns:
        column = columns.index("Exchange")
        codes = (row.split("|")[column] for row in rows)
        unknown.update(code for code in codes if code not in OTHER_LISTED_EXCHANGES)
    return {
        "file": name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": len(rows),
        "file_creation_time": created,
        # Counted here, unstripped; parse_directory returns these rows with their code and a
        # caller decides what to do with them.
        "unknown_exchange_rows": sum(unknown.values()),
        "unknown_exchange_codes": dict(sorted(unknown.items())),
    }


def creation_time(text: str) -> str:
    """The trailer's mmddyyyyHH:MM text, or "unknown"; for display, never a refusal."""
    lines = text.splitlines()
    match = TRAILER.fullmatch(lines[-1]) if lines else None
    return match.group(1) if match else "unknown"


def _replace_bytes(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def stage(
    directory: Path,
    *,
    timeout: float,
    get: Fetch = fetch,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Download and validate both files, then replace each and write the receipt."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fetched: dict[str, bytes] = {}
    receipts = []
    for name in HEADERS:
        try:
            data = get(BASE + name, timeout)
        except ListingError:
            raise
        except Exception as exc:  # network, TLS, HTTP: the previous files stay
            raise ListingError(f"{name} download failed: {type(exc).__name__}") from exc
        receipts.append(validate(name, data))
        fetched[name] = data
    for name, data in fetched.items():
        _replace_bytes(directory / name, data)
    receipt = {
        "schema": "sextant.tv-listings-receipt/1",
        "staged_at": now().isoformat(),
        "source": BASE,
        "files": receipts,
    }
    write_atomic(directory / RECEIPT, receipt)
    return receipt
