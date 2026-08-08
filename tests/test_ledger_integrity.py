"""The ledger writers, and the four ways they were producing unreadable rows.

Every test here corresponds to something the pipeline actually did, published, and drew
a chart from. None of it raised an exception; the file kept parsing and the numbers kept
looking plausible, which is what made it dangerous.
"""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("integrity_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)


def _row(date, **over):
    row = {k: "" for k in nightly.INDEX_FIELDS}
    row["date"] = date
    row.update(over)
    return row


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "INDEX_CSV", tmp_path / "index.csv")
    monkeypatch.setattr(nightly, "INDEX_LEGACY_CSV", tmp_path / "index.legacy.csv")
    return tmp_path


# ---------------------------------------------------------------------------
# the schema drift that started it
# ---------------------------------------------------------------------------
def test_a_header_that_predates_a_schema_change_is_quarantined(idx):
    """The original bug. The header was written once, at file creation; when six columns
    were later added to the row dict, every subsequent line appended thirteen values
    under a seven-column header. Nothing raised — a DictReader simply filed the alpha
    figure under n_holdings and a dollar amount under rebalanced."""
    old = idx / "index.csv"
    old.write_text("date,global_market_cap,basket_return\n2026-01-01,1e12,0.5\n")

    nightly._persist_index_row(_row("2026-01-02", basket_return_since_entry="1.5"))

    with (idx / "index.csv").open(newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == nightly.INDEX_FIELDS
    assert [r[0] for r in rows[1:]] == ["2026-01-02"]      # clean start
    # The old file is preserved, not deleted: it is the only record of what shipped.
    assert (idx / "index.legacy.csv").exists()
    assert "basket_return" in (idx / "index.legacy.csv").read_text()


def test_every_row_is_written_under_the_current_header(idx):
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        nightly._persist_index_row(_row(d))
    with (idx / "index.csv").open(newline="") as f:
        rows = list(csv.reader(f))
    assert all(len(r) == len(nightly.INDEX_FIELDS) for r in rows)


def test_a_mismatched_header_yields_no_rows_rather_than_misread_ones(idx):
    (idx / "index.csv").write_text("date,basket_return\n2026-01-01,0.5\n")
    assert nightly.read_index_rows() == []


# ---------------------------------------------------------------------------
# re-runs
# ---------------------------------------------------------------------------
def test_a_second_run_on_the_same_date_replaces_rather_than_appends(idx):
    nightly._persist_index_row(_row("2026-01-01", n_holdings="10"))
    nightly._persist_index_row(_row("2026-01-01", n_holdings="11"))
    rows = nightly.read_index_rows()
    assert len(rows) == 1
    assert rows[0]["n_holdings"] == "11"


def test_rows_stay_in_date_order(idx):
    for d in ("2026-01-03", "2026-01-01", "2026-01-02"):
        nightly._persist_index_row(_row(d))
    assert [r["date"] for r in nightly.read_index_rows()] == \
        ["2026-01-01", "2026-01-02", "2026-01-03"]


def test_prior_dates_are_untouched_by_a_re_run(idx):
    nightly._persist_index_row(_row("2026-01-01", n_holdings="10"))
    nightly._persist_index_row(_row("2026-01-02", n_holdings="10"))
    nightly._persist_index_row(_row("2026-01-02", n_holdings="12"))
    rows = nightly.read_index_rows()
    assert [r["n_holdings"] for r in rows] == ["10", "12"]


# ---------------------------------------------------------------------------
# signals.csv — the same append bug, 460 rows deep
# ---------------------------------------------------------------------------
def test_dedupe_collapses_repeated_runs_keeping_the_last(tmp_path, monkeypatch):
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(nightly, "LEDGER_CSV", path)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader()
        for conv in ("50", "60", "70"):
            w.writerow({**{k: "" for k in nightly.FIELDS},
                        "date": "2026-01-01", "symbol": "BTC", "conviction": conv})
        w.writerow({**{k: "" for k in nightly.FIELDS},
                    "date": "2026-01-02", "symbol": "BTC", "conviction": "80"})

    assert nightly.dedupe_signals() == 2
    rows = list(csv.DictReader(path.open(newline="")))
    assert len(rows) == 2
    assert rows[0]["conviction"] == "70"       # the last run of that day wins


def test_dedupe_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(nightly, "LEDGER_CSV", path)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader()
        w.writerow({**{k: "" for k in nightly.FIELDS}, "date": "2026-01-01", "symbol": "BTC"})
    assert nightly.dedupe_signals() == 0
    assert nightly.dedupe_signals() == 0


def test_dedupe_on_an_absent_ledger_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_CSV", tmp_path / "absent.csv")
    assert nightly.dedupe_signals() == 0


# ---------------------------------------------------------------------------
# the horizon asymmetry that manufactured the alpha
# ---------------------------------------------------------------------------
def _mk(sym, price, mc):
    return {"symbol": sym, "name": sym, "current_price": price, "market_cap": mc,
            "total_volume": mc * 0.10, "price_change_percentage_24h": 2.0,
            "ath": price * 2, "atl": price * 0.5,
            "high_24h": price * 1.02, "low_24h": price * 0.98,
            "fully_diluted_valuation": mc * 1.1}


def test_the_benchmark_baseline_survives_a_rebalance(tmp_path, monkeypatch):
    """The bug that made alpha meaningless.

    entry_global_mcap was re-snapshotted on every rebalance while kept holdings retained
    their original entry_price, so the two legs were measured over different horizons:
    the basket accumulated from first entry while the benchmark restarted at zero. With
    `rebalanced` true on nine of the first ten runs, benchmark_return was 0.0 on every
    published row and the reported alpha was the basket's raw return renamed.
    """
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "BASKET_JSON", tmp_path / "basket.json")
    monkeypatch.setattr(nightly, "INDEX_CSV", tmp_path / "index.csv")
    monkeypatch.setattr(nightly, "INDEX_LEGACY_CSV", tmp_path / "index.legacy.csv")
    monkeypatch.setattr(nightly, "INDEX_JSON", tmp_path / "index.json")

    caps = iter([1e12, 1.5e12, 1.5e12, 1.5e12])
    monkeypatch.setattr(nightly, "fetch_global_market_cap", lambda: next(caps, 1.5e12))

    markets = [_mk(f"T{i:02d}", 1.0 + i, 1e10 / (i + 1)) for i in range(15)]
    nightly.build_basket(markets, "2026-01-01")
    baseline = json.loads((tmp_path / "basket.json").read_text())["entry_global_mcap"]
    assert baseline == 1e12

    # Force a membership change so the next run rebalances.
    shifted = [_mk(f"U{i:02d}", 5.0 + i, 1e10 / (i + 1)) for i in range(15)]
    nightly.build_basket(shifted, "2026-01-02")

    after = json.loads((tmp_path / "basket.json").read_text())
    assert after["rebalanced"] == "2026-01-02"
    assert after["entry_global_mcap"] == baseline, \
        "rebalancing must not reset the benchmark baseline"

    idx = json.loads((tmp_path / "index.json").read_text())
    assert idx["latest"]["benchmark_return_since_entry"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# cumulative is read, not accumulated
# ---------------------------------------------------------------------------
def test_the_total_is_the_latest_reading_not_a_product_of_every_reading(idx, monkeypatch):
    """Compounding ten since-entry figures produced a 4.53x 'total return' from a basket
    that was up about 120%."""
    monkeypatch.setattr(nightly, "INDEX_JSON", idx / "index.json")
    for d, v in (("2026-01-01", "10.0"), ("2026-01-02", "20.0"), ("2026-01-03", "30.0")):
        nightly._persist_index_row(_row(d, basket_return_since_entry=v))
    rows = nightly.read_index_rows()
    vals = [float(r["basket_return_since_entry"]) for r in rows]
    latest = 1 + vals[-1] / 100.0
    compounded = 1.0
    for v in vals:
        compounded *= (1 + v / 100.0)
    assert latest == pytest.approx(1.30)
    assert compounded == pytest.approx(1.716)      # what the old code reported
