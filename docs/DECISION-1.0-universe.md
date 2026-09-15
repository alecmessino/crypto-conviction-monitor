# Decision memo — §1.0, the persisted universe

Status: **decision requested**. No implementation, no code touched. Written 2026-09-15
against `a4f0373` (`SPEC_HASH = 1a4ea6e4d77e`).

Companion to AUDIT-2026-09 §1.0. Every figure below is measured from this repository.

---

## 0. Three facts that decide most of this

### 0.1 The truncation depth is not a detail — it *is* the measurement

AUDIT §1.0 argued the recorded top-50-by-conviction cut attenuates |IC| toward zero.
**That framing was wrong, and the data is worse than it.** Re-running the same 40 legs
with the cross-section truncated at successive depths:

| deepest *k* by conviction | legs | mean n | mean IC | SE | t | quintile spread |
|---|---|---|---|---|---|---|
| 10 | 40 | 10.0 | +0.0201 | 0.0529 | +0.38 | −9.0 bp |
| 20 | 40 | 19.6 | +0.0523 | 0.0382 | +1.37 | +1.7 bp |
| 25 | 40 | 24.2 | +0.0588 | 0.0340 | +1.73 | +15.4 bp |
| 30 | 40 | 28.7 | +0.0029 | 0.0293 | +0.10 | −110.5 bp |
| 40 | 40 | 36.8 | +0.0072 | 0.0269 | +0.27 | +12.4 bp |
| 45 | 40 | 40.3 | −0.0302 | 0.0264 | −1.14 | −41.9 bp |
| **50 (published)** | 40 | 43.0 | **−0.0505** | 0.0270 | −1.87 | **−98.7 bp** |

These are nested and overlapping, so the rows are not nine independent measurements. The
comparison that *is* valid is paired, leg by leg, on the identical 40 legs:

> **k=50 minus k=40: −0.0576, SE 0.0168, t = −3.43.**

Adding the seven lowest-conviction names of an arbitrary window moves the IC by more than
the IC itself, significantly. The same monotone drift appears when the window is cut by
market cap instead of conviction (−0.003 at k=10 → −0.051 at k=50), so it is not a
conviction-boundary artefact: the negative signal lives in the smaller, lower-ranked
names, and the published number reports however much of them the window happens to admit.

**The published −0.0505 is a fact about where `rows[:50]` sits, not about the board.** It
is not attenuated; it is *arbitrary*. That is the finding this memo exists to act on, and
it is worth correcting in the audit register.

### 0.2 The full cross-section is already computed and then discarded

`nightly.py:4188` scores every de-duplicated non-stable market — all ~235 — with every
per-row column populated. `nightly.py:4137` sorts by conviction, `:4166` writes
`rows[:50]`, and the other ~185 fully-formed rows are dropped at write time.

Funding is fetched for `scored_syms`, which is the **full** universe, not the top 50:
`consolidated_markets: 153` on 2026-09-14. `funding.json` truncates its `assets` array to
mirror the persisted board, but the consolidation itself already covers the wide set.

> **Widening the persisted cross-section costs zero additional API calls, zero additional
> runtime, and no new feed.** It is a write-path decision only.

### 0.3 Storage is not the constraint. Browser payload is — and it is already failing

Measured, not estimated:

| artifact | per row | per night (50 rows) | per year |
|---|---|---|---|
| `ledger/signals.csv` (71 cols) | 276 B | 13.5 KB | 5.0 MB |
| `ledger/signals.json` (same rows, JSON) | **1,950 B** | **95.2 KB** | **35.6 MB** |
| a compact 25-column research row | 134 B | 6.5 KB (50) / **30.7 KB (235)** | 2.4 / **11.5 MB** |

Git is a non-issue: `signals.json` holds 34 distinct blobs totalling **92.8 MB
uncompressed** and packs to **0.63 MB** — a 147× delta ratio. The whole `.git` is 7.0 MB.
Nightly-rewritten text files delta almost perfectly. (Corollary: do **not** gzip a
committed ledger. A gz blob does not delta, so compressing before commit would make git
storage dramatically *worse*.)

The real constraint is `index.html:1914`:

```js
const r = await fetch("ledger/signals.json", {cache:"no-store"});
```

Every visitor downloads the entire history, uncached, on every page load.

| trajectory | now (45 nights) | +1 year |
|---|---|---|
| status quo, 50 rows/night | **4.39 MB** | **40.0 MB** |
| expanded in place, 235 rows/night | 20.6 MB | **187.9 MB** |

**This is an independent, pre-existing defect: at 50 rows a night the page is already on a
40 MB/year trajectory.** It is not created by this decision, but it rules out one option
outright and should be booked on the register regardless of which option wins.

---

## 1. The options

### Option 1 — keep top-50-by-conviction only

- **Persisted universe:** top 50 by conviction, unchanged.
- **Storage:** 13.5 KB/night CSV, 95.2 KB/night JSON; 40 MB/year of browser payload.
- **IC / quintile:** IC is the within-top-21%-of-board rank correlation. Per §0.1 it
  changes by 2 SE when the window moves by seven names, and the window is arbitrary.
  Quintile blocks are `k = max(3, 43//5) = 8`: "top quintile" is the top ~3.4% of the
  board and "bottom quintile" is the 79th–83rd percentile band. −98.7 bp is not a
  universe long/short and cannot be read as one.
- **Continuity:** perfect, by definition.
- **Weights:** none needed.
- **Phase 2:** the §2.5 reconciliation becomes **partly circular**. To know which names
  were top-50-by-conviction on a historical night, the backfill must score the whole
  universe and apply the same cut — so it would be checking whether it reproduces a
  selection that is itself a function of the scores being checked. Any systematic
  backfill bias that reorders names near the rank-50 boundary changes the comparison set,
  not just the values in it.
- **Migration / failure modes:** none. The failure mode is the status quo: continuing to
  publish a number whose value is set by a line of code nobody chose for statistical
  reasons.

### Option 2 — top-50 by conviction + market-cap-stratified remainder

- **Persisted universe:** the legacy 50, plus a market-cap-ranked sample of the other
  ~185, each row flagged with why it was kept.
- **Storage:** between Option 1 and Option 3, proportional to the sample fraction.
- **IC / quintile:** **this is where it fails.** The statistic is a *rank correlation over
  a cross-section*. Ranks are a property of the population, not of a sample: if 100 of 235
  names are sampled, the within-sample conviction ranks are not the population ranks, and
  — decisively — the forward returns of the 135 unsampled names are never observed, so
  the population return ranks cannot be recovered at all. A design-consistent estimator
  would be a Horvitz–Thompson-weighted rank correlation with an inclusion-probability
  model. That is defensible in a paper and not defensible on a page whose standing rule is
  that a number states what it is and what it is not.
- **Continuity:** the legacy 50 survive, so the existing series is preserved — the one
  thing this option does well.
- **Weights:** required, non-standard, and the estimator would need its own validation
  before any number it produced could be published.
- **Phase 2:** the backfill would have to reproduce the *sampling design* as well as the
  scores, which adds a second way for reconciliation to fail for reasons unrelated to the
  spec.
- **Migration / failure modes:** highest complexity of the three, for a statistic that is
  strictly worse than Option 3's. The only motivation for sampling is storage, and §0.3
  shows storage does not bind: the full cross-section in compact columns is **30.7 KB a
  night**, less than a third of what `signals.json` already writes for 50 rows.

  **Recommend rejecting.** It buys nothing and costs an estimator nobody can check.

### Option 3 — full ~235 cross-section in a compact dedicated research ledger

- **Persisted universe:** every scored name, every night — the same set `score()` already
  runs over.
- **Storage:** 25 columns × 134 B × 235 rows = **30.7 KB/night, 11.5 MB/year**. Not
  fetched by the browser, so **zero** added page weight.
- **IC / quintile:** a genuine cross-sectional IC with no weights and no restriction.
  Quintile blocks become `k = 47` — a real top-20% vs bottom-20% of the scored board.
  Per-leg noise falls from 1/√40 = 0.158 to 1/√232 = 0.066, and the legs needed for a
  given true signal fall with the square:

  | mean n | per-leg noise | legs for IC 0.02 | 0.03 | 0.05 |
  |---|---|---|---|---|
  | 43 (today) | 0.158 | 250 | 111 | 40 |
  | **235** | **0.066** | **43** | **19** | **7** |

  That is the largest single item in this memo. The panel currently says it needs
  111–250 legs; on the full cross-section the same targets are 19–43. Phase 2's backfill
  is still worth doing — more legs is more legs — but the **width** of the cross-section
  buys more precision per night of work than the **length** of the history does.
- **Continuity:** total. `signals.csv` and `signals.json` are untouched — same 50 rows,
  same 71 columns, same order, same bytes. The legacy 40-leg series is bit-identical and
  keeps running beside the new one. Neither is ever pooled into the other.
- **Weights:** none. This is the whole point.
- **Phase 2:** the natural target. The backfill must compute the full cross-section
  anyway (§0.2 logic in reverse), so writing it to the same schema with `src=backfill` is
  free, and the §2.5 reconciliation becomes a clean non-circular join on `(date, symbol)`
  against the live cross-section rows.
- **Migration / failure modes:** new directory, new writer, new reader. Enumerated in §3.

### Option 4 — Option 3, month-sharded, with a proved-subset invariant *(recommended)*

Option 3 with three refinements, each closing a specific failure mode:

1. **Month shards, not one growing file.** `ledger/xsec/2026-09.csv`, appended nightly and
   never rewritten once the month closes. One growing file would be rewritten in full
   every night forever; git absorbs that today, and a closed shard is simply never
   touched again. It also makes the Phase 2 backfill idempotent and restartable per month
   instead of all-or-nothing, and lets a consumer read only the months it needs.
2. **A proved-subset invariant, enforced by a test.** For every night, every row in
   `signals.csv` must appear in that night's shard with identical values on every shared
   column. The legacy series is then a *provable* subset of the wide one rather than a
   parallel write that can silently drift — which is exactly the failure that produced
   AUDIT §1.0 in the first place.
3. **Both ranks on every row.** `rank_mcap` and `rank_conv`, recorded rather than derived,
   so the wide ledger can reproduce any historical truncation — including the legacy
   top-50 cut — and the §0.1 sensitivity table can be regenerated at any depth without
   re-deriving ranks from row order. (Row order is a conviction sort; reading rank off it
   is how the "top 50 by market cap" comment came to be wrong.)

Proposed schema, 25 columns, 134 B/row measured on real data:

```
date symbol rank_mcap rank_conv conviction price market_cap turnover_pct
rs7 rs14 rs30 rs200 rs_blend rs_windows_n
c_depth c_momentum c_liquidity emission_mult perp_mult
fdv_usd funding_apr rsi7 beta_btc spec_hash src
```

`src` is `live` or `backfill` and never mixes in a computed statistic.
`rs_windows_n` anticipates AUDIT §1.7b (null 200 d windows currently scored as zero) so
the column exists before the fix needs it; until then it is a constant 4 and says so.

---

## 2. Decision table

| | 1 · top-50 only | 2 · stratified | 3 · full xsec | **4 · full xsec, sharded** |
|---|---|---|---|---|
| persisted universe | top 50 by conviction | 50 + mcap sample of ~185 | all ~235 | all ~235 |
| storage / night | 13.5 KB | ~20 KB | 30.7 KB | **30.7 KB** |
| storage / year | 5.0 MB | ~7 MB | 11.5 MB | **11.5 MB** |
| added browser payload | — | — | none | **none** |
| extra API calls | — | none | none | **none** |
| IC is design-unbiased | ✗ arbitrary window | ✗ needs HT weights | ✓ | **✓** |
| quintile = real universe block | ✗ top 3.4% vs p79–83 | ✗ | ✓ top 20% vs bottom 20% | **✓** |
| per-leg noise | 0.158 | ~0.10 | 0.066 | **0.066** |
| legs needed for IC 0.03 | 111 | ~46 | 19 | **19** |
| legacy 40-leg series intact | ✓ trivially | ✓ | ✓ untouched | **✓ untouched *and proved*** |
| sampling weights needed | no | **yes, non-standard** | no | **no** |
| Phase 2 reconciliation | ✗ partly circular | ✗ must replay design | ✓ clean join | **✓ clean join, per-month** |
| migration complexity | none | high | moderate | **moderate** |
| worst failure mode | keeps publishing an artefact | unverifiable estimator | wide/narrow drift | **caught by subset test** |

---

## 3. Recommendation

**Option 4.** Full ~235-name cross-section, 25 compact columns, month-sharded under
`ledger/xsec/`, never fetched by the browser, with the production top-50 series left
byte-identical and asserted to be a subset of it.

The case in one line: the data is already computed and thrown away, it costs 30.7 KB a
night and no API calls, it removes the need for sampling weights entirely, it cuts the
legs needed for a measurable IC from 111 to 19 — and it changes nothing about the series
already running.

Two things it is explicitly **not**:

- **Not a promotion.** The wide IC is a new measurement with its own history starting at
  night one. It must render beside the top-50 number, never replace it and never pool
  with it, until it has legs of its own.
- **Not a claim that the wide IC will be better.** §0.1 shows the sign moves with the
  window; the full cross-section may well be *more* negative than −0.0505. The argument
  is that it would be the honest number, not that it would be a flattering one. Anyone
  expecting the widening to rescue the edge should read the k-sweep table again.

### Sequencing, and what must be settled first

1. **Decide the schema and the shard boundary before writing a line** — Phase 2's backfill
   targets it, and changing it afterwards means rewriting the backfill's output.
2. **Land the writer alone**, with the subset invariant test, and let it accumulate live
   nights while Phase 2 is built. No panel, no reader, no presentation.
3. **Then** Phase 2 writes `src=backfill` into the same schema, and §2.5's reconciliation
   is a join on `(date, symbol)` against the live rows for the 40 overlapping nights.
4. **Then** Phase 3 reports IC per horizon on each source separately, pooled only with the
   pooling stated on the panel.

### Open questions for you

- **Input coverage across the wide set.** 153 consolidated funding markets against a
  235-name universe — so roughly 80 of the extra names will carry a null `funding_apr`,
  `rsi7` and `beta_btc`. That is the correct reading and matches how no-perp assets are
  already handled, but it means the wide cross-section is *systematically lower-coverage
  in its tail*. Should the panel report the wide IC on all 235, or additionally on the
  subset with full input coverage, with both stated? My view: all 235 as the headline,
  because excluding on coverage re-introduces a selection rule, with the coverage rate
  published per night beside it.
- **Retrofit.** The wide cross-section cannot be recovered for the 45 nights already
  recorded — those rows are gone. The wide series therefore starts at night one of the
  writer, unless Phase 2's backfill is pointed at the recent past as well as at
  2023–2026. Cheap to do and worth deciding now rather than later.

---

## Register

- **Deferred, not this boundary:** extract the `consolidated → perps_map` projection in
  `main()` into a named function so the last score-path hole can enter the specification
  capture. Named in `nightly.py` under the capture declarations.
- **Correction to AUDIT §1.0:** the restriction was described there as attenuating |IC|
  toward zero. Measured, it does not attenuate — it *moves the sign*, significantly
  (paired t = −3.43 between k=40 and k=50). The conclusion is unchanged and stronger.
- **Pre-existing, independent of this decision:** `ledger/signals.json` is fetched whole
  with `cache:"no-store"` and grows 95.2 KB a night — 4.39 MB today, ~40 MB within a year,
  at the *current* 50 rows. Needs its own fix (date-sharded fetch, or a rolling-window
  file plus an archive) whatever is decided here.
- **Presentation pass, flagged not edited:** `methodology.html:326` states that the RWA
  model's join-function capture "has no equivalent on the crypto side". False since
  `a4f0373`.
