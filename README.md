# sextant-movers

A Python package, `sextant_movers`, that ranks US equity movers as they start and shows that rank in TradingView.

## What it is

- **The ranking:** a score engine and a time-of-day volume baseline, generation, publisher, reader, consumer, rank gate, and a ranking Actor and node shell on [NautilusTrader](https://github.com/nautechsystems/nautilus_trader). Bars and listings come through [alpaca-py](https://github.com/alpacahq/alpaca-py); sessions through [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars).
- **The display:** a producer on a cadence, a ranked page, list sync through TradingView's official MCP server on the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), a stored sign-in, and a local chart page on [Lightweight Charts](https://github.com/tradingview/lightweight-charts).

No source code exists yet; [the design](docs/design.md) gives the build order of its ten parts.

## What it is not

- **Not a signal.** TradingView is a display; nothing it returns feeds the rank or anything else.
- **Not a trading system.** It places no orders and holds no order code.
- **Not a data store.** Recorded data, symbols, prices, quantities and account details never enter this repository.

## How Sextant consumes it

Sextant installs this package by a pinned 40-hex commit and imports only the small contract in `sextant_movers/contract.py` and `contract_data.py`. This package never imports Sextant. NautilusTrader and alpaca-py are pinned to the same commits in both. See [the design](docs/design.md#the-boundary) and [ADR-0002](docs/adr/0002-movers-foundation.md).

## Rules and the gate

| Path | What it is |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | The operating rules: Rule 1, Rule 2, lanes, landing, hard lines, model mix |
| `SOURCE-HIERARCHY.md` | Official upstream first; a community repository is a candidate until evaluated here |
| `docs/design.md`, `docs/adr/` | The design and the one-page decisions |
| `docs/manifest.md`, `MANIFEST-TEMPLATE.md` | Every pick with its pinned source and proof; the row pattern |
| `docs/handbook/`, `docs/LESSONS.md` | Practices, lifecycle, the ecosystem map; lessons that transfer |
| `.github/`, `dangerfile.js` | CI, the default-branch gate (Danger and dsh-github review), rulesets and the review rules |
| `justfile`, `scripts/`, `tests/` | `just pr`, `just ci`, `just prove`, `just session-start`; the proofs and size budgets |
| `UPSTREAMS.md`, `SETUP.md`, `ORIGINS.md` | Pinned upstreams; secrets and settings by name; where each seeded file came from |

A PR lands by squash auto-merge when `ci`, `source` and `review` pass. The harness was seeded from a template taken from the Sextant project ([ORIGINS.md](ORIGINS.md)).

## Licence

Not yet chosen; the owner decides. Until a `LICENSE` file is added, no licence is granted. Two vendored pieces keep their own terms: `.github/actions/dsh-github/` carries its upstream `LICENSE`, and the pinned upstreams in [UPSTREAMS.md](UPSTREAMS.md) keep theirs.
