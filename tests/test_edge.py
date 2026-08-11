"""Does conviction predict the next day's return?

This is the only question that decides whether the score is worth acting on, and it is
a different question from "is the basket beating the benchmark". The distinction is the
reason this module exists: the basket IS losing to equal weight — about -283bp over the
six legs since 2026-08-05 — and the obvious reading of that is "the selection is
subtracting value". The measurement does not support that reading. The information
coefficient over the same legs is +0.006 with a 95% interval of roughly [-0.09, +0.10].

A concentrated book with no measurable edge underperforms an equal-weight control as a
matter of course, because concentration adds variance without adding expected return.
So the tests below pin two things above all: that a null result is reported as a null
rather than as a small positive, and that the per-name attribution can never be mistaken
for evidence.
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("edge_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


def board(date, ranking, ret=None):
    """One night: {symbol: {conviction, price}} with an optional forward return applied."""
    out = {}
    for i, sym in enumerate(ranking):
        px = 100.0 * (1.0 + (ret or {}).get(sym, 0.0))
        out[sym] = {"symbol": sym, "conviction": float(100 - i * 2), "price": px}
    return out


def two_nights(ret):
    syms = [f"S{i:02d}" for i in range(20)]
    return {"2026-03-01": board("2026-03-01", syms),
            "2026-03-02": board("2026-03-02", syms, ret)}


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------
def test_a_perfectly_predictive_score_scores_ic_one():
    """Highest conviction gets the best return, monotonically."""
    syms = [f"S{i:02d}" for i in range(20)]
    ret = {s: (20 - i) / 100.0 for i, s in enumerate(syms)}
    legs = nightly._edge_legs(two_nights(ret), None)
    assert legs[0]["ic"] == pytest.approx(1.0)
    assert legs[0]["spread_bp"] > 0


def test_an_inverted_score_scores_ic_minus_one():
    """Worth a test of its own: an inverted ranking is a usable signal read backwards,
    and it must never be reported as 'no relationship'."""
    syms = [f"S{i:02d}" for i in range(20)]
    ret = {s: i / 100.0 for i, s in enumerate(syms)}
    assert nightly._edge_legs(two_nights(ret), None)[0]["ic"] == pytest.approx(-1.0)


def test_a_thin_board_is_skipped_rather_than_measured():
    """A rank correlation over a handful of names is noise with a decimal point."""
    thin = {"2026-03-01": board("2026-03-01", ["A", "B", "C"]),
            "2026-03-02": board("2026-03-02", ["A", "B", "C"], {"A": 0.1})}
    assert nightly._edge_legs(thin, None) == []


def test_legs_before_a_specification_boundary_are_excluded():
    """An IC averaged across two scoring functions is a number about a model that never
    existed."""
    syms = [f"S{i:02d}" for i in range(20)]
    days = {d: board(d, syms) for d in ("2026-03-01", "2026-03-02", "2026-03-03")}
    assert len(nightly._edge_legs(days, None)) == 2
    assert len(nightly._edge_legs(days, "2026-03-02")) == 1


# ---------------------------------------------------------------------------
# reporting a null as a null
# ---------------------------------------------------------------------------
def test_the_live_ledger_reports_no_measurable_edge():
    """The regression that matters. If this ever starts claiming a measurable edge on a
    handful of legs, the interval logic has broken."""
    e = nightly._compute_edge()
    assert e["measurable"] is False
    assert e["ci"][0] < 0 < e["ci"][1], "the interval must span zero on this data"
    assert "neither evidence" in e["verdict"]


def test_a_small_positive_mean_is_not_reported_as_an_edge():
    """+0.006 quoted alone reads as 'slightly positive'. With six legs the honest
    statement is 'cannot be distinguished from nothing', and the interval is what makes
    the difference visible."""
    e = nightly._compute_edge()
    assert e["mean_ic"] is not None
    assert abs(e["t_stat"]) < 2
    assert e["legs"] < e["min_legs"]


def test_the_sample_size_still_needed_is_stated():
    """Without it, 'not measurable yet' is indistinguishable from 'never will be'."""
    e = nightly._compute_edge()
    needed = e["legs_needed"]
    assert set(needed) == {"0.02", "0.03", "0.05"}
    # A weaker signal needs more history, always.
    assert needed["0.02"] > needed["0.03"] > needed["0.05"]


def test_too_few_legs_measures_nothing_at_all():
    by_date = {"2026-03-01": board("2026-03-01", [f"S{i:02d}" for i in range(20)])}
    assert nightly._edge_legs(by_date, None) == []


# ---------------------------------------------------------------------------
# attribution is arithmetic, not evidence
# ---------------------------------------------------------------------------
def test_the_attribution_reconciles_to_the_realised_gap():
    """It adds up by construction, which is exactly why it is seductive and exactly why
    it carries a label."""
    e = nightly._compute_edge()
    a = e["attribution"]
    gap = (e["book_total"] or 0) - (e["equal_weight_total"] or 0)
    assert a["total_bp"] == pytest.approx(gap * 100, abs=15)


def test_attribution_separates_a_selection_error_from_an_omission():
    """Different mistakes with different remedies: an overweight name that fell is the
    model picking badly, an underweight name that rose is the model not looking."""
    a = nightly._compute_edge()["attribution"]
    stances = {d["stance"] for d in a["detractors"]}
    assert stances <= {"overweight", "underweight"}
    assert any(d["stance"] == "underweight" for d in a["detractors"])


def test_the_attribution_says_it_is_not_evidence():
    a = nightly._compute_edge()["attribution"]
    assert "not evidence" in a["basis"]
    assert "sampling" in a["basis"]


def test_the_edge_panel_does_not_touch_the_specification():
    """Measuring the score must not change the score. The hash is the real assertion —
    it is derived from the scoring source itself, so it catches anything a name-based
    check would miss."""
    assert nightly.SPEC_HASH == "d600984ec00b"
    captured = nightly.spec()["functions"]
    for fn in captured.values():
        for name in ("_edge_legs", "_compute_edge", "_active_contributions"):
            assert name not in fn, f"{name} reached a scoring function"
