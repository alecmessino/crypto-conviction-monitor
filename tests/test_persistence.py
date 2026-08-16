"""Which names hold conviction across nights, and which clear the bar once.

The existing persistent_30d / persistent_90d fields answer a version of this, but they
require 30 and 90 *consecutive* nights above the level and the ledger has eleven — so
both have been empty lists since the day they were written. That is the same starvation
that made the change feed look broken: a feature that reports nothing until an arbitrary
threshold is crossed is indistinguishable from a feature that does not work.
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("persist_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

DATES = [f"2026-03-{i:02d}" for i in range(1, 12)]


def series(**names):
    """{symbol: [(date, conviction)]} from {symbol: [conv or None per date]}."""
    return {sym: [(d, c) for d, c in zip(DATES, vals) if c is not None]
            for sym, vals in names.items()}


def row(out, sym):
    return next(r for r in out["rows"] if r["symbol"] == sym)


# ---------------------------------------------------------------------------
# the distinction the panel exists to draw
# ---------------------------------------------------------------------------
def test_a_persistent_name_outranks_a_one_night_spike():
    out = nightly._persistence(series(
        STEADY=[75] * 11,
        SPIKE=[30] * 10 + [90],
    ), DATES)
    assert out["rows"][0]["symbol"] == "STEADY"
    assert row(out, "SPIKE")["spike"] is True
    assert row(out, "STEADY")["spike"] is False


def test_a_name_seen_once_cannot_outrank_one_backed_across_the_window():
    """A raw share makes 1-of-1 a perfect 1.0, which would put a name observed on a
    single night above one backed on nine of eleven. That is the opposite of what
    persistence means, and it happened on the real ledger — BONK and OP sorted above
    HYPE until the denominator was shrunk."""
    out = nightly._persistence(series(
        HYPE=[75] * 9 + [40, 40],
        ONCE=[None] * 10 + [75],
    ), DATES)
    assert row(out, "HYPE")["share_above"] < row(out, "ONCE")["share_above"]
    assert row(out, "HYPE")["persistence"] > row(out, "ONCE")["persistence"]
    assert out["rows"][0]["symbol"] == "HYPE"


# ---------------------------------------------------------------------------
# streaks
# ---------------------------------------------------------------------------
def test_a_streak_is_broken_by_a_night_off_the_board():
    """An asset that left the universe was not there to be held, so the run genuinely
    ends — this is not the same as a night the pipeline failed to run."""
    out = nightly._persistence(series(GONE=[75] * 4 + [None] + [75] * 6), DATES)
    r = row(out, "GONE")
    assert r["best_streak"] == 6
    assert r["nights"] == 10 and r["of"] == 11


def test_a_streak_is_broken_by_dropping_below_the_level():
    out = nightly._persistence(series(DIP=[75] * 3 + [60] + [75] * 7), DATES)
    assert row(out, "DIP")["best_streak"] == 7


def test_the_current_streak_is_the_run_ending_tonight():
    out = nightly._persistence(series(
        ENDED=[75] * 8 + [40, 40, 40],
        RUNNING=[40] * 8 + [75, 75, 75],
    ), DATES)
    assert row(out, "ENDED")["current_streak"] == 0
    assert row(out, "ENDED")["best_streak"] == 8
    assert row(out, "RUNNING")["current_streak"] == 3


# ---------------------------------------------------------------------------
# the heatmap row
# ---------------------------------------------------------------------------
def test_absence_is_a_distinct_cell_from_a_low_score():
    """Colouring both the same invents a low reading that was never taken."""
    out = nightly._persistence(series(PARTIAL=[None, None, 20] + [75] * 8), DATES)
    cells = row(out, "PARTIAL")["cells"]
    assert cells[0] is None and cells[1] is None
    assert cells[2] == 20
    assert len(cells) == len(DATES)


def test_cells_align_to_the_shared_date_axis():
    """Two names with different histories must line up column for column, or the grid
    is comparing different days side by side."""
    out = nightly._persistence(series(A=[50] * 11, B=[None] * 6 + [50] * 5), DATES)
    assert row(out, "A")["cells"][6] is not None
    assert row(out, "B")["cells"][5] is None and row(out, "B")["cells"][6] is not None
    assert out["dates"] == DATES


# ---------------------------------------------------------------------------
# the window is stated, not assumed
# ---------------------------------------------------------------------------
def test_the_window_travels_with_the_result():
    """Nine of eleven and nine of ninety are different claims."""
    out = nightly._persistence(series(A=[75] * 11), DATES)
    assert out["window"] == 11 and out["level"] == nightly.PERSIST_LEVEL
    assert row(out, "A")["of"] == 11


def test_a_short_history_still_reports_rather_than_returning_nothing():
    """The failure being fixed: persistent_30d needs thirty consecutive nights and has
    been an empty list for the whole life of the ledger."""
    out = nightly._persistence(series(A=[75, 75]), DATES[:2])
    assert out["rows"] and out["rows"][0]["nights_above"] == 2


def test_no_history_is_empty_rather_than_an_error():
    assert nightly._persistence({}, [])["rows"] == []


def test_the_live_ledger_produces_a_populated_panel():
    """The regression: this must not be another feature that reports nothing forever."""
    p = nightly._compute_market_breadth()["persistence"]
    assert p["rows"], "persistence is empty on the real ledger"
    assert p["n_backed"] > 0
    assert any(r["best_streak"] > 1 for r in p["rows"]), "no multi-night run found"


def test_persistence_does_not_touch_the_specification():
    # d600984ec00b -> 872935361713: the funding regime rewrite of lavl_perp_mult, which
    # is a scoring change and correctly broke this pin. See tests/test_perps.py.
    assert nightly.SPEC_HASH == "872935361713"
    for fn in nightly.spec()["functions"].values():
        assert "_persistence" not in fn
