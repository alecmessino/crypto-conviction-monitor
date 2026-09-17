# Deferred register

Four things this audit deliberately did not do, and the evidence that reopens each one.
Every trigger is an **evidence count or an inferential state**, never a date — a calendar
deadline is a commitment to act whether or not the evidence arrived.

Maintained alongside `docs/PHASE2A-CALIBRATION-2026-09-17.md`, which holds the
measurements behind the first three.

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

**Trigger:** **40 genuinely forward, cross-sectional usable legs** under the current
factor-logging regime — the same `EDGE_MIN_LEGS` the publication gate uses, measured on
`ledger/xsec/` rather than on the conviction-truncated `signals.csv`. As of 2026-09-17
that count is **2**. *Approximately 2026-10-25 at one leg a night, and the date is not
the gate.*

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

**Trigger:** the forward distribution stabilising enough that candidate bounds are
separable from ordinary movement — concretely, **between-night percentile drift at p25 /
p50 / p75 below ~0.01**, the level DEPTH and SUPPLY already sit at, sustained over enough
nights to be a property rather than a coincidence. Recorded per night in
`ledger/xsec/`; measurable today with `docs/phase2a_calibration.py`.

---

## FUNDING — percentile "short capitulation", OI floor, venue-count rule

**Deferred because** the factor has essentially no cross-sectional dispersion: 226 of 235
rows sat at exactly 1.000 on 2026-09-17, and every percentile from p5 to p95 is 1.0000.
There is no cross-section to take a percentile *of*. The right definition — each token's
funding measured against **its own** history — needs per-symbol time series, which only
began accumulating universe-wide at AUDIT-PHASE1.6 (before that, funding provenance was
recorded for the top fifty rows only).

**Trigger, two parts, both required:**
- **Percentile capitulation:** enough per-symbol funding history to estimate a token
  against itself — at minimum **40 recorded nights of `funding_apr` for the symbol in
  question**, applied per symbol rather than as a universe switch, so a name with history
  gets a percentile and a name without keeps the current absolute reading and says so.
- **OI floor / venue-count rule:** enough stored data to test whether predictive
  behaviour actually *degrades* below a candidate threshold — the cohort machinery for
  this is live in `ledger/walkforward.json` and needs **≥ 20 realised outcomes in both
  the below-threshold and above-threshold cohorts at the horizon being tested**. No
  threshold is adopted on the grounds that thin markets are intuitively less reliable;
  the point is to measure whether they are.

---

## REVENUE — Phase 5

**Deferred because** it is the next phase and has not been authorised. Not started; no
DefiLlama call is made anywhere in this repository.

**Trigger:** explicit authorisation, then **observational first** — a `REVENUE` column
recorded at ×1.0 for every row, a visible "no revenue data" badge for tokens with none
(never a silent penalty), and promotion into the score only once its **standalone forward
IC has been measured on stored history** under the same contract as every other cell in
the matrix: ≥ 40 usable legs, and an interval that does not span zero. A factor may not
enter the score on the strength of being a good idea.

---

## What "trigger" means here

None of these reopens automatically. Each states the evidence that would make the
question answerable; reaching it is permission to *ask*, not an instruction to change
anything. `ledger/walkforward.json` reports the counts every night and becomes more
informative on its own, so the register can be checked rather than remembered.
