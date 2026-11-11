# Philosophy

> Copied from Sextant's `docs/handbook/philosophy.md` (see [ORIGINS.md](../../ORIGINS.md)). Sextant's trading philosophy is not included; this page keeps **How we learn** (less its order-sending evaluation tiers, which have no counterpart here) and **One practice, one home** as written, and states what this repository is for.

## What this repository is for
It ranks market movers and shows the ranking on TradingView. Sextant installs it at a pinned commit. The display is display only and never a signal.

## What never changes
- No code here places an order or imports a trading client.
- TradingView is never a signal or an input to the rank, and it is reached only through its official interface.
- A broker stream opens only while it holds the account lease.
- No recorded data, symbol, price, quantity, order id, account id or private path enters the repository or PR text.

## How we learn
- **Every rule comes from the tape, in this order:** capture the incident's exact trades and quotes; write a replay test that fails first; write the rule so the test passes; write the decision, the registry row and the handbook page; record a paper run against hold-flat. No rule is adopted on opinion.
- **Numbers are calibrated, not argued.** Every threshold is a registry row, uncalibrated until a calibration record exists. A row changes between sessions only, when a walk-forward replay beats both the incumbent and hold-flat at stressed costs, beyond its measured noise.
- **Compounding by self-reflection.** Every session ends with a reflection on what each rule did against hold-flat and which movers it missed. Every exit leaves a counterfactual and a lesson. The nightly replay writes proposals, and only the rule above can adopt them.

## One practice, one home

Each rule of the practice has exactly one home in this repository, and everything else links to it. The order of authority is this page (why) → [the design](../design.md) (the ranking and display path) → [the ADRs](../adr/) (decisions with sources) → [the manifest](../manifest.md) (each pick, its source, its proof) → [AGENTS.md](../../AGENTS.md) and [CLAUDE.md](../../CLAUDE.md) (short operating rules that link, never restate) → settings, hooks, launch definitions and `just` recipes (the rule as mechanism). A rule stated in two places is a defect; the lower layer links upward. A relaunched session needs only this repository, with no ruling file, memory or hand-typed step in between.

Authority (a private Sextant ruling, named with its hash): owner direction D-13, Sextant ruling LEAD-2359, SHA-256 `1f357de2cf709978d46800eb1632c8adaf4841916c84a156e4bd2328b9432d1a`.
