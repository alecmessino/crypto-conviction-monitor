"""The deploy gate.

A validator that has never failed on a real defect is decoration. Every check here is
pinned to something this pipeline actually published, and each test drives the check
from a ledger constructed to contain that defect — so a refactor that quietly weakens a
check fails here rather than in production a week later.

The gate found two live problems on its first run against the real ledger: signals.json
holding 850 rows against the CSV's 390, and basket weights summing to 76.6 instead of 1.
Both have tests below.
"""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("validator", ROOT / "scripts" / "validate_ledger.py")
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)
nightly = v.nightly


# ---------------------------------------------------------------------------
# a healthy ledger, which every test then breaks in exactly one way
# ---------------------------------------------------------------------------
def _signals(path, days=3, assets=30):
    rows = []
    for d in range(days):
        for i in range(assets):
            conv = 90 - i * 2.5                      # wide, well-dispersed
            rows.append({
                **{k: "" for k in nightly.FIELDS},
                "date": f"2026-03-{d+1:02d}", "symbol": f"A{i:02d}", "name": f"A{i:02d}",
                "price": round(1.0 + i + d * 0.1, 4), "market_cap": 1e9 + i * 1e8,
                "conviction": conv, "signal": nightly._tier_for(conv),
            })
    with (path / "signals.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader()
        w.writerows(rows)
    (path / "signals.json").write_text(json.dumps({"total_signals": len(rows), "rows": rows}))
    return rows


def _index(path, n=3, bench=lambda i: i * 1.5):
    rows = []
    for i in range(n):
        rows.append({**{k: "" for k in nightly.INDEX_FIELDS},
                     "date": f"2026-03-{i+1:02d}", "global_market_cap": 1e12 + i * 1e10,
                     "basket_return_since_entry": i * 2.0,
                     "benchmark_return_since_entry": bench(i),
                     "n_holdings": "10", "rebalanced": "False"})
    with (path / "index.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.INDEX_FIELDS)
        w.writeheader()
        w.writerows(rows)


def _basket(path, weights=None):
    hs = [{"symbol": f"A{i:02d}", "conviction": 90 - i * 2.5,
           "weight": (weights[i] if weights else 0.1),
           "entry_price": 1.0 + i, "current_price": 1.1 + i} for i in range(10)]
    (path / "basket.json").write_text(json.dumps(
        {"rebalanced": "2026-03-03", "entry_global_mcap": 1e12, "holdings": hs}))


def _monitor(path, to="2026-03-03", health=None):
    (path / "monitor.json").write_text(json.dumps({
        "generated_at": "2026-03-03T00:00:00+00:00", "observations": 3,
        "from": "2026-03-01", "to": to,
        "health": health if health is not None else [
            {"name": "Data freshness", "status": "pass", "detail": "fresh"}],
    }))


@pytest.fixture
def ledger(tmp_path):
    _signals(tmp_path)
    _index(tmp_path)
    _basket(tmp_path)
    _monitor(tmp_path)
    return tmp_path


def run(ledger):
    return (v.check_headers(ledger) + v.check_no_duplicates(ledger) + v.check_mirror(ledger)
            + v.check_board(ledger, 25) + v.check_returns(ledger) + v.check_basket(ledger)
            + v.check_monitor(ledger))


def test_a_healthy_ledger_passes(ledger):
    assert run(ledger) == []


# ---------------------------------------------------------------------------
# schema drift — the bug that started all of this
# ---------------------------------------------------------------------------
def test_a_stale_header_fails(ledger):
    (ledger / "index.csv").write_text(
        "date,global_market_cap,basket_return\n2026-03-01,1e12,0.5\n")
    assert any("header does not match" in p for p in v.check_headers(ledger))


def test_rows_wider_than_the_header_fail(ledger):
    with (ledger / "index.csv").open("a", newline="", encoding="utf-8") as f:
        f.write(",".join(["2026-03-09"] + ["0"] * len(nightly.INDEX_FIELDS)) + "\n")
    assert any("rows of width" in p for p in v.check_headers(ledger))


# ---------------------------------------------------------------------------
# duplicates — 460 of them accumulated before anything noticed
# ---------------------------------------------------------------------------
def test_duplicate_date_symbol_pairs_fail(ledger):
    rows = list(csv.DictReader((ledger / "signals.csv").open(newline="")))
    with (ledger / "signals.csv").open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=nightly.FIELDS).writerow(rows[0])
    problems = v.check_no_duplicates(ledger)
    assert any("duplicate (date, symbol)" in p for p in problems)
    assert "dedupe_signals" in problems[0]        # says how to repair it


def test_duplicate_index_dates_fail(ledger):
    rows = nightly.read_index_rows(ledger / "index.csv")
    with (ledger / "index.csv").open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=nightly.INDEX_FIELDS).writerow(rows[0])
    assert any("duplicate dates" in p for p in v.check_no_duplicates(ledger))


def test_the_json_mirror_must_match_the_csv(ledger):
    """Found on the real ledger: the CSV was repaired and its JSON mirror was not."""
    payload = json.loads((ledger / "signals.json").read_text())
    payload["rows"] = payload["rows"][:5]
    (ledger / "signals.json").write_text(json.dumps(payload))
    assert any("one was rebuilt without the other" in p for p in v.check_mirror(ledger))


# ---------------------------------------------------------------------------
# degeneracy — the failure the sibling project shipped for weeks
# ---------------------------------------------------------------------------
def test_a_board_with_no_dispersion_fails(tmp_path):
    rows = _signals(tmp_path)
    for r in rows:
        r["conviction"] = 60
        r["signal"] = "HOLD"
    with (tmp_path / "signals.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader(); w.writerows(rows)
    problems = v.check_board(tmp_path, 25)
    assert any("dispersion" in p for p in problems)
    assert any("distinct tier" in p for p in problems)


def test_too_few_assets_fails(tmp_path):
    _signals(tmp_path, assets=5)
    assert any("assets scored" in p for p in v.check_board(tmp_path, 25))


def test_an_implausible_market_cap_fails(tmp_path):
    rows = _signals(tmp_path)
    rows[-1]["market_cap"] = 4.4e18          # the equity project's 1e6 unit error
    with (tmp_path / "signals.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader(); w.writerows(rows)
    assert any("market cap" in p for p in v.check_board(tmp_path, 25))


def test_a_zero_price_fails(tmp_path):
    rows = _signals(tmp_path)
    rows[-1]["price"] = 0
    with (tmp_path / "signals.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader(); w.writerows(rows)
    assert any("price" in p for p in v.check_board(tmp_path, 25))


# ---------------------------------------------------------------------------
# the fabricated alpha
# ---------------------------------------------------------------------------
def test_a_benchmark_that_never_moves_fails(ledger):
    """The single check that would have caught what shipped: benchmark_return was
    identically 0.0 on all ten published rows, so alpha was the raw return renamed."""
    _index(ledger, n=4, bench=lambda i: 0.0)
    problems = v.check_returns(ledger)
    assert any("constant" in p and "renamed" in p for p in problems)


def test_a_varying_benchmark_passes(ledger):
    _index(ledger, n=4, bench=lambda i: i * 1.1)
    assert v.check_returns(ledger) == []


def test_a_curve_claiming_to_render_early_fails(ledger):
    (ledger / "market_breadth.json").write_text(json.dumps({"performance": {
        "legs": 1, "min_days": 5, "renderable": True, "series": [], "book_total": 1.0}}))
    assert any("claims renderable" in p for p in v.check_returns(ledger))


def test_an_impossible_overnight_move_fails(ledger):
    (ledger / "market_breadth.json").write_text(json.dumps({"performance": {
        "legs": 4, "min_days": 5, "renderable": True, "benchmark_available": False,
        "book_total": 400.0,
        "series": [{"date": "2026-03-01", "book": 0.0, "benchmark": None},
                   {"date": "2026-03-02", "book": 400.0, "benchmark": None}]}}))
    assert any("data error, not a market" in p for p in v.check_returns(ledger))


def test_duplicates_the_reader_had_to_absorb_are_reported(ledger):
    """The performance module collapses duplicates so it can read a daily series at all.
    That it had to is itself a defect, and must not be silently tolerated."""
    (ledger / "market_breadth.json").write_text(json.dumps({"performance": {
        "legs": 4, "min_days": 5, "renderable": True, "duplicates_collapsed": 460,
        "benchmark_available": False, "book_total": 2.0, "series": []}}))
    assert any("collapse" in p for p in v.check_returns(ledger))


def test_tier_diff_counts_must_match_the_lists(ledger):
    (ledger / "market_breadth.json").write_text(json.dumps({"tier_diff": {
        "changed": [{"symbol": "A"}], "marginal": [],
        "counts": {"tier_changes": 5, "real": 1, "marginal": 0}}}))
    assert any("counts say" in p for p in v.check_returns(ledger))


# ---------------------------------------------------------------------------
# the basket
# ---------------------------------------------------------------------------
def test_weights_that_do_not_sum_to_one_fail(ledger):
    """Found on the real ledger. The normaliser's denominator was the top ten's
    convictions while the weights were applied to kept + new entrants, so the moment
    the hysteresis buffer held a name over, the sum drifted. It reached 76.6, inflating
    every weighted return in the index roughly seventy-six-fold."""
    _basket(ledger, weights=[7.6] * 10)
    assert any("weights sum to" in p for p in v.check_basket(ledger))


def test_a_basket_with_no_benchmark_baseline_fails(ledger):
    b = json.loads((ledger / "basket.json").read_text())
    del b["entry_global_mcap"]
    (ledger / "basket.json").write_text(json.dumps(b))
    assert any("no baseline" in p for p in v.check_basket(ledger))


def test_a_holding_without_an_entry_price_fails(ledger):
    b = json.loads((ledger / "basket.json").read_text())
    b["holdings"][0]["entry_price"] = 0
    (ledger / "basket.json").write_text(json.dumps(b))
    assert any("without an entry price" in p for p in v.check_basket(ledger))


# ---------------------------------------------------------------------------
# absence is not failure
# ---------------------------------------------------------------------------
def test_absent_optional_artifacts_do_not_fail(tmp_path):
    _signals(tmp_path)
    assert v.check_returns(tmp_path) == []
    assert v.check_basket(tmp_path) == []
    assert v.check_headers(tmp_path) == []


def test_a_missing_ledger_directory_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["v", "--ledger", str(tmp_path / "absent")])
    assert v.main() == 2
    assert "no ledger directory" in capsys.readouterr().out


def test_the_cli_exits_one_on_a_real_defect(ledger, monkeypatch, capsys):
    _index(ledger, n=4, bench=lambda i: 0.0)
    monkeypatch.setattr("sys.argv", ["v", "--ledger", str(ledger)])
    assert v.main() == 1
    assert "FAIL" in capsys.readouterr().out


def test_the_cli_exits_zero_on_a_healthy_ledger(ledger, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["v", "--ledger", str(ledger)])
    assert v.main() == 0
    assert "PASS" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the monitor artifact must itself be healthy
# ---------------------------------------------------------------------------
def test_a_missing_monitor_fails(ledger):
    (ledger / "monitor.json").unlink()
    assert any("health report" in p for p in v.check_monitor(ledger))


def test_a_monitor_that_did_not_rerun_fails(ledger):
    """The worst failure mode for a health report is going stale while still rendering:
    it keeps showing the last good reading while the thing it watches degrades."""
    _monitor(ledger, to="2026-03-01")
    assert any("did not rerun" in p for p in v.check_monitor(ledger))


def test_a_failing_health_check_blocks_the_build(ledger):
    _monitor(ledger, health=[{"name": "Ledger integrity", "status": "fail",
                              "detail": "4 duplicate rows"}])
    assert any("Ledger integrity" in p for p in v.check_monitor(ledger))


def test_a_warning_health_check_does_not_block(ledger):
    _monitor(ledger, health=[{"name": "Score dispersion", "status": "warn",
                              "detail": "compressed"}])
    assert v.check_monitor(ledger) == []


# ---------------------------------------------------------------------------
# the RWA board must carry its own provenance
# ---------------------------------------------------------------------------
def _rwa_artifact(path, run_ts="2026-09-01T21:29:18+00:00", promoted=True):
    (path / "rwa.json").write_text(json.dumps({
        "status": "live", "tape": [], "board": [],
        "model": {"max_coverage_on_this_plan": 83.3},
        "run": {"status": "complete", "coverage_pct": 100.0, "promoted": promoted,
                "run_ts": run_ts},
    }))


def _rwa_manifest(path, rows):
    fields = ["date", "run_ts", "run_status", "coverage_pct", "promoted"]
    with (path / "rwa_runs.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def test_an_rwa_board_without_its_manifest_row_fails(tmp_path):
    """The first RWA release commit (3394528) published rwa.json and the derived ledgers
    but not rwa_runs.csv: a git add that staged nothing. The board claimed a COMPLETE
    promoted run that no manifest row recorded. This is that ledger."""
    _rwa_artifact(tmp_path)
    _rwa_manifest(tmp_path, [{"date": "2026-09-01", "run_ts": "2026-09-01T18:46:44+00:00",
                              "run_status": "degraded", "coverage_pct": "82.93",
                              "promoted": "1"}])
    problems = v._check_rwa_artifact(tmp_path)
    assert any("not recorded as promoted in rwa_runs.csv" in p for p in problems), problems


def test_an_rwa_board_with_no_manifest_at_all_fails(tmp_path):
    _rwa_artifact(tmp_path)
    assert any("provenance is missing" in p for p in v._check_rwa_artifact(tmp_path))


def test_an_rwa_board_whose_run_is_in_the_manifest_passes(tmp_path):
    _rwa_artifact(tmp_path)
    _rwa_manifest(tmp_path, [{"date": "2026-09-01", "run_ts": "2026-09-01T21:29:18+00:00",
                              "run_status": "complete", "coverage_pct": "100.0",
                              "promoted": "1"}])
    assert not any("rwa_runs.csv" in p for p in v._check_rwa_artifact(tmp_path))


def test_a_quarantined_rwa_board_is_not_held_to_the_manifest(tmp_path):
    """rwa.degraded.json is the refused run's board and never canonical; rwa.json with
    promoted=false is the same state written by an older module. Neither claims a
    promoted run, so neither is asked to prove one."""
    _rwa_artifact(tmp_path, promoted=False)
    assert not any("rwa_runs.csv" in p for p in v._check_rwa_artifact(tmp_path))
