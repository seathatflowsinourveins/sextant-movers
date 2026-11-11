# ADR-0003: TradingView displays the rank and never drives anything

Status: accepted, 2026-11-11. Carried from Sextant's ADR-0021 (TradingView display, accepted 2026-11-06, with its 2026-11-10 stored-sign-in amendment) and the lead's approved plan for this repository, build part 7. Only the decisions that apply to this package are carried; the account details, host details and owner tickets in the original stay in Sextant.

## Context
TradingView is the owner's main display of the rank, charts and notifications. It is never the ranking universe, and its data is never truth for anything. Broker and engine data supply the rank's inputs.

## Decision
1. **Display only.** No agent or MCP client here gets order tools. No TradingView data feeds the rank, a machine signal, sizing or risk. No alert, webhook or panel places an order. Pine output and Strategy Tester fills are not evidence of anything.
2. **Official interface only.** List membership is written through TradingView's official cloud MCP server. Its dated beta contract, as Sextant recorded it: OAuth 2.1, Streamable HTTP, a non-trial Essential plan or higher, delayed market data and about 100 requests a minute per user. List limits are 1,000 symbols on Essential and above and 30 on Basic. The client is the official MCP Python SDK at v2.3.0 (`src/mcp/client/auth/oauth2.py` `OAuthClientProvider`, `src/mcp/client/streamable_http.py` `streamable_http_client`). TradingView's own terms text is read on its own page before the first sync; Sextant's survey saw only third-party summaries.
3. **What the sync writes.** The movers and focus lists show the published rank. Membership sync never reorders the rank and is not a rank-order guarantee. Stale, mixed or invalid input is unavailable: never an empty desired list and never a last-valid fallback. Every write is read back; at most five unmapped names are tolerated.
4. **Request meter and latched stop.** One account-wide meter counts every call against the rate budget, across every process that calls the server. A latched stop halts the sync and stays latched once tripped; its exact trip conditions are settled in part 7, and a 405 is not a refusal. No lock is held across a wait. These come from Sextant's resolver review rounds and are built in part 7.
5. **Stored sign-in.** RAM-only OAuth is the default. A kept login lets only the official SDK sync process store, read and rotate its own tokens and registration, encrypted at rest, with no plain file, environment variable, argv value, log line or stdout copy. A login the service definitely refuses (an RFC 6749 section 5.2 dead-grant code) is deleted; an outage keeps it. A browser sign-in runs only under an explicit authorize step. Forgetting the login deletes the local value; the owner also revokes the grant at TradingView, since local deletion is not server revocation. Key names only; the local store's form for this repository follows Sextant's names-only key injection pattern and is chosen in part 7.
6. **Zero-call path.** The producer also writes TradingView's documented watchlist import file (a text file of exchange-prefixed symbols) for the owner to load by hand.
7. **Local chart page.** Lightweight Charts v5.2.1 (`b2ce010e4ad59f6556c9ed60e2b2b30feccdd2d1`), Apache-2.0, keeping its NOTICE and attribution and link obligations.

## Rejected
- **Desktop debug-port controllers** (the pinned community MCP controllers Sextant runs under its Windows-only exception): reverse-engineered access. They are not used in this package; Sextant's exception stays in Sextant, outside it.
- **Community TradingView tools** for the sync or for any input: TradingView-Screener (undocumented endpoint), tvdatafeed (stale), tradingview-ta (archived).
- **Advanced Charts:** proprietary; not used.
- **A top-20 or focus-priority fallback list:** built only after a measured 429 or list cap, not before.

## Consequences
- There is no official unrestricted way to push a ranked order into TradingView; the lists carry membership only.
- The sync is a risk path: its PRs take the extra API review, and a review of the meter, the latch or the sign-in runs at max effort.

## Revisit when
- TradingView changes its MCP terms, entitlement, rate, tools or OAuth contract, or the SDK or chart pin moves: amend before the next sync. Owner: the lead.
- A measured 429 or list cap arrives: decide the fallback list. Owner: the lead.
- The owner gives new sign-in consent or revokes it. Owner: the owner.
