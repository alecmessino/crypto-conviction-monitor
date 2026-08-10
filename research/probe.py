#!/usr/bin/env python3
"""Re-measure every claim in FINDINGS.md against live keyless crypto sources.

Sources, all unauthenticated:
  * CoinGecko  /coins/{id}/market_chart  -- 365 days of daily closes
  * Deribit    /public/get_volatility_index_data -- DVOL, a real implied-vol index
                for BTC and ETH with two years of history

Deribit is the reason the VRP pillar is better measured here than on the equity side:
DVOL is a published implied-vol index, whereas per-name equity IV has to be rebuilt
from option chains. Note the coverage limit -- DVOL exists for BTC and ETH only, so
pillar 2 covers two assets, not the top 25.

    python3 research/probe.py [--coins bitcoin,ethereum,...] [--json out.json]
"""
from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "structural_yield", HERE / "structural_yield.py")
sy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sy)

CG = "https://api.coingecko.com/api/v3/coins/{cid}/market_chart?vs_currency=usd&days=364&interval=daily"
DVOL = ("https://www.deribit.com/api/v2/public/get_volatility_index_data"
        "?currency={cur}&start_timestamp={start}&end_timestamp={end}&resolution=43200")

DEFAULT_COINS = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "binancecoin": "BNB",
    "ripple": "XRP", "cardano": "ADA", "avalanche-2": "AVAX", "chainlink": "LINK",
    "litecoin": "LTC", "polkadot": "DOT",
}
UA = {"User-Agent": "structural-yield-probe/1.0"}

# Crypto trades continuously: 365 periods a year, not 252.
PERIODS = 365


def get(url: str, tries: int = 5):
    for a in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=45) as fh:
                return json.load(fh)
        except Exception as exc:                                # noqa: BLE001
            print(f"  retry {a + 1}: {exc}", file=sys.stderr)
            time.sleep(8 * (a + 1))
    return None


def load_prices(coins: dict[str, str], delay: float = 4.0) -> dict[str, list[float]]:
    """Daily closes per symbol. Sleeps between calls -- the free tier rate-limits."""
    px = {}
    for cid, sym in coins.items():
        d = get(CG.format(cid=cid))
        if d and d.get("prices"):
            px[sym] = [p[1] for p in d["prices"]]
            print(f"{sym:5s} {len(px[sym])} daily closes", file=sys.stderr)
        time.sleep(delay)
    return px


def pillar1(px: dict) -> dict:
    found = []
    for a, b in itertools.combinations(sorted(px), 2):
        n = min(len(px[a]), len(px[b]))
        r = sy.engle_granger(px[a][-n:], px[b][-n:])
        if r and r["adf"] is not None:
            found.append((a, b, r))
    stats = [r["adf"] for _, _, r in found]
    cutoff = sy.benjamini_hochberg(stats, 0.05) if stats else None
    naive = [x for x in found if x[2]["adf"] < sy.EG_CRITICAL[0.05]]
    survivors = [(a, b, r) for a, b, r in found if cutoff and sy.tradeable(r, cutoff)]
    # The thesis's own example, reported whether or not it passes.
    named = next((r for a, b, r in found if {a, b} == {"BTC", "ETH"}), None)
    return {
        "tested": len(found),
        "expected_false_positives_5pct": round(0.05 * len(found), 1),
        "passed_naive_5pct": len(naive),
        "fdr_cutoff": round(cutoff, 2) if cutoff else None,
        "btc_eth": ({"adf": round(named["adf"], 2),
                     "beta": round(named["hedge_ratio"], 3),
                     "half_life_d": round(named["half_life"], 1) if named["half_life"] else None,
                     "passes": bool(cutoff and sy.tradeable(named, cutoff))}
                    if named else None),
        "tradeable_after_all_gates": [
            {"pair": f"{a}/{b}", "adf": round(r["adf"], 2),
             "beta": round(r["hedge_ratio"], 2),
             "half_life_d": round(r["half_life"], 1), "z": round(r["z"], 2)}
            for a, b, r in survivors],
        "caveat": ("BH assumes the tests are near-independent. Pairs sharing a leg are "
                   "not, so a survivor set concentrated in one asset is optimistic."),
    }


def pillar2(px: dict) -> dict:
    now = int(time.time() * 1000)
    out = {}
    for cur in ("BTC", "ETH"):
        if cur not in px:
            continue
        d = get(DVOL.format(cur=cur, start=now - 365 * 86400 * 1000, end=now))
        data = (d or {}).get("result", {}).get("data") or []
        if not data:
            continue
        daily = [row[4] / 100.0 for row in data][::2]           # 12h bars -> ~daily
        closes = px[cur]
        k = min(len(daily), len(closes))
        daily, closes = daily[-k:], closes[-k:]
        # Forward VRP: DVOL on day i against the vol realised over the NEXT 30 days.
        spreads = []
        for i in range(k - 30):
            fwd = sy.realized_vol(closes[i:i + 31], 30, periods=PERIODS)
            if fwd is not None:
                spreads.append(daily[i] - fwd)
        spreads.sort()
        rv30 = sy.realized_vol(px[cur], 30, periods=PERIODS)
        out[cur] = {
            "dvol_now": round(daily[-1], 4),
            "rv30_trailing": round(rv30, 4),
            "spread_now": round(daily[-1] - rv30, 4),
            "forward_vrp": {
                "n_overlapping": len(spreads),
                "mean": round(sum(spreads) / len(spreads), 4),
                "median": round(spreads[len(spreads) // 2], 4),
                "pct_positive": round(sum(1 for s in spreads if s > 0) / len(spreads), 3),
                "worst": round(spreads[0], 4),
            } if spreads else None,
        }
    out["coverage_note"] = "DVOL covers BTC and ETH only; the other 23 have no IV index."
    return out


def pillar3(px: dict) -> dict:
    syms = sorted(px)
    m = min(len(px[s]) for s in syms)
    series = [px[s][-m:] for s in syms]
    w = [1 / len(syms)] * len(syms)
    cov = sy.cov_matrix(series, periods=PERIODS)
    gamma = sy.excess_growth(cov, w)
    years = (m - 1) / PERIODS
    res = {"basket": syms, "days": m,
           "excess_growth_annual": round(gamma, 4),
           "theorem_predicts_ratio": round(math.exp(gamma * years), 4),
           "avg_constituent_vol": round(
               sum(math.sqrt(cov[i][i]) for i in range(len(syms))) / len(syms), 3),
           "portfolio_vol": round(math.sqrt(sum(
               w[i] * cov[i][j] * w[j] for i in range(len(syms))
               for j in range(len(syms)))), 3)}
    for every, label in ((1, "daily"), (7, "weekly"), (30, "monthly")):
        bt = sy.rebalance_backtest(series, w, every=every)
        res[label] = {
            "rebalanced": round(bt["rebalanced"], 4),
            "buy_and_hold": round(bt["buy_and_hold"], 4),
            "geometric": round(bt["geometric"], 4),
            "measured_ratio_vs_geometric": round(bt["rebalanced"] / bt["geometric"], 4),
            "ratio_vs_buy_and_hold": round(bt["rebalanced"] / bt["buy_and_hold"], 4),
            "turnover": round(bt["turnover"], 2),
        }
    return res


def pillar4(px: dict) -> dict:
    per, total, pooled = {}, 0, []
    for s in sorted(px):
        closes = px[s]
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
                if closes[i - 1] > 0]
        hits, fwd = 0, []
        for i in range(60, len(rets) - 5):
            hist = rets[i - 60:i]
            mu = sum(hist) / 60
            sd = math.sqrt(sum((r - mu) ** 2 for r in hist) / 59)
            if sd and (rets[i] - mu) / sd <= -3:
                hits += 1
                fwd.append(sum(rets[i + 1:i + 6]))
        total += hits
        pooled.extend(fwd)
        ev = sy.sigma_event(closes, 60)
        per[s] = {"events_1y": hits,
                  "sigma_today": round(ev["sigma"], 2) if ev else None}
    return {
        "per_symbol": per,
        "events_across_universe_1y": total,
        "pooled_mean_fwd_5d_pct": round(100 * sum(pooled) / len(pooled), 2) if pooled else None,
        "pooled_win_rate": round(sum(1 for f in pooled if f > 0) / len(pooled), 2) if pooled else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default=",".join(DEFAULT_COINS))
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    coins = {c.strip(): DEFAULT_COINS.get(c.strip(), c.strip().upper()[:4])
             for c in args.coins.split(",") if c.strip()}

    px = load_prices(coins)
    if len(px) < 2:
        print("insufficient price data", file=sys.stderr)
        return 1

    out = {
        "symbols": sorted(px),
        "pillar1_cointegration": pillar1(px),
        "pillar2_vrp": pillar2(px),
        "pillar3_harvest": pillar3(px),
        "pillar4_overreaction": pillar4(px),
        "regime": {},
    }
    btc = px.get("BTC")
    if btc:
        ts = sy.trend_strength(btc, 60)
        vrp_now = out["pillar2_vrp"].get("BTC", {}).get("spread_now")
        out["regime"] = {"btc_trend_strength_60d": round(ts, 3) if ts else None,
                         "btc_vrp_spread": vrp_now,
                         **sy.regime_ok(vrp_now, ts)}
    text = json.dumps(out, indent=2)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
