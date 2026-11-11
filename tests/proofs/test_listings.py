"""Part 5a: the Nasdaq Trader symbol directory files: parse, validate and stage.

Synthetic directory text only: invented `ZZ` symbols, names and creation times, built here from
the two header lines; no fixture file and no recorded row. The reference is
`src/sextant/tradingview/listings.py` and `bars.py` at `06562bec0898a7203f4eaf69086ced83d2477e6b`;
this process never imports it. The `local` proof runs it in a child interpreter named by
`MOVERS_REFERENCE_PYTHON`, with the export's `src` directory named by `MOVERS_REFERENCE_SRC`.
The one socket opened is a loopback `http.server` on 127.0.0.1, started and closed in the test.
"""

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest
from sextant_movers.ranking.listings import (
    BASE,
    HEADERS,
    OTHER_LISTED_EXCHANGES,
    RECEIPT,
    TRAILER,
    Listing,
    ListingError,
    creation_time,
    fetch,
    parse_directory,
    stage,
    validate,
)

from sextant_movers.ranking import listings as lst

NASDAQ, OTHER = "nasdaqlisted.txt", "otherlisted.txt"
STAMP = "0102202609:30"
NOW = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
N1 = "ZZQAAA|Zzqaaa Synthetic Corp|Q|N|N|100|N|N"
N2 = "ZZQBBB|Zzqbbb Synthetic Fund|G|Y|D|100|Y|N"
O1 = "ZZNAAA|Zznaaa Synthetic Inc|N|ZZNAAA|N|100|N|ZZNAAA"
O2 = "ZZPBBB|Zzpbbb Synthetic Trust|P|ZZPBBB|Y|100|Y|ZZPBBB"
O3 = "ZZFCCC|Zzfccc Synthetic Co|F|ZZFCCC|N|100|N|ZZFCCC"
HTML = b"<html><head><title>Zz error</title></head><body>zz</body></html>"
REFERENCE_BLOBS = {  # git blob ids of the reference files at the pinned commit
    "listings.py": "60643fcf892a4530f73db8b4186692a95b32bce4",
    "bars.py": "8ae03acec28cabae0425cf6af1bce38b08910e60",
}
ANSWERS_DIGEST = "0" * 64  # SHA-256 of this side's canonical parity answers


def text(name: str, *rows: str, end: str = "\r\n", trailer: str | None = None) -> str:
    last = f"File Creation Time: {STAMP}|||||||" if trailer is None else trailer
    return end.join([HEADERS[name], *rows, last]) + end


def test_the_constants_another_program_reads_are_kept_word_for_word() -> None:
    assert BASE == "https://www.nasdaqtrader.com/dynamic/SymDir/"
    assert RECEIPT == "listings-receipt.json"
    assert HEADERS == {
        NASDAQ: "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares",
        OTHER: "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|"
        "NASDAQ Symbol",
    }
    assert TRAILER.pattern == r"File Creation Time: (\d{10}:\d{2})\|*\Z"
    assert OTHER_LISTED_EXCHANGES == {
        "A": "NYSE MKT",
        "N": "New York Stock Exchange (NYSE)",
        "P": "NYSE ARCA",
        "Z": "BATS Global Markets (BATS)",
        "V": "Investors' Exchange, LLC (IEXG)",
    }
    assert issubclass(ListingError, ValueError)


def test_a_good_file_of_each_kind_gives_its_exact_receipt() -> None:
    data = text(NASDAQ, N1, N2).encode()
    assert validate(NASDAQ, data) == {
        "file": NASDAQ,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": 2,
        "file_creation_time": STAMP,
        "unknown_exchange_rows": 0,
        "unknown_exchange_codes": {},
    }
    data = text(OTHER, O3, O1, O2, O3, O1.replace("|N|ZZNAAA|", "|Q|ZZNAAA|"), end="\n").encode()
    receipt = validate(OTHER, data)
    assert list(receipt) == list(validate(NASDAQ, text(NASDAQ, N1).encode()))
    assert receipt["rows"] == 5 and receipt["bytes"] == len(data)
    assert receipt["unknown_exchange_rows"] == 3
    assert receipt["unknown_exchange_codes"] == {"F": 2, "Q": 1}
    assert list(receipt["unknown_exchange_codes"]) == ["F", "Q"]


GOOD = text(NASDAQ, N1).encode()
REFUSED = {
    "not UTF-8": (GOOD.replace(b"Zzqaaa", b"Zz\xffaaa"), "is not UTF-8 text"),
    "wrong header": (GOOD.replace(b"Round Lot Size", b"Round Lot"), "header"),
    "the other file's header": (text(OTHER, O1).encode(), "header"),
    "missing header": (GOOD.split(b"\r\n", 1)[1], "header"),
    "an HTML page": (HTML, "header"),
    "an empty file": (b"", "header"),
    "a byte-order mark": (b"\xef\xbb\xbf" + GOOD, "header"),
    "no trailer": (GOOD[: GOOD.rindex(b"File")], "trailer"),
    "a blank line after the trailer": (GOOD + b"\r\n", "trailer"),
    "an impossible date": (GOOD.replace(STAMP.encode(), b"1332202609:30"), "date"),
    "no rows": (text(NASDAQ).encode(), "rows"),
    "too few fields": (text(NASDAQ, N1, "ZZQCCC|Zzqccc|Q").encode(), "rows"),
    "too many fields": (text(NASDAQ, N1 + "|N").encode(), "rows"),
    "a blank row": (text(NASDAQ, N1, "", N1).encode(), "rows"),
}
MESSAGES = {
    "is not UTF-8 text": f"{NASDAQ} is not UTF-8 text",
    "header": f"{NASDAQ} does not start with the directory header",
    "trailer": f"{NASDAQ} does not end with the File Creation Time row (truncated?)",
    "date": f"{NASDAQ} has an invalid File Creation Time",
    "rows": f"{NASDAQ} has no rows or a row with the wrong field count",
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_every_refusal_carries_its_exact_message(case: str) -> None:
    data, kind = REFUSED[case]
    with pytest.raises(ListingError) as refused:
        validate(NASDAQ, data)
    assert str(refused.value) == MESSAGES[kind]


def test_a_name_that_is_neither_file_is_a_key_error() -> None:
    with pytest.raises(KeyError) as refused:
        validate("mpidlist.txt", GOOD)
    assert refused.value.args == ("mpidlist.txt",)


def test_an_unstripped_exchange_code_is_counted_unknown_but_parsed_stripped() -> None:
    padded = O1.replace("|N|ZZNAAA|", "| N|ZZNAAA|")
    assert validate(OTHER, text(OTHER, padded).encode())["unknown_exchange_codes"] == {" N": 1}
    assert [row.exchange for row in parse_directory(text(OTHER, padded))] == ["N"]


def test_creation_time_reads_the_trailer_or_says_unknown() -> None:
    assert creation_time(text(OTHER, O1)) == STAMP
    assert creation_time(text(NASDAQ, N1, trailer=f"File Creation Time: {STAMP}")) == STAMP
    assert creation_time("") == "unknown"
    assert creation_time(text(NASDAQ, N1, trailer="File Creation Time|2026-01-02")) == "unknown"
    assert creation_time(HEADERS[NASDAQ] + "\n" + N1 + "\n") == "unknown"


def test_the_directory_parser_returns_every_row_with_its_flags() -> None:
    assert parse_directory(text(NASDAQ, N1, N2)) == (
        Listing("ZZQAAA", "Zzqaaa Synthetic Corp", False, False, None),
        Listing("ZZQBBB", "Zzqbbb Synthetic Fund", True, True, None),
    )
    other = parse_directory(text(OTHER, O1, O2, O3, end="\n"))
    assert [(row.symbol, row.test_issue, row.etf, row.exchange) for row in other] == [
        ("ZZNAAA", False, False, "N"),
        ("ZZPBBB", True, True, "P"),
        ("ZZFCCC", False, False, "F"),
    ]
    assert [f.name for f in Listing.__dataclass_fields__.values()] == [
        "symbol",
        "name",
        "test_issue",
        "etf",
        "exchange",
    ]


def test_blank_lines_the_trailer_and_lines_before_the_header_are_skipped() -> None:
    # Kept from the reference: a line before the header is skipped, never refused.
    body = "\n".join(
        [f"File Creation Time: {STAMP}", "Zz preamble", HEADERS[NASDAQ], "", "   ", N1 + "|N"]
    )
    assert [row.symbol for row in parse_directory(body + "\n" + " " + N2 + " ")] == [
        "ZZQAAA",
        "ZZQBBB",
    ]


def test_a_short_row_or_no_header_is_refused() -> None:
    with pytest.raises(ValueError, match=r"\AMalformed NASDAQ Trader directory row\Z"):
        parse_directory(text(NASDAQ, "ZZQAAA|Zzqaaa"))
    with pytest.raises(ValueError, match=r"\ANASDAQ Trader directory has no header\Z"):
        parse_directory(N1 + "\n" + N2)
    with pytest.raises(ValueError, match=r"\A'Test Issue' is not in list\Z"):
        parse_directory("Symbol|Security Name\nZZQAAA|Zzqaaa\n")


class Fake:
    """A stand-in for `fetch`: bytes per file, or an exception raised for one file."""

    def __init__(self, files: dict[str, bytes], fail: tuple[str, BaseException] | None = None):
        self.files, self.fail, self.calls = files, fail, []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append((url, timeout))
        name = url.removeprefix(BASE)
        if self.fail and self.fail[0] == name:
            raise self.fail[1]
        return self.files[name]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the default get was called")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


GOOD_FILES = {NASDAQ: text(NASDAQ, N1, N2).encode(), OTHER: text(OTHER, O1, O3).encode()}


@pytest.mark.usefixtures("no_network")
def test_both_good_files_are_staged_with_the_receipt(tmp_path: Path) -> None:
    get = Fake(GOOD_FILES)
    receipt = stage(tmp_path / "stage", timeout=7.5, get=get, now=lambda: NOW)
    assert get.calls == [(BASE + NASDAQ, 7.5), (BASE + OTHER, 7.5)]
    assert receipt == {
        "schema": "sextant.tv-listings-receipt/1",
        "staged_at": "2026-01-02T14:30:00+00:00",
        "source": BASE,
        "files": [validate(name, GOOD_FILES[name]) for name in HEADERS],
    }
    folder = tmp_path / "stage"
    assert sorted(p.name for p in folder.iterdir()) == sorted([*HEADERS, RECEIPT])
    assert all((folder / name).read_bytes() == GOOD_FILES[name] for name in HEADERS)
    assert (folder / RECEIPT).read_text() == json.dumps(receipt, sort_keys=True)
    assert stage.__kwdefaults__["get"] is fetch


FAILURES: dict[str, tuple[dict[str, bytes], Any, str]] = {
    "bad nasdaq file": ({NASDAQ: HTML}, None, MESSAGES["header"]),
    "bad other file": (
        {OTHER: GOOD_FILES[OTHER][:-30]},
        None,
        f"{OTHER} does not end with the File Creation Time row (truncated?)",
    ),
    "get raises ListingError": ({}, (OTHER, ListingError("zz refused")), "zz refused"),
    "get raises OSError": ({}, (NASDAQ, OSError("zz")), f"{NASDAQ} download failed: OSError"),
    "get raises TimeoutError": (
        {},
        (OTHER, TimeoutError("zz")),
        f"{OTHER} download failed: TimeoutError",
    ),
}


@pytest.mark.usefixtures("no_network")
@pytest.mark.parametrize("case", sorted(FAILURES))
def test_a_failure_leaves_every_previous_file_byte_for_byte(tmp_path: Path, case: str) -> None:
    overrides, fail, message = FAILURES[case]
    previous = {name: b"previous " + name.encode() for name in [*HEADERS, RECEIPT]}
    for name, data in previous.items():
        (tmp_path / name).write_bytes(data)
    with pytest.raises(ListingError) as refused:
        stage(tmp_path, timeout=7.5, get=Fake(GOOD_FILES | overrides, fail), now=lambda: NOW)
    assert str(refused.value) == message
    if fail and not isinstance(fail[1], ListingError):
        assert refused.value.__cause__ is fail[1]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == previous


@pytest.mark.usefixtures("no_network")
def test_the_timeout_is_a_required_keyword(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="timeout"):
        stage(tmp_path, get=Fake(GOOD_FILES))  # pyright: ignore[reportCallIssue]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("no_network")
def test_a_failed_second_replace_leaves_the_first_file_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Kept from the reference, reported as a defect: the two replaces are not one atomic step.
    for name in [*HEADERS, RECEIPT]:
        (tmp_path / name).write_bytes(b"previous")
    real = os.replace

    def replace(source: str, target: Path) -> None:
        if Path(target).name == OTHER:
            raise OSError("zz replace failed")
        real(source, target)

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError, match="zz replace failed"):
        stage(tmp_path, timeout=1.0, get=Fake(GOOD_FILES), now=lambda: NOW)
    assert (tmp_path / NASDAQ).read_bytes() == GOOD_FILES[NASDAQ]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.name != NASDAQ} == {
        OTHER: b"previous",
        RECEIPT: b"previous",
    }


ROUTES = {
    "/plain": (200, "text/plain", b"zz plain"),
    "/html": (200, "text/html", HTML),
    "/partial": (203, "text/plain", b"zz"),
    "/untyped": (200, None, b"zz untyped"),
}


@pytest.fixture
def loopback(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, list[str]]]:
    for name in [n for n in os.environ if n.lower().endswith("_proxy")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            status, kind, body = ROUTES.get(self.path, (404, "text/html", b"zz"))
            self.send_response(status)
            if kind:
                self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fetch_takes_text_and_refuses_the_rest(
    loopback: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, seen = loopback
    opened: list[tuple[str, float]] = []
    real = urllib.request.urlopen

    def spy(url: str, timeout: float) -> Any:
        opened.append((url, timeout))
        return real(url, timeout=timeout)

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    assert fetch(root + "/plain", 5.0) == b"zz plain"
    # Kept: an answer with no content type passes, since `get_content_type` defaults to text/plain.
    assert fetch(root + "/untyped", 5.0) == b"zz untyped"
    for path, answer in (("/html", "200 text/html"), ("/partial", "203 text/plain")):
        with pytest.raises(ListingError) as refused:
            fetch(root + path, 5.0)
        assert str(refused.value) == f"{root}{path} answered {answer}"
    with pytest.raises(HTTPError) as missing:
        fetch(root + "/missing", 5.0)
    assert missing.value.code == 404
    assert seen == ["/plain", "/untyped", "/html", "/partial", "/missing"]
    assert opened == [(root + path, 5.0) for path in seen]
    # A non-HTTP answer has no status, so it is refused by the status check alone.
    with pytest.raises(ListingError, match=r"\Adata:text/plain,zz answered None text/plain\Z"):
        fetch("data:text/plain,zz", 5.0)


def test_a_404_through_stage_is_the_download_failed_refusal(
    loopback: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, seen = loopback
    monkeypatch.setattr(lst, "BASE", root + "/dir/")
    with pytest.raises(ListingError, match=rf"\A{NASDAQ} download failed: HTTPError\Z"):
        stage(tmp_path, timeout=5.0, get=fetch, now=lambda: NOW)
    assert seen == ["/dir/" + NASDAQ]
    assert list(tmp_path.iterdir()) == []


def test_the_module_imports_the_standard_library_and_the_atomic_write_only() -> None:
    tree = ast.parse(Path(lst.__file__).read_text())
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    outside = {m for m in imported if m.split(".")[0] not in sys.stdlib_module_names}
    assert outside == {"sextant_movers.atomic"}
    names = {getattr(n, "id", None) or getattr(n, "name", None) for n in ast.walk(tree)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    unbuilt = {"_keeps", "build_universe", "NON_COMMON_NAME", "NON_COMMON_SUFFIX"}
    unbuilt |= {"DIRECTORY_SYMBOL", "load_timeout", "main", "DEFAULT_PARAMETERS"}
    assert not names & unbuilt


def build_cases(headers: dict[str, str], count: int = 240) -> list[tuple[str, bytes]]:
    import random

    rng = random.Random(20261111)  # noqa: S311 (seeded synthetic text, not a secret)
    damages = [
        *["good"] * 6,
        *("no_trailer", "bad_date", "unicode_digits", "short_row", "long_row", "blank_inside"),
        *("space_line", "no_rows", "preamble", "header_case", "header_swap", "bom", "html"),
        *("empty", "not_utf8", "truncate", "padded", "trailer_first", "trailer_in_row"),
        *("unknown_name", "trailing_blank", "trailer_space", "dup_header", "no_pipes"),
    ]
    names = list(headers)
    cases = []
    for i in range(count):
        damage = damages[i % len(damages)]
        name = names[rng.randrange(2)]
        rows = []
        for _ in range(0 if damage == "no_rows" else rng.randint(1, 6)):
            s = "ZZ" + "".join(rng.choice("BCDFGHJK") for _ in range(rng.randint(3, 5)))
            yn = [rng.choice("YN") for _ in range(2)]
            if name == names[0]:
                row = [s, f"Zz {s.title()} Corp", rng.choice("QGS"), yn[0], "N", "100", yn[1], "N"]
            else:
                code = rng.choice(["A", "N", "P", "Z", "V", "N", "Q", "F", "", " N"])
                row = [s, f"Zz {s.title()} Inc", code, s, yn[0], "100", yn[1], s]
            rows.append(row)
        stamp = (
            f"{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}2026"
            f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}"
        )
        if damage == "bad_date":
            stamp = rng.choice(["13", "00"]) + stamp[2:]
        if damage == "unicode_digits":
            stamp = stamp.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
        trailer = "File Creation Time: " + stamp + "|" * rng.choice([0, 3, 7])
        if damage == "no_pipes":
            trailer = "File Creation Time|" + stamp
        if damage == "trailer_space":
            trailer += " "
        if damage == "padded":
            rows = [[f" {field} " for field in row] for row in rows]
        if damage == "trailer_in_row":
            rows[0][0] = "File Creation Time " + rows[0][0]
        lines = [headers[name], *("|".join(row) for row in rows), trailer]
        if damage == "no_trailer":
            lines.pop()
        if damage in ("short_row", "long_row"):
            k = rng.randrange(1, len(lines) - 1)
            lines[k] = lines[k].rsplit("|", 1)[0] if damage == "short_row" else lines[k] + "|N"
        if damage in ("blank_inside", "space_line", "preamble", "dup_header"):
            line = {"blank_inside": "", "space_line": "  ", "preamble": "Zz preamble"}
            lines.insert(rng.randrange(0, len(lines)), line.get(damage, headers[name]))
        if damage == "header_case":
            lines[0] = lines[0].lower()
        if damage == "header_swap":
            lines[0] = headers[names[1 - names.index(name)]]
        if damage == "trailer_first":
            lines.insert(0, lines.pop())
        end = rng.choice(["\n", "\r\n", "\r"])
        data = end.join(lines) + rng.choice(["", end] if damage != "trailing_blank" else [end * 2])
        raw = data.encode()
        if damage == "bom":
            raw = b"\xef\xbb\xbf" + raw
        if damage == "html":
            raw = b"<html><body>Zz error</body></html>"
        if damage == "empty":
            raw = b""
        if damage == "not_utf8":
            raw = raw.replace(b"Zz", b"Z\xff", 1)
        if damage == "truncate":
            raw = raw[: rng.randrange(len(raw))]
        if damage == "unknown_name":
            name = "mpidlist.txt"
        cases.append((name, raw))
    return cases


def answer_all(m: Any, parse: Any, root: Any, runs: int = 48) -> dict[str, Any]:
    import hashlib
    from datetime import UTC, datetime
    from pathlib import Path

    def refusal(error: BaseException) -> list[Any]:
        cause = error.__cause__
        return [type(error).__name__, str(error), type(cause).__name__ if cause else None]

    cases = build_cases(m.HEADERS)
    checked = []
    for name, data in cases:
        out: dict[str, Any] = {"input": hashlib.sha256(data).hexdigest()}
        try:
            out["validate"] = m.validate(name, data)
        except Exception as error:
            out["validate"] = refusal(error)
        decoded = data.decode("utf-8", "replace")
        out["creation_time"] = m.creation_time(decoded)
        try:
            out["parse"] = [
                [r.symbol, r.name, r.test_issue, r.etf, r.exchange] for r in parse(decoded)
            ]
        except Exception as error:
            out["parse"] = refusal(error)
        checked.append(out)
    passed = [isinstance(out["validate"], dict) for out in checked]
    pool = {
        (n, any_case): [
            d for (c, d), ok in zip(cases, passed, strict=True) if c == n and (ok or any_case)
        ]
        for n in m.HEADERS
        for any_case in (False, True)
    }
    raises = [m.ListingError("zz refused"), OSError("zz"), TimeoutError("zz"), ValueError("zz")]
    staged = []
    for j in range(runs):
        files = {}
        for k, n in enumerate(m.HEADERS):
            choices = pool[n, j % 3 == 0 and k == j % 2]
            files[n] = choices[(j * 7 + k) % len(choices)]
        fail = (list(m.HEADERS)[j % 2], raises[(j // 2) % len(raises)]) if j % 3 == 2 else None
        folder = Path(root) / f"run{j:03d}"
        folder.mkdir()
        if j % 4:
            for n in [*m.HEADERS, m.RECEIPT]:
                (folder / n).write_bytes(b"previous " + n.encode())
        calls = []

        def get(url, timeout, files=files, fail=fail, calls=calls):
            calls.append([url, timeout])
            name = url[len(m.BASE) :]
            if fail and fail[0] == name:
                raise fail[1]
            return files[name]

        now = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
        try:
            out = {"receipt": m.stage(folder, timeout=2.5, get=get, now=lambda now=now: now)}
        except Exception as error:
            out = {"refusal": refusal(error)}
        out["calls"] = calls
        out["folder"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())
        }
        staged.append(out)
    return {"cases": checked, "stage": staged}


def canonical(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def test_the_seeded_answers_hold_their_pinned_digest(tmp_path: Path) -> None:
    answers = answer_all(lst, parse_directory, tmp_path)
    cases, staged = answers["cases"], answers["stage"]
    assert len(cases) == 240 and len(staged) == 48
    refused = [c for c in cases if isinstance(c["validate"], list)]
    assert 0 < len(refused) < len(cases)
    assert {c["validate"][0] for c in refused} == {"ListingError", "KeyError"}
    assert 0 < sum("receipt" in s for s in staged) < len(staged)
    assert hashlib.sha256(canonical(answers).encode()).hexdigest() == ANSWERS_DIGEST


CHILD = """
import json, os, sys, tempfile
SRC = os.environ["MOVERS_REFERENCE_SRC"]
sys.path.insert(0, SRC)
from sextant.tradingview import bars, listings
HOME = os.path.join(SRC, "sextant", "tradingview")
assert os.path.samefile(listings.__file__, os.path.join(HOME, "listings.py"))
with tempfile.TemporaryDirectory() as root:
    answers = answer_all(listings, bars.parse_directory, root)
print("PARITY " + json.dumps({"version": sys.version.split()[0], "answers": canonical(answers)}))
"""


@pytest.mark.local
def test_parity_with_the_reference_in_a_child_interpreter(tmp_path: Path) -> None:
    src, python = os.environ.get("MOVERS_REFERENCE_SRC"), os.environ.get("MOVERS_REFERENCE_PYTHON")
    if not src or not python:
        pytest.skip("set MOVERS_REFERENCE_SRC and MOVERS_REFERENCE_PYTHON to run the parity proof")
    for name, blob_id in REFERENCE_BLOBS.items():
        blob = (Path(src) / "sextant" / "tradingview" / name).read_bytes()
        git_blob = hashlib.sha1(b"blob %d\0" % len(blob) + blob, usedforsecurity=False)
        assert git_blob.hexdigest() == blob_id, name
    ours = canonical(answer_all(lst, parse_directory, tmp_path))
    prelude = "from typing import Any\n"
    script = prelude + "".join(map(inspect.getsource, (build_cases, answer_all, canonical)))
    done = subprocess.run(
        [python, "-I", "-B", "-c", script + CHILD],
        env={"MOVERS_REFERENCE_SRC": src},
        capture_output=True,
        text=True,
        timeout=600,
        check=True,
    )
    line = next(x for x in done.stdout.splitlines() if x.startswith("PARITY "))
    theirs = json.loads(line.removeprefix("PARITY "))
    answers = json.loads(ours)
    print(
        f"parity: {len(answers['cases'])} files, {len(answers['stage'])} stage runs; "
        f"this side Python {sys.version.split()[0]}, the reference Python {theirs['version']}"
    )
    assert theirs["answers"] == ours
