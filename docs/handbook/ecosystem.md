# The ecosystem map

> Copied from Sextant's `docs/handbook/ecosystem.md` (see [ORIGINS.md](../../ORIGINS.md)). The routing rule, the citing rule and the search order carry over; the routing rows and the model mix are this repository's.

One page for routing a question to the record that answers it. The generated
[llms.txt](../../llms.txt) lists every decision, lane, handbook page and research note;
this page says which tool to reach for first.

## Session start

Run `just session-start`. It checks the live dsh profile against
`config/dsh/profile.lock.json`, then checks that `llms.txt` is current. Drift stops the
session: fix the profile or the lock, never the check.

## Route a question

| Question | First move | Owner |
| --- | --- | --- |
| What this repository is for: the ranking and its display | `docs/design.md` | Design |
| Why a decision was made | Search `docs/adr`; cite the ADR number | Decision record |
| Which pick, version, source or proof is in force | `docs/manifest.md`; `just prove` for its test | Manifest |
| Where a symbol is defined or called | `codegraph_explore` first, then `read` | Code |
| What a previous session concluded | `session_search`; then `session_trace` | Session record |
| A library, SDK, CLI or cloud API | `Context7` docs before guessing | Upstream |
| A paper, article or external claim | `paper-search`, `deepread`, or `Exa`; keep the URL | Source |
| A pull request, issue, check or run | `gh pr view`, `gh issue view`, `gh run view` | GitHub |
| A run, worker or automation | `orca orchestration`; `worker-list` for state | Orca |
| A lifecycle gap | `handbook/lifecycle.md`, then the owner above | Delivery stage |
| Which runtime, model and effort a job runs on | the model-mix table below | Harness |

### The model mix

The gate's reviewers, as `.github/workflows/gate.yml` pins them:

| Review | Model | Effort |
| --- | --- | --- |
| dsh-github, every PR | `deepseek-flash` | max (`config/dsh/mcp/max-effort.cordis.yml`) |
| Codex quality review, when routed | `gpt-6.1-sol` | `max` |
| Claude quality review, when routed | `claude-fable-5-2` | `high` |

Every other model surface follows AGENTS.md Rule 2. Each value is uncalibrated until measured.

## The rule behind the table

Every question has one first move and one record owner; a second owner is a duplicate,
not redundancy. The delivery-stage owners are [lifecycle.md](lifecycle.md)'s table, not a
second list here.

## Cite

Every answer names its evidence: `path:line`, a commit or run URL, or a source URL.
"Cite" means the reader can open the same record and check the claim. An uncited claim is
a draft, not a finding.

## Search order

1. The generated index: `llms.txt`.
2. The written record: `docs/`, then `src/sextant_movers/`.
3. The code graph: `codegraph_explore`.
4. Session history: `session_search`.
5. External sources: `Context7`, then the cited paper or page.
6. GitHub and Orca: `gh`, then `orca orchestration`.

If a route is missing, add it here with the same one-owner rule; do not add a second
plugin or a second index.
