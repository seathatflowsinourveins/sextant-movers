"""The ranking engine is installed at the pinned release, and the names the ranking reads import.

NautilusTrader is the ranking's engine (docs/manifest.md, "NautilusTrader as the ranking engine").
The dependency pin in `pyproject.toml`, the tag in `UPSTREAMS.md` and the installed distribution
must name one release, so a line cited at the pinned commit is the code that runs. The window,
the baseline and the score engine read four names from it: `TradeTick`, `QuoteTick` and
`Quantity` (`python/nautilus_trader/model/__init__.pyi`) and `dt_to_unix_nanos`
(`python/nautilus_trader/core/datetime.py:81-104`), all at the pin.
"""

from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

from pins import pinned, upstream_tag

ROOT = Path(__file__).resolve().parents[2]
DISTRIBUTION = "nautilus-trader"
ROW = ("nautilus_trader", "nautechsystems/nautilus_trader")


def _pinned() -> str:
    return pinned((ROOT / "pyproject.toml").read_text(encoding="utf-8"), DISTRIBUTION)


def _upstream_tag() -> str:
    return upstream_tag((ROOT / "UPSTREAMS.md").read_text(encoding="utf-8"), *ROW)


def test_the_dependency_pin_is_the_upstream_tag() -> None:
    assert "v" + _pinned() == _upstream_tag()


def test_the_installed_engine_is_the_pinned_release() -> None:
    assert version(DISTRIBUTION) == _pinned()


def test_the_names_the_ranking_reads_import_at_the_pin() -> None:
    from nautilus_trader.core.datetime import dt_to_unix_nanos
    from nautilus_trader.model import Quantity, QuoteTick, TradeTick

    assert TradeTick.__name__ == "TradeTick" and QuoteTick.__name__ == "QuoteTick"
    assert Quantity.from_int(3).as_decimal() == Decimal(3)
    assert dt_to_unix_nanos(datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)) == 1_000_000_000
