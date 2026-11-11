# sextant-movers design

*Design, 2026-11-11. Every rule here is carried from Sextant's design and ADRs at its `main` (`0a4d42b2138a8ad511016505ec476d0fa0382ba2`) or from the lead's approved plan for this repository. Nothing here is a new decision: open choices sit in "What is not settled".*

## Purpose

Rank the US equity names most likely to become extreme movers, as they start, in every supported session, and show that rank to the owner in TradingView. The rank is a display and a contract input to Sextant; this repository places no order and makes no entry decision. Ranking supplies candidates; it does not establish eligibility, fills or an economic edge (Sextant ADR-0003, Context).

## The rank

Carried from Sextant `docs/design.md:21` and ADR-0003 (decisions K1, K2, K5, K6).

- **Universe:** every active NMS-listed common stock on Tapes A, B and C, every cap and price, sub-$1 included, plus TXSE listings and NMS-listed ADRs labelled `security_type = adr`. OTC stays out until an owner-approved data source exists. No price or volume floor applies to ranking (K1, K8). The listing source is the Nasdaq Trader symbol directory files.
- **Score:** `move × dvol_ratio`, ties by `trades_accel` (K2). `move` is measured from the most recently completed official regular-session close, split-adjusted; an undocumented fallback gives none. POST uses that day's close, OVERNIGHT the session just ended, PRE and RTH the previous session. `dvol_ratio` is segment-to-date dollar volume over the symbol's own time-matched baseline; `trades_accel` compares a short-window trade count with the prior baseline rate. Dollar volume never forms the score.
- **Recompute and publish:** re-evaluate on every eligible trade; publish at most once per `publish_min_interval_s`, a registry value.
- **Sessions (K5):** `exchange_calendars` XNYS actual open and close, early closes included. PRE runs 04:00 to the open, POST from the actual close to 20:00 ET, OVERNIGHT 20:00 to 04:00 ET, assigned to the next trading date. The actual closing print belongs to no extended segment.
- **Freshness (K6):** each generation carries its identity, coverage and known-at time. A missing heartbeat, or a stale, invalid or incomplete generation, makes the rank unavailable; there is no fallback and no reuse of a previous list. Unknown halt state makes that symbol ineligible.
- **Engine seam:** a generation is NautilusTrader custom data. An actor publishes it with `DataActor.publish_data` and a consumer receives it through `DataActor.subscribe_data` or the raw `MessageBus.subscribe` (NautilusTrader v2.0.0rc8 `d53849b2`, `crates/common/src/python/actor.rs:1537-1540,1739-1750`, `crates/common/src/python/msgbus.rs:669-671`). A raw bus publish does not reach a `DataActor`.
- **Ours by design:** no upstream ranks the whole universe live with a time-of-day volume baseline, so the score engine and the baseline are ours, cited to the papers in [ADR-0002](adr/0002-movers-foundation.md). The engine's indicators are not used: its rate of change counts updates, not time, and ignores trades (`crates/indicators/src/momentum/roc.rs:98,154-170` at `d53849b2`), and its VWAP works in floats on a bar's typical price (`crates/indicators/src/average/vwap.rs:36-102`), which would break the Decimal digests the parity proofs compare. The engine's tick and quantity types and its `dt_to_unix_nanos` are used as they are (`python/nautilus_trader/model/__init__.pyi:7035` `TradeTick`, `:6365` `QuoteTick`, `:6315` `Quantity`; `python/nautilus_trader/core/datetime.py:81`).
- **Every numeric parameter** is a registry row with its source and calibration state. Display ages are display values only; they grant no entry authority.

## The display path

Carried from Sextant `docs/design.md:48,84` and ADR-0021, decided in [ADR-0003](adr/0003-tradingview-display.md).

1. The **producer** reads the latest valid generation on its **cadence** and writes the **ranked page**. A stale or mixed generation is unavailable, never an empty list.
2. The **list sync** writes the movers and focus watchlists' membership through TradingView's official MCP server, with read-back. Its dated beta contract is OAuth 2.1, Streamable HTTP and about 100 requests a minute per user. The sync tolerates at most five unmapped names, never reorders the rank, and never feeds a machine signal back. Membership is not rank order: TradingView has no official unrestricted way to push a ranked order.
3. An account-wide **request meter** counts every call against that budget, and a **latched stop** halts the sync and stays latched once tripped. A 405 is not a refusal, and no lock is held across a wait (Sextant's resolver rounds). Who may clear the latch is open.
4. The **stored sign-in** is RAM-only by default. A kept login is allowed only to the official SDK sync process, encrypted at rest, with no plain file, environment variable, argv value, log line or stdout copy. A dead grant is deleted; local deletion is not server revocation.
5. The producer also writes TradingView's documented **watchlist import file**, a zero-call path the owner can load by hand.
6. The **local chart page** draws the rank's own data on Lightweight Charts, keeping its NOTICE and attribution.

## The boundary

One way: Sextant depends on `sextant_movers`; this package never imports Sextant.

**Lives here:** the ranking (score engine, baseline, generation, publisher, reader, consumer, rank gate, the ranking Actor and node shell) and the TradingView display (producer, cadence, ranked page, list sync, stored sign-in, display scripts), with the service units under `deploy/movers/` and the registry parameter files as package data. Small sourced helpers of its own, each with its upstream citation: atomic file write, the account lease and stream reservation, paced history fetch, session segments and stream facts.

**The lease:** it uses the same lock file name and `flock` as Sextant's custody lease, so an old and a new process still exclude each other. One stream per broker account; a stream opens only under the lease.

**Stays in Sextant:** its Alpaca package (the data client shares a package with Paper B's execution client), its custody module, the baseline and calibration jobs, and Paper B. The ranking node takes the data-client factory as an argument; Sextant keeps a thin entry that passes its own.

**Pins:** NautilusTrader and alpaca-py are pinned to the same commits in both repositories, this package bumped first. Each build part adds its own runtime dependency at Sextant's pin.

## The contract

`sextant_movers/contract.py`, stable and small: `BaselineTable`, `Segment`, `segment_start`, `NS`, `TradeWindow`, `RankingConsumer`, `ranking_data_type`, `RankGateDecision`, `RankGateStatus`, `decide_rank_gate`, `RegistryRow`, `Registry`. `contract_data.py` holds the listing and paced-bars names that Sextant's ranking-inputs module uses today. Parameter files ship as package data and Sextant reads them through `Registry`. The consumer exposes names and vetoes only and imports no order, execution, broker or account surface.

## Build order

Each PR is under 45,000 characters, upstream cited, red test first; a part that does not fit one PR names its PRs. A part is a clean build on its landscape row, not a file copy; Sextant's module and its tests are the reference and the acceptance.

1. **Scaffold:** adapted harness, CI, source check, empty contract, `docs/landscape.md` v1.
2. **Helpers:** calendars on `exchange_calendars`; a cross-process proof that the reference lease and the new one refuse each other.
3. **Generation and registry:** on NautilusTrader custom data and the message bus (`docs/concepts/custom_data.md`, `message_bus.md` at `d53849b2`); proof re-hashes the recorded generations.
4. **Score engine, window, baseline:** ours by design, in four PRs (window; segment and baseline table; baseline builder; engine); proof is seeded synthetic tapes whose digests equal the reference's in a locally marked child-process run. That a recorded session's rank matrix reproduces is proven once part 5 and a recording exist, and a recording needs the node of part 10: no recorded tape, prior closes or vendor-built baseline exist before then. The recorder is the engine's own, `LiveNodeConfig(streaming=StreamingConfig(...))` (`python/nautilus_trader/live/__init__.pyi:779,853`, `python/nautilus_trader/persistence/__init__.pyi:775`): a node setting that arrives with part 10, so no recorder is built here.
5. **Publisher, bars, listings:** in several PRs. Bars on alpaca-py `alpaca/data/historical/`; listings on the Nasdaq Trader symbol directory files; the official movers and most-actives endpoint as a cross-check report, never an input. The universe's operating-company filter has no source yet and is not built.
6. **Producer, cadence, ranked page, service unit:** the display runs from here, with incremental replay from the start.
7. **TradingView sync and stored sign-in:** on the official MCP Python SDK (`src/mcp/client/auth/`, `streamable_http.py`), with the request meter and latched stop.
8. **Local chart page:** on Lightweight Charts.
9. **Consumer and rank gate:** the contract complete; Sextant's rank-gate proofs run against it.
10. **Ranking Actor and node shell:** on NautilusTrader `docs/concepts/actors.md` and `live.md`, with the data-client factory injected.

## Parity

No live shadow run is possible: one stream per account. A locally marked proof reads a private directory named by `$MOVERS_PARITY_DIR`, runs the reference and the new code over the same recorded input in one process and compares digests. CI runs the same proof on a synthetic tape. PR bodies cite digest counts only; recorded data never enters the repository. No input tape is recorded today and no private recorder is built: the recorder is the engine's streaming setting on the node (build order, part 4), so until part 10 the recorded-input proof skips and only the synthetic one runs.

## Cutover

1. **Producer:** after part 6, install `sextant-movers-prestage.service` from a pinned checkout with the same data paths and lease root. Swap between 20:00 and 04:00 New York, since the unit serves PRE, RTH and POST. The old unit stays disabled for a week as the step back.
2. **Sextant PRs, in order:** add this package by pinned 40-hex commit with a proof that the shared pins match; switch every inbound import to the contract in one PR (the generation type registers as custom data, so two copies in one process are a hazard); repoint the Alpaca data client and the node entry; then delete Sextant's two packages and their tests.
3. Paper B in Sextant does not wait for any of this; its entries stay off in stage 1, so the consumer is not on its path until the contract switch.

## What is not settled

- **Saved baseline across a restart (open).** Today a restart costs a full baseline rebuild, during which the rank is unavailable. Part 6 answers whether a saved baseline lets a restart skip it, and under what freshness check. No mechanism is chosen.
- **Landscape coverage (done in part 1).** `docs/landscape.md` has one row per capability with its chosen repository, pin, licence, access path, runner-up and evaluation receipt, and `tests/repo/test_landscape.py` refuses a package no row names; every candidate still needs its evaluation before anything rests on it.
- **Unverified, checked in the first parts:** whether the screener endpoint works on the current data plan; TradingView's own terms text for the MCP server; the licence of any streaming engine not yet read.
- **Deletion in Sextant (the lead's decision).** About 25,000 lines leave Sextant. The deletion is the lead's decision and lands through Sextant's own gate; this repository only states its size.
