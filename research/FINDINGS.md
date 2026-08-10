# Structural yield harvesting — feasibility measurement (crypto)

A spike against the four-pillar thesis: cointegration, volatility risk premium,
volatility harvesting, microstructure overreaction. Everything below is measured on
keyless data (CoinGecko daily closes, Deribit DVOL) over the 365 days to 2026-08-10.
Nothing here touches `score()` or the published conviction ledger.

The sibling equity spike is at `equity-conviction-monitor/research/FINDINGS.md`; where
the two disagree it is noted, because the disagreements are the informative part.

| Pillar | Verdict | The number that decided it |
|---|---|---|
| 1. Cointegration | **Two real pairs — and not the one the thesis names** | 45 pairs tested, 4 passed at 5% (2.2 expected by chance), **2 survive FDR correction**. BTC/ETH fails at ADF −2.79. |
| 2. VRP | **Real, persistent, and better measured here than in equities** | DVOL vs forward 30-day RV: positive on **68.7%** of 335 windows (BTC), median +6.9 vol points. Worst window: **−44.5 points**. |
| 3. Vol harvesting | **Theorem exact; the claim built on it is false** | γ* = 5.56%/yr. Rebalanced/geometric = 1.0573 vs predicted 1.0570. Rebalanced/buy-and-hold = **0.9918**. |
| 4. Microstructure | **Actively negative over this sample** | 29 three-sigma events; pooled forward 5-day return **−6.02%**, win rate **41%**. |

---

## Pillar 1 — Two pairs survive, BTC/ETH is not one of them

The thesis names BTC/ETH as the canonical crypto pair. Measured over 365 daily closes
it is **not cointegrated**: ADF −2.79 against a 5% critical value of −3.34.

Searching all 45 pairs of the top 10 returns 4 passers at the nominal 5% level, against
2.2 expected from noise alone. Applying Benjamini-Hochberg across the search tightens the
cutoff to −4.27, and two survive with positive hedge ratios and actionable half-lives:

```
DOT/XRP   adf −4.46   beta 1.51   half-life 6.1 d   z +0.11   (at equilibrium)
LTC/XRP   adf −4.39   beta 0.97   half-life 5.9 d   z +2.18   (at entry threshold)
```

This is a genuinely better result than the equity side, where the same procedure left
**zero** survivors out of 136 pairs and where not one economically motivated pair
(V/MA, KO/PEP, XOM/CVX, GS/MS) came close.

Two caveats that must travel with the result. Both survivors contain XRP, so the tests
are not independent and BH — which assumes near-independence — is optimistic here; a
common-factor check belongs in front of any capital. And a six-day half-life against a
nightly job means roughly six observations per reversion cycle, which is thin.

## Pillar 2 — The strongest pillar, and crypto measures it better than equities

Deribit publishes DVOL, a genuine implied-volatility index for BTC and ETH, keyless,
with two years of history. That makes crypto VRP directly observable, where the equity
side has to reconstruct per-name IV from option chains.

Measured correctly — DVOL on day *t* against volatility realised over the **next** 30
days, not the previous 30:

```
              mean     median   positive   worst
BTC          +2.7 pts  +6.9 pts   68.7%    −44.5 pts
ETH          +3.5 pts  +7.8 pts   65.1%    −51.4 pts
                                          (335 overlapping windows each)
```

The median is far above the mean, which is the signature of exactly what this trade is:
a large number of small wins against a small number of very large losses. The worst
single window cost 44 vol points on BTC — more than five times the median gain, and
more than the entire current spread.

Today the premium is wide: DVOL 36.1% against 30-day realised 28.2%, a spread of **+7.9
points**. (The equity index premium is simultaneously near its lows at +0.9 points.)

**Coverage limit, stated plainly:** DVOL exists for BTC and ETH only. There is no
implied-vol index for the other 23 names in this universe, and none is obtainable
keylessly. Pillar 2 covers two assets, not the top 25, and claiming otherwise would be
the same category of error as fabricating a Dune column.

## Pillar 3 — The theorem holds exactly; rebalancing still lost

Fernholz's excess growth rate on the equal-weight top-10 basket, 365 days:

```
gamma* (excess growth)                 0.0556 / yr
rebalanced / weighted-avg-log-growth     1.0573   (theorem predicts 1.0570)
rebalanced / buy-and-hold                0.9918
turnover                                 2.16x notional / yr
```

The identity holds to four decimals — the 5.6% harvest is real and genuinely captured.
**And daily rebalancing still lost 0.82% to doing nothing.**

Both are true because the theorem's benchmark is the *weighted average of the
constituents' log growth rates*, not buy-and-hold. Buy-and-hold lets winners compound
their weight; constant-weight rebalancing systematically trims them. Over this window
BNB ended at 0.745× while the basket median ended near 0.39×, and trimming BNB all the
way down cost more than the harvest paid.

So the thesis's claim — "compounding geometric returns independently of macro
direction" — is not supported. The direction and dispersion of the constituents decide
whether the harvest turns into outperformance. Note also that this was not the regime
the thesis targets: the basket fell 60% over the year, which is a downtrend, not chop.

Costs compound the problem: 2.16× annual turnover at 10 bps round-trip is 22 bps of
drag against a 5.6% gross harvest; at 50 bps most of it is gone. `rebalance_backtest()`
takes a `cost_bps` argument for this reason and reports turnover alongside every result.

## Pillar 4 — Negative over this sample

Three-sigma down-days against a trailing 60-day window: **29 events across 10 symbols in
365 days**. Pooled forward 5-day return: **−6.02%, win rate 41%**.

Buying multi-sigma crypto dips over the past year did not revert — it kept falling. This
is the "resilience of top-tier assets" assumption failing empirically over a downtrend,
and it is the clearest illustration of why the regime gate below is not optional. The
equity side over two years was a coin flip (−0.15%, 52%); crypto over one year was
squarely negative.

The pillar may still be real over a longer horizon or with better conditioning
(liquidity, absence of a protocol event, sector-relative moves). This sample says only
that the naive version loses money, which is enough to keep it out of production.

---

## The structural point: these are not four independent edges

Pairs trading loses when a spread trends. Premium selling loses when the market gaps.
Constant-weight rebalancing loses when a constituent trends to zero. Dip-buying loses
when the dip was information. **All four are short convexity — four expressions of one
trade: sell insurance, collect premium, lose in the tail.**

This sample shows them failing together, which is the point: the same 365 days that
produced a −44.5 vol-point VRP window also produced the −6.02% dip-buying result and
the drawdown that made rebalancing lose to buy-and-hold. Sizing these as four
diversifying edges is the mechanism by which this strategy class blows up.

`structural_yield.regime_ok()` therefore gates all four on one shared pair of
conditions — premium above a floor, trend strength below a ceiling. It does not remove
the tail risk; that risk *is* the premium. It refuses to add exposure when the
environment already says the tail is opening. Measured at the time of writing:

```
BTC trend strength (60d)   0.007      pure chop
BTC VRP spread            +7.9 pts    wide
regime                     OK
```

## Why this is not wired into `score()`

`nightly.py` derives a `spec_hash()` from the source of every scoring function so the
signal history is segmentable, and the ledger is append-only for the same reason. These
pillars are orthogonal to conviction — market-neutral yield extraction, not
cross-sectional ranking of what to own — and folding them in would silently re-base
every night already recorded.

The recommendation is a **parallel engine**: its own ledger, its own spec hash, its own
track record, sharing only the price fetch and the regime gate. The one new data
dependency is Deribit DVOL, which is keyless and needs no secret.

## Reproducing

```bash
python3 -m pytest tests/test_structural_yield.py -q    # 18 property + trap tests
python3 research/probe.py                              # re-measures everything above
```

`probe.py` sleeps between CoinGecko calls; a full run takes about a minute.
