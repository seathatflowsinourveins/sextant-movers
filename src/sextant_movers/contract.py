"""The contract: the one module Sextant imports from this package.

Sextant depends on this package one way, through this module; this package never imports Sextant.
It stays stable and small. It will hold `BaselineTable`, `Segment`, `segment_start`, `NS`,
`TradeWindow`, `RankingConsumer`, `ranking_data_type`, `RankGateDecision`, `RankGateStatus`,
`decide_rank_gate`, `RegistryRow` and `Registry`, each added by the build part that builds it;
`contract_data.py` holds the listing and paced-bars names. Parameter files ship as package data
and Sextant reads them through `Registry`. The consumer exposes names and vetoes only and imports
no order, execution, broker or account surface. Display only, never a signal; no orders.

Source: docs/design.md, "The contract"; ADR-0002, decision 3.
"""

__all__: list[str] = []
