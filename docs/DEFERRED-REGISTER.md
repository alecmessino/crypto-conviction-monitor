# Deferred register

**Status: SCORING ENGINE HARDENING — CLOSED / FORWARD OBSERVATION** (2026-09-18)

Four things this audit deliberately did not do, and the evidence that reopens each one.
Every trigger is an **evidence count or an inferential state, never a date**. A calendar
deadline is a commitment to act whether or not the evidence arrived; these are
commitments to act only when it has.

**Which history a trigger counts against matters, and is stated on every one.** This
repository holds two information-coefficient samples and they are not the same
measurement:

| | LEGACY SELECTION HISTORY | FORWARD CROSS-SECTIONAL SAMPLE |
|---|---|---|
| source | `ledger/signals.csv` | `ledger/xsec/` |
| depth | 48 nights, from 2026-08-01 | **3 nights**, from 2026-09-15 |
| width | ~50 rows a night | 234–235 rows a night |
| population | the top fifty **by conviction** — selected on the variable being measured | the whole scored cross-section |
| status | 43 legs, composite 1d IC −0.0562, CI [−0.1065, −0.0060] → the board is DIAGNOSTIC ONLY | **0 of 18 cells measurable** |

Every count below is against the **FORWARD** sample unless it says otherwise. Three
nights admit at most two one-day legs, so no trigger here is close to being met, and the
legacy history — however deep — cannot satisfy one: it is the wrong population for every
question these triggers ask.

Measurements behind the first three: `docs/PHASE2A-CALIBRATION-2026-09-17.md`.
Live counts, refreshed every night: `ledger/walkforward.json`.

---

## LIQUIDITY — curve redesign

**Deferred because** the problem is the shape of the curve, not the absence of a clip.
Over `ledger/xsec/` (703 rows, 3 nights): 44.1% of rows sit at exactly the 0.40 floor,
8.3% take the `DEPTH ≥ 0.90` bypass, **not one of 645 non-bypass rows reached 1.0 through
turnover** (best: 0.9939), and the curve peaks at 45% turnover against a universe median
of 3.3%. The floor and the bypass together produce 52.4% of everything the factor emits.
Every available clip is a large re-valuation with no forward evidence: capping at the
non-bypass p95 moves 15 tiers and six of the top ten, and raising the floor re-prices
145–160 of 235 rows.

**Trigger — reopen at ≥ 40 genuine forward cross-sectional usable legs.** Measured on
the FORWARD sample (`ledger/xsec/`), never on the legacy one: the legacy history is
selected on conviction, and conviction is a function of turnover, so its LIQUIDITY column
spans a restricted range by construction and an IC over it is an IC over that
restriction. The bar is the same `EDGE_MIN_LEGS = 40` the publication gate uses.
**As of 2026-09-18 the count is 2.** No calendar date substitutes for it.

**Questions to answer then, in this order:**
1. Does the 0.40 floor have predictive justification, or is it only preventing a zero?
2. Should `DEPTH ≥ 0.90` bypass turnover at all, or is that a claim about blue chips that
   the returns do not support?
3. Zero volume versus missing volume — 15 rows a night are tokenised money-market
   instruments with no secondary market, currently scored identically to a token at 2.9%
   turnover. `liq_state` has separated them since 2026-09-17 without acting on them.
4. Where is the turnover optimum on this universe, as opposed to the one the curve
   assumes?
5. Does the 30–60% target band survive forward evidence at all, given 2.1% of rows
   occupy it?

---

## CONFIRM — bounds

**Deferred because** its cross-sectional percentiles move ~0.05 at the quartiles between
consecutive nights, which is the same order as the gap between candidate bounds. A bound
fitted now would be fitted to night-to-night movement. Separately, its declared ceiling
was wrong in the source for most of this repository's life — the comment claimed 0.91 and
the arithmetic gives 1.00 — and 37 rows of the 2026-09-17 board sat above 0.91. CONFIRM
also has the largest possible influence in the chain, 1.687 nats against LIQUIDITY's
0.916, and it is the factor 70% of dominance warnings name.

**Trigger — reopen when forward-distribution drift at p25 / p50 / p75 is consistently
below ~0.01**, the level DEPTH and SUPPLY already sit at, sustained over enough nights to
be a property rather than a coincidence — **or** when enough history exists to replace
that stability criterion with a better test, declared in advance of looking at the
result. CONFIRM currently drifts ~0.05 at the quartiles between consecutive nights.
Measured on the FORWARD sample and recorded per night in `ledger/xsec/`; computable today
with `docs/phase2a_calibration.py`.

---

## FUNDING — percentile "short capitulation", OI floor, venue-count rule

**Deferred because** the factor has essentially no cross-sectional dispersion: 226 of 235
rows sat at exactly 1.000 on 2026-09-17, and every percentile from p5 to p95 is 1.0000.
There is no cross-section to take a percentile *of*. The right definition — each token's
funding measured against **its own** history — needs per-symbol time series, which only
began accumulating universe-wide at AUDIT-PHASE1.6 (before that, funding provenance was
recorded for the top fifty rows only).

**Trigger — two parts, independently gated:**

- **Percentile "short capitulation": reopen at ≥ 40 nights of recorded `funding_apr`
  per symbol.** Applied per symbol, not as a universe switch: a name with 40 nights gets
  a percentile against its own history, a name without keeps the current absolute
  reading and says so. Universe-wide funding provenance only began accumulating at
  AUDIT-PHASE1.6 — before that it was recorded for the top fifty rows only.
- **OI floor / venue-count gating: require ≥ 20 realised observations in BOTH compared
  cohorts** at the horizon under test, before any threshold is adopted. The cohort
  machinery is live in `ledger/walkforward.json`. No threshold is adopted on the grounds
  that thin markets are intuitively less reliable — the point is to measure whether
  predictive behaviour actually degrades below it, and a cohort that cannot be compared
  has not measured that.

---

## REVENUE — Phase 5

**Deferred because** it is the next phase and has not been authorised. Not started; no
DefiLlama call is made anywhere in this repository.

**Trigger — remains unstarted.** Observational only when **explicitly authorised**:
a `REVENUE` column recorded at ×1.0 for every row, with a visible "no revenue data" badge
for tokens that have none — never a silent penalty, which would read as a signal.
**Score eligibility only after ≥ 40 legs in the FORWARD sample** (`ledger/xsec/`) **with
its standalone IC interval not spanning zero**, under the same contract as every other
cell in the matrix. The store matters and is stated because "the matrix" would otherwise
resolve to the published one — which is the legacy conviction-truncated sample and
already stands at 43 legs, so a REVENUE factor could appear to clear a bar it had never
been measured against. A factor may
not enter the score on the strength of being a good idea.

---

## What "trigger" means here

None of these reopens automatically, and none reopens on a date. Each states the evidence
that would make its question **answerable**; reaching it is permission to ask, not an
instruction to change anything.

While none is met, the model is frozen. Factor formulas, factor bounds, ranking, tiers,
weighting, the publication gate, basket construction and the dominance thresholds do not
change except to satisfy a trigger above or to correct a genuine integrity defect. No
speculative cleanup.

`ledger/walkforward.json` reports the counts every night and becomes more informative on
its own, so this register can be checked rather than remembered.
