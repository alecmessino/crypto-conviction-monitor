"""Pipeline monitoring, tailored to this architecture rather than ported from equity.

Two of the equity panels do not transfer, and the tests that matter here are the ones
pinning *why*:

* Equity ranks a fixed constituent list, so its rank correlation is a statement about
  scoring stability. This universe is "top N by market cap" and turns over 8-20% a
  night, so the same statistic across all names would mostly report which coins were
  large that morning. Stability must be measured on the surviving cohort.
* Equity's coverage measures imputation share. Nothing here imputes, so the equivalent
  is field presence — the early warning for a feed going dark.

The structural-break detector is pinned hardest, because it found a real one: on
2026-08-05 the median asset's conviction moved 36 points on a night the median price
moved 0.00%.
"""
import csv
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("monitor_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_CSV", tmp_path / "signals.csv")

    def write(rows):
        with (tmp_path / "signals.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({**{k: "" for k in nightly.FIELDS}, **r})
    return write


def board(date, n=30, conv=lambda i: 90 - i * 2.0, price=lambda i: 1.0 + i,
          spec="abc123", **extra):
    return [{"date": date, "symbol": f"A{i:02d}", "name": f"A{i:02d}",
             "conviction": conv(i), "signal": nightly._tier_for(conv(i)),
             "price": price(i), "market_cap": 1e9 + i * 1e8, "turnover_pct": 30.0,
             "rs7": 1.0, "rs14": 1.0, "rs30": 1.0, "rs200": 1.0, "perp_mult": 1.0,
             "era": 0.8, "high_24h": price(i) * 1.01, "low_24h": price(i) * 0.99,
             "spec_hash": spec, **extra}
            for i in range(n)]


def status(mon, name):
    return next(c["status"] for c in mon["health"] if c["name"] == name)


# ---------------------------------------------------------------------------
# the specification hash
# ---------------------------------------------------------------------------
def test_the_hash_is_stable_across_calls():
    assert nightly.spec_hash() == nightly.spec_hash()


def test_the_hash_covers_every_named_scoring_function():
    captured = nightly.spec()["functions"]
    assert set(captured) == set(nightly.SPEC_FUNCTIONS)
    # The thresholds themselves must be in the captured text, or the hash is decorative.
    # ast.unparse normalises numeric literals, so 0.30 round-trips as 0.3.
    assert "0.3" in captured["score"]


def test_a_renamed_scoring_function_is_a_hard_failure(monkeypatch):
    """Silently dropping a function from the specification is worse than crashing: the
    hash would keep matching while the thing it describes changed."""
    monkeypatch.setattr(nightly, "SPEC_FUNCTIONS", ("score", "no_such_function"))
    with pytest.raises(RuntimeError, match="cannot find scoring function"):
        nightly.spec()


# ---------------------------------------------------------------------------
# stability on the surviving cohort
# ---------------------------------------------------------------------------
def test_stability_is_measured_only_on_names_present_both_nights(ledger):
    """The whole reason this is not a port. Twenty of thirty names are replaced
    overnight; the ten survivors are unchanged. Stability must read as perfect, and the
    churn must be reported beside it rather than folded into it."""
    day1 = board("2026-03-01", n=30)
    day2 = board("2026-03-02", n=10)                       # A00-A09 survive, unchanged
    day2 += [{**r, "symbol": f"NEW{i:02d}"} for i, r in enumerate(board("2026-03-02", n=20))]
    ledger(day1 + day2)
    mon = nightly._compute_monitor()
    st = mon["stability"]
    assert st["shared"] == 10
    assert st["rank_correlation"] == 1.0
    assert len(st["entered"]) == 20
    assert st["churn"] == pytest.approx(20 / 30, abs=0.01)
    assert status(mon, "Ranking stability") == "pass"
    assert status(mon, "Universe churn") == "warn"          # 67% is not a normal night


def test_churn_within_the_normal_band_passes(ledger):
    day1 = board("2026-03-01", n=30)
    day2 = board("2026-03-02", n=28)
    day2 += [{**r, "symbol": "NEW00"} for r in board("2026-03-02", n=1)]
    ledger(day1 + day2)
    mon = nightly._compute_monitor()
    assert status(mon, "Universe churn") == "pass"


def test_a_reshuffled_board_lowers_the_correlation(ledger):
    ledger(board("2026-03-01", n=30)
           + board("2026-03-02", n=30, conv=lambda i: 30 + i * 2.0))   # order inverted
    mon = nightly._compute_monitor()
    assert mon["stability"]["rank_correlation"] == -1.0
    assert status(mon, "Ranking stability") == "warn"


def test_one_day_of_history_is_pending_not_a_failure(ledger):
    ledger(board("2026-03-01"))
    mon = nightly._compute_monitor()
    assert mon["stability"] is None
    assert status(mon, "Ranking stability") == "pending"


# ---------------------------------------------------------------------------
# field presence — the feed-dropout early warning
# ---------------------------------------------------------------------------
def test_a_field_going_dark_warns(ledger):
    """If CoinGecko stops returning rs200, the factor it feeds quietly stops
    contributing and nothing else raises."""
    ledger(board("2026-03-01") + [{**r, "rs200": ""} for r in board("2026-03-02")])
    mon = nightly._compute_monitor()
    assert mon["coverage"]["latest"]["rs200"] == 0.0
    assert status(mon, "Field presence") == "warn"


def test_full_presence_passes(ledger):
    ledger(board("2026-03-01") + board("2026-03-02"))
    mon = nightly._compute_monitor()
    assert status(mon, "Field presence") == "pass"
    assert all(v == 1.0 for v in mon["coverage"]["latest"].values())


def test_the_coverage_series_has_one_entry_per_day(ledger):
    ledger(board("2026-03-01") + board("2026-03-02") + board("2026-03-03"))
    assert len(nightly._compute_monitor()["coverage"]["series"]) == 3


# ---------------------------------------------------------------------------
# dispersion is a regime reading, never a build failure
# ---------------------------------------------------------------------------
def test_compressed_dispersion_warns_rather_than_fails(ledger):
    """Equity's percentiles guarantee dispersion, so a collapse there is a defect. These
    are absolute thresholds, so a collapse can be the market — everything correlated, or
    liquidity gone. It must never fail the build."""
    ledger(board("2026-03-01", conv=lambda i: 60 + (i % 2)))
    mon = nightly._compute_monitor()
    assert status(mon, "Score dispersion") == "warn"
    assert status(mon, "Score dispersion") != "fail"
    assert "can be the market" in next(
        c["detail"] for c in mon["health"] if c["name"] == "Score dispersion")


# ---------------------------------------------------------------------------
# the structural-break detector, which found a real one
# ---------------------------------------------------------------------------
def test_scores_moving_while_prices_do_not_is_a_specification_change(ledger):
    """2026-08-05 in the real ledger: median conviction moved 36 points, median price
    moved 0.00%. Conviction is a function of price and liquidity — if the inputs held
    still and the output did not, the function changed."""
    ledger(board("2026-03-01", conv=lambda i: 65.0)
           + board("2026-03-02", conv=lambda i: 30.0))        # same prices
    breaks = nightly._spec_breaks()
    assert len(breaks) == 1
    assert breaks[0]["to"] == "2026-03-02"
    assert breaks[0]["median_score_move"] == 35.0
    assert breaks[0]["median_price_move"] == 0.0


def test_a_real_market_move_is_not_a_specification_change(ledger):
    """Scores moving *because prices moved* is the model working, not a boundary."""
    ledger(board("2026-03-01", conv=lambda i: 65.0, price=lambda i: 100.0 + i)
           + board("2026-03-02", conv=lambda i: 30.0, price=lambda i: 60.0 + i))
    assert nightly._spec_breaks() == []


def test_a_break_in_the_recorded_past_warns_but_does_not_fail(ledger):
    """An immutable fact about history must not block every future deploy — that trains
    everyone to ignore the gate."""
    ledger(board("2026-03-01", conv=lambda i: 65.0)
           + board("2026-03-02", conv=lambda i: 30.0)
           + board("2026-03-03", conv=lambda i: 30.0))
    mon = nightly._compute_monitor()
    assert status(mon, "Undeclared specification change") == "warn"


def test_a_break_introduced_by_this_run_fails(ledger):
    ledger(board("2026-03-01", conv=lambda i: 65.0)
           + board("2026-03-02", conv=lambda i: 30.0))
    mon = nightly._compute_monitor()
    assert status(mon, "Undeclared specification change") == "fail"


def test_a_clean_history_passes(ledger):
    ledger(board("2026-03-01") + board("2026-03-02"))
    mon = nightly._compute_monitor()
    assert status(mon, "Undeclared specification change") == "pass"


# ---------------------------------------------------------------------------
# specification continuity
# ---------------------------------------------------------------------------
def test_unhashed_history_reports_unknown_rather_than_assuming_continuity(ledger):
    ledger(board("2026-03-01", spec="") + board("2026-03-02", spec=""))
    mon = nightly._compute_monitor()
    assert status(mon, "Specification history") == "pending"
    assert mon["specification"]["unknown_days"] == 2


def test_two_hashes_in_the_window_warn(ledger):
    ledger(board("2026-03-01", spec="aaa") + board("2026-03-02", spec="bbb"))
    mon = nightly._compute_monitor()
    assert status(mon, "Specification history") == "warn"
    assert len(mon["specification"]["spans"]) == 2


def test_one_hash_across_the_window_passes(ledger):
    ledger(board("2026-03-01") + board("2026-03-02"))
    assert status(nightly._compute_monitor(), "Specification history") == "pass"


# ---------------------------------------------------------------------------
# integrity and scope
# ---------------------------------------------------------------------------
def test_duplicate_rows_fail(ledger):
    ledger(board("2026-03-01") + board("2026-03-01"))
    assert status(nightly._compute_monitor(), "Ledger integrity") == "fail"


def test_an_empty_ledger_returns_nothing_rather_than_a_clean_bill(ledger):
    ledger([])
    assert nightly._compute_monitor() == {}


def test_the_scope_disclaims_predictive_power(ledger):
    ledger(board("2026-03-01"))
    scope = nightly._compute_monitor()["scope"].lower()
    assert "operational condition only" in scope
    assert "nothing here is evidence" in scope


def test_spearman_handles_ties_and_short_series():
    assert nightly._spearman([1, 2], [1, 2]) is None
    assert nightly._spearman([1, 1, 1, 1], [4, 3, 2, 1]) is None      # no variance
    assert nightly._spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0
    assert nightly._spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
