"""The IC matrix and the publication gate — AUDIT-PHASE3.

The board reported a 1-day information coefficient of -0.0562 with a 95% interval
entirely below zero, and went on ranking by it. This closes that loop the conservative
way: the ranking is not inverted, no horizon is selected on the sample that reports it,
and no score, tier, weight or basket weight moves. What changes is that the board says
what it is.

The four properties this file exists to hold:

  1. a significantly negative active-horizon IC triggers DIAGNOSTIC_ONLY;
  2. insufficient history never masquerades as a positive or validated signal;
  3. the gate changes no score and no ordering;
  4. a factor IC uses the value STORED BEFORE the forward return, never a later one.
"""
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("ic_nightly", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


def _series(n_days, ic_sign=0.0, start="2026-01-01", names=20, field="conviction"):
    """A synthetic ledger with a chosen relationship between the signal and the return."""
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    by = {}
    for i in range(n_days):
        day = (d0 + timedelta(days=i)).isoformat()
        by[day] = {}
        for k in range(names):
            # price is built so the NEXT day's return is monotone in this day's signal
            prev = by.get((d0 + timedelta(days=i - 1)).isoformat())
            p = 100.0
            if prev and f"S{k}" in prev:
                p0 = float(prev[f"S{k}"]["price"])
                sig_prev = float(prev[f"S{k}"][field])
                p = p0 * (1.0 + ic_sign * (sig_prev - names / 2) / 1000.0)
            by[day][f"S{k}"] = {"price": p, field: float(k), "conviction": float(k)}
    return by


# ------------------------------------------------------------------ the estimator

def test_the_matrix_reuses_the_repos_own_inferential_contract():
    m = nightly.ic_matrix({}, None)
    assert m["min_legs"] == nightly.EDGE_MIN_LEGS == 40
    assert m["min_names"] == nightly.EDGE_MIN_NAMES == 10
    assert m["horizons"] == [1, 7, 30]
    assert m["active_horizon"] == nightly.IC_ACTIVE_HORIZON == 1
    assert m["signals"] == ["composite", "DEPTH", "CONFIRM", "LIQUIDITY", "SUPPLY",
                            "FUNDING"]
    assert "Spearman" in m["estimator"] and "1.96" in m["estimator"]


def test_the_composite_one_day_cell_reproduces_the_incumbent_edge_panel():
    """Two estimators on one quantity is two estimators that can disagree.

    The Selection Edge panel is the incumbent series. If this matrix reported a
    different number for the same cell, one of them would be wrong and the board would
    show both.
    """
    path = ROOT / "ledger" / "market_breadth.json"
    if not path.exists():
        pytest.skip("no breadth artifact yet")
    doc = json.loads(path.read_text(encoding="utf-8"))
    edge, mtx = doc.get("edge"), doc.get("ic_matrix")
    if not edge or not mtx or edge.get("mean_ic") is None:
        pytest.skip("edge or matrix not measurable yet")
    c = mtx["cells"]["composite"][str(mtx["active_horizon"])]
    assert c["ic"] == edge["mean_ic"]
    assert c["ci"] == edge["ci"]
    assert c["legs"] == edge["legs"]


# ------------------------------------------------------------- 1. the gate fires

def test_a_significantly_negative_active_horizon_triggers_diagnostic_only():
    by = _series(60, ic_sign=-1.0)
    m = nightly.ic_matrix(by, None)
    c = m["cells"]["composite"]["1"]
    assert c["state"] == "NEGATIVE", c
    assert c["ci"][1] < 0
    g = nightly.publication_gate(m)
    assert g["gate"] == "DIAGNOSTIC_ONLY"
    assert "DIAGNOSTIC ONLY" in g["headline"]
    assert "not inverted" in g["detail"].lower() or "NOT inverted" in g["detail"]


def test_the_live_board_is_in_that_state_right_now():
    """The reported result should satisfy the contract when recomputed, not by assertion."""
    path = ROOT / "ledger" / "market_breadth.json"
    if not path.exists():
        pytest.skip("no breadth artifact yet")
    doc = json.loads(path.read_text(encoding="utf-8"))
    g = doc.get("publication_gate")
    if not g:
        pytest.skip("gate not published yet")
    assert g["gate"] == "DIAGNOSTIC_ONLY", g
    assert g["horizon"] == 1
    assert g["ci"][1] < 0 and g["legs"] >= nightly.EDGE_MIN_LEGS


def test_a_measurably_positive_horizon_publishes():
    by = _series(60, ic_sign=+1.0)
    m = nightly.ic_matrix(by, None)
    assert m["cells"]["composite"]["1"]["state"] == "POSITIVE"
    assert nightly.publication_gate(m)["gate"] == "PUBLISHED"


# ------------------------------------- 2. insufficiency never reads as validated

def test_insufficient_history_is_never_a_positive_or_a_zero():
    for n_days in (0, 2, 10, 39):
        by = _series(n_days, ic_sign=+1.0) if n_days else {}
        m = nightly.ic_matrix(by, None)
        c = m["cells"]["composite"]["1"]
        assert c["state"] in ("INSUFFICIENT", "DEGENERATE"), (n_days, c["state"])
        assert c["sufficient"] is False
        g = nightly.publication_gate(m)
        assert g["gate"] == "NOT_ESTABLISHED", n_days
        assert "PUBLISHED" not in g["headline"]
        # the wording must not read as a verdict in either direction
        assert "nothing here says the ranking works" in g["detail"].lower()


def test_a_cell_with_no_history_reports_no_interval_rather_than_a_neutral_one():
    c = nightly.ic_matrix({}, None)["cells"]["FUNDING"]["30"]
    assert c["ic"] is None and c["ci"] is None and c["se"] is None
    assert c["legs"] == 0 and c["state"] == "INSUFFICIENT"


def test_a_signal_that_does_not_vary_reads_degenerate_not_zero():
    """All rows identical: there is no ranking to correlate, and that is not an IC of 0."""
    from datetime import date, timedelta
    d0 = date.fromisoformat("2026-01-01")
    by = {}
    for i in range(50):
        day = (d0 + timedelta(days=i)).isoformat()
        by[day] = {f"S{k}": {"price": 100.0 + k + i, "conviction": 50.0,
                             "c_depth": 20.0} for k in range(20)}
    c = nightly.ic_matrix(by, None)["cells"]["composite"]["1"]
    assert c["state"] == "DEGENERATE"
    assert c["ic"] is None
    assert c["legs"] == 0 and c["legs_seen"] > 0
    assert nightly.publication_gate({"cells": {"composite": {"1": c}},
                                     "active_horizon": 1})["gate"] == "NOT_ESTABLISHED"


# ---------------------------------------- 3. the gate moves nothing but a label

def test_the_gate_changes_no_score_and_no_ordering():
    """Asserted on the source: the gate must not be reachable from the scoring path."""
    captured = nightly.spec()["functions"]
    for name in ("publication_gate", "ic_matrix", "_ic_legs", "_ic_cell", "ic_by_date"):
        assert name not in captured, name
        for body in captured.values():
            assert name not in body, f"{name} reached a scoring function"
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    # the browser's gate reads BREADTH and writes only into the status strip
    assert "function pubGate()" in page
    for forbidden in ("PERP = pubGate", "conv = pubGate", "STATE.sort"):
        assert forbidden not in page


def test_the_gate_is_visible_on_the_board_not_only_in_diagnostics():
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    assert 'id="label-status"' in page
    assert "line.innerHTML = gateChip()" in page, \
        "the gate must lead the board's own strip"
    assert 'id="ic-matrix"' in page, "the full matrix belongs in diagnostics too"


# ----------------------------------------------- 4. causality: no look-ahead

def test_a_leg_pairs_a_snapshot_only_with_a_strictly_later_price():
    by = _series(50, ic_sign=+1.0)
    for h in (1, 7, 30):
        for leg in nightly._ic_legs(by, "conviction", h, None):
            assert leg["to"] > leg["from"], leg
            from datetime import date
            assert (date.fromisoformat(leg["to"])
                    - date.fromisoformat(leg["from"])).days == h, leg


def test_a_missing_date_yields_no_leg_rather_than_the_nearest_one():
    """A 30-day horizon that quietly settles for 26 is a different statistic."""
    by = _series(40, ic_sign=+1.0)
    dates = sorted(by)
    # remove exactly the dates a 7-day leg from the first half would land on
    for d in dates[7:14]:
        del by[d]
    legs = nightly._ic_legs(by, "conviction", 7, None)
    landed = {l["to"] for l in legs}
    assert not (landed & set(dates[7:14])), "a leg landed on a date that is not there"
    for l in legs:
        assert l["to"] in by


def test_the_factor_value_is_the_one_recorded_before_the_return():
    """Hand-built: the signal flips sign between the two nights, so reading the wrong
    end of the leg produces the opposite IC. Only one reading can be right."""
    by = {
        "2026-01-01": {f"S{k}": {"price": 100.0, "c_depth": float(k),
                                 "conviction": float(k)} for k in range(20)},
        # next night: the factor order is REVERSED, and the return follows the FIRST
        # night's order. A correct estimator sees +1; one reading the later value sees -1.
        "2026-01-02": {f"S{k}": {"price": 100.0 + k, "c_depth": float(19 - k),
                                 "conviction": float(19 - k)} for k in range(20)},
    }
    leg = nightly._ic_legs(by, "c_depth", 1, None)[0]
    assert leg["ic"] > 0.99, leg
    assert leg["from"] == "2026-01-01" and leg["to"] == "2026-01-02"


def test_a_symbol_that_left_the_universe_is_dropped_not_imputed():
    by = _series(45, ic_sign=+1.0)
    dates = sorted(by)
    gone = "S5"
    for d in dates[1:]:
        by[d].pop(gone, None)
    for leg in nightly._ic_legs(by, "conviction", 1, None):
        assert leg["names"] <= 19, leg
    # and it is absent rather than counted as a flat return
    assert all(l["names"] > 0 for l in nightly._ic_legs(by, "conviction", 1, None))


def test_overlapping_horizons_are_marked_rather_than_silently_narrow():
    m = nightly.ic_matrix(_series(60, ic_sign=+1.0), None)
    c1 = m["cells"]["composite"]["1"]
    c7 = m["cells"]["composite"]["7"]
    assert "disjoint" in c1["overlap"]
    assert "overlap" in c7["overlap"] and "narrower" in c7["overlap"]
    assert c7["effective_legs"] < c7["legs"]
    assert c1["effective_legs"] == c1["legs"]


def test_the_wide_reader_keeps_the_factor_columns_and_dedupes_like_the_narrow_one():
    rows = [
        {"date": "2026-01-01", "symbol": "AAA", "price": "1", "conviction": "10",
         "c_depth": "20.0", "c_momentum": "11.0", "c_liquidity": "30.0",
         "emission_mult": "1.0", "perp_mult": "1.0"},
        # a second run on the same night must replace, not duplicate
        {"date": "2026-01-01", "symbol": "AAA", "price": "2", "conviction": "11",
         "c_depth": "19.0", "c_momentum": "11.0", "c_liquidity": "30.0",
         "emission_mult": "1.0", "perp_mult": "1.0"},
        {"date": "2026-01-01", "symbol": "BAD", "price": "0", "conviction": "5"},
    ]
    by = nightly.ic_by_date(rows)
    assert set(by["2026-01-01"]) == {"AAA"}, "a zero price is not a price"
    assert by["2026-01-01"]["AAA"]["price"] == "2"
    assert by["2026-01-01"]["AAA"]["c_depth"] == "19.0"
