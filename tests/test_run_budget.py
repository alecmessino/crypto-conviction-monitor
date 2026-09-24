"""The nightly's run budget and its run manifest.

Budget: the job has twenty minutes and a runner killed at the limit commits nothing. The
optional stages (Cryptometer, Binance long/short, the CoinGecko context feeds, DEX depth,
the RWA snapshot) are spent last-first against NIGHTLY_BUDGET_S; the mandatory work —
the markets fetch, scoring, every ledger write, every gate — is never budgeted.

Manifest: ledger/runs.csv, one appended row per run, whatever it decided. Provenance only.
"""
import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nightly = _load("nightly_budget_test", ROOT / "nightly.py")
record_run = _load("record_run_test", ROOT / "scripts" / "record_run.py")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------
def test_a_stage_starts_only_with_its_reserve_and_says_when_it_does_not():
    clk = Clock()
    b = nightly.RunBudget(100, clock=clk)
    assert b.allow("early", 60)
    clk.t = 50
    assert not b.allow("late", 60)
    assert b.skipped == ["late@50s"]


def test_an_expired_budget_stops_a_getter_without_a_request():
    clk = Clock()
    b = nightly.RunBudget(10, clock=clk)
    calls = []
    g = b.getter(lambda s, p, params=None, **k: calls.append(p) or {"status": "live"}, "rwa")
    assert g({}, "/one")["status"] == "live"
    clk.t = 11
    rep = g({}, "/two")
    assert rep["status"] == "unavailable" and "budget" in rep["detail"]
    assert calls == ["/one"], "a request was made after the budget was spent"
    assert "rwa@stopped" in b.skipped


def test_binance_long_short_stops_mid_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(nightly, "_get_json", lambda url, *a, **k: calls.append(url) or
                        [{"longShortRatio": "1.1"}])
    left = iter([False, False, True, True, True])
    perps = {}
    got = nightly.fetch_long_short(perps, {"AAA", "BBB", "CCC", "DDD"},
                                   deadline=lambda: next(left))
    assert got == 2 and len(calls) == 2
    # The rest keep a null ratio — never a manufactured neutral.
    assert set(perps) == {"AAA", "BBB"}


@pytest.mark.parametrize("fn", ["fetch_liquidations", "fetch_positioning"])
def test_cryptometer_stops_mid_loop_and_records_why(monkeypatch, fn):
    cm = nightly.cryptometer
    monkeypatch.setattr(cm, "paid_enabled", lambda: True, raising=False)
    calls = []
    monkeypatch.setattr(cm, "call", lambda *a, **k: calls.append(k.get("symbol")) or (None, "x"))
    rep = getattr(cm, fn)("key", ["BTC", "ETH", "SOL"], deadline=lambda: True)
    assert calls == [], "a request was made after the budget was spent"
    assert "budget" in rep["detail"]


def test_main_records_the_run_even_when_it_crashes(monkeypatch, tmp_path):
    info = tmp_path / "run.json"
    monkeypatch.setenv("NIGHTLY_RUN_INFO", str(info))

    def boom(run, budget):
        run["stage"] = "markets"
        raise RuntimeError("socket closed")
    monkeypatch.setattr(nightly, "_main", boom)
    with pytest.raises(RuntimeError):
        nightly.main()
    rec = json.loads(info.read_text())
    assert rec["outcome"].startswith("crashed at markets: RuntimeError")
    assert rec["spec_hash"] == nightly.SPEC_HASH and "elapsed_s" in rec


# ---------------------------------------------------------------------------
# end to end: a run with no budget still publishes, and skips every optional stage
# ---------------------------------------------------------------------------
HARNESS = textwrap.dedent('''
    import csv, os, sys, time, urllib.error, urllib.request
    sys.path.insert(0, os.getcwd())
    for k in ("COINGECKO_API_KEY", "CRYPTOMETER_API_KEY", "DUNE_API_KEY"):
        os.environ.pop(k, None)
    time.sleep = lambda s: None
    def offline(*a, **k):
        raise urllib.error.URLError("offline")
    urllib.request.urlopen = offline
    import nightly
    rows = [r for r in csv.DictReader(open("ledger/xsec/2026-09.csv"))
            if r["date"] == "2026-09-23"]
    def f(x):
        try: return float(x)
        except (TypeError, ValueError): return None
    mk = []
    for r in rows:
        p = f(r["price"]) or 1.0
        mk.append({"id": r["symbol"].lower(), "symbol": r["symbol"].lower(),
                   "name": r["symbol"], "current_price": p,
                   "market_cap": f(r["market_cap"]),
                   "total_volume": f(r["total_volume"]) or 0.0,
                   "fully_diluted_valuation": f(r["fdv_usd"]),
                   "price_change_percentage_24h": f(r["price_chg_24h"]),
                   "high_24h": p * 1.02, "low_24h": p * 0.98,
                   **{f"price_change_percentage_{d}d_in_currency": f(r[f"rs{d}"])
                      for d in (7, 14, 30, 200)}})
    nightly.fetch_markets = lambda **k: mk
    touched = []
    real = nightly.rwa.snapshot
    nightly.rwa.snapshot = lambda *a, **k: touched.append(1) or real(*a, **k)
    rc = nightly.main()
    print("RWA_CALLED", len(touched))
    sys.exit(rc)
''')


def _rwa_files(ledger):
    """Every RWA ledger FILE, keyed by path relative to the ledger — recursive, because
    the flow, wrapper and observation ledgers are month shards under ledger/rwa/. A flat
    glob("rwa*") would see only the directory and silently stop covering them."""
    return {str(p.relative_to(ledger)): p.read_bytes() for p in sorted(ledger.rglob("*"))
            if p.is_file() and p.relative_to(ledger).parts[0].startswith("rwa")}


def test_a_run_out_of_budget_skips_the_optional_stages_and_still_publishes(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    for name in ("nightly.py", "funding.py", "quant.py", "coingecko.py", "cryptometer.py",
                 "rwa.py", "contract_specs.json"):
        shutil.copy(ROOT / name, work / name)
    shutil.copytree(ROOT / "ledger", work / "ledger")
    rwa_before = _rwa_files(work / "ledger")
    (work / "harness.py").write_text(HARNESS)
    info = tmp_path / "run.json"
    env = {**os.environ, "NIGHTLY_BUDGET_S": "0", "NIGHTLY_RUN_INFO": str(info)}
    res = subprocess.run([sys.executable, "harness.py"], cwd=work, env=env,
                         capture_output=True, text=True, timeout=300)
    assert res.returncode == 0, res.stderr[-3000:]
    run = json.loads(info.read_text())
    skipped = run["budget_skipped"]
    for stage in ("cryptometer liquidations", "dex depth", "rwa snapshot"):
        assert stage in skipped, skipped
    assert "RWA_CALLED 0" in res.stdout, "the RWA snapshot ran with no budget left"
    assert run["rwa_status"] == "skipped: run budget"
    # The optional stages never touched the RWA ledger ...
    assert _rwa_files(work / "ledger") == rwa_before
    assert any(k.startswith("rwa/flow/") for k in rwa_before), "the walk missed the shards"
    # ... and the mandatory work ran in full: tonight's rows and the transport exist.
    assert run["outcome"] == "completed"
    assert run["scored"] > 200 and run["persisted"] == 50 and run["xsec_rows"] > 200
    doc = json.loads((work / "ledger" / "perp.json").read_text())
    assert doc["as_of"] == run["date"] == run["perp_as_of"]
    with (work / "ledger" / "signals.csv").open(newline="", encoding="utf-8") as fh:
        assert any(r["date"] == run["date"] for r in csv.DictReader(fh))


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------
STEPS_OK = {"nightly_step": "success", "rwa_gate": "success", "parity_gate": "success",
            "crypto_gate": "success", "atr_gate": "success"}


def test_the_manifest_is_append_only(tmp_path):
    path = tmp_path / "runs.csv"
    nightly.append_run_row({"date": "2026-09-24", "outcome": "completed"}, path)
    first = path.read_bytes()
    nightly.append_run_row({"date": "2026-09-24", "outcome": "crashed at markets"}, path)
    assert path.read_bytes().startswith(first), "a prior run row was rewritten"
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["outcome"] for r in rows] == ["completed", "crashed at markets"]
    assert tuple(rows[0].keys()) == nightly.RUN_FIELDS


def test_the_row_says_what_each_commit_step_will_do(tmp_path):
    (tmp_path / "perp.json").write_text(json.dumps({"as_of": "2026-09-24"}))
    ok = record_run.build_row({"date": "2026-09-24"}, STEPS_OK, tmp_path, env={})
    assert (ok["crypto_publish"], ok["rwa_publish"]) == ("commit", "commit")
    assert ok["perp_as_of"] == "2026-09-24" and ok["event"] == "local"
    rwa_refused = record_run.build_row({}, {**STEPS_OK, "rwa_gate": "failure"}, tmp_path, env={})
    assert (rwa_refused["crypto_publish"], rwa_refused["rwa_publish"]) == ("commit", "withheld")
    assert rwa_refused["perp_as_of"] == "2026-09-24"
    crypto_refused = record_run.build_row({}, {**STEPS_OK, "crypto_gate": "failure"},
                                          tmp_path, env={})
    assert (crypto_refused["crypto_publish"], crypto_refused["rwa_publish"]) == ("withheld", "commit")
    # A date of a file that will not be committed is never recorded as published.
    assert crypto_refused["perp_as_of"] == "withheld:2026-09-24"
    # nightly.py failing after writing the RWA rows does not withhold them by itself.
    crashed = record_run.build_row({}, {"nightly_step": "failure", "rwa_gate": "success"},
                                   tmp_path, env={})
    assert crashed["rwa_publish"] == "commit" and crashed["crypto_publish"] == "withheld"


def test_a_run_that_never_reported_is_recorded_as_such(tmp_path):
    row = record_run.build_row({}, {"nightly_step": "failure"}, tmp_path,
                               env={"GITHUB_RUN_ID": "42", "GITHUB_EVENT_NAME": "schedule",
                                    "GITHUB_SHA": "abcdef0123456789"})
    assert row["outcome"].startswith("no run record")
    assert (row["run_id"], row["event"], row["sha"]) == ("42", "schedule", "abcdef012345")
    assert row["crypto_publish"] == "withheld" and row["rwa_publish"] == "withheld"


def test_the_cli_appends_one_row(tmp_path):
    info = tmp_path / "run.json"
    info.write_text(json.dumps({"date": "2026-09-24", "outcome": "completed",
                                "snapshot_ts": "2026-09-24T11:42:03+00:00"}))
    args = ["--ledger", str(tmp_path), "--info", str(info)] + \
        [a for k, v in STEPS_OK.items() for a in ("--step", f"{k}={v}")]
    assert record_run.main(args) == 0
    assert record_run.main(args) == 0
    with (tmp_path / "runs.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2 and rows[0]["snapshot_ts"] == "2026-09-24T11:42:03+00:00"
    assert record_run.main(["--ledger", str(tmp_path), "--step", "bogus=x"]) == 2


def test_nothing_backfills_the_missing_nights():
    """2026-09-21 and -22 never ran. The manifest starts when it starts."""
    path = ROOT / "ledger" / "runs.csv"
    if not path.exists():
        pytest.skip("no run recorded yet")
    with path.open(newline="", encoding="utf-8") as fh:
        dates = {r["date"] for r in csv.DictReader(fh)}
    assert not dates & {"2026-09-21", "2026-09-22"}
