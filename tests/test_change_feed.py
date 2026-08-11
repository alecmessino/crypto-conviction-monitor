"""The conviction change feed, and why it was empty rather than broken.

`_at(seq, 10)` needs eleven recorded boards. The ledger has had fewer since the day it
was created, so `d10` was None for all 113 assets on every run, `movers` was empty, and
the feed serialised as {"gains": [], "losses": []}. The panel rendered nothing, which
looks identical to a panel whose data pipeline has failed.

The worse half of that: it would have started working on its own, silently, on the
eleventh night. Nobody watching would have been able to tell whether it had been fixed
or was still faulty. So the feed now degrades to the longest horizon that has data and
says which one it used.
"""
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("feed_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


def trend(**deltas):
    """One asset per named horizon value, plus a filler that never moves."""
    base = {"conviction": 60.0, "d1": None, "d7": None, "d10": None, "d30": None}
    return {"UP": {**base, **{k: v for k, v in deltas.items()}},
            "DOWN": {**base, **{k: -v for k, v in deltas.items()}}}


def test_the_longest_horizon_with_data_wins():
    t = trend(d1=3.0, d7=9.0)
    out = nightly._change_feed(t, days_recorded=8)
    assert out["horizon"] == "d7" and out["days"] == 7
    assert out["gains"][0] == {"symbol": "UP", "delta": 9.0, "d7": 9.0}
    assert out["losses"][0]["delta"] == -9.0


def test_a_horizon_without_enough_recorded_days_is_not_used():
    """Ten recorded days cannot produce a ten-day delta: that needs eleven endpoints.
    Off-by-one here is the entire bug."""
    out = nightly._change_feed(trend(d1=3.0, d7=9.0, d10=20.0), days_recorded=10)
    assert out["horizon"] == "d7"
    assert out["pending"]["d10"] == {"needs": 11, "have": 10}


def test_eleven_days_unlocks_the_ten_day_view():
    out = nightly._change_feed(trend(d1=3.0, d7=9.0, d10=20.0), days_recorded=11)
    assert out["horizon"] == "d10"
    assert "d10" not in out["pending"]


def test_with_nothing_measurable_it_says_so_rather_than_returning_a_bare_empty_feed():
    """An empty feed and an unmeasurable one look the same on screen unless the payload
    distinguishes them. `pending` is what lets the panel say which it is."""
    out = nightly._change_feed(trend(), days_recorded=1)
    assert out["horizon"] is None and out["gains"] == [] and out["losses"] == []
    assert set(out["pending"]) == {"d1", "d7", "d10", "d30"}
    assert out["pending"]["d1"] == {"needs": 2, "have": 1}


def test_a_horizon_with_enough_days_but_no_values_falls_through():
    """Recorded days are necessary, not sufficient — a board can span eleven dates while
    an individual asset joined the universe yesterday."""
    t = {"NEW": {"conviction": 60.0, "d1": 4.0, "d7": None, "d10": None, "d30": None}}
    out = nightly._change_feed(t, days_recorded=40)
    assert out["horizon"] == "d1"


def test_unmoved_assets_appear_in_neither_column():
    t = {"FLAT": {"conviction": 60.0, "d1": 0.0, "d7": None, "d10": None, "d30": None}}
    out = nightly._change_feed(t, days_recorded=2)
    assert out["gains"] == [] and out["losses"] == []
    assert out["horizon"] == "d1"     # measured, and the answer was "nothing moved"


def test_each_column_is_capped():
    t = {f"S{i}": {"conviction": 60.0, "d1": float(i - 20), "d7": None,
                   "d10": None, "d30": None} for i in range(40)}
    out = nightly._change_feed(t, days_recorded=2)
    assert len(out["gains"]) == nightly.FEED_LIMIT
    assert len(out["losses"]) == nightly.FEED_LIMIT
    assert out["gains"][0]["delta"] > out["gains"][-1]["delta"]
    assert out["losses"][0]["delta"] < out["losses"][-1]["delta"]


def test_the_live_ledger_produces_a_populated_feed():
    """The regression this file exists for: on the real ledger the feed must not be
    empty. It was, every night, for the whole life of the project."""
    b = nightly._compute_market_breadth()
    cf = b["conviction_change_feed"]
    assert cf["horizon"] is not None, "no horizon resolved on the real ledger"
    assert cf["gains"] or cf["losses"], "the feed is still empty on real data"
