# Phase 1A — factor audit, 2026-09-17

Read-only. No scoring logic was changed by this document or by the commit that carries
it; the only code change is Phase 1B's factor-level logging, described in §7.

Every number below was reproduced from this repository at `333072b` against a live
`/coins/markets` pull taken 2026-09-17 (250 rows, 234 after the stablecoin filter),
`ledger/signals.json` (48 nights, 2026-08-01 … 2026-09-17, 2,390 rows) and
`ledger/xsec/2026-09.csv` (3 nights, 703 rows). The reproduction harness is the same
arithmetic as `index.html`'s MODEL PORT, run in Python; it reconciles against
`nightly.score()` on all 235 scored rows with zero disagreements.

This continues `AUDIT-2026-09.md` and does not restate it. Where the two touch the same
ground — §1.1 saturation, §1.0 ledger truncation — the earlier finding stands and is
referenced rather than re-derived. **One earlier conclusion is overturned: §1.1's
"the HBAR line did not reproduce". It reproduces exactly, on the published board, and
§3 below explains why the earlier harness could not see it.**

---

## 1 — What each factor is

The chain, in `index.html:2115` (`conviction`) and `nightly.py:824` (`score`), is
`100 × DEPTH × CONFIRM × LIQUIDITY × SUPPLY × FUNDING`, rounded and clamped to `[0,100]`.

| factor | definition | source | lookback | cadence |
|---|---|---|---|---|
| **DEPTH** | `clamp((log10(market_cap) − 6) / 4, 0, 1)` | CoinGecko `/coins/markets` → `market_cap` | none — point-in-time | every page load (board) / nightly (ledger) |
| **CONFIRM** | `0.10 + 0.90 · (tanh(rs_blend/25) + 1)/2`, where `rs_blend = 0.30·rs7 + 0.25·rs14 + 0.25·rs30 + 0.20·rs200` and each `rsN` is the asset's N-day % change minus BTC's | CoinGecko `/coins/markets` → `price_change_percentage_{7,14,30,200}d_in_currency` | 7 / 14 / 30 / 200 calendar days, blended | every page load / nightly |
| **LIQUIDITY** | `1.0` if `DEPTH ≥ 0.90`; else `max(0.40, liquidityFit(vol/mc)/30)` | CoinGecko `/coins/markets` → `total_volume ÷ market_cap` | trailing 24h volume | every page load / nightly |
| **SUPPLY** | `1 − 0.10 · tanh(ln(r/1.10)/scale)` for `r = FDV/MC > 1.10`; exactly `1.0` when `r ≤ 1.10` **and** exactly `1.0` when FDV is unpublished | CoinGecko `/coins/markets` → `fully_diluted_valuation` | none — point-in-time | every page load / nightly |
| **FUNDING** | `funding.regime_modifier(funding_apr, price_chg_24h, rsi7)` | **neither.** Direct perp venue APIs via `funding.py`: binance, bybit, gateio, kraken, dydx, hyperliquid, coinbase, xoomar. One venue is *selected* by `VENUE_PRIORITY`, not consolidated | latest funding print (one settlement interval); `rsi7` is 7 recorded nightly closes + tonight's live price | nightly only — **and this is the problem, see §3** |

**Provenance summary.** Four of the five factors are CoinGecko-derived. **None is Module
B / Dune.** The Dune feed populates `era`, `unlocks_usd`, `supply_increase_pct` and
`addr_growth_pct`; `score()` computes an `era` proxy and a Module-B component `b`, and
then uses neither — they appear in `comp` for attribution only. FUNDING is the one
factor from neither source.

## 2 — Bounds, and where the clamp is

| factor | stated bound | actual reachable bound | observed on 234 rows |
|---|---|---|---|
| DEPTH | `[0, 1]` by construction | `[0, 1]` | **`[0.516, 1.000]`** — a top-250 universe has no member below $116M, so the lower half of this factor's range is unreachable by construction |
| CONFIRM | **code comment says `[0.10, 0.91]`** in both `index.html:2126` and `nightly.py:880` | **`(0.10, 1.00)`.** `0.10 + 0.90·1 = 1.00`, not 0.91 — the comment is wrong in both files | `[0.177, 1.000]`; **37 rows sit at ≥ 0.9099**, i.e. above the bound the code claims |
| LIQUIDITY | floor `0.40` explicit; ceiling `1.0` | `[0.40, 1.00]` | `[0.400, 1.000]` |
| SUPPLY | `EMISSION_MAX_PENALTY = 0.90` floor, `1.0` ceiling | `[0.90, 1.00]` | `[0.904, 1.000]` |
| FUNDING | `[MOD_MAX_PENALTY, MOD_MAX_BOOST]` = **`[0.85, 1.15]`** | `[0.85, 1.15]` **from `regime_modifier`** | **`[0.916, 17.400]` as applied by the board** — see §3 |

**The clamp** is applied in exactly two places and nowhere else:

- `index.html:2133` — `Math.max(0, Math.min(100, Math.round(100*depth*cm*risk)))`
- `nightly.py:906` — `max(0, min(100, int(round(100 * depth * cm * risk))))`

`index.html:3539` `convictionFactors()` records `raw`, `rounded` and `clamped`
separately, and `factorBreakdown()` prints "clamped from N" — but only inside the
inspector, for the one selected row. Nothing on the board, in `signals.csv`, in
`signals.json` or (until this commit) in `ledger/xsec/` recorded the pre-clamp value.

## 3 — The defect: FUNDING is not read from tonight

This is the finding. It is not saturation, and it is not a funding reading.

`index.html:1936-1939` builds the board's funding overlay:

```js
PERP = {};
(j.rows||[]).forEach(r=>{ const pm=r.perp_mult;
  if(pm!=null && pm!=="None" && pm!==""){ const v=parseFloat(pm);
    if(!isNaN(v)) PERP[(r.symbol||"").toUpperCase()]=v; } });
```

**`j.rows` is every row in `signals.json` — all 48 nights.** There is no date filter and
no envelope check. Last write in file order wins, and `signals.csv` is append-by-date,
so a symbol keeps whatever multiplier it carried on the last night it was in the
top fifty — for as long as it stays out.

Measured on tonight's board:

| provenance of the FUNDING multiplier the page applies | rows |
|---|---|
| from tonight (2026-09-17) | 50 |
| **from an earlier night** | **91** |
| no ledger row at all → neutral `1.0` | 93 |

`signals.csv` persists `rows[:50]` sorted by conviction (`AUDIT-2026-09` §1.0), so 184
of the 234 scored rows can never be refreshed by construction. The stale entries span
**42 distinct nights**, back to 2026-08-02.

### HBAR

`ledger/signals.csv` carries four rows dated **2026-08-03** whose tail columns are
misaligned — `survived` holds `20` where it should hold a boolean, and `perp_mult` holds
a number that is not a multiplier:

| date | symbol | perp_mult | spec_hash | funding_rate | funding_apr | oi_usd |
|---|---|---|---|---|---|---|
| 2026-08-03 | BEAT | 15.1 | *(empty)* | — | — | — |
| 2026-08-03 | **HBAR** | **17.4** | *(empty)* | — | — | — |
| 2026-08-03 | MON | 12.0 | *(empty)* | — | — | — |
| 2026-08-03 | UAI | 10.4 | *(empty)* | — | — | — |

They are the only four rows on that night carrying Dune columns, they carry no
`spec_hash`, and they predate this repository's first commit — the seeded portion of the
ledger — so git cannot say which writer produced them. `funding.regime_modifier` cannot
return any of these values: its envelope is `[0.85, 1.15]` and always has been.

HBAR has not been in the top fifty since. So the page has been applying `×17.4` to HBAR
every night for forty-five nights. Tonight's chain, reproduced exactly:

| step | multiplier | running |
|---|---|---|
| START | — | 100.0 |
| DEPTH (log10 mcap $3.3B) | ×0.880 | 88.0 |
| CONFIRM (rs_blend −8.5) | ×0.398 | 35.0 |
| LIQUIDITY (turnover 1.8%, below the 0.90 depth bypass) | ×0.400 | 14.0 |
| SUPPLY (FDV/MC 1.14×) | ×0.9965 | 14.0 |
| **FUNDING (stale 2026-08-03 row)** | **×17.400** | **242.9 → clamped to 100** |

Pre-funding HBAR scores 14/100, the 130th row on the board. The published board puts it
first, tied at the ceiling with ZEC, with a 142.9-point overshoot that the clamp hides.
This is exactly the chain in the brief, to the third decimal on every factor.

**Why `AUDIT-2026-09` §1.1 concluded it "did not reproduce".** That harness scored HBAR
through `nightly.score()` with the nightly's own `perps_map`, which returns `1.0` for
HBAR because there is no live HBAR perp feed — and got 14, correctly. The published
board does not use that path. `nightly.score()` and `index.html`'s `build()` agree on
everything except where the funding multiplier comes from, and no test, gate or parity
check covers that one input: `tests/test_parity.py` executes the MODEL PORT block under
node and compares it to `nightly.score()`, but `PERP` is loaded by `loadLedger()`, which
is *outside* the port. The parity gate has been green throughout.

### It is not only HBAR

Nine further rows are served a multiplier of `1.150` — the exact boost ceiling — from
nights in early August, against a modelled value of `1.000` tonight: ONDO, OP, ETC, CRO,
BONK, SNX, WLFI, STABLE, and six more differ by smaller amounts. Re-scoring the board
with FUNDING taken from tonight's cross-section instead:

| sym | rank now | rank on tonight's funding | applied | modelled |
|---|---|---|---|---|
| HBAR | **1** | **130** | 17.400 | 1.000 |
| ONDO | 29 | 39 | 1.150 | 1.000 |
| OP | 53 | 59 | 1.150 | 1.000 |
| ETC | 100 | 114 | 1.150 | 1.000 |
| CRO | 102 | 117 | 1.150 | 1.000 |
| BONK | 144 | 179 | 1.150 | 1.000 |

11 rows move five places or more, and the board goes from two clamped rows to one.

### The Hyperliquid observation in the brief

Confirmed as consistent, and it points at the same thing from the other side.
Hyperliquid showing HBAR at baseline funding with no premium is not a stale *venue* —
it is the correct reading of a market with nothing in it. The `×17.4` did not come from
a venue at all. Separately, tonight's genuine funding readings are thin in a way that
matters for Phase 2: across the top fifty, open interest runs from $103K to $2.46B with
a **median of $2.7M**, the cross-venue APR spread has a **median of 31 percentage
points** and a p95 of **169pp**, and eight of the 44 rows with a reading have it from a
single venue. `VENUE_PRIORITY` picks one of those, and nothing checks the others agree.

## 4 — Empirical distributions

Live `/coins/markets`, 2026-09-17, 234 rows after the stablecoin filter.

### Multipliers

| factor | min | p1 | p5 | p25 | median | p75 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|
| DEPTH | 0.5160 | 0.5184 | 0.5250 | 0.5629 | 0.6309 | 0.7640 | 0.9906 | 1.0000 | 1.0000 |
| CONFIRM | 0.1769 | 0.1911 | 0.3018 | 0.4258 | 0.4804 | 0.6794 | 0.9995 | 1.0000 | 1.0000 |
| LIQUIDITY | 0.4000 | 0.4000 | 0.4000 | 0.4000 | 0.4256 | 0.6209 | 1.0000 | 1.0000 | 1.0000 |
| SUPPLY | 0.9037 | 0.9073 | 0.9151 | 0.9663 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| FUNDING *(as the board applies it)* | 0.9160 | 0.9400 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0320 | 1.1500 | **17.4000** |
| FUNDING *(as tonight models it, 235 rows)* | 0.9240 | — | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | — | 1.1330 |

Structure worth stating before any bound is proposed:

- **LIQUIDITY is very nearly binary.** 106 of 234 rows (45.3%) sit at exactly the `0.40`
  floor. 20 rows sit at exactly `1.00` and **every one of them got there through the
  `DEPTH ≥ 0.90` bypass — not one row in the universe reached `1.0` on turnover.** The
  `liquidityFit` curve peaks at 30-60% daily turnover; the universe's median turnover is
  **3.1%** and its p95 is 25%. Only five rows are in the sweet spot at all. A curve
  tuned for a range the population does not occupy is a floor and a bypass wearing a
  curve's clothes.
- **SUPPLY is inert for 135 of 234 rows** (58%) — `FDV/MC ≤ 1.10`, or FDV unpublished
  (1 row). Where it bites, it bites weakly: p5 is 0.9151 against a hard floor of 0.90.
- **CONFIRM saturates**, as `AUDIT-2026-09` §1.1 established. 37 rows are at ≥ 0.9099;
  `rs_blend` spans −29.6 to **+2,445** and everything above ~65 maps to the same score.
- **FUNDING has essentially no cross-sectional dispersion.** On tonight's modelled
  values, 226 of 235 rows are at exactly `1.000`. p5 through p95 are all `1.0000`.

### Score, pre- and post-clamp

| series | min | p1 | p5 | p25 | median | p75 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|
| pre-clamp raw | 4.12 | 5.06 | 7.69 | 11.60 | 15.07 | 25.68 | 62.81 | 88.40 | **242.91** |
| published | 4 | 5 | 8 | 12 | 15 | 26 | 62.7 | 88.3 | 100 |

**Clamp incidence: 2 of 234 rows (0.9%)** tonight — ZEC at 107.1 (+7.1) and HBAR at
242.9 (+142.9). Zero rows clamp at the low end. On the recorded ledgers the rate looks
lower still, and is not comparable: `signals.csv` shows exactly one 100 a night for the
last 25 nights (ZEC), and `ledger/xsec/` one a night for its three, because neither file
records a board-applied FUNDING multiplier. **The clamp binds roughly twice as often on
the published board as anything on disk shows.** That gap is what §7 closes.

### Factor dominance

Share of the chain's total `|log|` held by the largest single factor:

| series | min | p1 | p5 | p25 | median | p75 | p95 | p99 | max |
|---|---|---|---|---|---|---|---|---|---|
| largest factor's share | 0.340 | 0.358 | 0.385 | 0.425 | 0.465 | 0.553 | 0.954 | 1.000 | 1.000 |
| that factor's `\|log\|` | 0.069 | 0.096 | 0.428 | 0.734 | 0.916 | 0.916 | 1.203 | 1.677 | 2.856 |

Which factor dominates, across the universe:
`LIQUIDITY 122 · CONFIRM 88 · DEPTH 21 · FUNDING 2 · SUPPLY 1`.

**Share alone is the wrong statistic and Phase 2 should not use it.** ZEC carries four
factors at exactly `1.000` and one at `1.071`: its largest factor holds **100%** of the
chain's total log, which is a perfect dominance score for the most nearly neutral chain
on the board. The magnitude has to be paired with the share. Jointly:

| threshold | rows | % |
|---|---|---|
| share > 0.50 and `\|log\|` > 0.10 | 86 | 36.8% |
| share > 0.55 and `\|log\|` > 0.15 | 55 | 23.5% |
| share > 0.60 and `\|log\|` > 0.20 | 39 | 16.7% |
| share > 0.70 and `\|log\|` > 0.25 | 23 | 9.8% |

Read by band, the dominance is not evenly distributed and its source changes:

| band | n | med DEPTH | med CONFIRM | med LIQ | med SUPPLY | med FUND | med share |
|---|---|---|---|---|---|---|---|
| 80-100 | 5 | 0.919 | 0.992 | 1.000 | 0.9965 | 1.000 | 0.719 |
| 70-79 | 2 | 0.994 | 0.770 | 1.000 | 0.9670 | 1.000 | 0.850 |
| 55-69 | 9 | 0.953 | 0.639 | 1.000 | 1.0000 | 1.000 | 0.865 |
| 40-54 | 12 | 0.819 | 0.726 | 0.945 | 1.0000 | 1.000 | 0.814 |
| 0-39 | 206 | 0.614 | 0.456 | 0.400 | 1.0000 | 1.000 | 0.456 |

The top of the board is where a single factor is most often doing all the work, and for
the 206 rows below 40 the answer is nearly always the same one: LIQUIDITY at its floor.

## 5 — What can be reconstructed, and how far back

| factor | reconstructible? | source | depth | cost |
|---|---|---|---|---|
| DEPTH | **yes** | `/coins/{id}/market_chart?days=365&interval=daily` → `market_caps` | ~365 d, or listing date | 1 request per coin |
| LIQUIDITY | **yes** | same call → `total_volumes ÷ market_caps` | ~365 d | shared with DEPTH |
| CONFIRM | **yes** | same call → `prices`, differenced at 7/14/30/200 d against BTC's series | ~165 d usable (200 d of prior history is needed for the `rs200` leg) | shared with DEPTH |
| SUPPLY | **no** | FDV = price × total supply. CoinGecko publishes no supply history and no FDV history on any tier | — | — |
| FUNDING — as modelled | **partial** | Binance `/fapi/v1/fundingRate` and Bybit `/v5/market/funding/history` are public and keyless; `rsi7` and `price_chg_24h` follow from the price series | venue-dependent, months | 1-2 requests per coin per venue |
| FUNDING — **as the board applies it** | **yes, exactly, and it is not market data** | `board_perp_map()` over `signals.csv` rows dated ≤ the target date | full ledger, 2026-08-01 | free, no network |

Two things follow.

**A full-chain backfill is not available at any price.** SUPPLY cannot be reconstructed
from any free source, and the applied FUNDING multiplier is a function of this
repository's own write history rather than of the market. A backfilled `conviction_raw`
would therefore be a *different model's* score wearing this model's name — which is
precisely the pooling `ledger/xsec/`'s `src` column exists to prevent.

**And the three that can be reconstructed do not reconstruct exactly.** Measured,
rather than assumed. `/coins/hedera-hashgraph/market_chart?days=365&interval=daily`
returns 366 daily points; taking each date's 00:00 UTC point and re-deriving HBAR's
chain against the three nights `ledger/xsec/` recorded live:

| date | market cap recorded | reconstructed | error | DEPTH rec / live | LIQUIDITY rec / live |
|---|---|---|---|---|---|
| 2026-09-15 | $3.3726B | $3.4082B | **+1.06%** | 0.8831 / 0.8800 | 0.4000 / 0.4000 |
| 2026-09-16 | $3.2598B | $3.2542B | −0.17% | 0.8781 / 0.8800 | **0.4062 / 0.4000** |
| 2026-09-17 | $3.2469B | $3.2383B | −0.26% | 0.8776 / 0.8800 | 0.4000 / 0.4000 |

The nightly runs at a wall-clock hour that is not midnight UTC, and `market_chart`'s
daily points are. The residual is **0.003 on DEPTH and 0.006 on LIQUIDITY** — enough, on
2026-09-16, to lift a row off the `0.40` floor that was on it. That is the *same order*
as the night-to-night movement of the percentiles a Phase 2 bound would be fitted to
(§8): a backfilled row would carry noise comparable to the signal being measured.

**The rate limit is the other constraint.** The keyless tier returned HTTP 429 on the
first `market_chart` call of this audit and 200 on the retry. At that throughput a
234-coin backfill is 20-40 minutes of wall clock with retries, per run, and it cannot be
made incremental — `market_chart` returns the whole window every time.

## 6 — The IC the board already ignores

`market_breadth.json` → `edge`, unchanged by this audit and restated for the Phase 3
record. It is measured on the **LEGACY selection history** — `ledger/signals.csv`, the
persisted top fifty *by conviction*, which is a truncation of the board and not the board:
**43 legs, mean IC −0.0562, 95% CI [−0.1065, −0.0060], t = −2.195, `measurable: true`**,
16 of 43 legs positive, mean quintile spread −119.3bp. Verdict as written by
`_compute_edge()`: *"Conviction orders the universe backwards — the ranking is
inverted."*

Three properties of that number Phase 3 has to carry:

1. **It is one horizon.** `_edge_legs` pairs consecutive recorded dates. There is no 7d
   or 30d IC anywhere in the pipeline, and no per-factor IC at all.
2. **It is measured on the top ~21% of the board**, by the variable being measured
   (`AUDIT-2026-09` §1.0). `ledger/xsec/` was built to fix this and holds three nights.
3. **It is measured on `nightly.score()`'s output, not on the published board.** Given
   §3, those are different rankings on 16 of 234 rows tonight, including the #1.

The label gate (`labelGateState`, `index.html:2180`) already reads this and has
correctly withdrawn the action vocabulary — the board says `LABELS: DESCRIPTIVE`. What
it does *not* do is gate publication or reorder anything, which is Phase 3's decision.

## 7 — Phase 1B: what is now being recorded

`ledger/xsec/` schema **v1 → v2**, append-only: the first 25 columns are byte-identical
in name and order, so v1 readers still parse a v2 shard. 19 columns added.

| group | columns | why |
|---|---|---|
| the chain, exactly | `total_volume`, `depth`, `confirm`, `liquidity` | v1 recorded `c_depth = depth×20`, `c_momentum = cm×20`, `c_liquidity = a_frac×30`, each rounded to 0.1 — that is 0.005 of a multiplier, which is coarser than the gap between adjacent published scores and far coarser than a percentile bound needs |
| the clamp | `conviction_raw`, `clamped` | the pre-clamp product existed nowhere on disk; "how often does the clamp bind and by how much" was not a query |
| dominance | `dom_factor`, `dom_share`, `dom_logabs` | both share and magnitude, for the reason in §4 |
| **applied vs modelled funding** | `perp_mult_board`, `perp_mult_board_date` | `board_perp_map()` reproduces `loadLedger()`'s rule literally, bug included, so the §3 divergence becomes a recorded column instead of an argument |
| funding inputs, cross-sectionally | `price_chg_24h`, `perp_path`, `funding_venue`, `funding_venues_n`, `funding_apr_spread`, `funding_interval_h`, `funding_regime`, `oi_usd` | all but `price_chg_24h` and `perp_path` already existed in `signals.csv` — for the top fifty rows only. Phase 2's liquidity-floor question needs them over the population |

**The chain is reconciled, not asserted.** `factor_chain()` re-derives the five
multipliers; `factor_chain_reconciles()` checks them against `score()`'s own `comp` on
every row, every night — the three display components to the decimal they publish at,
plus `emission_mult`, `perp_mult`, and the clamped integer. A row that fails is written
with its v2 columns **empty** and the disagreement printed to stderr. It reconciles on
all 235 of tonight's rows. `factor_chain` is deliberately **not** in `SPEC_FUNCTIONS`,
and `test_the_writer_is_not_part_of_the_specification` asserts it is unreachable from
any captured function. Phase 1B landed under `SPEC_HASH = 1a4ea6e4d77e` and moved no
digest of its own; **Phase 1.5, below, moved it to `8e750228e15a`.**

Columns added at v2 are **empty** on the three nights recorded under v1. Several are
reconstructible; they are left blank deliberately, because those rows carry `src="live"`,
which this ledger defines as *observed on the night it is dated*.

## 8 — Recommendation on calibrating the Phase 2 bounds

**Split it by what each bound is a bound on.**

**Calibrate now, from the wide ledger: DEPTH, LIQUIDITY, SUPPLY.** These are slow
functions of market cap, turnover and FDV, and their cross-sectional percentiles are
already stable. Across the three recorded nights:

| factor | p5 spread across 3 nights | p25 | p50 | p75 |
|---|---|---|---|---|
| DEPTH | 0.005 | 0.003 | 0.005 | 0.008 |
| LIQUIDITY | 0.000 | 0.000 | 0.003 | 0.044 |
| SUPPLY | 0.000 | 0.001 | 0.000 | 0.000 |
| CONFIRM | **0.045** | **0.050** | **0.030** | **0.046** |

A cross-sectional bound needs cross-sectional *width*, not time depth, and 3 × 234 = 702
rows is ample for a percentile on a distribution that moves by 0.005 a night.

**Wait, for CONFIRM.** It moves ~0.05 at the quartiles night to night, which is the same
order as the gap between candidate bounds. Two more weeks makes that decidable.

**Wait, necessarily, for FUNDING.** It cannot be calibrated cross-sectionally at any
sample size: 226 of 235 rows sit at exactly 1.000 and every percentile from p5 to p95 is
1.0000. There is no cross-section to take a percentile of. The brief's own instruction —
"redefine short capitulation as a percentile of each token's own funding history" — is
the right shape and needs **time series per symbol**, which now starts accumulating
across the whole universe rather than the top fifty. `funding_apr` is present on 153 of
235 rows and `rsi7` on 80; the squeeze boost cannot fire for the other 155 under any
funding reading, which is itself a Phase 2 finding.

**Do not backfill.** For the reasons in §5, and now with a measurement behind them: two
of five factors cannot be reconstructed at all, the other three come back with a 0.003-
to-0.006 residual on the multiplier — the same order as the quantity a bound is being
fitted to — the rate limit makes the fetch expensive and fragile, and a partial chain
recorded beside a complete one invites exactly the pooling `src` was added to prevent.
A backfill is worth revisiting if the reconstruction can be anchored to the nightly's
own run hour rather than to 00:00 UTC; nothing in the free tier offers that today. The one
backfill worth doing costs no network and is exact — `perp_mult_board` over the recorded
ledger — and even that is being withheld from the three v1 nights so the `live`/`backfill`
distinction stays enforceable the first time it matters.

**Anything requiring forward returns is live-forward only.** The per-horizon IC, the
per-factor IC, the funding liquidity floor, and whether dominance predicts anything all
need the outcome side, and nothing reconstructs that.

---

# Phase 1.5 — funding overlay integrity, 2026-09-17

Accepted from §3 and shipped as its own boundary, ahead of Phase 2 calibration. Scope
was deliberately narrow: this is the removal of a stale/corrupted-data path, not factor
calibration. No factor curve, threshold or bound was touched.

**Specification boundary `1a4ea6e4d77e` → `8e750228e15a`.** (Superseded the same day by
Phase 1.6 → `ab16684ad5c1`; §1.5.5 names the gap 1.6 closes.)

## 1.5.1 — The rule

`perpOverlay(rows, asOf)` and its three helpers now live inside `index.html`'s ported
scoring block and are captured in `SPEC_FUNCTIONS`, mirrored verbatim in `nightly.py`.
Two independent defences:

| defence | rule | what it stops |
|---|---|---|
| **date** | only rows carrying the snapshot's own date are read at all | a symbol outside the persisted fifty keeping its last-seen multiplier |
| **envelope** | a value outside `[0.85, 1.15]` is refused, not consumed | a value `funding.regime_modifier` cannot produce reaching a score, whatever its date |

`tests/test_perp_overlay.py` proves **each defence stops HBAR's 17.4 with the other
disabled** — one for the date filter with the envelope widened to admit it, one for the
envelope with the row re-dated to the current snapshot. A single defence is a single
point of failure, and the value in question already got past everything once.

Beyond those two: the whole overlay is withheld if the newest snapshot is more than
`PERP_MAX_AGE_DAYS = 1` old, or is dated ahead of the caller. Parsing is a shared regex
rather than `parseFloat`/`float()`, which disagree on `"1.07 garbage"` — the exact class
of malformed cell this function exists to refuse.

Three states are recorded and shown, because a bare `×1.000` cannot tell them apart:
`current` (a reading from this snapshot), `absent` (recorded with no reading), `rejected`
(refused). A symbol with no row at all reads `no-row`.

## 1.5.2 — The gate that missed it, closed

The old rule lived in `loadLedger()`, outside the markers `tests/test_parity.py`
extracts. The gate was green for the entire six weeks the board was wrong. It now runs
**two** node drivers — one for the scoring chain, one for overlay selection — and
asserts the two implementations return the same map, the same states, the same refusals
and the same snapshot date over ten synthetic cases covering every branch, plus every
night of the real `signals.json` replayed through both. `check_the_gate_reads_the_real_terminal`
now also requires the four overlay functions to be inside the markers, so they cannot
drift back out.

## 1.5.3 — Board reconciliation, 2026-09-17

Both boards computed by executing the real ported block under node, one with the retired
rule and one with the shipped one, against the same live `/coins/markets` payload.

Overlay coverage: **170 symbols → 50**, all `current`, zero rejected on this snapshot.
The other 184 rows are neutral by absence — which was already the case for 93 of them.

| | before | after |
|---|---|---|
| rows whose score changed | — | **11** |
| rows whose tier changed | — | **1** (HBAR, STRONG → AVOID) |
| rows that re-ranked on displacement alone | — | 167 (median displacement 1 place) |
| rank moves ≥ 5 places | — | 10 |
| clamped rows | 2 | **1** |
| largest pre-clamp overshoot | **+142.9** | **+7.1** |
| top ten | ZEC HBAR UNI HYPE NEAR WBT XMR DASH PONS ETH | ZEC UNI HYPE NEAR WBT XMR DASH PONS ETH **SOL** |

The eleven rows whose score moved, and why:

| sym | rank | score | tier | FUNDING before → after |
|---|---|---|---|---|
| **HBAR** | 2 → **121** | 100 → **14** | STRONG → **AVOID** | 17.400 → 1.000 *(2026-08-03 row, out of envelope)* |
| ONDO | 29 → 38 | 39 → 34 | AVOID | 1.150 → 1.000 *(2026-08-08)* |
| OP | 53 → 58 | 28 → 25 | AVOID | 1.150 → 1.000 *(2026-08-02)* |
| CRO | 99 → 110 | 17 → 15 | AVOID | 1.150 → 1.000 *(2026-08-02)* |
| ETC | 102 → 115 | 17 → 15 | AVOID | 1.150 → 1.000 *(2026-08-02)* |
| BONK | 149 → 184 | 13 → 11 | AVOID | 1.150 → 1.000 *(2026-08-02)* |
| WLFI | 189 → 204 | 10 → 9 | AVOID | 1.150 → 1.000 *(2026-08-02)* |
| SNX | 215 → 224 | 9 → 8 | AVOID | 1.150 → 1.000 *(2026-08-04)* |
| FF | 75 → 68 | 22 → 23 | AVOID | 0.942 → 1.000 *(2026-09-16)* |
| PIEVERSE | 82 → 69 | 21 → 23 | AVOID | 0.916 → 1.000 *(2026-09-08)* |
| H | 226 → 223 | 7 → 8 | AVOID | 0.966 → 1.000 *(2026-08-18)* |

**HBAR**: rank 2 → 121, score 100 → 14, pre-clamp **242.9 → 14.0**, STRONG → AVOID.
**ZEC**: unmoved — rank 1, score 100, pre-clamp 107.1, FUNDING 1.071. Its row is from
this snapshot and inside the envelope, so both rules agree on it. That is the control:
the fix removes a stale value and leaves a current one exactly where it was.

Note what the fix does **not** remove: ZEC still clamps, at +7.1. That is CONFIRM
saturation (`AUDIT-2026-09` §1.1), a separate defect on a separate boundary, and it is
Phase 2's.

## 1.5.4 — What did not change

`nightly.score()` never read the ledger for funding — it reads the live venue feed — so
**no recorded score, no basket weight, no leg and no information coefficient moves.**
The 43-leg IC (LEGACY sample — `ledger/signals.csv`) stands at −0.0562, CI [−0.1065, −0.0060] — measured on the LEGACY selection history (`ledger/signals.csv`, the persisted top fifty by conviction), which is a different sample from the wide cross-section this section is otherwise about. `_spec_breaks()` detects
boundaries from recorded score movement, not from the digest, so the edge series is not
re-segmented either.

The digest does segment: rows written from tonight carry `8e750228e15a`. Prior rows keep
what they carried. **No `SPEC_EQUIVALENT` entry was added, deliberately** — both existing
entries are corrections to the ruler (same arithmetic, different digest), and this one
changed which input arrives and moved eleven published scores. Folding it in would claim
the board said the same thing either side of it.

The four malformed 2026-08-03 rows are **left exactly as recorded**, misalignment and
all, and a test asserts they still are. They are the record of what happened, and they
are what every number in §3 was reconstructed from.

## 1.5.5 — The cost, stated

Coverage of the funding overlay drops from 170 symbols to 50, because the ledger
persists fifty rows a night and the terminal reads only the ledger. 184 of 234 board
rows are now scored at a neutral funding multiplier. That is the honest reading of what
the published artifact contains — but it is a real loss of information relative to what
the nightly computed, which is a live cross-venue reading for 153 of 235 symbols.

**Closed by Phase 1.6, below.**

---

# Phase 1.6 — funding transport parity, 2026-09-17

Data plumbing. No factor curve, threshold or bound was touched, and no calibration was
attempted. **Specification boundary `8e750228e15a` → `ab16684ad5c1`.**

## 1.6.1 — The artifact

`ledger/perp.json`, written by the nightly from the same row loop that feeds `score()`.
One row per scored symbol for the current snapshot: **235 rows, 13.4 KB**.

```
{"schema_version":1, "as_of":"2026-09-17", "generated_at":"...", "spec_hash":"...",
 "source":"nightly", "universe":235, "with_reading":153,
 "envelope":[0.85,1.15], "max_age_days":1, "row_keys":{...},
 "rows":{"ZEC":{"m":1.071,"s":"current","apr":-25.185,"v":"gateio","n":5,
                "sp":33.8855,"ih":8.0,"rg":"SHORT_SQUEEZE_RISK","rsi":74.96},
         "PONS":{"m":1.0,"s":"absent"}, ...}}
```

Short keys, with `row_keys` documenting every one of them inside the file. Deliberately
**not** `ledger/funding.json`: that is the rich artifact — eight venues nested per asset,
the carry screen, 70 KB, fifty rows. This is the transport and stays slim, because a
transport that can grow is a transport that will. Neither is derived from the other;
both are projections of the same row loop. `tests/test_perp_transport.py` fails the build
above 64 KB and if `by_venue` ever appears in it.

**Quote age, honestly.** The consolidation layer publishes no per-quote timestamp — no
venue returns an "as of", and Binance's `nextFundingTime` is a next-settlement stamp
from which the age of the reading before it cannot be recovered. The artifact therefore
carries `generated_at` (the age of the *snapshot*) and `ih` per row, the settlement
clock, which is the nearest honest proxy for how often a rate refreshes. Nothing claims
a per-quote age that was not measured.

## 1.6.2 — Authority: availability is the writer's, validity is the consumer's

The single property that makes this a transport and not a second model.

- `s: "absent"` is the writer asserting **no funding reading existed** for that symbol.
  Only the writer can know that, so the consumer accepts it — a ×1.000 that means "the
  market is flat" and one that means "there is no market" are different facts.
- **Validity is never the artifact's to assert.** Every value goes through `perpEntry`
  at the point of use, every time. `tests/test_perp_transport.py` feeds HBAR's 17.4 back
  in through the new path with the writer vouching for it four ways — `s:"current"`,
  `s:"absent"`, `s:"verified"`, no `s` at all — and a fifth with the artifact declaring
  its own wider `envelope`. All five are refused.

**No fallback.** A missing, undated or stale artifact withholds the overlay *whole*; the
page does not revert to the `signals.json` path. A fallback would reintroduce the
divergence this phase removes, silently, on exactly the nights something is already
wrong. `test_the_page_has_no_path_back_to_the_signals_overlay` asserts it on the source,
because a fallback is the kind of thing someone adds back later.

## 1.6.3 — Capture, and what left it

`perp_entry` (the envelope rule, now the single definition shared by both paths) and
`perp_feed` (the transport) join `SPEC_FUNCTIONS`. **Three functions left it** —
`ledger_latest_date`, `overlay_as_of`, `perp_overlay` — with the `signals.json` path
they served. They reach no published score any more, and capturing dead code means an
edit to dead code re-segments the track record: the mirror image of the hole
`AUDIT-2026-09` §1.8 closed, and it costs just as much. All are retained, uncaptured, so
the regressions can run each generation of the rule against the one before it:
`board_perp_map` (pre-1.5) → `perp_overlay` (1.5) → `perp_feed` (1.6).

The parity gate runs **nine** checks now, including a third node driver over the
transport and a coverage regression against the artifact actually on disk.

## 1.6.4 — Coverage and board reconciliation

Three boards, all computed by executing the real ported block under node against the
same live payload.

| | pre-1.5 | post-1.5 | **post-1.6** |
|---|---|---|---|
| symbols in the overlay | 170 *(42 nights)* | 50 | **235** *(one snapshot)* |
| board rows with a real reading | 50 current + 120 stale | 50 | **153** |
| board rows neutral-by-absence | 93 | 184 | **81** |
| non-neutral multipliers applied | 15 *(10 of them stale)* | 5 | **9** |
| values refused | 0 — the 17.4 was applied | 0 | **0** |

The nine now applied: `BTW 0.939 · ETHFI 1.039 · FF 0.977 · PONS 1.075 · RAY 1.133 ·
STX 1.015 · XPL 1.109 · ZEC 1.071 · ZEN 0.924`.

**Phase 1.6 in isolation** — the shipped post-1.5 board against the transport:

| | |
|---|---|
| scores changed | **2** — XPL 18 → 20 *(FUND 1.000 → 1.109)*, ETHFI 21 → 22 *(1.000 → 1.039)* |
| tiers changed | **0** |
| rank moves ≥ 5 | 2 (max 10) |
| rows that re-ranked at all | 19 |
| clamped rows | 1 → **1** |
| largest pre-clamp | 107.1 → **107.1** |
| top ten | unchanged |

That is the shape a transport fix should have: the information set widens, a handful of
names that genuinely had a reading get it, and nothing at the top of the board moves —
because the names at the top were in the persisted fifty and already had theirs.

**Cumulative across 1.5 + 1.6**, against the board as it stood this morning: 13 scores
changed, 1 tier (HBAR STRONG → AVOID, rank 2 → 121), clamped 2 → 1, largest pre-clamp
242.9 → 107.1, top ten loses HBAR and gains SOL.

## 1.6.5 — Remaining browser/nightly mismatches

**Multiplier mismatches on the 232 symbols the two share: zero.**

The residual is membership, not value. The board fetches the live top-250 and the
artifact is last night's snapshot, so the two universes drift intraday:

- 2 board rows are absent from the artifact (`MARSCOIN`, `SENT`) — too new. They read
  `no-row` → neutral 1.000, with the reason on the panel.
- 3 artifact rows are absent from the board (`U`, `JPYC`, `PC0000023`) — they left the
  top 250. Harmless; nothing looks them up.

This is inherent to a nightly snapshot feeding a live board and cannot be closed by a
transport. It is bounded (single digits), visible per row, and fails to neutral. The
nightly prints the count of rows where `perp_mult` and `perp_mult_board` disagree on
every run, so a regression in the transport is loud rather than quiet.

## 1.6.6 — What did not change

`nightly.score()` is untouched — it reads the live venue feed and always did. No
recorded score, basket weight, leg or information coefficient moves; the 43-leg IC (LEGACY sample — `ledger/signals.csv`)
stands at −0.0562, CI [−0.1065, −0.0060]. Prior history keeps its digest. **No
`SPEC_EQUIVALENT` entry**, for the same reason as 1.5: published scores moved, so this
is a re-valuation and not a correction to the ruler.

---

## 9 — What this audit does not claim

- That any proposed Phase 2 bound will improve the information coefficient. Nothing here
  was tested against forward returns.
- That the four misaligned 2026-08-03 rows are the only ledger corruption. They are the
  only rows whose `perp_mult` is outside `[0.85, 1.15]`; other columns were not swept.
- That correcting the funding overlay improved the IC. It cannot have: the measured IC
  is computed on `nightly.score()`'s output, which never consumed the stale overlay.
  What Phase 1.5 fixed is the gap between the published board and the measured model,
  not the model. Whether the board's ranking is any good remains what §6 says it is.
