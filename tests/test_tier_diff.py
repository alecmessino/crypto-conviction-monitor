"""Overnight tier transitions, split into reclassifications and boundary noise.

The distinction this file protects is the whole reason the diff exists. A tier is a cut
through a continuous score, so a name at 69 and a name at 71 are the same holding wearing
different labels. On the first night measured, three of five crypto tier changes came
from moves of two points or less — a "new BUY" list built without the split would be
wrong in detail most mornings.

The alternative fix is hysteresis: refusing to change a tier until a name moves far
enough past the threshold. That puts a memory of yesterday inside tonight's score, so a
name's tier would depend on the path it took rather than on its conviction. These tests
pin the split at the presentation boundary instead.
"""
import csv
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tier_diff_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)

COLUMNS = ["date", "symbol", "name", "conviction", "signal"]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the module at a throwaway signals ledger."""
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(nightly, "LEDGER_CSV", path)

    def write(rows):
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            for date, sym, conv, sig in rows:
                w.writerow({"date": date, "symbol": sym, "name": sym + " Coin",
                            "conviction": conv, "signal": sig})
    return write


# ---------------------------------------------------------------------------
# the split
# ---------------------------------------------------------------------------
def test_a_two_point_crossing_is_marginal_and_a_ten_point_one_is_not(ledger):
    ledger([
        ("2026-01-01", "TINY", 69, "HOLD"), ("2026-01-02", "TINY", 71, "BUY"),
        ("2026-01-01", "REAL", 80, "STRONG"), ("2026-01-02", "REAL", 70, "BUY"),
    ])
    d = nightly._compute_tier_diff()
    assert [e["symbol"] for e in d["marginal"]] == ["TINY"]
    assert [e["symbol"] for e in d["changed"]] == ["REAL"]
    assert d["counts"] == {"tier_changes": 2, "real": 1, "marginal": 1,
                           "marginal_share": 0.5}


def test_the_threshold_is_inclusive(ledger):
    """A move of exactly MARGINAL_MOVE is a boundary crossing, not a reclassification —
    the same convention the equity terminal uses, so the two read alike."""
    ledger([
        ("2026-01-01", "EDGE", 69.0, "HOLD"),
        ("2026-01-02", "EDGE", 71.0, "BUY"),
    ])
    d = nightly._compute_tier_diff()
    assert abs(d["marginal"][0]["delta"]) == nightly.MARGINAL_MOVE
    assert d["counts"]["marginal"] == 1


def test_a_name_that_moved_without_changing_tier_is_not_listed(ledger):
    """A ten-point move inside HOLD is a real move and not a tier event. It belongs to
    the change feed, not here — this panel is about the label."""
    ledger([
        ("2026-01-01", "SAME", 56, "HOLD"),
        ("2026-01-02", "SAME", 66, "HOLD"),
    ])
    d = nightly._compute_tier_diff()
    assert d["changed"] == [] and d["marginal"] == []
    assert d["counts"]["tier_changes"] == 0


def test_direction_is_preserved_on_downgrades(ledger):
    ledger([
        ("2026-01-01", "DOWN", 71, "BUY"),
        ("2026-01-02", "DOWN", 69, "HOLD"),
    ])
    e = nightly._compute_tier_diff()["marginal"][0]
    assert e["delta"] == -2.0
    assert e["from_tier"] == "BUY" and e["to_tier"] == "HOLD"


# ---------------------------------------------------------------------------
# the comparison window
# ---------------------------------------------------------------------------
def test_only_the_two_most_recent_dates_are_compared(ledger):
    """An overnight diff is overnight. Comparing the newest against the oldest would
    report a week of drift as last night's news."""
    ledger([
        ("2026-01-01", "X", 40, "WATCH"),
        ("2026-01-02", "X", 60, "HOLD"),
        ("2026-01-03", "X", 61, "HOLD"),
    ])
    d = nightly._compute_tier_diff()
    assert (d["from"], d["to"]) == ("2026-01-02", "2026-01-03")
    assert d["counts"]["tier_changes"] == 0


def test_a_name_absent_last_night_is_not_a_tier_change(ledger):
    """It entered the universe. Reporting it as an upgrade would be a rating action
    the model never took."""
    ledger([
        ("2026-01-01", "OLD", 60, "HOLD"),
        ("2026-01-02", "OLD", 60, "HOLD"), ("2026-01-02", "NEW", 85, "STRONG"),
    ])
    d = nightly._compute_tier_diff()
    assert d["changed"] == [] and d["marginal"] == []
    assert d["names_compared"] == 1


def test_a_single_day_of_history_is_pending_rather_than_empty(ledger):
    """Distinguishable from 'nothing changed', which is a measurement."""
    ledger([("2026-01-01", "X", 60, "HOLD")])
    d = nightly._compute_tier_diff()
    assert d["pending"] is True
    assert d["counts"] == {}


def test_no_ledger_at_all_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_CSV", tmp_path / "absent.csv")
    assert nightly._compute_tier_diff() == {}


# ---------------------------------------------------------------------------
# tier derivation
# ---------------------------------------------------------------------------
def test_a_recorded_signal_is_trusted_over_a_recomputed_one(ledger):
    """A historical row keeps the label it was published with. Re-deriving every old
    row under today's cuts would silently restate what the board said on a past night —
    the same reason snapshots elsewhere in this project record their own spec."""
    ledger([
        ("2026-01-01", "X", 71, "HOLD"),     # published HOLD despite clearing 70
        ("2026-01-02", "X", 71, "BUY"),
    ])
    e = nightly._compute_tier_diff()["marginal"][0]
    assert e["from_tier"] == "HOLD" and e["to_tier"] == "BUY"
    assert e["delta"] == 0.0


def test_tier_cuts_match_the_scorer(ledger):
    """The fallback for a row with no recorded signal must agree with _score()."""
    for conv, tier in [(80, "STRONG"), (79.9, "BUY"), (70, "BUY"), (69.9, "HOLD"),
                       (55, "HOLD"), (54.9, "WATCH"), (40, "WATCH"), (39.9, "AVOID"),
                       (0, "AVOID")]:
        assert nightly._tier_for(conv) == tier
