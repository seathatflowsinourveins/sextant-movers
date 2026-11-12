"""Official screener reads and generation pairing, synthetic values only, no socket.

alpacahq/alpaca-py v0.44.0 cc4cb3b7ba50ae250e621983c2779047fb16bb28:
tests/data/test_screener.py supplies the answer shape; tests/conftest.py:84-90 supplies
the two placeholder strings. The SDK runs above a scripted requests session.
"""

import ast
import inspect
import json
import socket
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, get_type_hints

import pytest
from alpaca.common.exceptions import APIError
from alpaca.common.rest import RESTClient
from alpaca.data.enums import MarketType, MostActivesBy
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.models.screener import MostActives, Movers
from alpaca.data.requests import MarketMoversRequest, MostActivesRequest
from requests import Response

from sextant_movers import contract, contract_data
from sextant_movers.ranking import screener as m
from sextant_movers.ranking.generation import (
    Coverage,
    CoverageState,
    GenerationId,
    GenerationStatus,
    RankingGeneration,
    RankingRow,
)

MOVERS_TIME = "2026-01-02T14:30:00.123456789+05:30"
ACTIVES_TIME = "2026-01-02T09:01:00.987654321Z"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object) -> None:
        raise AssertionError("a test opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def mover(symbol: str) -> dict[str, Any]:
    return {"symbol": symbol, "change": 1.25, "percent_change": 12.5, "price": 11.25}


def movers(*gainers: str, losers: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "gainers": [mover(s) for s in gainers],
        "losers": [mover(s) for s in losers],
        "market_type": "stocks",
        "last_updated": MOVERS_TIME,
    }


def actives(*symbols: str) -> dict[str, Any]:
    return {
        "most_actives": [{"symbol": s, "volume": 1234, "trade_count": 56} for s in symbols],
        "last_updated": ACTIVES_TIME,
    }


class Count:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def wait(self) -> None:
        self.events.append("wait")


class Session:
    def __init__(self, answers: tuple[tuple[int, Any], ...], events: list[str]) -> None:
        self.answers, self.events = iter(answers), events
        self.seen: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **opts: Any) -> Response:
        self.events.append("wire")
        self.seen.append((method, url, opts["params"]))
        status, body = next(self.answers)
        response = Response()
        response.status_code, response.url = status, url
        response._content = json.dumps(body).encode()
        return response


def client(*answers: tuple[int, Any], raw_data: bool = False) -> tuple[Any, Session, list[str]]:
    events: list[str] = []
    paced: Any = m.PacedScreener(
        pacer=Count(events),
        api_key="key-id",
        secret_key="secret-key",  # noqa: S106 (SDK's explicit test placeholder)
        raw_data=raw_data,
    )
    paced._session = session = Session(answers, events)
    return paced, session, events


def read(paced: Any, **changes: Any) -> tuple[Movers, MostActives]:
    args = {"movers_top": 3, "actives_top": 4, "by": MostActivesBy.VOLUME}
    return m.read(paced, **(args | changes))


def plain_client() -> tuple[ScreenerClient, Session, list[str]]:
    events: list[str] = []
    plain: Any = ScreenerClient(
        api_key="key-id",
        secret_key="secret-key",  # noqa: S106 (SDK's explicit test placeholder)
    )
    plain._session = session = Session(((200, movers()), (200, actives())), events)
    return plain, session, events


def test_plain_sdk_client_is_refused_without_a_wire_request() -> None:
    plain, session, events = plain_client()
    with pytest.raises(ValueError) as caught:
        read(plain)
    assert str(caught.value) == "screener reads need the paced transport, PacedScreener"
    assert session.seen == [] and events == []


def test_non_client_double_is_refused_without_calling_either_method() -> None:
    calls: list[str] = []

    class Double:
        def get_market_movers(self, request_params: MarketMoversRequest) -> Movers:
            calls.append("movers")
            return Movers(**movers())

        def get_most_actives(self, request_params: MostActivesRequest) -> MostActives:
            calls.append("actives")
            return MostActives(**actives())

    with pytest.raises(ValueError) as caught:
        read(Double())
    assert str(caught.value) == "screener reads need the paced transport, PacedScreener"
    assert calls == []


def test_client_refusal_precedes_movers_bounds_without_a_wire_request() -> None:
    plain, session, events = plain_client()
    with pytest.raises(ValueError) as caught:
        read(plain, movers_top=0)
    assert str(caught.value) == "screener reads need the paced transport, PacedScreener"
    assert session.seen == [] and events == []


@pytest.mark.parametrize("by", list(MostActivesBy))
@pytest.mark.parametrize(("movers_top", "actives_top"), [(1, 1), (50, 100)])
def test_two_sdk_reads_keep_paths_queries_order_pacing_and_models(
    by: MostActivesBy, movers_top: int, actives_top: int
) -> None:
    paced, session, events = client((200, movers("ZZAAA")), (200, actives("ZZBBB")))
    got, active = read(paced, movers_top=movers_top, actives_top=actives_top, by=by)
    assert isinstance(got, Movers) and isinstance(active, MostActives)
    assert [
        (method, url.split("/v1beta1/")[1], params) for method, url, params in session.seen
    ] == [
        ("GET", "screener/stocks/movers", {"top": movers_top}),
        ("GET", "screener/stocks/most-actives", {"top": actives_top, "by": by.value}),
    ]
    assert all(
        url.endswith("/v1beta1/" + suffix)
        for (_, url, _), suffix in zip(
            session.seen, ("screener/stocks/movers", "screener/stocks/most-actives"), strict=True
        )
    )
    assert events == ["wait", "wire", "wait", "wire"]
    assert session.seen[1][2]["by"] is by  # the SDK leaves the str enum for requests to encode
    assert got.gainers[0].symbol == "ZZAAA" and active.most_actives[0].symbol == "ZZBBB"
    assert got.last_updated == datetime.fromisoformat(MOVERS_TIME[:26] + "+05:30")
    assert got.last_updated.utcoffset() == timedelta(hours=5, minutes=30)
    assert got.last_updated.microsecond == 123456
    assert active.last_updated.utcoffset() == timedelta(0)
    assert active.last_updated.microsecond == 987654


def refused_argument(**changes: Any) -> None:
    paced, session, events = client()
    with pytest.raises(ValueError):
        read(paced, **changes)
    assert session.seen == [] and events == []


@pytest.mark.parametrize("value", [0, -1, 51])
def test_movers_top_outside_documented_bounds_makes_no_request(value: int) -> None:
    refused_argument(movers_top=value)


@pytest.mark.parametrize("value", [0, -1, 101])
def test_actives_top_outside_documented_bounds_makes_no_request(value: int) -> None:
    refused_argument(actives_top=value)


@pytest.mark.parametrize("value", [True, False])
def test_boolean_movers_top_makes_no_request(value: bool) -> None:
    refused_argument(movers_top=value)


@pytest.mark.parametrize("value", [True, False])
def test_boolean_actives_top_makes_no_request(value: bool) -> None:
    refused_argument(actives_top=value)


@pytest.mark.parametrize("value", [1.0, "2", None])
def test_non_integer_movers_top_makes_no_request(value: Any) -> None:
    refused_argument(movers_top=value)


@pytest.mark.parametrize("value", [1.0, "2", None])
def test_non_integer_actives_top_makes_no_request(value: Any) -> None:
    refused_argument(actives_top=value)


@pytest.mark.parametrize("value", ["volume", "trades", None, 1, MarketType.STOCKS])
def test_non_member_metric_makes_no_request(value: Any) -> None:
    refused_argument(by=value)


def test_all_read_choices_are_required_keywords() -> None:
    paced, session, events = client()
    for omitted in ("movers_top", "actives_top", "by"):
        args = {"movers_top": 1, "actives_top": 1, "by": MostActivesBy.TRADES}
        del args[omitted]
        with pytest.raises(TypeError):
            m.read(paced, **args)
    with pytest.raises(TypeError):
        m.read(paced, 1, 1, MostActivesBy.VOLUME)  # type: ignore[call-arg]
    assert session.seen == [] and events == []


def test_raw_data_client_is_refused_after_one_wire_request() -> None:
    paced, session, events = client((200, movers()), raw_data=True)
    with pytest.raises(TypeError, match="Movers"):
        read(paced)
    assert len(session.seen) == 1 and events == ["wait", "wire"]


def test_crypto_movers_are_refused_after_one_wire_request() -> None:
    paced, session, events = client((200, movers() | {"market_type": "crypto"}))
    with pytest.raises(ValueError, match="stocks"):
        read(paced)
    assert len(session.seen) == 1 and events == ["wait", "wire"]


def test_non_model_actives_are_refused_after_two_wire_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ScreenerClient.get_most_actives

    def raw_answer(self: ScreenerClient, request_params: MostActivesRequest) -> Any:
        self._use_raw_data = True
        return original(self, request_params)

    monkeypatch.setattr(ScreenerClient, "get_most_actives", raw_answer)
    paced, session, events = client((200, movers()), (200, actives()))
    with pytest.raises(TypeError, match="MostActives"):
        read(paced)
    assert len(session.seen) == 2 and events == ["wait", "wire"] * 2


def test_first_request_failure_does_not_read_actives() -> None:
    paced, session, events = client((400, {"message": "synthetic refusal"}))
    with pytest.raises(APIError):
        read(paced)
    assert len(session.seen) == 1 and events == ["wait", "wire"]


@pytest.mark.parametrize("status", [429, 504])
def test_sdk_retry_waits_on_the_same_pacer_for_every_attempt(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("alpaca.common.rest.time.sleep", sleeps.append)
    paced, session, events = client(
        (status, {"message": "synthetic retry"}), (200, movers()), (200, actives())
    )
    read(paced)
    assert len(session.seen) == 3 and events == ["wait", "wire"] * 3
    assert len(sleeps) == 1
    assert session.seen[0] == session.seen[1]


def generation(*rows: RankingRow) -> RankingGeneration:
    return RankingGeneration(
        generation_id=GenerationId("synthetic", 2, 1),
        session_date=date(2026, 1, 2),
        input_cutoff=12,
        ts_event=13,
        ts_init=14,
        known_at=15,
        method_version="synthetic",
        profile_version="synthetic",
        ranker_version="synthetic",
        coverage=Coverage(CoverageState.READY, True),
        status=GenerationStatus.PROVISIONAL,
        rows=rows,
    )


def test_cross_check_keeps_vendor_order_positions_identities_and_generation_bytes() -> None:
    gen = generation(
        RankingRow("ZZBBB", rank=2),
        RankingRow("ZZAAA", rank=1),
        RankingRow("ZZCCC"),
        RankingRow("ZZDDD", rank=3),
        RankingRow("ZZEEE", rank=4),
        RankingRow("ZZFFF"),
    )
    moving = Movers(**movers("ZZCCC", "ZZAAA", "ZZOUT", losers=("ZZBBB", "ZZAAA")))
    active = MostActives(**actives("ZZAAA", "ZZOUT", "ZZBBB"))
    before = gen.to_json().encode()
    report = m.cross_check(gen, moving, active)
    assert isinstance(report, m.Report)
    assert report.generation_id is gen.generation_id and report.ts_event == gen.ts_event
    assert report.movers_updated is moving.last_updated
    assert report.actives_updated is active.last_updated
    rows = {row.symbol: row for row in gen.rows}
    for pairs, entries in (
        (report.gainers, moving.gainers),
        (report.losers, moving.losers),
        (report.actives, active.most_actives),
    ):
        assert isinstance(pairs, tuple) and len(pairs) == len(entries)
        for position, (pair, entry) in enumerate(zip(pairs, entries, strict=True), 1):
            assert isinstance(pair, m.Pair)
            assert pair.position == position and pair.entry is entry
            assert pair.row is rows.get(entry.symbol)
    assert report.gainers[0].row is rows["ZZCCC"]
    assert rows["ZZCCC"].rank is None
    assert report.gainers[2].row is None
    assert report.gainers[1].row is report.losers[1].row is report.actives[0].row
    assert [row.symbol for row in report.unlisted] == ["ZZDDD", "ZZEEE"]
    assert report.unlisted[0] is rows["ZZDDD"] and report.unlisted[1] is rows["ZZEEE"]
    assert gen.to_json().encode() == before
    assert {field.name for field in fields(report)} == {
        "generation_id",
        "ts_event",
        "movers_updated",
        "actives_updated",
        "gainers",
        "losers",
        "actives",
        "unlisted",
    }
    assert {field.name for field in fields(report.gainers[0])} == {"position", "entry", "row"}
    with pytest.raises(FrozenInstanceError):
        report.ts_event = 0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        report.gainers[0].position = 0  # type: ignore[misc]
    assert not hasattr(report, "__dict__") and not hasattr(report.gainers[0], "__dict__")


def test_matching_is_exact_without_case_or_suffix_mapping() -> None:
    gen = generation(RankingRow("ZZAAA", rank=1), RankingRow("ZZBBB.X", rank=2))
    moving = Movers(**movers("ZZaaa", "ZZBBB"))
    report = m.cross_check(gen, moving, MostActives(**actives()))
    assert all(pair.row is None for pair in report.gainers)
    assert report.unlisted == gen.rows


def test_empty_vendor_lists_leave_only_ranked_rows_unlisted() -> None:
    gen = generation(RankingRow("ZZCCC"), RankingRow("ZZBBB", rank=2), RankingRow("ZZAAA", rank=1))
    report = m.cross_check(gen, Movers(**movers()), MostActives(**actives()))
    assert report.gainers == report.losers == report.actives == ()
    assert report.unlisted == gen.rows[:2]
    assert all(a is b for a, b in zip(report.unlisted, gen.rows, strict=False))


def test_empty_generation_pairs_every_vendor_entry_with_none() -> None:
    report = m.cross_check(
        generation(), Movers(**movers("ZZAAA", losers=("ZZBBB",))), MostActives(**actives("ZZCCC"))
    )
    assert report.unlisted == ()
    assert all(pair.row is None for pair in (*report.gainers, *report.losers, *report.actives))


@pytest.mark.parametrize("name", ["generation", "movers", "most_actives"])
def test_cross_check_refuses_non_models(name: str) -> None:
    args: dict[str, Any] = {
        "generation": generation(),
        "movers": Movers(**movers()),
        "most_actives": MostActives(**actives()),
    }
    args[name] = {}
    with pytest.raises(TypeError):
        m.cross_check(**args)


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    result = {alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            result.add(node.module or "")
            result.update(f"{node.module or ''}.{alias.name}" for alias in node.names)
    return result


def imports_screener(path: Path) -> bool:
    return any(name == m.__name__ or name.startswith(m.__name__ + ".") for name in imports(path))


def test_sdk_screener_import_is_not_an_import_of_our_screener(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text("import alpaca.data.historical.screener\n")
    assert imports(path) == {"alpaca.data.historical.screener"}
    assert not imports_screener(path)


@pytest.mark.parametrize(
    "line",
    [
        "import sextant_movers.ranking.screener\n",
        "import sextant_movers.ranking.screener as shown\n",
        "from sextant_movers.ranking import screener\n",
        "from sextant_movers.ranking.screener import read\n",
    ],
)
def test_an_import_of_our_screener_is_seen(tmp_path: Path, line: str) -> None:
    path = tmp_path / "module.py"
    path.write_text(line)
    assert imports_screener(path)


def test_screener_stays_beside_the_rank_and_out_of_both_contracts() -> None:
    own_path = Path(m.__file__)
    for path in (ROOT / "src" / "sextant_movers").rglob("*.py"):
        if path != own_path:
            assert not imports_screener(path), path
    local = {name for name in imports(own_path) if name.startswith("sextant_movers")}
    assert all(
        name.startswith(("sextant_movers.ranking.generation", "sextant_movers.ranking.bars"))
        for name in local
    )
    assert {"sextant_movers.ranking.generation", "sextant_movers.ranking.bars"} <= local
    text = own_path.read_text()
    assert all(
        word not in text for word in ("environ", "getenv", "socket", "thread", "ScreenerClient(")
    )
    names = {"PacedScreener", "read", "cross_check", "Pair", "Report"}
    names |= {"ScreenerClient", "Movers", "MostActives", "Mover", "ActiveStock"}
    assert not names.intersection(contract.__all__)
    assert not names.intersection(contract_data.__all__)


def test_sdk_signatures_request_fields_and_paced_hook_at_the_pin() -> None:
    for method, request_type in (
        (ScreenerClient.get_market_movers, MarketMoversRequest),
        (ScreenerClient.get_most_actives, MostActivesRequest),
    ):
        assert list(inspect.signature(method).parameters) == ["self", "request_params"]
        assert get_type_hints(method)["request_params"] is request_type
    assert set(MarketMoversRequest.model_fields) == {"top", "market_type"}
    assert set(MostActivesRequest.model_fields) == {"top", "by"}
    assert MarketMoversRequest.model_fields["market_type"].annotation is MarketType
    assert MostActivesRequest.model_fields["by"].annotation is MostActivesBy
    assert MarketMoversRequest.model_fields["top"].annotation is int
    assert MostActivesRequest.model_fields["top"].annotation is int
    assert MostActivesRequest(top=7, by=MostActivesBy.TRADES).to_request_fields() == {
        "top": 7,
        "by": "trades",
    }
    assert (
        MostActivesRequest(top=7, by=MostActivesBy.TRADES).to_request_fields()["by"]
        is MostActivesBy.TRADES
    )
    assert MarketMoversRequest(top=8, market_type=MarketType.STOCKS).to_request_fields() == {
        "top": 8,
        "market_type": "stocks",
    }
    assert issubclass(m.PacedScreener, ScreenerClient) and issubclass(ScreenerClient, RESTClient)
    assert "_one_request" not in vars(ScreenerClient)
    assert (
        list(inspect.signature(m.PacedScreener._one_request).parameters)
        == [
            "self",
            "method",
            "url",
            "opts",
            "retry",
        ]
        == list(inspect.signature(RESTClient._one_request).parameters)
    )
    assert "self._one_request(method, url, opts, retry)" in inspect.getsource(RESTClient._request)
    assert inspect.signature(m.PacedScreener).parameters["pacer"].default is inspect.Parameter.empty
    paced, _, _ = client()
    assert (paced._api_key, paced._secret_key) == ("key-id", "secret-key")
