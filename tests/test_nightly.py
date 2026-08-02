"""Tests for nightly.py engine: rebalance hysteresis (A), ejection delta (C),
execution-adjusted counterfactual (B), and macro regime (D, passive).

These run without network: fetch_global_market_cap and fetch_markets are
monkeypatched. The hysteresis test is the key regression guard for the
#10<->#11 churn fix.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
NIGHTLY = HERE.parent / "nightly.py"

spec = importlib.util.spec_from_file_location("nightly_test_mod", NIGHTLY)
assert spec is not None, f"could not load spec for {NIGHTLY}"
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)


def _mk(sym, price, mc, chg=0.0):
    return {
        "symbol": sym, "name": sym, "current_price": price,
        "market_cap": mc, "total_volume": mc * 0.10,
        "price_change_percentage_24h": chg,
        "ath": price * 2, "atl": price * 0.5,
        "high_24h": price * 1.02, "low_24h": price * 0.98,
        "fully_diluted_valuation": mc * 1.1,
    }


def _markets(n=15):
    # 15 assets, descending market cap so stable ordering
    out = []
    for i in range(n):
        sym = f"T{i:02d}"
        mc = 1e10 / (i + 1)
        out.append(_mk(sym, 1.0 + i * 0.1, mc, chg=2.0))
    return out


def test_hysteresis_keeps_rank10_rank11_flip(monkeypatch, tmp_path):
    """Swapping #10 and #11 must NOT trigger a rebalance (turnover drag guard)."""
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "BASKET_JSON", tmp_path / "basket.json")
    monkeypatch.setattr(nightly, "INDEX_CSV", tmp_path / "index.csv")
    monkeypatch.setattr(nightly, "INDEX_JSON", tmp_path / "index.json")
    monkeypatch.setattr(nightly, "fetch_global_market_cap", lambda: 1e12)

    markets = _markets()
    today = "2026-08-02"

    # First run: establishes the basket (genesis rebalance)
    nightly.build_basket(markets, today)
    idx1 = json.loads((tmp_path / "index.json").read_text())
    assert idx1["latest"]["rebalanced"] is True
    prev_syms = {h["ticker"] for h in idx1["current_holdings"]}
    assert len(prev_syms) == 10

    # Swap #10 (T09) and #11 (T10): give #11 higher mcap so it ranks above #10.
    markets2 = _markets()
    for m in markets2:
        if m["symbol"] == "T10":
            m["market_cap"] = 1e10 / 9.5  # between T08 and T09
        if m["symbol"] == "T09":
            m["market_cap"] = 1e10 / 10.5  # now below T10

    nightly.build_basket(markets2, today)
    idx2 = json.loads((tmp_path / "index.json").read_text())
    # No one fell to rank >=13 and no score gap >5, so NO rebalance.
    assert idx2["latest"]["rebalanced"] is False, "hysteresis should suppress #10<->#11 flip"
    assert {h["ticker"] for h in idx2["current_holdings"]} == prev_syms


def test_hysteresis_ejects_on_rank13_drop(monkeypatch, tmp_path):
    """An asset falling to rank >=13 must be ejected (hysteresis boundary)."""
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "BASKET_JSON", tmp_path / "basket.json")
    monkeypatch.setattr(nightly, "INDEX_CSV", tmp_path / "index.csv")
    monkeypatch.setattr(nightly, "INDEX_JSON", tmp_path / "index.json")
    monkeypatch.setattr(nightly, "fetch_global_market_cap", lambda: 1e12)

    markets = _markets()
    nightly.build_basket(markets, "2026-08-02")
    idx1 = json.loads((tmp_path / "index.json").read_text())
    prev_syms = {h["ticker"] for h in idx1["current_holdings"]}

    # Drop T09 (was rank 10) far down: make its mcap tiny so it ranks last.
    markets2 = _markets()
    for m in markets2:
        if m["symbol"] == "T09":
            m["market_cap"] = 1.0  # ranks ~15th

    nightly.build_basket(markets2, "2026-08-02")
    idx2 = json.loads((tmp_path / "index.json").read_text())
    assert idx2["latest"]["rebalanced"] is True
    assert "T09" not in {h["ticker"] for h in idx2["current_holdings"]}
    assert "T10" in {h["ticker"] for h in idx2["current_holdings"]}  # promoted in


def test_exec_adjusted_zero_on_genesis(monkeypatch, tmp_path):
    """Genesis day charges no execution cost (no prior basket)."""
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "BASKET_JSON", tmp_path / "basket.json")
    monkeypatch.setattr(nightly, "INDEX_CSV", tmp_path / "index.csv")
    monkeypatch.setattr(nightly, "INDEX_JSON", tmp_path / "index.json")
    monkeypatch.setattr(nightly, "fetch_global_market_cap", lambda: 1e12)

    nightly.build_basket(_markets(), "2026-08-02")
    row = json.loads((tmp_path / "index.json").read_text())["latest"]
    assert row["turnover_bps"] == 0.0
    assert row["exec_adjusted_return"] == 0.0


def test_macro_regime_passive_na_without_history(monkeypatch, tmp_path):
    """Macro regime is passive: returns N/A with no ledger history, no error."""
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "INDEX_CSV", tmp_path / "index.csv")
    assert nightly._macro_regime_from_ledger() == "N/A"
