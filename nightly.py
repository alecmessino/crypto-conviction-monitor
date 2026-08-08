#!/usr/bin/env python3
"""Standalone nightly signal persistence for the public monitor.

Self-contained so it runs in CI without the full launch_skew package.
Appends one row per (date, symbol) to ledger/signals.csv + ledger/signals.json
so a REAL backtest ledger accumulates over time. No fabricated history.

Dune: if DUNE_API_KEY + DUNE_UNLOCK_QUERY_ID are set, unlock context is
fetched; otherwise Module B columns stay null (never fabricated).
"""
from __future__ import annotations

import csv
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

LEDGER_DIR = Path(__file__).resolve().parent / "ledger"
LEDGER_CSV = LEDGER_DIR / "signals.csv"
LEDGER_JSON = LEDGER_DIR / "signals.json"
FIELDS = ["date", "symbol", "name", "price", "market_cap", "turnover_pct",
          "erosion_ratio", "conviction", "signal",
          "rs7", "rs14", "rs30", "rs200", "rs_blend",
          "c_liquidity", "c_era", "c_depth", "c_momentum",
          "unlocks_usd", "supply_increase_pct", "addr_growth_pct", "era",
          "roi_30d", "roi_90d", "survived", "perp_mult"]

# Dune Analytics (Module B: vesting / emission-vs-adoption ERA).
# Key is read ONLY from env DUNE_API_KEY (supplied by the CI secret). Never hardcoded.
# A public unlock-schedule query is configured via DUNE_UNLOCK_QUERY_ID.
DUNE_BASE = "https://api.dune.com/api/v1"
# CoinGecko free markets endpoint (separate host from Dune).
CG_BASE = "https://api.coingecko.com/api/v3"

STABLES = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD", "USDE",
           "USD1", "USDS", "PYUSD", "GUSD", "USDG", "FRAX", "USDD", "TUSD",
           "XAUT", "PAXG"}




def _get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "conviction-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec
        return json.loads(resp.read().decode())


def fetch_dune_module_b(query_id: str, api_key: str) -> dict:
    """Fetch a saved Dune query's latest results -> {SYMBOL: {unlocks_usd, supply_increase_pct,
    addr_growth_pct, era}}. Returns {} if the call fails — caller falls back to null (no fabricate).

    Expected query shape (columns): symbol, unlocks_usd, supply_increase_pct, addr_growth_pct.
    ERA = supply_increase_pct / addr_growth_pct when both present and addr_growth_pct > 0.
    """
    out: dict = {}
    try:
        url = f"{DUNE_BASE}/query/{query_id}/results?limit=5000"
        data = _get_json(url, headers={"X-Dune-Api-Key": api_key})
        rows = (data.get("result") or {}).get("rows") or []
        for r in rows:
            sym = str(r.get("symbol") or r.get("token") or r.get("SYMBOL") or "").upper()
            if not sym:
                continue
            unlocks = r.get("unlocks_usd") or r.get("unlocks") or r.get("UNLOCKS_USD")
            sup = r.get("supply_increase_pct") or r.get("supply_increase")
            addr = r.get("addr_growth_pct") or r.get("address_growth")
            era = r.get("era")
            rec = {"unlocks_usd": _num(unlocks), "supply_increase_pct": _num(sup),
                   "addr_growth_pct": _num(addr), "era": _num(era)}
            if rec["era"] is None and sup is not None and addr not in (None, 0, "0"):
                try:
                    rec["era"] = round(float(sup) / float(addr), 3)
                except (ValueError, ZeroDivisionError):
                    rec["era"] = None
            out[sym] = rec
    except Exception as e:  # noqa: BLE001
        print(f"[dune] fetch failed, Module B -> null: {e}", file=__import__("sys").stderr)
    return out


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def fetch_markets(total: int = 250, per_page: int = 125, delay: float = 3.5) -> list[dict]:
    """Fetch the full universe in chunked pages with exponential backoff on 429.

    Splits `total` coins across multiple /coins/markets pages (CoinGecko caps
    per_page at 250 and rate-limits free keys), sleeping between calls so the
    job stays under the per-IP budget. On HTTP 429 it backs off and retries.
    """
    import time
    out: list[dict] = []
    pages = max(1, (total + per_page - 1) // per_page)
    for page in range(1, pages + 1):
        url = (f"{CG_BASE}/coins/markets?vs_currency=usd"
               f"&order=market_cap_desc&per_page={per_page}&page={page}"
               f"&price_change_percentage=24h,7d,14d,30d,200d")
        backoff = 5.0
        for attempt in range(4):
            try:
                data = _get_json(url)
                if isinstance(data, list):
                    out.extend(data)
                break
            except urllib.error.HTTPError as e:
                if getattr(e, "code", None) == 429:
                    print(f"[429] page {page} attempt {attempt+1}: backing off {backoff:.0f}s",
                          file=__import__("sys").stderr)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                else:
                    print(f"[warn] page {page} HTTP {getattr(e, 'code', '?')}; skipping",
                          file=__import__("sys").stderr)
                    break
        else:
            print(f"[warn] page {page} gave up after retries", file=__import__("sys").stderr)
        time.sleep(delay)
    return out


def score(t: dict, perps_map: dict | None = None,
          btc: dict | None = None) -> tuple[float, int, str, dict]:
    """Conviction score (0-100) with multi-timeframe relative strength.

    Momentum (Module C) now uses RELATIVE STRENGTH vs BTC across 7d/14d/30d/200d
    instead of 24h price change alone — 24h is mostly noise; a coin quietly
    outperforming BTC over 30-200d is the structural signal. Returns
    (era, total, sig, components) where components decomposes the score for
    attribution (liquidity/era/depth/momentum/rs_blend/perp_mult).
    """
    mc = t.get("market_cap") or 0
    vol = t.get("total_volume") or 0
    chg = t.get("price_change_percentage_24h") or 0.0
    turnover = (vol / mc) if mc else 0.0

    # --- Multi-timeframe relative strength vs BTC (7/14/30/200d; 90d is null
    # on the free tier, so it is intentionally excluded — no fabrication) ---
    def _pct(tf: int) -> float:
        return float(t.get(f"price_change_percentage_{tf}d_in_currency") or 0.0)
    def _btc(tf: int) -> float:
        return float((btc or {}).get(f"price_change_percentage_{tf}d_in_currency") or 0.0)
    rs = {tf: _pct(tf) - _btc(tf) for tf in (7, 14, 30, 200)}
    # Blended RS: weight recent Horizons but keep the long window loud enough
    # to surface quiet 60-200d outperformers. Volatility-adjustment deferred
    # (needs a return series the free tier doesn't provide).
    rs_blend = 0.30 * rs[7] + 0.25 * rs[14] + 0.25 * rs[30] + 0.20 * rs[200]

    # Module A (0-30): liquidity fit, soft curve, peak 30-60%
    if turnover <= 0:
        a = 0
    elif turnover <= 0.30:
        a = 10 + (turnover / 0.30) * 20
    elif turnover <= 0.60:
        a = 30 - abs(turnover - 0.45) / 0.15 * 6
    elif turnover <= 1.20:
        a = 20 - (turnover - 0.60) / 0.60 * 12
    else:
        a = max(2, 8 - (turnover - 1.20) * 4)

    # Module B (0-30): ERA proxy from 24h stability (Dune overrides if present)
    import math
    ag = 15 if abs(chg) < 5 else 10 if abs(chg) < 15 else 5
    era = 5.0 / ag
    b = 20 if era < 0.7 else 15 if era < 1.0 else 10 if era < 1.5 else 5 if era < 2.0 else 0

    # v2 MULTIPLICATIVE conviction = Quality x Confirmation x RiskAdjustment.
    # This replaces the additive model that saturated momentum at a hard clamp
    # (PUMP and HYPE collided at c_momentum=20). Composition:
    #   Q (Structural Quality)   = log-mcap depth, 0-1        (thin caps shrink the floor)
    #   C (Market Confirmation)  = soft sigmoid over RS_blnd, 0-1  (NO clamp -> rankable within tier)
    #   R (Risk Adjustment)      = mcap-aware liquidity, 0.4-1.0  (low turnover does NOT punish blue-chips)
    # A high-mcap asset (depth~1) with low turnover keeps R=1.0; only micro-caps /
    # low-depth names get the liquidity haircut. This is what pushes PUMP below HYPE/ADA.
    depth = max(0.0, min(1.0, (math.log10(mc) - 6) / 4.0)) if mc else 0
    cm = 0.10 + 0.90 * ((math.tanh(rs_blend / 25.0) + 1.0) / 2.0)  # confirmation, [0.10,0.91]
    # Risk: liquidity-fit fraction, but floored to 1.0 (no penalty) for established
    # depth (>=0.90 log-mcap) so blue-chip perps are not punished for low %.
    if depth >= 0.90:
        a_frac = 1.0
    else:
        if turnover <= 0:
            a_frac = 0.0
        elif turnover <= 0.30:
            a_frac = (10 + (turnover / 0.30) * 20) / 30.0
        elif turnover <= 0.60:
            a_frac = (30 - abs(turnover - 0.45) / 0.15 * 6) / 30.0
        elif turnover <= 1.20:
            a_frac = (20 - (turnover - 0.60) / 0.60 * 12) / 30.0
        else:
            a_frac = max(2.0, 8 - (turnover - 1.20) * 4) / 30.0
        a_frac = max(0.4, a_frac)  # never zero out a name; cap the haircut at 60%
    risk = a_frac
    if perps_map is not None:
        risk *= lavl_perp_mult((t.get("symbol") or "").upper(), perps_map)

    total = max(0, min(100, int(round(100 * depth * cm * risk))))
    sig = "STRONG" if total >= 80 else "BUY" if total >= 70 else "HOLD" if total >= 55 \
        else "WATCH" if total >= 40 else "AVOID"
    comp = {
        "liquidity": round(a_frac * 30, 1), "era": round(b, 1),
        "depth": round(depth * 20, 1), "momentum": round(cm * 20, 1),  # confirmation, display-scaled
        "risk_adjustment": round(risk, 3), "rs_blend": round(rs_blend, 2),
        "rs7": round(rs[7], 2), "rs14": round(rs[14], 2),
        "rs30": round(rs[30], 2), "rs200": round(rs[200], 2),
        "perp_mult": round(lavl_perp_mult((t.get("symbol") or "").upper(),
                                           perps_map or {}), 3),
    }
    return era, total, sig, comp


def _lavl_regime(t: dict) -> str:
    """Lightweight LAVL regime (mirrors lavl.py) for the conjunctive gate.

    Uses only free-payload fields: 24h change, 24h range, vol/mc, range-tightness.
    Perp multiplier is neutral (1.0) until a derivatives feed is wired.
    """
    price = t.get("current_price") or 0
    chg = t.get("price_change_percentage_24h") or 0.0
    high = t.get("high_24h") or price or 1
    low = t.get("low_24h") or price or 1
    vol = t.get("total_volume") or 0
    mc = t.get("market_cap") or 0
    if price <= 0 or mc <= 0:
        return "COMPRESS"
    spread = (high - low) / price if price else 1
    if spread <= 0:
        spread = 0.01
    depth = vol / mc if mc else 0
    if depth <= 0:
        depth = 1e-6
    velo = (abs(chg) / 100.0 / spread) * math.log(max(2.0, depth * 1e6)) if spread else 0
    velo = min(4.0, velo)
    range_tight = 1 - (high - low) / high if high else 0
    diverge = (vol / mc) * max(0.0, range_tight)
    diverge = min(2.3, diverge)
    lavl = 0.6 * velo + 0.4 * diverge  # risk_mult neutral (1.0)
    if lavl > 2.5:
        return "ALPHA RUSH"
    if lavl >= 0.5:
        return "STABLE"
    return "COMPRESS"


def _conjunctive_gate(t: dict, conv: int) -> bool:
    """Replicates the front-end gated flag so the basket uses the same universe."""
    mc = t.get("market_cap") or 0
    vol = t.get("total_volume") or 0
    turnover = (vol / mc) if mc else 0.0
    fdv = t.get("fully_diluted_valuation") or 0
    dilution = (fdv / mc) if mc else None
    gate_a = 0.30 <= turnover <= 0.60
    gate_b = (dilution is None) or (dilution <= 2.0)
    band = _lavl_regime(t)
    gate_c = band not in ("COMPRESS", "LIQ TRAP") and turnover < 0.90
    return bool(gate_a and gate_b and gate_c)


def fetch_perps_map(symbols: set[str] | None = None) -> dict:
    """Live perpetual funding rates -> {BASE: {funding_rate, oi_usd}}.

    Primary: Bybit V5 linear tickers (one call, full map, has funding + OI).
    Fallback: OKX public funding-rate, fetched per-instrument but ONLY for the
    symbols we actually score (bounded ~10-50 calls) when `symbols` is given;
    if omitted, OKX is skipped to avoid 400+ requests.
    Both key-less. Returns {} on total failure -> callers use neutral 1.0.
    No fabrication: missing perps simply fall back to neutral per-asset.
    """
    # Primary: Bybit linear perps
    try:
        data = _get_json("https://api.bybit.com/v5/market/tickers?category=linear")
        items = (data.get("result") or {}).get("list") or []
        m = {}
        for it in items:
            sym = (it.get("symbol") or "")
            if sym.endswith("USDT"):
                base = sym[:-4].upper()
                m[base] = {
                    "funding_rate": float(it.get("fundingRate") or 0.0),
                    "oi_usd": float(it.get("openInterestValue") or 0.0),
                }
        if m:
            return m
    except Exception as e:  # noqa: BLE001
        print(f"[perp] Bybit unavailable ({e}); trying OKX.", file=__import__("sys").stderr)
    # Fallback: OKX funding-rate, per requested symbol (bounded)
    if not symbols:
        print("[perp] OKX fallback skipped (no symbol filter); neutral.", file=__import__("sys").stderr)
        return {}
    m = {}
    for base in symbols:
        inst = f"{base}-USDT-SWAP"
        try:
            data = _get_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}")
            row = (data.get("data") or [{}])[0]
            m[base.upper()] = {"funding_rate": float(row.get("fundingRate") or 0.0), "oi_usd": None}
        except Exception:  # noqa: BLE001
            continue  # per-asset neutral fallback
    return m


def lavl_perp_mult(ticker: str, perps_map: dict) -> float:
    """LAVL leverage-micro-regime multiplier (RiskMult_perp).

    - funding > +0.05% per interval => overheated longs, liquidation flush risk => penalize (0.85)
    - funding < 0 (shorts paying) => capitulation / squeeze asymmetry => reward (1.15)
    - otherwise neutral 1.0; spot-only microcaps (no perp) default safely to 1.0.
    """
    info = perps_map.get(ticker)
    if not info:
        return 1.0
    fr = info.get("funding_rate") or 0.0
    if fr > 0.0005:
        return 0.85
    if fr < 0.0:
        return 1.15
    return 1.0

def fetch_global_market_cap() -> float | None:
    """Total crypto market cap (USD) from CoinGecko's free /global endpoint.

    Used as the apples-to-apples macro benchmark: benchmark_total_return is
    current_global / entry_global, computed over the same window as the basket.
    Returns None on failure so the benchmark falls back to neutral (no fabrication).
    """
    try:
        data = _get_json("https://api.coingecko.com/api/v3/global")
        return float(data.get("data", {}).get("total_market_cap", {}).get("usd", 0) or 0) or None
    except Exception as e:  # noqa: BLE001
        print(f"[global] fetch failed: {e}", file=__import__("sys").stderr)
        return None


def _risk_stats(idx_rows: list[dict]) -> dict | None:
    """Risk statistics derived purely from the ledger (no fabrication).

    Sharpe: rf=0, mean(daily basket_return) / std(daily basket_return) * sqrt(365).
    Max drawdown: largest peak-to-trough decline of the cumulative basket curve.
    Only meaningful with enough history; caller gates on len(idx_rows) >= 30.
    """
    if len(idx_rows) < 30:
        return None
    rets = [float(r.get("basket_return") or 0) / 100.0 for r in idx_rows]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    std = math.sqrt(var) if var > 0 else 0.0
    sharpe = (mean / std) * math.sqrt(365) if std > 0 else 0.0
    cum = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        cum *= (1 + r)
        peak = max(peak, cum)
        if peak > 0:
            max_dd = min(max_dd, (cum - peak) / peak)
    return {
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "n_days": len(rets),
        "convention": "rf=0; annualized daily x sqrt(365)",
    }


# A tier change caused by a move no larger than this is the label crossing a threshold,
# not a change of view. Matched to the equity terminal, where the same diagnostic
# measured 47 of 73 overnight tier changes falling inside it — a "new BUY" list built
# without the distinction is wrong in detail most mornings.
MARGINAL_MOVE = 2.0

# The same cuts _score() applies. Used only to fill a tier for a historical row that
# predates the signal column; a row that recorded its own signal keeps it, so an old
# night is never silently reinterpreted under today's thresholds.
TIER_CUTS = ((80, "STRONG"), (70, "BUY"), (55, "HOLD"), (40, "WATCH"), (0, "AVOID"))


def _tier_for(conviction: float) -> str:
    for cut, name in TIER_CUTS:
        if conviction >= cut:
            return name
    return "AVOID"


def _compute_tier_diff() -> dict:
    """Overnight tier transitions, separating real reclassifications from boundary noise.

    This is a diff, not a rule. Nothing here feeds back into scoring. The alternative
    fix for boundary churn is hysteresis — refusing to change a tier until a name moves
    far enough past the threshold — and that puts a memory of yesterday inside tonight's
    score, so a name's tier would depend on the path it took to reach its conviction
    rather than on the conviction itself. The score stays a pure function of tonight's
    inputs; the presentation carries the caveat.

    Derived entirely from the signals ledger. No new feeds.
    """
    if not LEDGER_CSV.exists():
        return {}
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    by_date: dict[str, dict[str, dict]] = {}
    for r in rows:
        d, sym = r.get("date"), r.get("symbol")
        if not d or not sym:
            continue
        try:
            conv = float(r.get("conviction") or 0)
        except ValueError:
            continue
        by_date.setdefault(d, {})[sym] = {
            "conviction": conv,
            "tier": r.get("signal") or _tier_for(conv),
            "name": r.get("name") or sym,
        }

    dates = sorted(by_date)
    if len(dates) < 2:
        return {"from": None, "to": dates[-1] if dates else None, "pending": True,
                "marginal_move": MARGINAL_MOVE, "changed": [], "marginal": [],
                "counts": {}}

    prev_d, curr_d = dates[-2], dates[-1]
    prev, curr = by_date[prev_d], by_date[curr_d]

    changed, marginal = [], []
    for sym, now in curr.items():
        was = prev.get(sym)
        if not was or was["tier"] == now["tier"]:
            continue
        delta = now["conviction"] - was["conviction"]
        entry = {
            "symbol": sym, "name": now["name"],
            "from": round(was["conviction"], 1), "to": round(now["conviction"], 1),
            "delta": round(delta, 1),
            "from_tier": was["tier"], "to_tier": now["tier"],
            "marginal": abs(delta) <= MARGINAL_MOVE,
        }
        (marginal if entry["marginal"] else changed).append(entry)

    changed.sort(key=lambda e: -abs(e["delta"]))
    marginal.sort(key=lambda e: -abs(e["delta"]))
    total = len(changed) + len(marginal)
    return {
        "from": prev_d, "to": curr_d, "pending": False,
        "marginal_move": MARGINAL_MOVE,
        "names_compared": len(set(prev) & set(curr)),
        "changed": changed,
        "marginal": marginal,
        "counts": {
            "tier_changes": total,
            "real": len(changed),
            "marginal": len(marginal),
            "marginal_share": round(len(marginal) / total, 3) if total else None,
        },
    }


def _compute_market_breadth() -> dict:
    """Conviction as a time-series (reviewer #3) + breadth/dispersion/persistence
    (the three differentiated signals). Purely derived from the signals ledger —
    no new feeds. Returns a dict written to ledger/market_breadth.json.

    Per asset: conviction delta over 10d and 30d (trend/acceleration) from the
    stored daily conviction series.
    Market level: % assets scoring >70 / >80 (breadth), conviction dispersion
    (stdev across the universe, the Conviction Dispersion Index), and persistent
    leadership (assets >=70 for 30d and 90d).
    """
    if not LEDGER_CSV.exists():
        return {}
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Build per-symbol series of (date, conviction)
    series: dict[str, list] = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        try:
            conv = float(r.get("conviction") or 0)
        except ValueError:
            continue
        series.setdefault(sym, []).append((r.get("date"), conv))

    # Sort each series by date and compute deltas
    def _at(seq, days_back):
        if len(seq) <= days_back:
            return None
        return seq[-1][1] - seq[-(days_back + 1)][1]

    trend = {}
    for sym, seq in series.items():
        seq.sort(key=lambda x: x[0])
        trend[sym] = {
            "conviction": seq[-1][1] if seq else 0,
            "d10": _at(seq, 10),
            "d30": _at(seq, 30),
        }

    # Market-level aggregates are computed over the LATEST daily snapshot only
    # (the current investable universe), not the union of all history — otherwise
    # assets that left the universe would distort breadth/dispersion.
    all_dates = [d for seq in series.values() for d, _ in seq]
    latest_date = max(all_dates) if all_dates else None
    latest_conv = [
        seq[-1][1] for seq in series.values()
        if seq and seq[-1][0] == latest_date and seq[-1][1] is not None
    ]
    n = len(latest_conv) or 1
    above70 = sum(1 for c in latest_conv if c >= 70)
    above80 = sum(1 for c in latest_conv if c >= 80)
    dispersion = (sum((c - (sum(latest_conv) / n)) ** 2 for c in latest_conv) / n) ** 0.5 if n > 1 else 0.0

    # Persistence: assets >=70 for the last 30 / 90 consecutive daily snapshots
    persistent30, persistent90 = [], []
    for sym, seq in series.items():
        seq.sort(key=lambda x: x[0])
        cons30 = all(c >= 70 for _, c in seq[-30:]) if len(seq) >= 30 else False
        cons90 = all(c >= 70 for _, c in seq[-90:]) if len(seq) >= 90 else False
        if cons30:
            persistent30.append(sym)
        if cons90:
            persistent90.append(sym)

    # Conviction change feed: largest increases / collapses over 10d
    movers = [(sym, t["d10"]) for sym, t in trend.items() if t.get("d10") is not None]
    movers.sort(key=lambda x: x[1], reverse=True)
    gains = [{"symbol": s, "d10": round(d, 1)} for s, d in movers[:8] if d > 0]
    losses = [{"symbol": s, "d10": round(d, 1)} for s, d in reversed(movers[-8:]) if d < 0]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_assets": n,  # latest-day investable universe (not the historical union)
        "breadth_above70": above70,
        "breadth_above80": above80,
        "breadth_pct_above70": round(100 * above70 / n, 1),
        "dispersion": round(dispersion, 2),
        "persistent_30d": persistent30,
        "persistent_90d": persistent90,
        "conviction_change_feed": {"gains": gains, "losses": losses},
        "trend": {s: {"conviction": round(t["conviction"], 1),
                       "d10": round(t["d10"], 1) if t["d10"] is not None else None,
                       "d30": round(t["d30"], 1) if t["d30"] is not None else None}
                 for s, t in sorted(trend.items(), key=lambda x: -x[1]["conviction"])},
    }


def _macro_regime_from_ledger() -> str:
    """Macro Liquidity Gate (D) — PASSIVE / display-only.

    Reads the stored index.csv and compares today's global mcap to the value
    ~7 days ago. RISK-OFF if global mcap is down > 8% over that window.
    Returns "RISK-OFF" / "RISK-ON" / "N/A" (no history). This is a logged
    context signal only — it NEVER changes basket holdings.
    """
    if not INDEX_CSV.exists():
        return "N/A"
    try:
        with INDEX_CSV.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        caps = [(r["date"], float(r["global_market_cap"])) for r in rows
                if r.get("global_market_cap") not in (None, "", "None")]
        if len(caps) < 2:
            return "N/A"
        today_cap = caps[-1][1]
        # find the cap closest to 7 days before the latest date
        latest = date.fromisoformat(caps[-1][0])
        target = latest - timedelta(days=7)
        past = min(caps, key=lambda c: abs(date.fromisoformat(c[0]) - target))
        past_cap = past[1]
        if past_cap <= 0:
            return "N/A"
        if today_cap / past_cap < 0.92:
            return "RISK-OFF"
        return "RISK-ON"
    except Exception:  # noqa: BLE001
        return "N/A"


BASKET_JSON = LEDGER_DIR / "basket.json"
INDEX_CSV = LEDGER_DIR / "index.csv"
INDEX_JSON = LEDGER_DIR / "index.json"
MARKET_BREADTH_JSON = LEDGER_DIR / "market_breadth.json"
REBALANCE_DAYS = 7
# Rebalance hysteresis (A): don't eject a holding just because it slipped one
# rank. Keep it unless it drops to rank >= EJECT_RANK or its score falls more
# than EJECT_GAP below the marginal entrant. Controls turnover drag.
EJECT_RANK = 13
EJECT_GAP = 5.0
# Execution friction (B): one-way cost (bps) applied to turnover at rebalance to
# build an execution-adjusted COUNTERFACTUAL track record. Does NOT mutate the
# raw paper return. Calibrated from measured turnover once history accrues.
EXEC_BPS_ONEWAY = 25.0


def build_basket(markets: list[dict], today: str, btc: dict | None = None) -> dict:
    """Conviction-weighted Top-10 basket with score-proportional target weights.

    Tracks, per day:
      #2  benchmark via total crypto market cap (entry->now), horizon-matched.
      #5  live-drifted weights (target * price move) vs target weights.
      #6  per-asset audit trail (entry/current price, target/live weight, return).
    Rebalances weekly or when a holding drops out of the gated Top 10.
    """
    scored = []
    for t in markets:
        sym = (t.get("symbol") or "").upper()
        if not sym or sym in STABLES:
            continue
        era, conv, _, _ = score(t, None, btc)
        if _conjunctive_gate(t, conv):
            scored.append((sym, t, conv))
    scored.sort(key=lambda x: x[2], reverse=True)
    # The strict conjunctive gate rarely fires on real large-cap data (turnover
    # for top caps is usually < 0.30). Fall back to Top-N by conviction so the
    # basket always reflects the strongest non-stable assets when the gate is empty.
    if not scored:
        for t in markets:
            sym = (t.get("symbol") or "").upper()
            if not sym or sym in STABLES:
                continue
            era, conv, _, _ = score(t, None, btc)
            scored.append((sym, t, conv))
        scored.sort(key=lambda x: x[2], reverse=True)
    top = scored[:10]
    # Rank 11..N (marginal entrants) — used by the hysteresis buffer below.
    rest = scored[10:]
    if not top:
        # Truly empty universe — still record an empty-basket index row so the
        # workflow's git add never fails on a missing file.
        _write_index_row(today, [], {}, False)
        return {"holdings": [], "rebalanced": today, "note": "no assets"}

    # --- Rebalance hysteresis (A) ---
    # On a non-calendar rebalance, only eject a prior holding if it dropped to
    # rank >= EJECT_RANK, OR its score fell more than EJECT_GAP below the
    # marginal entrant (rank 11). This stops #10<->#11 flips from rebalancing
    # daily and burning alpha in turnover.
    keep = set()
    prev_syms = set()
    prev = {}
    if BASKET_JSON.exists():
        try:
            prev = json.loads(BASKET_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            prev = {}
    prev_syms = {h["symbol"] for h in prev.get("holdings", [])}
    if prev_syms:
        rest_conv = {sym: c for sym, _, c in rest}
        for h in prev.get("holdings", []):
            sym = h["symbol"]
            if sym in {s for s, _, _ in top}:
                keep.add(sym); continue
            rank = next((i for i, (s, _, _) in enumerate(scored, 1) if s == sym), None)
            # marginal entrant = best of rank 11+ (or rank 10 if basket < 10)
            margin_conv = rest[0][2] if rest else (top[-1][2] if top else 0)
            if rank is not None and rank >= EJECT_RANK:
                pass  # eject (do not keep)
            elif (h.get("conviction") or 0) + EJECT_GAP < margin_conv:
                pass  # structurally outclassed — eject
            else:
                keep.add(sym)
    # Build holdings: keep previous holdings still retained, add new entrants.
    kept_holdings = [h for h in prev.get("holdings", []) if h["symbol"] in keep]
    new_top = [x for x in top if x[0] not in prev_syms]
    holdings = []
    total_conv = sum(c for _, _, c in top) or 1
    for h in kept_holdings:
        sym = h["symbol"]
        # refresh weight/conviction from current scoring
        nh = next((x for x in top if x[0] == sym), None)
        conv = nh[2] if nh else h.get("conviction", 0)
        w = conv / total_conv
        holdings.append({
            "symbol": sym, "conviction": conv, "weight": round(w, 4),
            "entry_price": h.get("entry_price") or 0,
            "current_price": (next((t.get("current_price") for _, t, _ in top if _ and (t.get("symbol") or "").upper() == sym), None)) or h.get("current_price") or 0,
        })
    for sym, t, conv in new_top:
        w = conv / total_conv
        holdings.append({
            "symbol": sym, "conviction": conv, "weight": round(w, 4),
            "entry_price": t.get("current_price") or 0,
            "current_price": t.get("current_price") or 0,
        })

    # Rebalance decision: calendar OR membership changed vs the (hysteresis-
    # filtered) previous basket. A pure #10<->#11 flip no longer triggers.
    prev_date = prev.get("rebalanced", today)
    cur_syms = {h["symbol"] for h in holdings}
    try:
        days_since = (date.fromisoformat(today) - date.fromisoformat(prev_date)).days
    except ValueError:
        days_since = REBALANCE_DAYS
    rebalanced = (days_since >= REBALANCE_DAYS) or (cur_syms != prev_syms)
    if rebalanced:
        # Snapshot the macro baseline at rebalance so the benchmark is horizon-matched.
        gmc = fetch_global_market_cap()
        basket = {"rebalanced": today, "entry_global_mcap": gmc, "holdings": holdings}
        BASKET_JSON.write_text(json.dumps(basket, indent=2))
    else:
        # keep entry prices + entry_global_mcap from prev basket for unchanged holdings
        basket = prev
        for h in basket.get("holdings", []):
            if h["symbol"] in cur_syms:
                nh = next(x for x in holdings if x["symbol"] == h["symbol"])
                h["weight"] = nh["weight"]
                h["conviction"] = nh["conviction"]

    # Live prices + benchmark
    live = {(tk.get("symbol") or "").upper(): tk.get("current_price") or 0 for tk in markets}
    gmc_now = fetch_global_market_cap()
    entry_gmc = basket.get("entry_global_mcap") or gmc_now

    # --- Ejection Alpha Delta (C) + Execution turnover (B) ---
    # Computed only on rebalance days (when holdings actually change).
    ejected_syms, entrants_syms = [], []
    ejected_avg_return = 0.0
    if rebalanced:
        prev_h = {h["symbol"]: h for h in prev.get("holdings", [])}
        ejected_syms = [s for s in prev_h if s not in cur_syms]
        entrants_syms = [s for s in cur_syms if s not in prev_h]
        ej_rets = []
        for s in ejected_syms:
            ep = prev_h[s].get("entry_price") or 0
            cur = live.get(s) or 0
            if ep and cur:
                ej_rets.append((cur - ep) / ep)
        ejected_avg_return = (sum(ej_rets) / len(ej_rets)) if ej_rets else 0.0
    # Δ_eject = R_entrant(0 at entry) - R_ejected(realized). Full R_entrant
    # window requires the next rebalance's entry prices; we store the symbol
    # lists so it can be refined once >=2 rebalances exist. Honest, no fabrication.
    eject_delta = -ejected_avg_return
    # One-way turnover (bps) = Σ |new_w - old_w| over changed holdings.
    # Genesis (no prior basket) is the baseline — no execution cost is charged.
    turnover_bps = 0.0
    if rebalanced and prev.get("holdings"):
        old_w = {h["symbol"]: h.get("weight", 0) for h in prev.get("holdings", [])}
        for h in holdings:
            s = h["symbol"]
            turnover_bps += abs(h.get("weight", 0) - old_w.get(s, 0)) * 1e4

    # Per-asset audit trail + live-drift weights (#5, #6)
    audit = []
    vals = []
    for h in basket.get("holdings", []):
        cur = live.get(h["symbol"]) or 0
        ep = h.get("entry_price") or 0
        tw = h.get("weight", 0)
        if cur and ep:
            ret = (cur - ep) / ep
            v = tw * (cur / ep)          # drifted value
        else:
            ret = 0.0
            v = tw
        vals.append(v)
        audit.append({
            "ticker": h["symbol"], "entry_price": round(ep, 8), "current_price": round(cur, 8),
            "target_weight": round(tw, 4), "return": round(ret, 4),
        })
    vtot = sum(vals) or 1
    for a, v in zip(audit, vals):
        a["live_weight"] = round(v / vtot, 4)

    # Basket return = Σ target_weight * asset_return (target-weighted, not drifted)
    wret = sum(a["target_weight"] * a["return"] for a in audit)
    # Execution-adjusted counterfactual return (B): raw wret minus one-way cost
    # on turnover. Never mutates the raw paper return stored alongside it.
    exec_adjusted_return = wret - (EXEC_BPS_ONEWAY / 1e4) * (turnover_bps / 1e4)
    # Benchmark: total crypto market cap, entry -> now (horizon-matched) (#2)
    bench_total = (gmc_now / entry_gmc) if (gmc_now and entry_gmc) else 1.0

    # Append daily row + recompute cumulative from stored rows
    _write_index_row(today, audit, basket, rebalanced, live,
                     macro_regime=None, eject_delta=eject_delta,
                     ejected_syms=ejected_syms, entrants_syms=entrants_syms,
                     exec_adjusted_return=exec_adjusted_return, turnover_bps=turnover_bps)
    return basket


def _normalize_live(audit: list, live_holdings: list) -> list:
    """Active-basket slice of the audit trail with target weights Σ=1.0.

    ``current_holdings`` (the audit trail) can carry stale rows from ejected
    symbols whose ``target_weight`` no longer represents the live basket — that
    made the terminal's allocation column sum to >100% (e.g. 383.3%). This
    returns only the symbols currently in the live basket, with their
    target weights re-normalised so Σ target_weight == 1.0, and synthesises a
    parallel ``live_weight`` (drifted by price) and ``return`` so the terminal
    needs zero schema changes.
    """
    live_syms = {h.get("symbol") for h in live_holdings}
    active = [h for h in audit if h.get("ticker") in live_syms]
    if not active:
        return []
    raw_sum = sum(h.get("target_weight", 0) for h in active) or 1.0
    out = []
    for h in active:
        tw = h.get("target_weight", 0) / raw_sum
        ep = h.get("entry_price") or 0
        cur = h.get("current_price") or 0
        ret = ((cur - ep) / ep) if (cur and ep) else 0.0
        live_w = tw * (cur / ep) if (cur and ep) else tw
        out.append({"ticker": h.get("ticker"), "entry_price": ep,
                    "current_price": cur, "target_weight": round(tw, 4),
                    "live_weight": round(live_w, 4), "return": round(ret, 4)})
    return out


def _write_index_row(today: str, audit: list, basket: dict, rebalanced: bool,
                     live: dict | None = None, macro_regime: str | None = None,
                     eject_delta: float = 0.0, ejected_syms: list | None = None,
                     entrants_syms: list | None = None,
                     exec_adjusted_return: float = 0.0, turnover_bps: float = 0.0) -> None:
    """Append today's basket/index row and rewrite index.json.

    Always writes ledger/index.csv + ledger/index.json (creating them on first
    run) so the CI workflow's `git add` never fails on a missing file, even when
    the basket is empty. `live` maps symbol->current price for non-rebalance-day
    return computation.

    New columns (this build):
      macro_regime      (D) RISK-ON / RISK-OFF from 7d global mcap trend (passive)
      eject_delta       (C) R_entrant(0) - R_ejected(realized) on rebalance days
      ejected_syms / entrants_syms (C) the rotation that occurred
      exec_adjusted_return (B) paper return minus one-way execution cost on turnover
      turnover_bps      (B) one-way turnover at rebalance
    """
    gmc_now = fetch_global_market_cap()
    entry_gmc = basket.get("entry_global_mcap") or gmc_now
    # Basket return = Σ target_weight * asset_return (target-weighted, not drifted)
    wret = 0.0
    for h in basket.get("holdings", []):
        tw = h.get("weight", 0) or 0
        ep = h.get("entry_price") or 0
        if live:
            cur = live.get(h.get("symbol"), h.get("current_price")) or 0
        else:
            cur = h.get("current_price") or 0
        ret = ((cur - ep) / ep) if (cur and ep) else 0.0
        wret += tw * ret
    bench_total = (gmc_now / entry_gmc) if (gmc_now and entry_gmc) else 1.0

    # --- Macro Liquidity Gate (D): PASSIVE / display-only ---
    # 7-day total mcap trend from the stored ledger. RISK-OFF if down > 8%.
    # Logged + shown in the panel; never alters holdings (no premature overlay).
    if macro_regime is None:
        macro_regime = _macro_regime_from_ledger()

    row = {
        "date": today,
        "global_market_cap": round(gmc_now, 0) if gmc_now else None,
        "basket_return": round(wret * 100, 3),
        "benchmark_return": round((bench_total - 1) * 100, 3),
        "alpha_vs_benchmark": round((wret - (bench_total - 1)) * 100, 3),
        "exec_adjusted_return": round(exec_adjusted_return * 100, 3),
        "turnover_bps": round(turnover_bps, 1),
        "macro_regime": macro_regime,
        "eject_delta": round(eject_delta * 100, 3) if rebalanced else "",
        "ejected_syms": ",".join(ejected_syms) if (rebalanced and ejected_syms) else "",
        "entrants_syms": ",".join(entrants_syms) if (rebalanced and entrants_syms) else "",
        "n_holdings": len(audit),
        "rebalanced": rebalanced,
    }
    INDEX_CSV.parent.mkdir(parents=True, exist_ok=True)
    idx_exists = INDEX_CSV.exists()
    with INDEX_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not idx_exists:
            w.writeheader()
        w.writerow(row)
    idx_rows = []
    if INDEX_CSV.exists():
        with INDEX_CSV.open(newline="", encoding="utf-8") as f:
            idx_rows = list(csv.DictReader(f))
    cum_basket = 1.0
    for r in idx_rows:
        cum_basket *= (1 + (float(r.get("basket_return") or 0) / 100.0))
    # Cumulative execution-adjusted track record (B counterfactual) — parallel
    # to the raw paper curve, never replacing it.
    cum_exec = 1.0
    for r in idx_rows:
        cum_exec *= (1 + (float(r.get("exec_adjusted_return") or 0) / 100.0))
    gcaps = [float(r["global_market_cap"]) for r in idx_rows if r.get("global_market_cap") not in (None, "", "None")]
    bench_cum = (gcaps[-1] / gcaps[0]) if len(gcaps) >= 2 and gcaps[0] else 1.0
    # Macro regime summary (D, passive) + ejection-alpha tally (C)
    regimes = [r.get("macro_regime") for r in idx_rows if r.get("macro_regime") in ("RISK-ON", "RISK-OFF")]
    macro_now = regimes[-1] if regimes else "N/A"
    macro_riskoff_days = sum(1 for x in regimes if x == "RISK-OFF")
    eject_deltas = [float(r["eject_delta"]) for r in idx_rows
                    if r.get("eject_delta") not in (None, "", "None")]
    eject_alpha_cum = round(sum(eject_deltas) / 100.0, 4) if eject_deltas else None
    # Risk stats (Sharpe / max drawdown) — derived from the ledger once >=30 days
    # of history exist. Convention: rf=0, daily returns annualized x sqrt(365).
    risk = _risk_stats(idx_rows) if len(idx_rows) >= 30 else None
    INDEX_JSON.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest": row,
        "basket_total_return": round(cum_basket, 4),
        "benchmark_total_return": round(bench_cum, 4),
        "exec_adjusted_total_return": round(cum_exec, 4),
        "macro_regime": macro_now,
        "macro_riskoff_days": macro_riskoff_days,
        "eject_alpha_cumulative": eject_alpha_cum,
        "sharpe_convention": "rf=0; daily returns annualized x sqrt(365); computed when len(rows)>=30",
        "risk": risk,
        "current_holdings": audit,
        "latest_holdings": _normalize_live(audit, basket.get("holdings", [])),
        "rows": idx_rows,
    }, indent=2))


def main() -> int:
    today = date.today().isoformat()

    # Module B (Dune): only when BOTH key and a saved query id are present.
    dune_b: dict = {}
    api_key = os.environ.get("DUNE_API_KEY")
    query_id = os.environ.get("DUNE_UNLOCK_QUERY_ID")
    if api_key and query_id:
        dune_b = fetch_dune_module_b(query_id, api_key)
        print(f"[dune] Module B active: {len(dune_b)} tokens enriched.", file=__import__("sys").stderr)
    else:
        print("[dune] not configured — Module B columns null (no fabricated data).",
              file=__import__("sys").stderr)

    markets = fetch_markets()
    scored_syms = {(t.get("symbol") or "").upper() for t in markets
                   if (t.get("symbol") or "").upper() and (t.get("symbol") or "").upper() not in STABLES}
    perps_map = fetch_perps_map(scored_syms)  # live perp funding (Bybit->OKX fallback); {} => neutral
    if perps_map:
        print(f"[perp] {len(perps_map)} perps live; LAVL leverage regime active.",
              file=__import__("sys").stderr)
    else:
        print("[perp] no perp feed; RiskMult_perp neutral (1.0) for all.", file=__import__("sys").stderr)
    # BTC = market-neutral reference for multi-timeframe relative strength.
    btc = next((m for m in markets if (m.get("symbol") or "").upper() == "BTC"), None)
    basket = build_basket(markets, today, btc)
    rows = []
    seen = set()
    for t in markets:
        sym = (t.get("symbol") or "").upper()
        if not sym or sym in seen or sym in STABLES:
            continue
        seen.add(sym)
        era, conv, sig, comp = score(t, perps_map, btc)
        pm = lavl_perp_mult(sym, perps_map)
        b = dune_b.get(sym)  # real Dune fields if present, else None -> null
        rows.append({
            "date": today, "symbol": sym, "name": t.get("name", ""),
            "price": t.get("current_price") or 0, "market_cap": t.get("market_cap") or 0,
            "turnover_pct": round((t.get("total_volume", 0) / t.get("market_cap", 1)) * 100, 2) if t.get("market_cap") else 0,
            "erosion_ratio": round(era, 3), "conviction": conv, "signal": sig,
            "rs7": comp["rs7"], "rs14": comp["rs14"], "rs30": comp["rs30"],
            "rs200": comp["rs200"], "rs_blend": comp["rs_blend"],
            "c_liquidity": comp["liquidity"], "c_era": comp["era"],
            "c_depth": comp["depth"], "c_momentum": comp["momentum"],
            "unlocks_usd": b["unlocks_usd"] if b else None,
            "supply_increase_pct": b["supply_increase_pct"] if b else None,
            "addr_growth_pct": b["addr_growth_pct"] if b else None,
            "era": b["era"] if b else None,
            "roi_30d": None, "roi_90d": None, "survived": None,
            "perp_mult": round(pm, 3),
        })
    rows.sort(key=lambda r: r["conviction"], reverse=True)

    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    exists = LEDGER_CSV.exists()
    with LEDGER_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        for r in rows[:50]:
            w.writerow(r)

    # Backfill ROI from today's live prices for aged rows
    live = { (t.get("symbol") or "").upper(): t.get("current_price") or 0 for t in markets }
    all_rows = []
    if LEDGER_CSV.exists():
        with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
            all_rows = list(csv.DictReader(f))
    updated = 0
    for r in all_rows:
        cur = live.get(r["symbol"])
        if not cur or not r["price"] or r["price"] in ("0", "0.0"):
            continue
        age = (date.today() - date.fromisoformat(r["date"])).days
        roi = (cur - float(r["price"])) / float(r["price"])
        if r.get("roi_30d") in (None, "", "None") and age >= 30:
            r["roi_30d"] = round(roi * 100, 2); updated += 1
        if r.get("roi_90d") in (None, "", "None") and age >= 90:
            r["roi_90d"] = round(roi * 100, 2); r["survived"] = "True" if cur > 0 else "False"; updated += 1
    with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
        for r in all_rows:
            # normalize: keep only current FIELDS, fill missing with None
            w.writerow({k: r.get(k) for k in FIELDS})

    # Rewrite JSON summary
    deciles = {i: {"count": 0, "survived": 0, "roi_30d": [], "roi_90d": []} for i in range(1, 11)}
    for r in all_rows:
        try:
            cv = int(r["conviction"] or 0)
        except ValueError:
            cv = 0
        d = deciles[max(1, min(10, (cv // 10) + 1))]
        d["count"] += 1
        if r.get("survived") == "True":
            d["survived"] += 1
        for k, key in (("roi_30d", "roi_30d"), ("roi_90d", "roi_90d")):
            v = r.get(k)
            if v not in (None, "", "None"):
                try:
                    d[key].append(float(v))
                except ValueError:
                    pass
    summary = {
        "total_signals": len(all_rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "by_decile": {str(k): {
            "count": v["count"], "survived": v["survived"],
            "win_rate": round(v["survived"] / v["count"], 3) if v["count"] else 0,
            "avg_roi_30d": round(sum(v["roi_30d"]) / len(v["roi_30d"]), 3) if v["roi_30d"] else None,
            "avg_roi_90d": round(sum(v["roi_90d"]) / len(v["roi_90d"]), 3) if v["roi_90d"] else None,
        } for k, v in deciles.items()},
        "rows": all_rows,
    }
    with LEDGER_JSON.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # Conviction time-series + breadth/dispersion/persistence (reviewer #3 + the
    # three differentiated signals). Derived purely from the signals ledger.
    breadth = _compute_market_breadth()
    if breadth:
        # Overnight tier transitions, split into real reclassifications and boundary
        # crossings. Attached to breadth rather than given its own file: it is the same
        # kind of derived-from-the-ledger diagnostic and the terminal already loads this.
        breadth["tier_diff"] = _compute_tier_diff()
        MARKET_BREADTH_JSON.write_text(json.dumps(breadth, indent=2))
        td = breadth["tier_diff"]
        if td and not td.get("pending"):
            c = td["counts"]
            print(f"[tiers] {td['from']} -> {td['to']}: {c['tier_changes']} changed "
                  f"({c['real']} real, {c['marginal']} on <= {td['marginal_move']} pts)")

    print(f"Nightly {today}: wrote {len(rows[:25])} signals, backfilled {updated}. "
          f"Ledger total: {len(all_rows)}.")
    if basket.get("holdings"):
        hs = ", ".join(f"{h['symbol']} {h['weight']*100:.1f}%" for h in basket["holdings"])
        print(f"[basket] Top-10 conviction-weighted | rebalanced={basket.get('rebalanced')} | {hs}")
        print(f"[index] cumulative return tracked in ledger/index.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
