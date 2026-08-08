"""Paper return of the published basket, chained across recorded days.

A return series is the easiest artifact here to make flattering by accident: score with
tonight's weights against tonight's prices and it prints alpha every day forever, looking
entirely plausible while doing it. These tests are mostly about *which* weights and
*when* the curve refuses to exist, not about the arithmetic.

They also pin the reason this reads signals.csv rather than index.json. That file's row
series declares seven columns in its header while eight of its ten rows carry thirteen,
repeats dates, and reports benchmark_return as 0.0 on every row — which made its "alpha"
the raw basket return under a different name.
"""
import csv
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("perf_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)

COLUMNS = ["date", "symbol", "name", "price", "conviction", "signal"]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(nightly, "LEDGER_CSV", path)

    def write(rows):
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=COLUMNS)
            w.writeheader()
            for date, sym, price, conv in rows:
                w.writerow({"date": date, "symbol": sym, "name": sym,
                            "price": price, "conviction": conv, "signal": "HOLD"})
    return write


def days(n, rows_for_day):
    """n consecutive dates, each built by a callable taking the day index."""
    out = []
    for i in range(n):
        out.extend((f"2026-03-{i+1:02d}", *r) for r in rows_for_day(i))
    return out


# ---------------------------------------------------------------------------
# the look-ahead boundary
# ---------------------------------------------------------------------------
def test_weights_come_from_the_earlier_night(ledger):
    """RISER is top-conviction on night one and worthless on night two; FALLER is the
    reverse. A leg weighted by the later night reports the faller's loss."""
    ledger([
        ("2026-03-01", "RISER", 100.0, 90), ("2026-03-01", "FALLER", 100.0, 1),
        ("2026-03-02", "RISER", 110.0, 1),  ("2026-03-02", "FALLER", 90.0, 90),
        ("2026-03-03", "RISER", 110.0, 1),  ("2026-03-03", "FALLER", 90.0, 90),
    ])
    legs = nightly._compute_performance()
    # 90/91 of the book in RISER on the first night.
    assert legs["series"][1]["book"] == pytest.approx(9.78, abs=0.1)


def test_duplicate_runs_on_one_day_are_collapsed(ledger):
    """The real ledger recorded 2026-08-02 nine times. Read naively that is nine days
    of zero return, which both dilutes the curve and lies about its length."""
    ledger([
        ("2026-03-01", "A", 100.0, 80), ("2026-03-01", "A", 100.0, 80),
        ("2026-03-01", "A", 100.0, 80),
        ("2026-03-02", "A", 110.0, 80),
    ])
    out = nightly._compute_performance()
    assert out["duplicates_collapsed"] == 2
    assert out["days"] == 2 and out["legs"] == 1


def test_the_last_run_of_a_day_wins(ledger):
    ledger([
        ("2026-03-01", "A", 100.0, 80), ("2026-03-01", "A", 200.0, 80),
        ("2026-03-02", "A", 220.0, 80),
    ])
    assert nightly._compute_performance()["book_total"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# book construction
# ---------------------------------------------------------------------------
def test_only_the_top_n_are_held(ledger):
    """An eleventh name must not contribute, however it moves."""
    rows = []
    for d, px in (("2026-03-01", 100.0), ("2026-03-02", 100.0)):
        for i in range(nightly.PERF_TOP_N):
            rows.append((d, f"IN{i}", px, 90))
        rows.append((d, "OUT", px * (3.0 if d.endswith("02") else 1.0), 1))
    ledger(rows)
    assert nightly._compute_performance()["book_total"] == pytest.approx(0.0)


def test_a_name_that_becomes_unpriceable_renormalises(ledger):
    """Dropping out of the feed is not a total loss and must not be booked as one."""
    ledger([
        ("2026-03-01", "A", 100.0, 50), ("2026-03-01", "GONE", 100.0, 50),
        ("2026-03-02", "A", 110.0, 50),
    ])
    out = nightly._compute_performance()
    # 50% of the book vanished, which is over the tolerance, so the leg is dropped.
    assert out["legs"] == 0 and out["legs_dropped"] == 1


def test_a_small_dropout_is_tolerated_and_renormalised(ledger):
    """The dropped name carries ~5% of the book, comfortably inside the tolerance.

    Deliberately not one of ten equal weights: that lands exactly on the 10% boundary,
    where whether the leg survives is decided by floating-point error rather than by the
    rule. A test whose outcome turns on that is testing the FPU.
    """
    rows = [("2026-03-01", "SMALL", 100.0, 50)]
    rows += [("2026-03-01", f"A{i}", 100.0, 100) for i in range(9)]
    rows += [("2026-03-02", f"A{i}", 110.0, 100) for i in range(9)]
    ledger(rows)
    out = nightly._compute_performance()
    assert out["legs"] == 1
    assert out["book_total"] == pytest.approx(10.0)     # not 9.5


# ---------------------------------------------------------------------------
# benchmark and control
# ---------------------------------------------------------------------------
def test_the_benchmark_is_btc_and_the_control_is_the_whole_universe(ledger):
    ledger([
        ("2026-03-01", "BTC", 100.0, 10), ("2026-03-01", "ALT", 100.0, 90),
        ("2026-03-02", "BTC", 105.0, 10), ("2026-03-02", "ALT", 120.0, 90),
    ])
    out = nightly._compute_performance()
    assert out["benchmark"] == "BTC"
    assert out["benchmark_total"] == pytest.approx(5.0)
    # Equal weight over both names: (5% + 20%) / 2
    assert out["equal_weight_total"] == pytest.approx(12.5)


def test_a_missing_benchmark_is_a_gap_not_a_flat_line(ledger):
    ledger([
        ("2026-03-01", "ALT", 100.0, 90),
        ("2026-03-02", "ALT", 110.0, 90),
    ])
    out = nightly._compute_performance()
    assert out["benchmark_available"] is False
    assert out["benchmark_total"] is None
    assert all(p["benchmark"] is None for p in out["series"])


# ---------------------------------------------------------------------------
# the origin and the render gate
# ---------------------------------------------------------------------------
def test_the_origin_is_the_first_measured_night_not_the_first_recorded_one(ledger):
    """The real ledger drops its first three legs, so anchoring at the first recorded
    date drew a segment spanning four days and attributed one night's move to all of it."""
    rows = [("2026-03-01", "A", 100.0, 90), ("2026-03-01", "GONE", 100.0, 90)]
    rows += [(f"2026-03-{d:02d}", "A", 100.0 + d, 90) for d in (2, 3, 4, 5, 6)]
    ledger(rows)
    out = nightly._compute_performance()
    assert out["legs_dropped"] == 1
    assert out["from"] == "2026-03-02"          # not 2026-03-01
    assert out["recorded_from"] == "2026-03-01"
    assert out["series"][0]["date"] == "2026-03-02"


def test_the_curve_refuses_to_render_below_the_threshold(ledger):
    ledger([("2026-03-01", "A", 100.0, 90), ("2026-03-02", "A", 110.0, 90)])
    out = nightly._compute_performance()
    assert out["renderable"] is False
    assert out["min_days"] == nightly.PERF_MIN_DAYS


def test_the_curve_renders_once_enough_legs_are_measured(ledger):
    ledger([(f"2026-03-{i+1:02d}", "A", 100.0 + i, 90)
            for i in range(nightly.PERF_MIN_DAYS)])
    out = nightly._compute_performance()
    assert out["renderable"] is True
    assert out["legs"] == nightly.PERF_MIN_DAYS - 1


def test_a_single_day_is_not_a_series(ledger):
    ledger([("2026-03-01", "A", 100.0, 90)])
    out = nightly._compute_performance()
    assert out["legs"] == 0 and out["series"] == []
    assert out["renderable"] is False


def test_no_ledger_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_CSV", tmp_path / "absent.csv")
    out = nightly._compute_performance()
    assert out["days"] == 0 and out["series"] == []


def test_returns_compound_rather_than_summing(ledger):
    ledger([("2026-03-01", "A", 100.0, 90), ("2026-03-02", "A", 110.0, 90),
            ("2026-03-03", "A", 121.0, 90)])
    assert nightly._compute_performance()["book_total"] == pytest.approx(21.0)
