"""The ledger writers, and the four ways they were producing unreadable rows.

Every test here corresponds to something the pipeline actually did, published, and drew
a chart from. None of it raised an exception; the file kept parsing and the numbers kept
looking plausible, which is what made it dangerous.

The index.csv / basket.json WRITER tests left with the writer (the hysteresis basket was
retired and its writer removed on 2026-09-24; tests/test_basket_retirement.py pins the
archived record instead). What remains is the reader that refuses a mismatched header,
which the archive still depends on, and the signals.csv repair.
"""
import csv
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("integrity_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "INDEX_CSV", tmp_path / "index.csv")
    monkeypatch.setattr(nightly, "INDEX_LEGACY_CSV", tmp_path / "index.legacy.csv")
    return tmp_path


# ---------------------------------------------------------------------------
# the archive reader: a header that does not match is refused, not misread
# ---------------------------------------------------------------------------
def test_a_mismatched_header_yields_no_rows_rather_than_misread_ones(idx):
    (idx / "index.csv").write_text("date,basket_return\n2026-01-01,0.5\n")
    assert nightly.read_index_rows() == []


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
