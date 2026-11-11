"""The broker SDK is installed at the pinned release, and the names the bars part reads import.

alpaca-py is the source of historical bars (docs/manifest.md, "alpaca-py for bars and the movers
cross-check"). The dependency pin in `pyproject.toml`, the tag in `UPSTREAMS.md` and
the installed distribution must name one release, so a line cited at the pinned commit is the
code that runs. The names, at alpacahq/alpaca-py v0.44.0
`cc4cb3b7ba50ae250e621983c2779047fb16bb28`: `StockHistoricalDataClient`
(`alpaca/data/historical/stock.py:25`, `get_stock_bars` :75), `StockBarsRequest`
(`alpaca/data/requests.py:86`), `DataFeed` and `Adjustment` (`alpaca/data/enums.py:56,76`),
`Sort` (`alpaca/common/enums.py:36`), `TimeFrame` (`alpaca/data/timeframe.py:25`), `BarSet`
(`alpaca/data/models/bars.py:54`), and the one-request hook a paced client overrides,
`RESTClient._one_request(self, method, url, opts, retry)` (`alpaca/common/rest.py:176`, called
from the retry loop at :131).

The SDK's own `alpaca/data/requests.py:18` imports `alpaca.trading.enums`, so importing the data
names loads `alpaca.trading` inside the SDK. This package names no trading or broker symbol:
`tests/repo/test_landscape.py` refuses `alpaca.trading` and `alpaca.broker` in its source.
"""

import inspect
from importlib.metadata import version
from pathlib import Path
from typing import cast

from pins import pinned, upstream_tag

ROOT = Path(__file__).resolve().parents[2]
DISTRIBUTION = "alpaca-py"
ROW = ("alpaca-py", "alpacahq/alpaca-py")


def _pinned() -> str:
    return pinned((ROOT / "pyproject.toml").read_text(encoding="utf-8"), DISTRIBUTION)


def _upstream_tag() -> str:
    return upstream_tag((ROOT / "UPSTREAMS.md").read_text(encoding="utf-8"), *ROW)


def test_the_dependency_pin_is_the_upstream_tag() -> None:
    assert "v" + _pinned() == _upstream_tag()


def test_the_installed_sdk_is_the_pinned_release() -> None:
    assert version(DISTRIBUTION) == _pinned()


def test_the_names_the_bars_part_reads_import_at_the_pin() -> None:
    from alpaca.common.enums import Sort
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical.stock import StockHistoricalDataClient
    from alpaca.data.models.bars import BarSet
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    assert (DataFeed.SIP.value, DataFeed.BOATS.value) == ("sip", "boats")
    assert (Adjustment.RAW.value, Adjustment.SPLIT.value) == ("raw", "split")
    assert Sort.ASC.value == "asc"
    # `Minute` and `Day` are class properties (`alpaca/data/timeframe.py:105,123`); the cast names
    # what they return for the type checker.
    minute, day = cast(TimeFrame, TimeFrame.Minute), cast(TimeFrame, TimeFrame.Day)
    assert (minute.value, day.value) == ("1Min", "1Day")
    assert callable(StockHistoricalDataClient.get_stock_bars)
    assert {"symbol_or_symbols", "timeframe", "start", "end", "limit", "adjustment", "feed"} <= set(
        StockBarsRequest.model_fields
    )
    assert BarSet.__name__ == "BarSet"


def test_the_one_request_hook_a_paced_client_overrides_keeps_its_signature() -> None:
    from alpaca.common.rest import RESTClient
    from alpaca.data.historical.stock import StockHistoricalDataClient

    assert issubclass(StockHistoricalDataClient, RESTClient)
    assert "_one_request" not in vars(StockHistoricalDataClient)
    hook = inspect.signature(RESTClient._one_request)
    assert list(hook.parameters) == ["self", "method", "url", "opts", "retry"]
    assert "self._one_request(method, url, opts, retry)" in inspect.getsource(RESTClient._request)
