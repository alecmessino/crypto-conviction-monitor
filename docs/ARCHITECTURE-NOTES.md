# Architecture notes

Written 2026-09-14 as Phase 0 of the audit. Factual map of file → responsibility,
so the Phase 1 findings can be reviewed against something. Nothing here is a
proposal; everything is what the code does today at `6492e5d`.

## 1. The shape of the thing

One scheduled Python job writes a directory of flat files; one self-contained HTML
page reads them and also re-fetches CoinGecko live in the browser. There is no build
step and no server.

```
.github/workflows/nightly.yml   06:17 UTC daily  ──►  python nightly.py
                                                        │
                                       fetch ───────────┤
                                  CoinGecko /coins/markets (250 by mcap)
                                  CoinGecko categories / global / trending / DEX
                                  8 perp venues (funding.py)
                                  Cryptometer (liquidations, optional key)
                                  Dune (Module B unlocks, optional key)
                                                        │
                                       write ───────────┤
                                  ledger/*.csv, ledger/*.json
                                                        │
                                       gate  ───────────┤
                                  scripts/validate_ledger.py
                                                        ▼
                                                git commit + push
                                                        │
index.html ◄── GitHub Pages ────────────────────────────┘
   │  reads ledger/*.json + ledger/*.csv
   └─ ALSO fetches CoinGecko live in the browser and re-scores client-side
```

The last arrow is the one that surprises people, and it is the source of several
Phase 1 items: **the board you look at is not the ledger.** The browser scores ~235
names from a live fetch; the ledger holds 50 rows a night. The two use ports of the
same scoring function that a test asserts are in parity.

## 2. Files

### Scoring and persistence

| File | Responsibility |
|---|---|
| `nightly.py` (4,540 ln) | Everything the scheduled job does. Fetch, score, all derived panels, all ledger writes. |
| `funding.py` (1,214 ln) | Eight perp venues, cross-venue consolidation, the funding regime curve and the score multiplier that comes off it. |
| `quant.py` (835 ln) | Pure functions over recorded data — no network. Correlation/beta/effective breadth, sector rotation, ADX/ATR, stablecoin regime, trending divergence. |
| `coingecko.py` (613 ln) | Credentialed CoinGecko session (probed, not assumed) plus the context feeds: categories, global, trending, DEX. |
| `cryptometer.py` (371 ln) | Liquidations and long/short positioning. Explicitly *not* funding. Optional key. |
| `rwa.py` (2,942 ln) | Tokenized real-world assets. A separate model on a separate release cadence; not part of the conviction board. |

### Front end

| File | Responsibility |
|---|---|
| `index.html` (7,769 ln) | The whole terminal. Inlined CSS + JS, no bundler, no dependencies. Contains a JS port of `score()` marked `/* MODEL PORT */ … /* END MODEL PORT */`. |
| `methodology.html` (477 ln) | The Method page. Every published number is supposed to have a "what this is / what it is not" line here. |

### Scripts and gates

| File | Responsibility |
|---|---|
| `scripts/validate_ledger.py` | Anti-degeneracy gate. Runs after the nightly and fails the job rather than publishing a bad ledger. |
| `scripts/observe.py` | Computes the two figures under standing observation so nobody has to go and look. |
| `scripts/check_dune.py` | Tells you whether a Dune query id is the right one in one command. |
| `scripts/{check,sync}_contract_specs.py` | Keeps `contract_specs.json` and its inlined copy in `index.html` from drifting. |

### Workflows

| Workflow | Trigger | Does |
|---|---|---|
| `nightly.yml` | cron `17 6 * * *` (06:17 UTC) | `python nightly.py`, Cryptometer smoke test, ledger gates, commit + push. 20-min timeout. |
| `tests.yml` | push / PR | pytest + playwright over `tests/` (30 files). |
| `rwa_release.yml` | separate cadence | RWA snapshot and its own model gate. |
| `check-dune.yml` | manual | Verifies the configured Dune query id. |

Secrets, all optional, all read from env, none in code: `COINGECKO_API_KEY`,
`DUNE_API_KEY` + `DUNE_UNLOCK_QUERY_ID`, `CRYPTOMETER_API_KEY`.

## 3. The scoring function

`nightly.score()` — `nightly.py:691`. Multiplicative, five terms:

```
conviction = clamp(round(100 · depth · cm · a_frac · emission_mult · perp_mult), 0, 100)

depth         = clamp((log10(mcap) − 6) / 4, 0, 1)            structural quality
cm            = 0.10 + 0.90 · (tanh(rs_blend / 25) + 1) / 2   market confirmation
rs_blend      = 0.30·rs7 + 0.25·rs14 + 0.25·rs30 + 0.20·rs200 (each rs = asset % − BTC %)
a_frac        = 1.0 if depth ≥ 0.90 else max(0.4, liquidity_fit(turnover)/30)
emission_mult = 1 − 0.10 · tanh(log(FDV/MC / 1.10) / scale)   Module F, null ⇒ 1.0
perp_mult     = funding.regime_modifier(apr, price_chg, rsi7) ∈ [0.85, 1.15], null ⇒ 1.0
```

Tiers, `nightly.py:1171`: `TIER_CUTS = ((80,"STRONG"),(70,"BUY"),(55,"HOLD"),(40,"WATCH"),(0,"AVOID"))`.
The header's "BUY+" means `conviction ≥ 70`.

The JS port is `conviction()` at `index.html:2108`, with `rsBlendOf()` at 2138 inside
the same guarded block, and `convictionFactors()` at 3429 rendering the decomposition.

## 4. The specification hash

`nightly.spec()` (`nightly.py:250`) does not enumerate constants — it **parses the
source**, strips docstrings, and unparses each scoring function to canonical text.
Captured: `score`, `_lavl_regime`, `lavl_perp_mult`, `_tier_for`, `emission_drag`,
`emission_mult`, six `nightly` constants, eight `funding` functions and fifteen
`funding` constants. `spec_hash()` is a 12-char SHA-256 of that blob, assigned at the
*bottom* of the module (`nightly.py:4536`) — deliberately, because assigning it at the
top once hashed five not-yet-defined constants as null.

**Current hash: `6f98778fa627`.** Recorded boundaries in `ledger/signals.csv`:

| First night | Hash | Note |
|---|---|---|
| 2026-08-01 | *(none)* | pre-hash |
| 2026-08-09 | `d600984ec00b` | |
| 2026-08-16 | `e65f7dc59d55` | |
| 2026-08-19 | `2da60f7efd7b` | Module F lands |
| 2026-08-20 | `6f98778fa627` | current; `2da60f7efd7b` canonicalises onto it |

What it does **not** capture — verified by running `spec()` — is the layer that decides
*which* funding reading reaches the score: `funding.VENUE_PRIORITY`,
`funding.consolidate()`, `funding.INTERVAL_BASIS_REAL`, `funding.VENUE_DEFAULT_INTERVAL`,
`nightly.perp_context()` and `nightly._rsi_by_symbol()` are all outside it. The funding
*curve* is hashed; the inputs handed to it are not. See AUDIT-2026-09 §1.8.

`SPEC_EQUIVALENT` (`nightly.py:367`) holds exactly one entry, collapsing
`2da60f7efd7b` → `6f98778fa627` as an instrumentation fix rather than a model change,
and `spec_hash_as_recorded_before()` makes that claim re-derivable rather than asserted.

**The 2026-08-05 boundary the brief refers to is not a hash boundary.** It predates the
hash entirely and is detected from the data by `_spec_breaks()` (`nightly.py:2276`):
a night where the median asset's conviction moved ≥ 10 points while the median asset's
price moved ≤ 2%. That detected date is what `_perf_legs()` uses as `spec_boundary`,
and it is what the Selection Edge panel starts counting legs from. Confirmed by running
`_compute_edge()` today: `boundary = "2026-08-05"`, `legs = 40`.

## 5. The ledger

`ledger/` is flat files committed by the job. The two that matter here:

**`ledger/signals.csv`** — 71 columns, 2,240 rows, 45 nights (2026-08-01 … 2026-09-14),
**50 rows per night**. Written at `nightly.py:4166` as `rows[:50]` *after* `rows.sort(key=conviction, reverse=True)`
at 4137. Today's rows are replaced rather than appended, so a re-run does not duplicate a night.

> Note for the audit: four comments in `nightly.py` (lines 1486, 1689, 1693, 1779)
> describe this as "the top 50 **by market cap**". It is the top 50 by **conviction**.
> See AUDIT-2026-09 §1.0 — this is the finding with the widest blast radius.

**`ledger/sectors.csv`** — one row per surviving category per night, 416 KB. The
category endpoint has no history at all, so multi-day sector flow is *accumulated* here
rather than fetched.

Other ledger artifacts: `index.{csv,json}` (the paper book), `monitor.json` (pipeline
health), `market_breadth.json`, `market_intel.json` (sectors / correlation / trending /
DEX), `funding.json` (per-venue funding detail), `venue_health.csv`, `macro.csv`,
`dex.csv`, plus the RWA set.

## 6. Fetch layer

**CoinGecko.** `coingecko.open_session()` probes `/ping` against the Demo host and then
the Pro host and reports which one accepted — it never infers the plan from the key's
shape. Today's run: `plan="demo", status="live"`. `nightly.fetch_markets()` pages
`/coins/markets` at `per_page=125` with a 3.5 s delay; the browser asks for
`per_page=250&page=1` in one call (`index.html:1779`).

**Perps.** `funding.py` has eight fetchers behind `VENUE_FETCHERS`, consolidated by
`consolidate()` with `VENUE_PRIORITY = (binance, bybit, gateio, kraken, dydx, …)`. The
headline APR is taken from the highest-priority venue **whose settlement interval is
published**, never averaged; cross-venue dispersion is kept separately as `apr_spread`.
On 2026-09-14 Binance returned 451 and Bybit 403 (both geo-blocked from the runner), so
gate.io supplied the headline for 43 of 50 rows. `ledger/venue_health.csv` records this.

**Dune.** Module B only. Absent key ⇒ the columns are null, which is where they are.

## 7. Selection Edge / IC

`_compute_edge()` (`nightly.py:1871`) → `_edge_legs()` (1306):

- one leg per consecutive night pair, starting at the detected boundary;
- per leg, Spearman ρ between night-t conviction and night-t→t+1 simple return, over
  names priced on both nights; legs with < 10 names are dropped (`EDGE_MIN_NAMES`);
- quintile spread with `k = max(3, n // 5)`;
- across legs: mean IC, SD, SE, 95% interval `mean ± 1.96·SE`, t-stat;
- `measurable` is true only when `legs ≥ 40` **and** the interval excludes zero.

Reproduced today, verbatim: `legs=40 · mean_ic=−0.0505 · ci=[−0.1034, +0.0024] ·
t=−1.87 · mean_spread_bp=−98.7 · measurable=false`, with `book_total=32.49` against
`equal_weight_total=53.87` (the −21.4 pp). `legs_needed = {0.02: 250, 0.03: 111, 0.05: 40}`
— the 111–250 figure in the brief.

`_active_contributions()` (1371) does the per-name attribution, Carino-linked, and is
labelled `_ATTRIB_BASIS` = "Arithmetic, not evidence."

## 8. Front-end tiers and labels

- `signal(c)` at `index.html:2134` maps conviction → STRONG / BUY / HOLD / WATCH / AVOID,
  the same cuts as `_tier_for`.
- Header "BUY+ (≥70)" is `#rb-str`, filled at 2778 from `STATE.filter(t=>t.conv>=70)`.
- "QUALIFIED n/N" (`#skew`, 2754) and System-panel "Skew Qualified" (`#sys-skew`, 2755)
  are **the same number** — the conjunctive gate `gated` computed in `build()` at 2192:
  turnover 30–60%, dilution ≤ 2.0 (with proxy-ERA ≤ 1.5 when FDV exists), LAVL band not
  COMPRESS/LIQ TRAP and turnover < 90%.
- `factorBreakdown()` (3480) already self-checks: it recomputes the product and prints
  "does not reconstruct: board says N" when its own chain disagrees with the published
  score.
- `rowToState()` (6896) renders a recorded night through the same path as a live one.
- `nightlyFeedState()` (6100) renders the "DEMO KEY" chip — and the comment above it is
  explicit that the chip describes *the nightly's* credential, not the browser's.

## 9. Tests

30 files under `tests/`. The ones that constrain this audit most:

- `test_persistence.py` — re-derives `2da60f7efd7b` from today's source; the equivalence
  claim is executable, not a comment.
- `test_parity.py` — asserts the JS port and the Python `score()` agree.
- `test_edge.py` — asserts the semantics the edge panel protects.
- `test_validate_ledger.py`, `test_ledger_integrity.py` — the anti-degeneracy gates.
- `test_labels.py`, `test_a11y.py`, `test_contrast.py`, `test_render.py`, `test_terminal.py`,
  `test_drawer.py` — the front end.

Any scoring change has to move the hash *and* keep `test_parity` green, which means
`index.html` and `nightly.py` change together or not at all.
