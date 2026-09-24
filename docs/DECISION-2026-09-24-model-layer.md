# Decision memo — audit §6: qualification parity and the basket's funding omission

**Status: FOR DECISION. Nothing here is implemented.** Written 2026-09-24 after the
first scheduled nightly on `e8c94db` (run 61 → commit `207ea9e`) passed. Every figure
below comes from committed data or from the committed code run over a named payload.
None of it is inferred. `SPEC_HASH` is `91bbc2a7e466` throughout.

## 0. Context the question depends on

This repository has **two books**, and the code and the Method page already say which
one is canonical:

| | Canonical Index | Experimental basket |
|---|---|---|
| Built by | `_perf_weights` over `signals.csv` | `build_basket()` |
| Label | "THE INDEX" (`index.json.canonical`) | "EXPERIMENTAL / NON-CANONICAL" (`index.json.basket_note`) |
| Universe | ungated. Method: "No qualification gate" | gated by `_conjunctive_gate`, with an ungated fallback |
| Score | **published conviction, funding included** | `score(t, None, btc)`, **funding forced neutral** |
| Size | top 10, score-proportional | hysteresis. **44 holdings** on 2026-09-24 |

Two consequences follow:
- The conjunctive gate constructs only the experimental basket.
- The funding omission affects only that basket.

The canonical Index already consumes the canonical score (Architecture A below).

---

## A. Qualification parity

### A.1 Every differing input and threshold

| Leg | Terminal (`build()` + `computeLAVL`) | Nightly (`_conjunctive_gate` + `_lavl_regime`, captured) |
|---|---|---|
| **A: turnover** | `30 ≤ vol/mc·100 ≤ 60` | `0.30 ≤ vol/mc ≤ 0.60`. **Identical.** |
| **B: dilution** | FDV/MC ≤ 2.0, **and** proxy-ERA ≤ 1.5 whenever it is computable. proxy-ERA = (FDV/MC − 1) / (\|24h chg\| or turnover%) | FDV/MC ≤ 2.0, or FDV absent. **No proxy-ERA term.** |
| **C: regime** | band not COMPRESS/LIQ TRAP, and turnover < 90% | band not COMPRESS/LIQ TRAP, and turnover < 0.90 |
| C, velocity | clamp(**signed** chg/100 ÷ spread × **ln(max(vol/$5M, 1e-4))**, ±4) | min(4, **\|chg\|**/100 ÷ spread × **ln(max(2, vol/mc × 1e6))**) |
| C, spread | max((h−l)/p, 0.001) | (h−l)/p; 0 → 0.01 |
| C, divergence | clamp(clamp(turn/0.05, 0, 4) × clamp(1−spread, 0, 1) × (1 + clamp(**mcapChg**/5, ±1)), ±3) | min(2.3, vol/mc × max(0, 1 − (h−l)/**h**)) |
| C, LAVL | clamp(0.6v + 0.4d, ±5) | 0.6v + 0.4d, no clamp |
| C, bands | > 2.5 ALPHA RUSH · ≥ 0.5 STABLE · **< −1 LIQ TRAP** · else COMPRESS | > 2.5 ALPHA RUSH · ≥ 0.5 STABLE · else COMPRESS. **No LIQ TRAP**; price ≤ 0 or mc ≤ 0 → COMPRESS |
| Fallbacks | `high_24h \|\| 0`, `low_24h \|\| 0`, `chg \|\| 0` | `high_24h or price or 1`, `low_24h or price or 1`, `chg or 0.0` |

### A.2 Does the browser have every field? Yes

`_lavl_regime` reads `current_price`, `price_change_percentage_24h`, `high_24h`,
`low_24h`, `total_volume` and `market_cap`. `_conjunctive_gate` also reads
`fully_diluted_valuation`. All of them are in the `/coins/markets` payload the terminal
already fetches.

The port must read the **raw** row inside `build()`, before the mapped fields coerce
null to 0, so that the nightly's fallbacks are reproduced exactly. Python's `or` and
JS's `||` agree on null, 0 and missing.

### A.3 Names whose verdict changes

Method: the committed terminal (`207ea9e`) was driven in Chromium over two real,
complete payloads, and every row was compared with `_conjunctive_gate` on identical
inputs (234 non-stable names each). The two payloads are:
- the Sep 23 payload, captured at 23:33 UTC;
- the Sep 24 payload, captured at 13:30 UTC.

Neither is the nightly's own 11:5x UTC snapshot: the persisted cross-section has no 24h
high/low, so the nightly's exact gate input is not reconstructable.

| Payload | Terminal QUALIFIED | Nightly gate | Verdict changes (all false → true) | Legs disagreeing |
|---|---|---|---|---|
| Sep 23 23:33 | 2 | 9 | UNI, NEAR, PEPE, ARB, BONK, OP, WIF | A: 0 · B: 3 · C: 173 (bands differ on 177/234) |
| Sep 24 13:30 | 0 | 5 | NEAR, PENGU, BONK, OP, WIF | A: 0 · B: 0 · C: 174 (bands differ on 178/234) |

- **Every verdict change comes from leg C.** None goes the other way: the terminal's set
  is a strict subset of the nightly's.
- **Leg B's disagreements never change a verdict.** The three Sep 23 names already fail
  leg A.

### A.4 SPEC_HASH

- **Unchanged if only the browser changes.** No Python edit is needed.
  - `_lavl_regime` is captured and stays byte-identical.
  - `_conjunctive_gate` is not captured.
- **Placement:** put the JS port (`lavlRegime`, `conjunctiveGate`) **inside** the MODEL
  PORT block. The parity gate then executes it against Python on fixtures and on a real
  payload, and a future drift fails CI.

### A.5 Every surface that changes

**`index.html`**
- The gate in `build()` (the `gateA`/`gateB`/`gateC` lines).
- `computeLAVL` and its band set, including `lavlBandClass` (LIQ TRAP goes).
- The LAVL table's **Velo / Diverge / LAVL / Regime** columns. They must show the
  canonical regime, or the panel shows a regime that is not the one gating. Velocity
  becomes direction-free (\|chg\|), so the Velo column changes meaning; relabel it.
- The inspector's LAVL tip.
- `#skew` "QUALIFIED n/N", `#sys-skew` "Skew Qualified" and HIDE NON-QUALIFIED. The
  labels can stay; their definition changes.
- Alpha-map "qualified" highlight and label priority, which read `t.gated`.
- Rewound rows keep `gated:false`. Recorded rows carry no 24h change, so they read "not
  evaluable".
- The panel note added in PR #40 ("not reconciled") is removed.

**`methodology.html`**
- Line 141, the gate callout: drop proxy-ERA and LIQ TRAP, and state `_lavl_regime`'s
  formula.
- Line 445: keep it, now accurate.
- Line 448: the §6 sentence is removed.

**`nightly.py`**
- The `_conjunctive_gate` docstring (no hash impact).

**`docs/`**
- AUDIT-2026-09-23 §6.1 closed.
- ARCHITECTURE-NOTES §8, the gate description.

**Tests**
- New parity check `conjunctiveGate(js) == _conjunctive_gate(py)` over fixtures, edge
  cases (null high/low, zero volume, zero mc, FDV absent) and a committed payload.
- `test_funding_semantics.py`: `computeLAVL` moves or is renamed, so update the target
  of `test_lavl_carries_no_funding_term_at_all`.
- `test_terminal` / `test_render`: LAVL band assertions, if any.

### A.6 Recommendation

**A narrow terminal-parity PR.** QUALIFIED then means exactly the persisted nightly gate,
and the LAVL panel shows that same regime. Do not change the nightly rule.

Contrary evidence, stated so the decision is made knowingly: that gate constructs only
the **experimental** basket, and nothing canonical is gated. Parity is still the right
narrow fix, because today QUALIFIED names a set that no persisted artifact uses. The
only alternative is to relabel QUALIFIED as a screen, and that leaves two
definitions alive.

---

## B. The basket's funding omission

### B.1 Intent (history and docs)

- **Origin:** `build_basket`'s `score(t, None, btc)` arrived in `0818f29`, which is the
  repository's **root commit** (2026-08-20). It came in the same commit as
  `lavl_perp_mult` and the funding-aware `score()` call in `main()`.
- **No stated reason:** no commit message, code comment, doc or Method sentence gives a
  reason for excluding funding from basket construction.
- **What the documentation says:** the Method page says that funding and supply are "the
  two things that reach the score", and that the Index is ranked by conviction. It says
  nothing about the basket using a different score.
- **Conclusion:** there is no evidence the exclusion was intentional. The canonical
  Index already ranks with funding.

### B.2 Magnitude — full universe (the 8 cross-section nights, Sep 15–24)

This measures top 10 by published conviction against top 10 by funding-neutral
conviction:
- Sep 18–24: exact, from the recorded factors.
- Sep 15–17: derived from the published score and the recorded multiplier. The
  rounding-ambiguous rows all sit outside the top 10, except ZEC on 09-17 (93 or 94), which
  is in the top 10 either way.

| Night | Non-neutral rows | Top-10 membership change | Weight L1/2 | Top-20 rank moves | #8–#13 moves (canonical → neutral) |
|---|---|---|---|---|---|
| 09-15 | 8 | none | 0.00% | 3 | TRX 13→14, STONK 14→13 |
| 09-16 | 9 | none | 0.13% | 7 | — |
| 09-17 | 9 | none | 0.82% | 8 | PONS 12→14, DASH 13→12, BTC 15→13 |
| 09-18 | 4 | none | 0.69% | 6 | XMR 9→6, ARB 7→8, ENA 8→9, PONS 12→13, INJ 13→12 |
| 09-19 | 7 | none | 1.47% | 2 | — |
| 09-20 | 7 | none | 0.00% | 0 | — |
| 09-23 | 13 | none | 0.00% | 0 | — |
| 09-24 | 9 | none | 0.00% | 0 | — |

- **Entrants/ejections caused solely by funding: 0.**
- **Target-weight turnover between recorded nights:** canonical vs neutral differ by at
  most 0.8 pp per leg (e.g. 09-19→20: 1.77% vs 2.57%).
- **Forward return differences** (canonical − neutral, top-10 book, exact offsets):

| Horizon | Legs | Range |
|---|---|---|
| 1d | 6 | −3.7 to +14.9 bp |
| 3d | 4 | −9.0 to 0 bp |
| 7d | 2 | −10.3, −0.6 bp |

  These figures are **magnitudes, not evidence**, and are not a basis for the decision.

### B.3 Magnitude — before the cross-section (persisted top 50, Aug 1–Sep 14)

- 45 nights, 43 of them with a non-neutral multiplier on some persisted row.
- Top-10 membership differs on **8 nights**:
  - **Bounded exactly (5):** 08-05, 08-07, 08-12, 08-15, 09-09. On these, no name outside
    the top 50 could reach a neutral top 10, so the result holds for the full universe.
  - **Not bounded (3):** 08-02, 08-03, 08-04, the pre-hash era.
- **Example:** on 09-09, ARB and ENA sit in one book and ETH and PONS in the other.
- **Forward returns:** not honestly measurable for these nights. A neutral-only name can
  lack a price the next night, because only the top 50 are persisted.

### B.4 What "the basket" actually is today — a separate defect, also frozen

The experimental basket **cannot be reconstructed** from committed data, for two reasons:
- Its gate needs 24h high/low, which is persisted for the top 50 only.
- Its path depends on hysteresis state.

Its committed history also shows a construction defect larger than the funding
question. It has grown from 29 holdings (09-13) to **44** (09-24):
- a held name that is not in the gated set gets `rank = None`, so it is never ejected by
  rank;
- its `conviction` is refreshed only while it is in `top`. HYPE 90 and ZEC 100, for
  example, have been frozen since entry.

On both complete payloads the gated set was ≤ 10 names (9 and 5). On nights like
those, funding **cannot** change the basket's membership, only its weights and the
ejection-margin comparisons. Whether the gated set ever exceeded 10 on an earlier night
is **not reconstructable**, because the gate's inputs were not persisted for the full
universe.

### B.5 The two coherent architectures

**A — CANONICAL SCORE.** Funding is part of conviction everywhere.
- **Code:** `build_basket(markets, today, btc, perps_map)` calls
  `score(t, perps_map, btc)`, and `main()` passes the same `perps_map` `score()` used
  for the board. The gate is untouched: `_conjunctive_gate` ignores its `conv` argument.
- **SPEC_HASH:** unchanged. `score()` is unchanged and `build_basket` is not captured.
- **History:** `basket.json` and `index.csv` history stays valid as the record of what
  the experimental basket was, under the neutral score. The canonical Index is
  unaffected, because it already works this way.
- **Migration:** add `basket_score: "canonical"` and a `basket_score_since` date to
  `basket.json` and a column in `index.csv`. Never recompute past rows.
- **Tests:**
  - every basket holding's conviction equals the published conviction for that symbol
    and date;
  - `build_basket` receives the same `perps_map` object `score()` used;
  - the version marker is present.

**B — PORTFOLIO SCORE.** Funding intentionally does not affect basket construction.
- **Code:** name the construction score distinctly, e.g. `portfolio_score` = conviction
  with the funding multiplier fixed at 1.0.
  - `basket.json` holdings carry `portfolio_score`. The reader accepts the legacy
    `conviction` key for old rows.
  - `basket_note`, Method line 162 and the terminal copy must state that the basket is
    ranked by `portfolio_score`, not by the published conviction.
- **SPEC_HASH:** unchanged, unless you choose to capture `build_basket`. That choice
  would be a new segment.
- **History:** valid as it stands, because it was always this score, under the old name.
- **Migration:** a field rename with a back-compatible reader.
- **Tests:**
  - a key-rename test;
  - a test that no UI or Method sentence calls the basket's score "conviction";
  - a test that `portfolio_score == score(t, None, btc)`.

**The cleaner contract.** Judged as a contract rather than by returns, A is simpler:
- it is the rule the canonical Index already follows;
- it matches the Method page's "two things reach the score";
- B is coherent only if there is a reason funding should not drive construction, and
  none is recorded anywhere.

The empirical magnitude is small on the full-universe nights (no membership change, and
weights within 1.5%). It is non-trivial on the earlier nights: 5 bounded membership
changes in 45. Whichever you choose, the accretion defect in B.4 needs its own decision
first. A funding choice applied to a 44-name basket with frozen scores fixes the smaller
problem.

---

## C. Recommended next PRs

1. **Terminal qualification parity:**
   - port `_lavl_regime` / `_conjunctive_gate` into the MODEL PORT, with a parity test;
   - make the LAVL panel show the canonical regime;
   - update the copy.
   - `SPEC_HASH` is unchanged. The PR is narrow and ready once you approve it.
2. **Your decision, then one small PR:** basket score A or B, together with a decision
   on the basket's accretion and frozen-conviction defect (B.4). Options for the latter:
   fix the hysteresis rule, or retire the experimental basket in favour of the canonical
   Index.
3. **RWA month-sharding** (`docs/DESIGN-RWA-SHARDING.md`, frozen). Merge before
   2026-10-01, in a window with no nightly in flight.
