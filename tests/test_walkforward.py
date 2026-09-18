"""The durable store and the walk-forward harness — AUDIT-PHASE4.

Formalisation, not another redesign. ledger/xsec/ already held what a walk-forward study
needs; what it lacked was a documented contract, an outcome layer that cannot look ahead,
and a report that runs TODAY and states its own insufficiency rather than waiting to be
declared ready.

The properties this file holds, in the order they can fail:

  append-only        a writer touches its own (date, src) and nothing else
  causality          a snapshot joins only to strictly later prices, at an exact offset
  completeness       an unelapsed horizon is never a shorter return relabelled
  no leakage         a symbol that left the universe has no return, not a zero
  immutability       a recorded factor is read as recorded, never recomputed
  determinism        the same ledger produces the same report
"""
import copy
import csv
import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("wf_nightly", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


def _ledger(n_days, names=25, start="2026-01-01", **extra):
    d0 = date.fromisoformat(start)
    by = {}
    for i in range(n_days):
        day = (d0 + timedelta(days=i)).isoformat()
        by[day] = {}
        for k in range(names):
            by[day][f"S{k}"] = {"date": day, "symbol": f"S{k}",
                                "price": str(100.0 + k + i * 0.5),
                                "conviction": str(k * 3),
                                "c_depth": str(10.0 + k), "c_momentum": str(5.0 + k),
                                "c_liquidity": str(12.0 + k), "emission_mult": "1.0",
                                "perp_mult": "1.0", **extra}
    return by


# ------------------------------------------------------------------- the contract

def test_the_store_documents_what_it_is():
    doc = json.loads((ROOT / "ledger" / "xsec" / "SCHEMA.json").read_text(encoding="utf-8"))
    c = doc["contract"]
    assert set(c) == {"role", "append_only", "reproducibility", "provenance", "outcomes"}
    assert "append-only" in c["role"] or "append only" in c["role"]
    assert "not a truncation" in c["role"]
    assert doc["row_key"] == ["date", "symbol", "src"]
    assert doc["sources"] == ["live", "backfill"]
    # the reproducibility claim must be true of the field list it sits beside
    for f in ("market_cap", "total_volume", "fdv_usd", "rs_windows_n", "depth",
              "confirm", "liquidity", "emission_mult", "perp_mult", "conviction_raw",
              "conviction", "clamped", "spec_hash", "src"):
        assert f in doc["fields"], f


def test_returns_are_not_stored_which_is_why_leakage_is_impossible():
    """The strongest form of the no-look-ahead claim: there is no field to leak into."""
    for f in nightly.XSEC_FIELDS:
        assert "ret" not in f.split("_"), f
        assert not f.startswith("fwd") and not f.startswith("future"), f
    assert "roi_30d" not in nightly.XSEC_FIELDS
    assert "roi_90d" not in nightly.XSEC_FIELDS


def test_the_reader_never_pools_live_and_backfill(tmp_path):
    d = tmp_path / "xsec"
    d.mkdir()
    rows = [{k: "" for k in nightly.XSEC_FIELDS} for _ in range(2)]
    rows[0].update({"date": "2026-01-01", "symbol": "AAA", "price": "1", "src": "live"})
    rows[1].update({"date": "2026-01-01", "symbol": "AAA", "price": "9",
                    "src": "backfill"})
    with (d / "2026-01.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.XSEC_FIELDS)
        w.writeheader(); w.writerows(rows)
    assert nightly.xsec_by_date(d, "live")["2026-01-01"]["AAA"]["price"] == "1"
    assert nightly.xsec_by_date(d, "backfill")["2026-01-01"]["AAA"]["price"] == "9"


def test_a_rerun_replaces_only_its_own_date_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "XSEC_DIR", tmp_path / "xsec")
    monkeypatch.setattr(nightly, "XSEC_SCHEMA_JSON", tmp_path / "xsec" / "SCHEMA.json")
    row = lambda d, c: {"date": d, "symbol": "AAA", "conviction": c, "market_cap": 1e9,
                        "price": 1.0}
    nightly.write_xsec([row("2026-01-01", 10)], "2026-01-01")
    nightly.write_xsec([row("2026-01-02", 20)], "2026-01-02")
    nightly.write_xsec([row("2026-01-02", 22)], "2026-01-02")       # a re-run
    got = {(r["date"], r["src"]): r["conviction"] for r in
           csv.DictReader((tmp_path / "xsec" / "2026-01.csv").open(newline="",
                                                                  encoding="utf-8"))}
    assert got == {("2026-01-01", "live"): "10", ("2026-01-02", "live"): "22"}


# --------------------------------------------------------------------- causality

def test_an_outcome_joins_only_to_a_strictly_later_price_at_an_exact_offset():
    by = _ledger(40)
    for rec in nightly.link_outcomes(by):
        for h, o in rec["outcomes"].items():
            assert o["to"] > rec["date"], (rec["date"], o)
            assert (date.fromisoformat(o["to"])
                    - date.fromisoformat(rec["date"])).days == int(h)


def test_a_missing_target_night_is_unpriced_not_the_nearest_one():
    by = _ledger(40)
    dates = sorted(by)
    victim = dates[7]
    del by[victim]
    for rec in nightly.link_outcomes(by):
        for h, o in rec["outcomes"].items():
            assert o["to"] != victim or o["state"] == "unpriced"
            if o["state"] == "realised":
                assert o["to"] in by


def test_an_unelapsed_horizon_is_never_a_shorter_return_relabelled():
    by = _ledger(5)                  # nowhere near 7 or 30 days of history
    recs = nightly.link_outcomes(by)
    for rec in recs:
        for h in ("7", "30"):
            assert rec["outcomes"][h]["state"] == "horizon-incomplete"
            assert rec["outcomes"][h]["ret"] is None
    # and the last recorded night has no 1-day outcome either
    last = max(by)
    for rec in recs:
        if rec["date"] == last:
            assert rec["outcomes"]["1"]["state"] == "horizon-incomplete"


def test_a_symbol_that_left_the_universe_gets_no_return():
    by = _ledger(10)
    dates = sorted(by)
    for d in dates[1:]:
        by[d].pop("S3", None)
    gone = [r for r in nightly.link_outcomes(by)
            if r["symbol"] == "S3" and r["date"] == dates[0]]
    assert gone and gone[0]["outcomes"]["1"]["state"] == "left-universe"
    assert gone[0]["outcomes"]["1"]["ret"] is None
    # and it never appears as a realised zero anywhere
    for r in nightly.link_outcomes(by):
        if r["symbol"] == "S3" and r["date"] != dates[0]:
            continue
        for o in r["outcomes"].values():
            assert not (o["state"] == "realised" and o["ret"] == 0.0 and r["symbol"] == "S3")


def test_the_four_outcome_states_are_distinct_and_all_reachable():
    assert nightly.OUTCOME_STATES == ("realised", "horizon-incomplete",
                                      "left-universe", "unpriced")
    by = _ledger(12)
    dates = sorted(by)
    del by[dates[3]]                                   # -> unpriced
    for d in dates[5:]:
        by[d].pop("S1", None)                          # -> left-universe
    by[dates[6]]["S2"]["price"] = "0"                  # -> unpriced (no usable price)
    seen = {o["state"] for r in nightly.link_outcomes(by) for o in r["outcomes"].values()}
    assert seen == set(nightly.OUTCOME_STATES)


# ------------------------------------------------------------ immutability

def test_the_report_reads_the_recorded_factor_not_a_recomputed_one():
    """The factor order reverses between two nights; reading the wrong end flips the IC."""
    by = {
        "2026-01-01": {f"S{k}": {"price": "100", "c_depth": str(k),
                                 "conviction": str(k)} for k in range(20)},
        "2026-01-02": {f"S{k}": {"price": str(100 + k), "c_depth": str(19 - k),
                                 "conviction": str(19 - k)} for k in range(20)},
    }
    leg = nightly._ic_legs(by, "c_depth", 1, None)[0]
    assert leg["ic"] > 0.99


def test_a_snapshot_is_never_mutated_by_reading_it():
    by = _ledger(12)
    before = copy.deepcopy(by)
    nightly.link_outcomes(by)
    nightly.walkforward_report(by)
    assert by == before, "the harness mutated the ledger it was handed"


# ------------------------------------------------------------ the report itself

def test_the_report_runs_on_no_history_at_all():
    r = nightly.walkforward_report({})
    assert r["nights"] == 0 and r["measurable_cells"] == 0
    assert r["from"] is None and r["to"] is None
    for sig in r["ic"].values():
        for cell in sig.values():
            assert cell["state"] in ("INSUFFICIENT", "DEGENERATE")
            assert cell["ic"] is None


def test_an_insufficient_cohort_reports_its_count_and_no_statistic():
    r = nightly.walkforward_report(_ledger(4, names=5))
    for h in r["hit_rate_by_tier"]["T1"].values():
        assert h["state"] == "INSUFFICIENT"
        assert h["hit_rate"] is None and h["hit_ci"] is None
        assert "of " + str(nightly.WALKFWD_MIN_COHORT) in h["detail"]


def test_an_unrecorded_column_is_not_an_empty_cohort():
    """Two different facts. A count cannot tell them apart, so the report says which."""
    r = nightly.walkforward_report(_ledger(10))          # no v4 columns at all
    c = r["cohorts"]["dominance_warning"]
    assert c["recorded"] is False
    assert "has not been written" in c["detail"]
    assert c["by_horizon"]["1"]["flagged"] is None
    # with the column present but nothing flagged, it IS an empty cohort
    r2 = nightly.walkforward_report(_ledger(10, dom_warn="False"))
    c2 = r2["cohorts"]["dominance_warning"]
    assert c2["recorded"] is True
    assert c2["by_horizon"]["1"]["flagged"]["n"] == 0


def test_the_regime_split_omits_nights_with_no_regime_rather_than_bucketing_them():
    by = _ledger(30)
    dates = sorted(by)
    regimes = {d: ("RISK-ON" if i % 2 else "RISK-OFF") for i, d in enumerate(dates[:20])}
    r = nightly.walkforward_report(by, regimes)
    assert set(r["by_regime"]) == {"RISK-ON", "RISK-OFF"}
    total = sum(v["1"]["n"] for v in r["by_regime"].values())
    realised = r["coverage"]["1"]["realised"]
    assert total < realised, "nights with no recorded regime were folded into a bucket"
    assert nightly.walkforward_report(by, {})["by_regime"] is None


def test_the_report_is_deterministic_for_a_given_ledger():
    by = _ledger(30)
    a = nightly.walkforward_report(by, {})
    b = nightly.walkforward_report(by, {})
    a.pop("generated_at"); b.pop("generated_at")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_the_hit_rate_interval_is_wilson_and_stays_inside_zero_and_one():
    assert nightly._wilson(0, 0) is None
    for k, n in ((0, 25), (25, 25), (1, 30), (13, 27)):
        lo, hi = nightly._wilson(k, n)
        assert 0.0 <= lo <= hi <= 1.0, (k, n)
    assert nightly._wilson(0, 25)[0] == 0.0


def test_nothing_in_the_harness_is_part_of_the_specification():
    """A report about a model is not the model."""
    captured = nightly.spec()["functions"]
    for name in ("walkforward_report", "link_outcomes", "xsec_by_date", "_cohort",
                 "_wilson", "recorded_regimes", "write_walkforward"):
        assert name not in captured, name
        for body in captured.values():
            assert name not in body, f"{name} reached a scoring function"


def test_the_published_report_matches_the_ledger_on_disk():
    path = ROOT / "ledger" / "walkforward.json"
    if not path.exists():
        pytest.skip("no walk-forward report written yet")
    doc = json.loads(path.read_text(encoding="utf-8"))
    by = nightly.xsec_by_date()
    assert doc["nights"] == len(by)
    assert doc["from"] == (min(by) if by else None)
    assert doc["to"] == (max(by) if by else None)
    assert doc["horizons"] == list(nightly.WALKFWD_HORIZONS)
    assert doc["min_legs"] == nightly.EDGE_MIN_LEGS
    # the report must not claim more than the ledger can support
    for h, cov in doc["coverage"].items():
        assert cov["total"] == sum(len(v) for v in by.values())
        assert cov["realised"] + cov["horizon-incomplete"] + cov["left-universe"] \
               + cov["unpriced"] == cov["total"]
