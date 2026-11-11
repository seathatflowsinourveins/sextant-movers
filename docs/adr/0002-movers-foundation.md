# ADR-0002: sextant-movers is a clean, one-way package for the ranking and its display

Status: accepted, 2026-11-11. Decided by the owner (scope, timing, its own lead, public visibility) and recorded by this repository's lead. [ADR-0001](0001-sextant-foundation.md) keeps its file name because the gate's workflows and proofs cite it; this record is the repository's own foundation.

## Context
In Sextant the ranking and the TradingView display are about 10,350 source lines that import its execution, data, Alpaca and paper packages in both directions, and every change queues behind Paper B in one serial gate. Many ranking citations point at earlier repositories that Sextant's source rule treats as candidates, not official upstream.

## Decision
1. **Scope.** This package holds the ranking (score engine, time-of-day volume baseline, generation, publisher, reader, consumer, rank gate, the ranking Actor and node shell) and the TradingView display (producer, cadence, ranked page, list sync, stored sign-in, local chart page). It is display only, never a signal, and holds no order code. Paper B stays in Sextant.
2. **One-way dependency.** Sextant installs this package by pinned commit and deletes its own copy; this package never imports Sextant. The ranking node takes the data-client factory as an argument.
3. **Contract.** Sextant imports only `sextant_movers/contract.py` (`BaselineTable`, `Segment`, `segment_start`, `NS`, `TradeWindow`, `RankingConsumer`, `ranking_data_type`, `RankGateDecision`, `RankGateStatus`, `decide_rank_gate`, `RegistryRow`, `Registry`) and `contract_data.py` (listing and paced-bars names). Parameter files ship as package data.
4. **Same pins as Sextant** for the engine and the broker SDK: nautechsystems/nautilus_trader@v2.0.0rc8 `d53849b216e20f2c122ca01b4dd0da377638b186` and alpacahq/alpaca-py@v0.44.0 `cc4cb3b7ba50ae250e621983c2779047fb16bb28`, this package bumped first. Sessions come from gerrymanoim/exchange_calendars@4.13.2 `dbe38b1f6887434bbdd1a7d2df6ff8f1742a048a`.
5. **Landscape rule.** Official upstream first. A community repository is allowed where it is the best maintained practice; it is a candidate until a recorded end-to-end run and evaluation here. A tool that reaches TradingView by a reverse-engineered or terms-violating path is never used ([SOURCE-HIERARCHY.md](../../SOURCE-HIERARCHY.md)). `docs/landscape.md` (build part 1) holds one row per capability, and a repo check will refuse a source file whose package has no row.
6. **The lease.** One stream per broker account. The account lease and stream reservation use the same lock file name and `flock` as Sextant's custody lease, so an old and a new process exclude each other.
7. **Ours by design.** No maintained engine ranks the whole universe live across pre-market to post-market with a time-of-day volume baseline; the three open-source scanners found are hobby repositories, read for ideas only. So the score engine and the baseline are ours, cited to:
   - Białkowski, Darolles and Le Fol (2008), "Improving VWAP strategies: A dynamic volume approach", Journal of Banking & Finance 32(9):1709-1722, doi:10.1016/j.jbankfin.2007.09.023 (DOI read from bibliographic indexes, 2026-11-11);
   - Chen, Feng and Palomar (2016), "Forecasting intraday trading volume: a Kalman filter approach", SSRN abstract 3101695; its reference implementation is the R package intradayModel;
   - Satish, Saxena and Palmer (2014), "Predicting intraday trading volume and volume percentages", The Journal of Trading 9(3) (DOI not verified).
   No Python reference implementation of these models exists. The score itself is Sextant's house policy (Sextant ADR-0003, Evidence: no upstream mover-times-volume score exists).
8. **Parity before cutover.** No live shadow run is possible. A locally marked proof compares the reference and the new code by digest over private recorded input; CI runs it on a synthetic tape.
9. **Own lead.** This repository has its own lead, dispatcher and lander, an owner exception to Sextant's one-lead rule for this repository only.

## Rejected
- Keeping the two packages in Sextant: every change stays behind Paper B's serial gate.
- Renaming ADR-0001 to this file: every link in the gate files would change in the same PR.
- Rebuilding a mechanism an upstream ships (calendars, custom data, message bus, actors, the SDK's data clients, the MCP client's OAuth and transport).

## Consequences
- About 10,350 lines and 15,000 test lines leave Sextant; their deletion is the lead's decision and lands through Sextant's own gate.
- A second copy of the generation type in one process is a hazard, so Sextant switches every inbound import to the contract in one PR.

## Revisit when
- A maintained upstream ranks the full universe with a time-of-day baseline: evaluate it as a candidate. Owner: the lead.
- A shared pin moves in Sextant: bump here first. Owner: the lead.
