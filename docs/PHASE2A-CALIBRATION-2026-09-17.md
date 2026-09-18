# Phase 2A — mature-factor calibration evidence, 2026-09-17

**Analysis only. No scoring logic, no bound, no constant and no published score was
changed by this document or the commit that carries it.** `SPEC_HASH` is unchanged at
`ab16684ad5c1`.

Scope: DEPTH, LIQUIDITY, SUPPLY, and the dominance statistic. CONFIRM bounds, FUNDING
bounds, the short-capitulation percentile, the liquidity floor on funding, the revenue
factor and the IC publication gate are all out of scope and untouched.

Every number below is reproduced by `docs/phase2a_calibration.py`, which is committed
beside this file and runs read-only against `ledger/xsec/` and `ledger/signals.csv`.

---

## 0 — The sample, and why it can be trusted

`ledger/xsec/` — **3 nights, 2026-09-15 … 2026-09-17, 703 rows, 234±1 per night.** The
whole scored cross-section, not a truncation. This is the ledger Phase 1B started
accumulating; `signals.csv` is not used for any distribution here, because it keeps the
top fifty *by conviction* and every factor below is an input to conviction.

Multipliers are **recomputed from the recorded inputs** (`market_cap`, `turnover_pct`,
`fdv_usd`) rather than recovered from the display columns. The display columns are ×20
and ×30 scales rounded to one decimal — 0.005 and 0.0033 of a multiplier, coarser than
the percentiles being fitted. All **2,109 recomputations reconcile** against the recorded
`c_depth` / `c_liquidity` / `emission_mult` to within one rounding step.

One precision caveat, stated rather than buried: `turnover_pct` is itself rounded to 0.01
of a percent, which at the observed turnovers is about **0.0002** of a LIQUIDITY
multiplier. Phase 1B's v2 columns carry the multipliers unrounded, but no night has yet
been written by the v2 writer, so this is the best precision the recorded history
currently offers.

---

## 1 — Distributions, pooled and by night

**DEPTH**

| series | min | p1 | p5 | p25 | med | p75 | p95 | p99 | max | n |
|---|---|---|---|---|---|---|---|---|---|---|
| pooled | 0.5102 | 0.5138 | 0.5209 | 0.5594 | 0.6275 | 0.7591 | 0.9928 | 1.0000 | 1.0000 | 703 |
| 2026-09-15 | 0.5102 | 0.5120 | 0.5183 | 0.5593 | 0.6277 | 0.7563 | 0.9929 | 1.0000 | 1.0000 | 234 |
| 2026-09-16 | 0.5128 | 0.5140 | 0.5187 | 0.5591 | 0.6268 | 0.7585 | 0.9922 | 1.0000 | 1.0000 | 234 |
| 2026-09-17 | 0.5147 | 0.5162 | 0.5240 | 0.5618 | 0.6281 | 0.7631 | 0.9918 | 1.0000 | 1.0000 | 235 |

Largest between-night gap at any reported percentile: **0.0068**.

**LIQUIDITY**

| series | min | p1 | p5 | p25 | med | p75 | p95 | p99 | max | n |
|---|---|---|---|---|---|---|---|---|---|---|
| pooled | 0.4000 | 0.4000 | 0.4000 | 0.4000 | 0.4187 | 0.6112 | 1.0000 | 1.0000 | 1.0000 | 703 |
| 2026-09-15 | 0.4000 | 0.4000 | 0.4000 | 0.4000 | 0.4182 | 0.5925 | 1.0000 | 1.0000 | 1.0000 | 234 |
| 2026-09-16 | 0.4000 | 0.4000 | 0.4000 | 0.4000 | 0.4214 | 0.6316 | 1.0000 | 1.0000 | 1.0000 | 234 |
| 2026-09-17 | 0.4000 | 0.4000 | 0.4000 | 0.4000 | 0.4207 | 0.6366 | 1.0000 | 1.0000 | 1.0000 | 235 |

Largest between-night gap at any reported percentile: **0.0441** (at p75 — the loosest of
the three, and still small).

**SUPPLY**

| series | min | p1 | p5 | p25 | med | p75 | p95 | p99 | max | n |
|---|---|---|---|---|---|---|---|---|---|---|
| pooled | 0.9037 | 0.9059 | 0.9151 | 0.9680 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 703 |
| 2026-09-15 | 0.9037 | 0.9073 | 0.9151 | 0.9689 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 234 |
| 2026-09-16 | 0.9037 | 0.9073 | 0.9151 | 0.9681 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 234 |
| 2026-09-17 | 0.9037 | 0.9073 | 0.9151 | 0.9679 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 235 |

Largest between-night gap at any reported percentile: **0.0009**.

### Where each factor piles up

| factor | at its floor | at exactly 1.000 | strictly inside | floor |
|---|---|---|---|---|
| DEPTH | 0 (0.0%) | 30 (4.3%) | 673 | 0.0 |
| LIQUIDITY | **310 (44.1%)** | 58 (8.3%) | 335 | 0.40 |
| SUPPLY | 0 (0.0%) | **411 (58.5%)** | 292 | 0.90 |

### The quantity a bound is actually for

How far one factor can move the product, in nats of log — observed, and by construction.

| factor | observed range | max \|log\| observed | constructional range | max \|log\| possible |
|---|---|---|---|---|
| DEPTH | 0.5102 – 1.0000 | 0.673 | 0.0 – 1.0 | ∞ *(unreachable above $1M mcap)* |
| CONFIRM *(out of scope)* | 0.1850 – 1.0000 | **1.687** | 0.10 – 1.00 | 2.303 |
| LIQUIDITY | 0.4000 – 1.0000 | **0.916** | 0.40 – 1.00 | 0.916 |
| SUPPLY | 0.9037 – 1.0000 | 0.101 | 0.90 – 1.00 | 0.105 |
| FUNDING *(out of scope)* | 0.8620 – 1.1430 | 0.149 | 0.85 – 1.15 | 0.163 |

This table is the spine of everything below. The factor with the most influence is
CONFIRM, which Phase 2A cannot touch. The largest *in scope* is LIQUIDITY. SUPPLY's
entire possible influence — 0.105 nats, a 10% haircut at its hardest — is **smaller than
the magnitude threshold the dominance flag uses**, which means a SUPPLY-dominated chain
is by construction a chain in which nothing is dominant.

---

## 2 — LIQUIDITY: calibration problem, or misspecified curve?

### 2.1 The multiplier, decomposed

| series | min | p1 | p5 | p25 | med | p75 | p95 | p99 | max | n |
|---|---|---|---|---|---|---|---|---|---|---|
| as scored (floor + bypass) | 0.4000 | 0.4000 | 0.4000 | 0.4000 | 0.4187 | 0.6112 | 1.0000 | 1.0000 | 1.0000 | 703 |
| bypass only, no 0.40 floor | 0.0000 | 0.0000 | 0.0000 | 0.3528 | 0.4187 | 0.6112 | 1.0000 | 1.0000 | 1.0000 | 703 |
| curve alone, no floor no bypass | 0.0000 | 0.0000 | 0.0000 | 0.3512 | 0.4071 | 0.5210 | 0.8140 | 0.9428 | 0.9939 | 703 |
| curve alone, non-bypass rows | 0.0000 | 0.0000 | 0.0000 | 0.3500 | 0.4053 | 0.5353 | 0.8242 | 0.9433 | 0.9939 | 645 |

- Rows taking the `DEPTH ≥ 0.90` bypass: **58/703 (8.3%)** — every one set to exactly 1.000.
- Non-bypass rows reaching 1.000 through turnover: **0**. The highest any achieved is
  **0.9939** (ARB).
- Non-bypass rows the 0.40 floor *rescued* from a lower curve value: **310/645 (48.1%)**.
  Without the floor their median would be **0.3489** and their minimum **0.0000**.

### 2.2 Turnover — the input the curve is a function of

| series | min | p1 | p5 | p25 | med | p75 | p95 | p99 | max | n |
|---|---|---|---|---|---|---|---|---|---|---|
| turnover (vol/mcap) | 0.0000 | 0.0000 | 0.0000 | 0.0081 | 0.0332 | 0.0850 | 0.2214 | 0.3871 | 1.2896 | 703 |

| turnover band | rows | share | what the curve does there |
|---|---|---|---|
| exactly 0 | 64 | 9.1% | curve → 0, floor rescues to 0.40 |
| 0 – 3% | 269 | 38.3% | curve < 0.40, **floor binds** |
| 3 – 10% | 217 | 30.9% | curve rising, 0.40 – 0.55 |
| 10 – 30% | 136 | 19.3% | curve rising, 0.55 – 1.00 |
| **30 – 60% (the design peak)** | **15** | **2.1%** | curve at or near its 1.00 peak |
| 60 – 120% | 1 | 0.1% | curve falling |
| > 120% (the wash-trading band) | 1 | 0.1% | curve collapsing |

The curve peaks at 45% turnover. The universe's median is **3.32%**, its p95 is **22.1%**.
**97.5% of rows sit on the curve's left-hand rising limb**, below the band it was shaped
around, and 47.4% sit low enough that the floor — not the curve — decides their value.

**This is a misspecified curve, not a miscalibrated one.** The evidence is that the two
mechanisms designed as guard rails (the 0.40 floor, the blue-chip bypass) are between
them producing 52.4% of all LIQUIDITY values, while the curve's designed operating range
holds 2.1% of the universe.

### 2.3 Turnover against forward return — what the history can support

| sample | legs | mean IC | SE | 95% CI | legs + | verdict |
|---|---|---|---|---|---|---|
| turnover — wide cross-section | 2 | +0.1398 | 0.3797 | [−0.6043, +0.8840] | 1 | **not evidence** |
| LIQUIDITY mult — wide | 2 | +0.1156 | 0.2684 | [−0.4105, +0.6418] | 1 | **not evidence** |
| turnover — narrow (top-50) | 47 | +0.0194 | 0.0424 | [−0.0638, +0.1026] | 26 | spans zero |
| LIQUIDITY mult — narrow (top-50) | 47 | −0.0287 | 0.0258 | [−0.0793, +0.0219] | 23 | spans zero |

The wide ledger is three nights old, so it yields **two** forward legs, with intervals
±0.6 wide. The narrow ledger has forty-seven and is the wrong sample for this question in
a specific way: `signals.csv` keeps the top fifty **by conviction**, and turnover is an
input to conviction — selecting on conviction selects on turnover. Its LIQUIDITY column
spans a restricted range by construction, so an IC over it is an IC over that restriction.

> **Why forty-seven here and forty-three on the published matrix — the same ledger.** Both
> read the LEGACY selection history (`ledger/signals.csv`, 48 nights, 2026-08-01 …
> 2026-09-17). This table is a calibration probe and pairs *every* consecutive night:
> 48 − 1 = **47**. The published estimator does not. `_edge_legs()` starts at the spec
> boundary `_spec_breaks()` detects — 2026-08-05, the night the median asset's score moved
> 36 points on a 1.21% price move — and discards the four nights before it, because a
> conviction recorded under a different scoring function is not comparable to one recorded
> under this one: 2026-08-05 … 2026-09-17 is 44 nights, so **43** legs. Neither figure is
> the forward sample, which admits at most two. The difference is the boundary, not the
> source, and the two numbers should not be reconciled by averaging them.

**Nothing available justifies redesigning the curve.** That is the finding, and it is the
reason the recommendation below is conservative.

### 2.4 One defect found in passing, reported not fixed

**15 rows per night have `total_volume` exactly zero** — BUIDL, USYC, JAAA, USTB, OUSG,
EUTBL and other tokenised money-market instruments — and are scored at LIQUIDITY 0.40,
*identical to a token with 2.9% turnover*. Four more trade at under 0.005% turnover.

The score cannot distinguish "no venue lists this" from "listed and thin". That is the
same null/zero collapse Module F refuses for FDV and `funding.py` refuses for a missing
venue reading, and it should be refused here too. It is **not** fixed in Phase 2A: the
right answer depends on whether an untraded instrument is maximally illiquid (0.40 is
roughly right) or unmeasured (neutral, as Module F would say), and that decision belongs
with the curve redesign rather than ahead of it.

---

## 3 — Proposed bounds

### 3.1 DEPTH — **no new bound**

DEPTH is already hard-bounded to `[0, 1]` by `max(0, min(1, (log10(mc) − 6)/4))`. It
cannot escape, and the empirical distribution confirms it never tries: observed range
0.5102 – 1.0000, between-night drift 0.0068.

An empirically-fitted floor at the pooled p1 (0.506) **touches zero rows** — verified,
below. It would be a no-op today and would only ever bind if the universe widened below
$110M market cap — the smallest recorded across the three nights, which is where the
0.5102 minimum comes from — at which point it would be capping a factor that was
correctly reporting a genuinely tiny asset. Fitting it now means fitting three nights of one
regime to constrain a case that has never occurred.

**What I propose instead:** lift the existing `[0, 1]` clamp out of the arithmetic and
into the named config object, so it is visible and auditable rather than implicit in a
`min`/`max`; and record `depth_raw` (pre-clamp) in snapshots so cap hits become
measurable.

**Observation for a later phase:** 30 of 703 rows (4.3%) sit at exactly 1.000 — every
asset above $10B market cap. BTC at $1.5T and a $10B token are indistinguishable to this
factor. That is a mild version of CONFIRM's saturation, and fixing it is a curve change,
deferred on the same logic as LIQUIDITY.

### 3.2 SUPPLY — **no new bound**

SUPPLY is hard-bounded to `[0.90, 1.00]` by `EMISSION_MAX_PENALTY`. Observed range
0.9037 – 1.0000; between-night drift **0.0009**, the tightest of the three. An
empirically-fitted floor at the pooled p1 (0.9059) touches 2 rows and changes **zero**
published scores.

The stronger argument is the influence table in §1: SUPPLY's entire possible range is
0.105 nats. It cannot dominate a chain under any threshold worth setting, so a bound on
it constrains nothing that was ever at risk.

**What I propose instead:** name `EMISSION_MAX_PENALTY` and the free/anchor ratios in the
same config object, and record `em_raw` and `emission_drag` in snapshots. (`emission_drag`
is already recorded; this is about co-locating the constants.)

### 3.3 LIQUIDITY — **bound conservatively, defer the curve, make the structure visible**

Every value-changing bound available is a large re-valuation with no forward-return
evidence behind it. Measured on the 2026-09-17 cross-section (235 rows):

| candidate bound | rows touched | scores changed | tiers changed | clamps | max rank move | top-10 change |
|---|---|---|---|---|---|---|
| DEPTH floor at p1 (0.506) | **0** | **0** | 0 | 1 → 1 | 0 | none |
| DEPTH cap at p99 (1.000) | **0** | **0** | 0 | 1 → 1 | 0 | none |
| SUPPLY floor at p1 (0.906) | 2 | **0** | 0 | 1 → 1 | 1 | none |
| LIQUIDITY cap at p95 of the non-bypass curve (0.824) | 31 | 28 | **15** | 1 → 0 | 30 | DASH, ETH, PONS, RAY, SOL, XMR |
| LIQUIDITY floor 0.40 → 0.55 | 163 | **160** | 2 | 1 → 1 | 56 | none |
| LIQUIDITY floor 0.40 → 0.50 | 151 | **145** | 0 | 1 → 1 | 36 | none |

Reading that table:

- **Capping LIQUIDITY at 0.824** changes 15 tiers and six of the top ten. It works by
  capping the blue-chip bypass, because the bypass is the only thing producing values
  above 0.824. That is a bypass redesign wearing a bound's clothes, and it is exactly
  what "fitting around a structural defect" means. **Reject.**
- **Raising the floor** to 0.50 or 0.55 re-prices 145–160 of 235 rows on three nights of
  evidence and a curve nobody has validated. It also makes the floor do *more* of the
  work, when the finding is that it already does too much. **Reject.**

**Proposal: no change to the LIQUIDITY value in Phase 2A.** Instead:

1. Name `LIQ_FLOOR = 0.40` and `LIQ_BYPASS_DEPTH = 0.90` in the config object. They are
   currently bare literals inside the scoring arithmetic; a threshold nobody can point at
   is a threshold nobody can question.
2. Record `liq_raw` — the curve value before floor and before bypass — in snapshots, so
   the 48.1% rescue rate and the 0-of-645 ceiling failure are a query rather than a
   re-derivation.
3. Surface on the board row when LIQUIDITY is **at the floor** or **took the bypass**.
   Today a 0.40 from a 2.9%-turnover token and a 0.40 from a token with no volume at all
   are the same cell.
4. **Defer the curve redesign with a stated trigger**: revisit when `ledger/xsec/` holds
   **40 forward legs** — the same `EDGE_MIN_LEGS` the label gate already uses — which at
   one leg a night is approximately 2026-10-25.

This is the conservative option the brief asks for, and it is conservative in the
direction that matters: it changes no published score while making the thing that is
actually wrong impossible to overlook.

---

## 4 — The dominance statistic

### 4.1 The framework

Per factor *i* in the chain:

```
signed contribution   c_i     = log(m_i)
absolute magnitude    |c_i|
concentration share   s_i     = |c_i| / Σ_j |c_j|
```

For the largest factor, expose **name**, **share**, and **absolute magnitude**. All five
factors participate — dominance is a property of the whole chain, so CONFIRM and FUNDING
are in the statistic even though their bounds are out of scope.

### 4.2 The empirical distribution

| series | min | p1 | p5 | p25 | med | p75 | p95 | p99 | max | n |
|---|---|---|---|---|---|---|---|---|---|---|
| concentration share of largest | 0.000 | 0.365 | 0.379 | 0.423 | 0.472 | 0.563 | 0.948 | 1.000 | 1.000 | 703 |
| absolute magnitude \|log\| | 0.000 | 0.101 | 0.437 | 0.817 | 0.916 | 0.942 | 1.366 | 1.635 | 1.687 | 703 |
| total \|log\| across the chain | 0.000 | 0.178 | 0.598 | 1.479 | 2.017 | 2.282 | 2.668 | 3.073 | 3.195 | 703 |

Which factor is largest, pooled: **LIQUIDITY 48.5% · CONFIRM 43.8% · DEPTH 6.8% ·
SUPPLY 0.4% · FUNDING 0.1%.**

| night | median share | p90 share | median \|log\| | p90 \|log\| |
|---|---|---|---|---|
| 2026-09-15 | 0.470 | 0.694 | 0.916 | 1.238 |
| 2026-09-16 | 0.474 | 0.705 | 0.916 | 1.286 |
| 2026-09-17 | 0.471 | 0.711 | 0.916 | 1.118 |

### 4.3 Why share alone is the wrong flag — demonstrated

124 rows (17.6%) have share > 0.60. Three of them have a "dominant" factor that moves the
score by less than a quarter:

| date | sym | share | \|log\| | total \|log\| | dominant | conviction |
|---|---|---|---|---|---|---|
| 2026-09-17 | **ZEC** | **1.000** | **0.0686** | 0.0686 | FUNDING | 100 |
| 2026-09-16 | WBT | 0.7200 | 0.1985 | 0.2756 | CONFIRM | 76 |
| 2026-09-17 | WBT | 0.7145 | 0.1924 | 0.2693 | CONFIRM | 77 |

ZEC is the case exactly: four factors at precisely 1.0000 and a fifth at 1.0710, so one
factor holds **100% of a total of 0.0686 nats**. A perfect dominance score, for the most
nearly neutral chain on the board. A flag that fires on it is a flag nobody reads by the
second week.

### 4.4 Proposed threshold

> **Warn iff `share > 0.70` AND `|log| > 0.22`.**

- **`share > 0.70`** is the pooled 90th percentile (0.709), stable per night
  (0.694 / 0.705 / 0.711). The rule is therefore *"the decile of chains most concentrated
  in one factor"* — a statement about the distribution, not a number chosen to produce a
  pleasing count.
- **`|log| > 0.22`** is `log(1.25)`: the dominant factor, on its own, moves the score by
  at least a quarter. Stated as a multiplier rather than a percentile because a
  percentile of magnitude drifts with the regime while "a quarter" does not.

**Flags 69 of 703 rows (9.8%) pooled — 23 / 23 / 23 per night.** The magnitude leg
rejects exactly the three rows above, which is the whole reason it is there.

| dominant factor | rows flagged | share of flags | in Phase 2A scope? |
|---|---|---|---|
| CONFIRM | 48 | 69.6% | **no — deferred** |
| DEPTH | 11 | 15.9% | yes |
| LIQUIDITY | 10 | 14.5% | yes |

**Two things to be clear about before adopting this.**

1. **Seven in ten flags will point at CONFIRM**, the factor Phase 2A cannot touch. That
   is the flag telling the truth — CONFIRM has the largest influence in the chain
   (1.687 nats) and the known saturation defect from `AUDIT-2026-09` §1.1. It is an
   argument for shipping the flag, not against, but the board will read as "CONFIRM is
   doing everything" for as long as CONFIRM remains unbounded, and that should be
   expected rather than discovered.
2. **Every flag is a haircut. None is a boost — 69 of 69.** By construction: only FUNDING
   can exceed 1.0, and its maximum possible magnitude (0.163) is below the 0.22 threshold.
   A dominance warning can therefore *never* mean "one factor inflated this score" under
   the current envelopes. That is worth saying on the board, because the natural reading
   of a dominance flag is the opposite one.

---

## 5 — ZEC, as a diagnostic

| night | DEPTH | CONFIRM | LIQUIDITY | SUPPLY | FUNDING | pre-clamp | published |
|---|---|---|---|---|---|---|---|
| 2026-09-15 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 100.0 | 100 |
| 2026-09-16 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | 100.0 | 100 |
| 2026-09-17 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | **1.0710** | **107.1** | 100 |

| factor | multiplier | signed log | share of chain \|log\| |
|---|---|---|---|
| DEPTH | 1.0000 | +0.0000 | 0.000 |
| CONFIRM | 1.0000 | +0.0000 | 0.000 |
| LIQUIDITY | 1.0000 | +0.0000 | 0.000 |
| SUPPLY | 1.0000 | +0.0000 | 0.000 |
| **FUNDING** | **1.0710** | **+0.0686** | **1.000** |

**The overshoot is entirely FUNDING, and it is 7.1 points.** ZEC's other four factors
multiply to exactly 100.0 — they are each saturated at their own ceiling of 1.0 and
cannot push past it. The 1.0710 is a genuine current short-squeeze reading (APR −25.2%,
RSI 75.0, five venues, snapshot 2026-09-17), inside the `[0.85, 1.15]` envelope.

**Phase 2A changes nothing about ZEC**, and should not. Nothing proposed here touches a
factor that is at 1.0000 for ZEC.

The real ZEC finding is CONFIRM, and it is out of scope: `rs_blend = 154.62` maps to
`cm = 1.0000`, and so would 60, and so would 4,000. That is `AUDIT-2026-09` §1.1, and it
is the reason ZEC sits at the ceiling on all three nights regardless of funding.

### 5.1 The invariant the clamp now has

Only one factor in the chain can exceed 1.0. DEPTH, CONFIRM, LIQUIDITY and SUPPLY are
each bounded above at exactly 1.0 by construction, so four saturated factors give exactly
100.0 and cannot overshoot. FUNDING's envelope is `[0.85, 1.15]`. Therefore:

```
max possible pre-clamp = 100 × 1 × 1 × 1 × 1 × 1.15 = 115.0
```

**Every clamp overshoot is, necessarily, funding — and is at most 15 points.** Before
Phase 1.5 this was not true: the overlay could serve any number the ledger happened to
hold, and did (HBAR, 242.9). The largest pre-clamp anywhere in the wide ledger is
**107.1**, against a 115 ceiling.

The bound that fixed the clamp was the funding envelope, and it has already landed. No
Phase 2A bound is needed to protect the clamp, because the clamp is already protected.

---

## 6 — What I propose to implement, pending approval

**No published score changes. No spec boundary. `SPEC_HASH` stays at `ab16684ad5c1`.**

| # | change | kind |
|---|---|---|
| 1 | One `FACTOR_BOUNDS` config object at the top of the scoring section, holding `DEPTH_MIN/MAX = 0, 1`, `SUPPLY_FLOOR = EMISSION_MAX_PENALTY`, `LIQ_FLOOR = 0.40`, `LIQ_BYPASS_DEPTH = 0.90` — the constants that already govern these factors, named and co-located rather than inline literals | refactor, score-identical |
| 2 | `DOM_SHARE_WARN = 0.70`, `DOM_MAGNITUDE_WARN = 0.22` in the same object, with the two-part rule | new observable |
| 3 | Record `depth_raw`, `liq_raw`, `em_raw` (pre-bound values) plus the signed contribution, share and magnitude per factor in `ledger/xsec/` | snapshot widening |
| 4 | Surface on the **board row**: bound-hit markers (LIQUIDITY at floor / took bypass / DEPTH at cap), the dominance flag with its factor name, and the pre-clamp value with clamp state | presentation |
| 5 | Parity tests over the bounds object and the dominance statistic, JS and Python, plus a regression asserting the refactor changes no score on the recorded cross-section | tests |
| 6 | METHOD entry dated 2026-09-17 with the evidence above | docs |

Item 1 is the part that could move a score and must not. The test I would write for it is
the one Phase 1B already established: recompute every row of the recorded cross-section
both ways and assert the published integer is identical on all 703.

**Deferred, with triggers:**

| deferred | trigger |
|---|---|
| LIQUIDITY curve redesign | 40 forward legs on `ledger/xsec/` (≈ 2026-10-25) |
| DEPTH top-end saturation | same, and only after CONFIRM's — the same defect, larger there |
| zero-volume null handling | with the curve redesign; it is a null-semantics decision, not a bound |
| CONFIRM bounds | Phase 2B, per the accepted Phase 1 recommendation |
| FUNDING bounds / short-capitulation percentile | per-symbol funding history |

---

## 7 — What this report does not claim

- That any proposed or rejected bound would improve the information coefficient. Nothing
  here was tested against forward returns, because nothing available can be.
- That the LIQUIDITY curve is wrong *in its shape*. The finding is narrower and firmer:
  the universe does not occupy the range the curve was designed for, and 52.4% of its
  output comes from the floor and the bypass rather than the curve. Whether a better
  curve exists is a question the two available forward legs cannot touch.
- That three nights is enough to calibrate anything requiring time-series depth. It is
  enough for a *cross-sectional* percentile on a factor whose percentiles move by less
  than 0.007 a night — which is DEPTH and SUPPLY, and marginally LIQUIDITY at p75 — and
  it is not enough for anything else.
