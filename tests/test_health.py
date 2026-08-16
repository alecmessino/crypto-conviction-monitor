"""The model-health ribbon: is tonight's board a trend or a twitch?

Stickiness is the share of last night's top cohort still in tonight's. It answers the
question a holder actually has — "would I have been churning this book" — which rank
correlation across the whole universe does not: a board can reorder its tail violently
while the top ten sit still, and score a low correlation for movement nobody would have
traded.

The thing these tests guard hardest is the labelling. "Persistence 88.4% (30D)" is the
badge a terminal naturally writes, and here it would be a fabrication twice over: there
are eleven nights, not thirty, and the colour cannot be graded against a historical
norm because eleven nights is not a history to average.
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("health_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

DATES = ["2026-03-01", "2026-03-02", "2026-03-03"]


def series(**names):
    return {sym: [(d, c) for d, c in zip(DATES, vals) if c is not None]
            for sym, vals in names.items()}


def board(top, rest, n_rest=12):
    """`top` names scoring high, `n_rest` filler below them."""
    out = {s: [90 - i] * len(DATES) for i, s in enumerate(top)}
    out.update({f"F{i:02d}": [30] * len(DATES) for i in range(n_rest)})
    return out


# ---------------------------------------------------------------------------
# stickiness
# ---------------------------------------------------------------------------
def test_an_unchanged_top_book_is_fully_sticky():
    s = series(**board([f"T{i}" for i in range(10)], None))
    h = nightly._model_health(s, DATES, None)
    assert h["stickiness"] == 1.0
    assert h["latest"]["retained"] == 10 and h["latest"]["entered"] == []


def test_a_completely_reordered_top_book_is_not_sticky():
    """Ten names swapped out for ten others is the twitch case the ribbon exists to
    catch before capital is allocated."""
    names = {f"A{i}": [90 - i, 10, 10] for i in range(10)}
    names.update({f"B{i}": [10, 90 - i, 90 - i] for i in range(10)})
    h = nightly._model_health(series(**names), DATES, None)
    assert h["stickiness"] == pytest.approx(0.5)      # one clean pair, one churned
    assert h["latest"]["retained"] == 10              # the second pair is stable again


def test_stickiness_measures_the_top_cohort_not_the_whole_universe():
    """A board can churn its tail violently while the top sits still. Rank correlation
    scores that as instability; a holder of the top book would have traded nothing."""
    names = {f"T{i}": [90 - i] * 3 for i in range(10)}
    # The tail reverses completely every night.
    for i in range(12):
        names[f"F{i:02d}"] = [10 + i, 40 - i, 10 + i]
    h = nightly._model_health(series(**names), DATES, None)
    assert h["stickiness"] == 1.0


def test_a_single_night_cannot_produce_a_stickiness():
    h = nightly._model_health(series(A=[90]), ["2026-03-01"], None)
    assert h["stickiness"] is None and h["pairs"] == 0
    assert h["latest"] is None


def test_a_board_thinner_than_the_cohort_is_skipped_not_padded():
    """Retention out of ten when only four names exist would read as 40% churn that
    never happened."""
    s = series(A=[90] * 3, B=[80] * 3, C=[70] * 3)
    assert nightly._model_health(s, DATES, None)["stickiness"] is None


# ---------------------------------------------------------------------------
# the window is stated, never implied
# ---------------------------------------------------------------------------
def test_the_window_and_pair_count_travel_with_the_number():
    """88.4% over thirty nights and 88.4% over two are different claims, and the badge
    renders whichever it is given."""
    h = nightly._model_health(series(**board([f"T{i}" for i in range(10)], None)), DATES, None)
    assert h["window"] == 3 and h["pairs"] == 2 and h["cohort"] == 10


def test_the_threshold_is_a_stated_convention_not_a_historical_norm():
    """There is no history to average yet. A colour graded against "normal for this
    model" would invent the baseline it claims to measure against."""
    h = nightly._model_health(series(**board([f"T{i}" for i in range(10)], None)), DATES, None)
    assert h["sticky_warn"] == nightly.HEALTH_STICKY_WARN
    assert "historical norm" in h["basis"]


# ---------------------------------------------------------------------------
# flips
# ---------------------------------------------------------------------------
def diff(changed):
    return {"pending": False, "changed": changed}


def test_a_promotion_into_buy_is_counted_and_named():
    """The counter is only useful if it can take you to the names."""
    h = nightly._model_health({}, [], diff([
        {"symbol": "AAA", "from_tier": "HOLD", "to_tier": "BUY"},
        {"symbol": "BBB", "from_tier": "WATCH", "to_tier": "STRONG"},
    ]))
    assert h["flips"]["into_buy"] == ["AAA", "BBB"]
    assert h["flips"]["into_strong"] == ["BBB"]


def test_a_demotion_out_of_buy_is_counted_separately():
    h = nightly._model_health({}, [], diff([
        {"symbol": "CCC", "from_tier": "BUY", "to_tier": "HOLD"},
    ]))
    assert h["flips"]["out_of_buy"] == ["CCC"]
    assert h["flips"]["into_buy"] == []


def test_movement_that_does_not_cross_the_buy_line_is_neither():
    """AVOID to WATCH is a real change and belongs in the change feed, but it is not a
    flip into or out of the book."""
    h = nightly._model_health({}, [], diff([
        {"symbol": "DDD", "from_tier": "AVOID", "to_tier": "WATCH"},
    ]))
    assert h["flips"]["into_buy"] == [] and h["flips"]["out_of_buy"] == []


def test_a_pending_diff_reports_pending_rather_than_zero():
    """Zero flips and no comparison available are different states; showing both as 0
    would say the board was quiet on a night nothing was measured."""
    h = nightly._model_health({}, [], {"pending": True})
    assert h["flips"]["pending"] is True


# ---------------------------------------------------------------------------
# live data
# ---------------------------------------------------------------------------
def test_the_live_ledger_produces_a_usable_ribbon():
    h = nightly._compute_market_breadth()["health"]
    assert h["stickiness"] is not None and h["pairs"] > 1
    assert h["window"] >= h["pairs"]


def test_the_ribbon_does_not_touch_the_specification():
    # d600984ec00b -> e65f7dc59d55: the funding regime rewrite of lavl_perp_mult, which
    # is a scoring change and correctly broke this pin. See tests/test_perps.py.
    assert nightly.SPEC_HASH == "e65f7dc59d55"
    for fn in nightly.spec()["functions"].values():
        assert "_model_health" not in fn
