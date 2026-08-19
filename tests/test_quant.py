"""Modules G-J — the readings derived from bars this pipeline recorded itself.

The properties this file exists to hold:

  * An indicator that does not have enough history returns None and says how much it
    would need. It never returns a shorter reading wearing the longer label, which is
    the trap ``nightly.choppiness`` documents and the one a 14-period ADX computed over
    nine bars would fall straight into.
  * ``None`` is not zero, anywhere. A correlation over too few nights is unknown, not
    independent; a missing volume is unknown cost, not free; an unpublished FDV is an
    unknown overhang, not a fully circulating float. Every one of those collapses would
    flatter exactly the asset it should warn about.
  * A classifier that puts three quarters of its input in one bucket is not
    classifying. The trending divergence had that defect on a live board and the test
    that pins the fix is here.
  * Nothing in quant.py reaches score(). That is asserted rather than assumed, because
    the whole reason these live outside the specification hash is that they cannot move
    a published number.
"""
import importlib.util
import math
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


quant = _load("q_quant", "quant.py")
nightly = _load("q_nightly", "nightly.py")


def _bars(closes, spread=0.02):
    """Bars with a fixed relative range around each close."""
    return [{"high": c * (1 + spread), "low": c * (1 - spread), "close": c}
            for c in closes]


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------
def test_adx_needs_twice_its_period_and_says_so():
    """Wilder consumes the period twice — once for the DI, once to average the DX.

    A library call hides this and a 15-bar "14-period ADX" comes back looking fine. The
    count and the requirement travel with the null so the terminal can render
    "accumulating (9/29)" instead of an empty cell, which reads as a broken column.
    """
    r = quant.adx(_bars([100 + i for i in range(20)]))
    assert r["adx"] is None
    assert r["bars"] == 20
    assert r["needed"] == 29 == quant.ADX_MIN_BARS
    assert r["regime"] is None


def test_a_clean_uptrend_reads_as_a_strong_up_trend():
    r = quant.adx(_bars([100 * (1.02 ** i) for i in range(40)]))
    assert r["adx"] is not None and r["adx"] > quant.ADX_TRENDING
    assert r["plus_di"] > r["minus_di"]
    assert r["regime"] == "TRENDING UP"


def test_a_clean_downtrend_is_a_strong_trend_too():
    """ADX measures strength without direction. A screen that reads a high ADX as
    bullish is reading half the indicator, so the direction is asserted separately."""
    r = quant.adx(_bars([100 * (0.98 ** i) for i in range(40)]))
    assert r["adx"] > quant.ADX_TRENDING
    assert r["minus_di"] > r["plus_di"]
    assert r["regime"] == "TRENDING DOWN"


def test_a_flat_market_has_no_trend():
    r = quant.adx(_bars([100 + (1 if i % 2 else -1) for i in range(40)]))
    assert r["adx"] < quant.ADX_TRENDING
    assert r["regime"] in ("NO TREND", "EMERGING")


def test_a_missing_bar_field_refuses_rather_than_guesses():
    """A bar without its range is not a bar. Admitting it with the close substituted for
    high and low feeds a zero true range in and silently depresses every reading."""
    bars = _bars([100 + i for i in range(40)])
    bars[10]["low"] = None
    assert quant.adx(bars)["adx"] is None


def test_wilder_smoothing_is_not_an_ema():
    """The recursion is S = S - S/period + v, seeded with a sum. A standard EMA gives
    numbers close enough to look right and different enough to disagree with every other
    terminal on the desk."""
    vals = [1.0] * 20
    out = quant._wilder_smooth(vals, 14)
    assert out[0] == pytest.approx(14.0)          # seed is the SUM, not the mean
    assert out[1] == pytest.approx(14.0 - 1.0 + 1.0)
    assert len(out) == len(vals) - 14 + 1


# ---------------------------------------------------------------------------
# strategy selection
# ---------------------------------------------------------------------------
def test_no_history_is_not_stand_aside():
    """"Nothing is known" and "stand aside" are different instructions and only one of
    them is a view."""
    r = quant.strategy_for(None, {"adx": None})
    assert r["strategy"] is None
    assert "enough recorded bars" in r["basis"]


def test_the_indices_disagreeing_is_named_rather_than_arbitrated():
    r = quant.strategy_for("RANGE-BOUND", {"adx": 33.0, "regime": "TRENDING UP"})
    assert r["strategy"] == "STAND ASIDE"
    assert r["confidence"] == "conflicted"


def test_agreement_is_confirmed_and_one_leg_is_partial():
    both = quant.strategy_for("TRENDING", {"adx": 31.0, "regime": "TRENDING UP"})
    assert both["strategy"] == "TREND / ALPHA BASKET" and both["confidence"] == "confirmed"
    one = quant.strategy_for(None, {"adx": 31.0, "regime": "TRENDING UP"})
    assert one["strategy"] == "TREND / ALPHA BASKET" and one["confidence"] == "partial"


def test_a_directionless_market_selects_the_grid():
    r = quant.strategy_for("RANGE-BOUND", {"adx": 12.0, "regime": "NO TREND"})
    assert r["strategy"] == "GRID / RANGE HARVEST"


# ---------------------------------------------------------------------------
# correlation, beta, effective breadth
# ---------------------------------------------------------------------------
def test_too_few_observations_is_unknown_not_uncorrelated():
    a = [0.01, -0.02, 0.03]
    assert quant.pearson(a, a) is None
    assert quant.beta(a, a) is None


def test_a_constant_series_is_undefined_rather_than_independent():
    """A stale recorded price has no variance. Reporting that as r=0 would make the
    stalest name in the book look like its best diversifier."""
    flat = [0.0] * 12
    moves = [0.01 * (1 if i % 2 else -1) for i in range(12)]
    assert quant.pearson(flat, moves) is None


def test_identical_series_correlate_perfectly_and_beta_one():
    r = [0.01, -0.02, 0.03, 0.00, 0.015, -0.01, 0.02, -0.005, 0.01, 0.0]
    assert quant.pearson(r, r) == pytest.approx(1.0)
    assert quant.beta(r, r) == pytest.approx(1.0)


def test_a_doubled_series_has_beta_two_and_correlation_one():
    """Correlation and beta answer different questions. An asset that moves exactly
    twice as hard as the benchmark is perfectly correlated with it and twice as risky,
    and a book capped on the first alone would size it as though it were not."""
    r = [0.01, -0.02, 0.03, 0.00, 0.015, -0.01, 0.02, -0.005, 0.01, 0.0]
    d = [2 * x for x in r]
    assert quant.pearson(d, r) == pytest.approx(1.0)
    assert quant.beta(d, r) == pytest.approx(2.0)


def test_log_returns_skip_unusable_pairs_rather_than_zero_them():
    assert quant.log_returns([100, 0, 110]) == []
    assert quant.log_returns([100, None, 110]) == []
    out = quant.log_returns([100, 110])
    assert len(out) == 1 and out[0] == pytest.approx(math.log(1.1))


def test_a_perfectly_correlated_book_is_one_bet():
    """The number the whole panel exists to produce. Fifteen names that move together
    are not fifteen positions, and every per-position risk statistic computed as though
    they were is wrong in the same direction."""
    base = [100 * (1.01 ** i) for i in range(20)]
    rep = quant.correlation_report({f"C{i}": list(base) for i in range(5)},
                                   benchmark="C0")
    assert rep["mean_correlation"] == pytest.approx(1.0)
    assert rep["effective_n"] == pytest.approx(1.0, abs=0.01)


def test_the_benchmark_correlates_with_itself_by_construction():
    base = [100 * (1.01 ** i) for i in range(20)]
    rep = quant.correlation_report({"BTC": base, "ALT": base})
    assert rep["matrix"]["BTC"]["BTC"] == 1.0
    # ...and is not given a beta to itself, which would be a tautology in a column of
    # readings about other assets.
    assert "BTC" not in rep["beta_to_btc"]


def test_a_cluster_is_flagged_symmetrically():
    base = [100 * (1.01 ** i) for i in range(20)]
    rep = quant.correlation_report({"A": base, "B": list(base),
                                    "C": [100 - i for i in range(20)]})
    assert "B" in rep["clusters"]["A"] and "A" in rep["clusters"]["B"]


# ---------------------------------------------------------------------------
# trending divergence
# ---------------------------------------------------------------------------
def _trending(order):
    return {"coins": {s: {"rank": i + 1, "name": s, "mcap": 1e9, "chg24h": 1.0}
                      for i, s in enumerate(order)}}


def test_the_rankings_are_compared_within_their_overlap():
    """The defect this replaced: ranking each side against its own population.

    The trending list is a top-15 slice and the board is the whole universe, so their
    percentiles do not share a scale. On a live 234-name board that put eleven of
    fifteen names in one bucket. Here, an ordering the model exactly agrees with must
    produce zero divergence for every name — which the old method did not.
    """
    order = ["A", "B", "C", "D"]
    conv = {"A": 90, "B": 80, "C": 70, "D": 60}
    # ...plus a long tail the model ranks below all of them, which is what used to skew
    # the conviction percentiles away from the trending ones.
    conv.update({f"Z{i}": 10 for i in range(46)})
    rep = quant.trending_divergence(_trending(order), conv)
    assert rep["n_overlap"] == 4
    for a in rep["assets"]:
        assert a["divergence"] == pytest.approx(0.0)
        assert a["label"] == "ALIGNED"


def test_a_reversed_ranking_splits_the_labels():
    """Perfect disagreement must produce both labels, not one. The name the crowd ranks
    first and the model ranks last is crowded; the reverse is quiet."""
    order = ["A", "B", "C", "D"]
    conv = {"A": 10, "B": 20, "C": 30, "D": 40}
    rep = quant.trending_divergence(_trending(order), conv)
    by = {a["symbol"]: a for a in rep["assets"]}
    assert by["A"]["label"] == "FOMO_CROWDED"
    assert by["D"]["label"] == "QUIET_ACCUMULATION"


def test_a_trending_coin_the_model_does_not_rank_is_reported_not_dropped():
    """Most trending coins fall below the universe's market-cap cut. "The crowd is
    looking at something this model does not rank" is itself an answer."""
    rep = quant.trending_divergence(_trending(["A", "GHOST"]), {"A": 90, "B": 50})
    ghost = next(a for a in rep["assets"] if a["symbol"] == "GHOST")
    assert ghost["label"] == "UNRANKED"
    assert ghost["conviction"] is None and ghost["divergence"] is None
    # Same keys on every row, so a consumer never tests for a key to learn what a row is.
    assert "overlap_conv_pct" in ghost


def test_a_single_overlapping_name_has_no_ordering_to_disagree_about():
    rep = quant.trending_divergence(_trending(["A", "GHOST"]), {"A": 90})
    a = next(x for x in rep["assets"] if x["symbol"] == "A")
    assert a["divergence"] is None


def test_backed_but_unsearched_is_the_other_direction():
    rep = quant.trending_divergence(_trending(["A"]), {"A": 90, "QUIET": 88})
    assert [q["symbol"] for q in rep["backed_but_unsearched"]] == ["QUIET"]


def test_no_feed_is_reported_as_no_overlap_rather_than_an_empty_market():
    rep = quant.trending_divergence({}, {"A": 90})
    assert rep["assets"] == [] and "no trending feed" in rep["detail"]


# ---------------------------------------------------------------------------
# sector rotation
# ---------------------------------------------------------------------------
def test_relative_strength_is_against_the_whole_market():
    """The absolute number mostly answers "did Bitcoin go up". The relative one answers
    "did capital move here", which is the only question a rotation matrix is for."""
    rep = quant.sector_rotation({"ai": {"name": "AI", "chg24h": 12.0, "mcap": 1e10}},
                                [], market_chg_24h=8.0)
    assert rep["sectors"][0]["rs24h"] == pytest.approx(4.0)


def test_no_benchmark_degrades_rather_than_fabricates_one():
    rep = quant.sector_rotation({"ai": {"name": "AI", "chg24h": 12.0, "mcap": 1e10}},
                                [], market_chg_24h=None)
    assert rep["sectors"][0]["rs24h"] is None
    assert rep["sectors"][0]["chg24h"] == 12.0
    assert "no market benchmark" in rep["basis"]


def test_a_multi_day_flow_is_not_claimed_on_the_first_night():
    """/coins/categories publishes a 24h column and no history. A 7d rotation therefore
    cannot be fetched, only accumulated — and a 7d figure taken on night one is a 24h
    figure with a longer label on it."""
    rep = quant.sector_rotation({"ai": {"name": "AI", "chg24h": 5.0, "mcap": 1.1e10}}, [])
    assert rep["sectors"][0]["flow_pct"] is None
    assert rep["sectors"][0]["flow_days"] == 0


def test_the_flow_uses_the_oldest_row_inside_the_window():
    hist = [{"date": f"2026-08-{d:02d}", "category_id": "ai", "mcap": 1.0e10}
            for d in range(1, 8)]
    rep = quant.sector_rotation({"ai": {"name": "AI", "chg24h": 1.0, "mcap": 1.2e10}},
                                hist, lookback_days=7)
    assert rep["sectors"][0]["flow_pct"] == pytest.approx(20.0)
    assert rep["sectors"][0]["flow_days"] == 7


# ---------------------------------------------------------------------------
# stablecoin regime
# ---------------------------------------------------------------------------
def test_the_regime_needs_both_legs():
    """Velocity alone cannot tell new dollars from the same dollars moving faster, and
    float growth alone cannot tell deployed capital from parked capital."""
    hot_and_growing = quant.stablecoin_regime(0.30, 1.5, 7)
    assert hot_and_growing["regime"] == "RISK-ON"
    parked = quant.stablecoin_regime(0.05, 1.5, 7)
    assert parked["regime"] == "CAPITAL PARKED"
    rotating = quant.stablecoin_regime(0.30, -1.5, 7)
    assert rotating["regime"] == "ROTATION"


def test_no_prior_night_is_unconfirmed_not_risk_off():
    """Treating "no history" as "no growth" would print RISK-OFF on the first night of
    every deployment."""
    r = quant.stablecoin_regime(0.30, None, 0)
    assert r["regime"] == "UNCONFIRMED"
    assert "no prior night" in r["basis"]


def test_no_feed_at_all_is_none():
    assert quant.stablecoin_regime(None, 1.0, 7)["regime"] is None


# ---------------------------------------------------------------------------
# fallen kings
# ---------------------------------------------------------------------------
def _king(closes, rank=5, rsi=35.0):
    return [{"date": f"2026-08-{i+1:02d}", "close": c, "rank": rank,
             "rsi7": rsi} for i, c in enumerate(closes)]


def test_a_quality_name_in_a_real_drawdown_qualifies():
    closes = [100] * 10 + [95, 90, 87, 85, 82]
    out = quant.fallen_kings({"SOL": _king(closes)})
    assert len(out) == 1
    assert out[0]["symbol"] == "SOL"
    assert out[0]["drawdown_pct"] == pytest.approx(-18.0)
    assert out[0]["peak_from_bars"] == len(closes)


def test_a_name_that_only_just_became_large_is_not_a_fallen_king():
    """Today's rank is not enough. A token that fell INTO the top 25 this week is a
    mover, not a king."""
    seq = _king([100] * 10 + [95, 90, 87, 85, 82])
    for r in seq[:12]:
        r["rank"] = 400
    assert quant.fallen_kings({"NEW": seq}) == []


def test_a_shallow_dip_and_a_collapse_are_both_excluded():
    shallow = _king([100] * 10 + [99, 98, 97, 96, 95])       # -5%
    collapse = _king([100] * 10 + [80, 60, 50, 45, 40])      # -60%
    assert quant.fallen_kings({"A": shallow}) == []
    assert quant.fallen_kings({"B": collapse}) == []


def test_exhaustion_is_required_not_just_drawdown():
    """A drawdown screen with no exhaustion test is a screen for things going down."""
    seq = _king([100] * 10 + [95, 90, 87, 85, 82], rsi=70.0)
    assert quant.fallen_kings({"C": seq}) == []


def test_a_divergence_outranks_a_bare_oversold_reading():
    """An oversold reading is a condition; a divergence is an event, and the sort puts
    events first."""
    plain = _king([100] * 10 + [95, 90, 87, 85, 82], rsi=30.0)
    diverging = [dict(r) for r in _king([100] * 10 + [95, 88, 84, 83, 82])]
    for r, v in zip(diverging[10:], [55.0, 20.0, 25.0, 30.0, 45.0]):
        r["rsi7"] = v
    out = quant.fallen_kings({"PLAIN": plain, "DIV": diverging})
    assert out[0]["symbol"] == "DIV" and out[0]["divergence"] is True


def test_too_short_a_history_is_skipped():
    assert quant.fallen_kings({"X": _king([100, 95, 90])}) == []


# ---------------------------------------------------------------------------
# execution drag
# ---------------------------------------------------------------------------
def test_no_volume_is_unknown_cost_not_free():
    """A sizer that assumes infinite liquidity for a token it has no volume for sizes
    the least liquid names on the board the largest, which is precisely backwards."""
    d = quant.execution_drag(100_000, None, 4.0)
    assert d["total_bps"] is None and d["impact_bps"] is None
    assert "unknown, not zero" in d["basis"]


def test_no_volatility_reports_the_spread_as_a_floor():
    d = quant.execution_drag(100_000, 1e8, None)
    assert d["impact_bps"] is None and d["estimate"] is False
    assert d["spread_bps"] == quant.DEFAULT_SPREAD_BPS
    assert d["days_to_exit"] is not None      # the liquidity constraint is still known


def test_impact_grows_with_the_square_root_of_participation():
    """The well-established half of the model. Quadrupling the order doubles the
    impact — anything linear would be a different claim about market microstructure."""
    small = quant.execution_drag(1_000_000, 1e9, 4.0)
    big = quant.execution_drag(4_000_000, 1e9, 4.0)
    # Tolerance covers the published rounding, not the model: impact_bps is reported to
    # one decimal, so 12.649 prints as 12.6 while its exact double prints as 25.3.
    # Pinning to 1e-6 would be pinning the rounding, and the rounding is presentation.
    assert big["impact_bps"] == pytest.approx(2 * small["impact_bps"], abs=0.11)
    # The shape itself, checked without rounding in the way: sixteen times the order is
    # four times the impact, which linear impact could not produce.
    huge = quant.execution_drag(16_000_000, 1e9, 4.0)
    assert huge["impact_bps"] == pytest.approx(4 * small["impact_bps"], abs=0.21)


def test_days_to_exit_scales_with_the_participation_cap():
    slow = quant.execution_drag(1e8, 1e9, 4.0, participation_pct=10.0)
    fast = quant.execution_drag(1e8, 1e9, 4.0, participation_pct=20.0)
    assert slow["days_to_exit"] == pytest.approx(2 * fast["days_to_exit"])


def test_atr_needs_its_period_plus_one_bar():
    assert quant.atr(_bars([100 + i for i in range(10)])) is None
    assert quant.atr(_bars([100 + i for i in range(20)])) is not None


# ---------------------------------------------------------------------------
# liquidity shock
# ---------------------------------------------------------------------------
def test_a_collapse_against_the_assets_own_baseline_is_the_event():
    """A cross-sectional liquidity screen flags the same illiquid names every night,
    which is a list of facts about the universe rather than news."""
    base = [40.0, 41.0, 39.0, 40.5, 40.2, 39.8, 40.1, 40.0]
    r = quant.liquidity_shock(base, 4.0)
    assert r["shock"] is True and r["z"] < quant.LIQ_SHOCK_Z


def test_a_normal_night_is_not_a_shock():
    base = [40.0, 41.0, 39.0, 40.5, 40.2, 39.8, 40.1, 40.0]
    assert quant.liquidity_shock(base, 39.5)["shock"] is False


def test_a_baseline_with_no_dispersion_is_undefined_not_extreme():
    """A constant history gives an infinite z-score and reports every small change as a
    crisis."""
    r = quant.liquidity_shock([40.0] * 10, 39.0)
    assert r["z"] is None and r["shock"] is False
    assert "no dispersion" in r["basis"]


def test_too_short_a_baseline_says_what_it_needs():
    r = quant.liquidity_shock([40.0, 41.0], 20.0)
    assert r["z"] is None
    assert r["needed"] == quant.LIQ_SHOCK_MIN_OBS
    assert f"{quant.LIQ_SHOCK_MIN_OBS}" in r["basis"]


# ---------------------------------------------------------------------------
# the boundary
# ---------------------------------------------------------------------------
def test_nothing_in_quant_reaches_the_specification():
    """The whole reason these live outside the hash is that they cannot move a published
    score. Asserted against the captured names rather than trusted to stay true.
    """
    captured = set(nightly.SPEC_FUNCTIONS) | set(nightly.SPEC_FUNDING_FUNCTIONS)
    assert not captured & set(dir(quant))


def test_quant_touches_neither_the_network_nor_the_disk():
    """Every function here is a pure function over recorded data. That is the design
    constraint that makes them testable with a list, and it is worth pinning: an import
    added later would make this file a integration suite without anyone deciding to."""
    src = (ROOT / "quant.py").read_text(encoding="utf-8")
    for banned in ("import urllib", "import requests", "open(", "Path(", "import os"):
        assert banned not in src, f"quant.py should not contain {banned!r}"


# ---------------------------------------------------------------------------
# live ledger — the regression that matters
# ---------------------------------------------------------------------------
def test_the_live_ledger_produces_a_correlation_reading():
    """The recurring failure mode in this repo is a feature that reports nothing
    forever. The ledger has enough closes for a correlation today, so this must not be
    another empty panel."""
    series = nightly._series_from_ledger()
    top = sorted(series["closes"], key=lambda s: -len(series["closes"][s]))[:15]
    rep = quant.correlation_report({s: series["closes"][s] for s in top})
    assert rep["effective_n"] is not None, "correlation is empty on the real ledger"
    assert 1.0 <= rep["effective_n"] <= rep["n"]


def test_the_live_ledger_reports_adx_as_accumulating_not_broken():
    """high_24h/low_24h were appended partway through, so the bar series is shorter than
    the close series and ADX genuinely cannot be computed yet. What must hold is that it
    says so with a count rather than returning a number over too few bars."""
    series = nightly._series_from_ledger()
    trend = nightly._trend_structure(series)
    assert trend, "no symbol has any recorded bars at all"
    for sym, rec in trend.items():
        if rec["adx"] is None:
            assert rec["bars"] < rec["needed"]
        else:
            assert rec["bars"] >= rec["needed"]
