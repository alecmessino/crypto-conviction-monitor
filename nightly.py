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
import sys
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
          "roi_30d", "roi_90d", "survived", "perp_mult",
          # Appended, never inserted: the columns are positional in every reader, and
          # inserting one mid-list would reinterpret every row already written.
          # Historical rows carry an empty value — their specification is genuinely
          # unknown and must report as unknown rather than be assumed to match today's.
          "spec_hash",
          # Derived from the Dune columns above. Observational: score() reads neither,
          # and adopting them would be a separate, hashed decision. Appended at the end
          # for the reason stated above — the file on disk was written under the shorter
          # header, and a strict prefix is what lets the next run widen it cleanly.
          "unlock_overhang_pct", "adoption_dilution",
          # Module 1 — derivatives. funding_rate already reaches score() through
          # lavl_perp_mult and has done since before this column existed; recording it
          # changes nothing about that. Everything else here is observational, on the
          # same terms as the Dune columns: score() reads none of it, and adopting any
          # of it would be a separate decision that moves the specification hash.
          # oi_usd in particular was already being fetched from Bybit on every run and
          # thrown away — the ingestion cost was being paid and the data discarded.
          "funding_rate", "funding_ann_pct", "oi_usd", "oi_chg_24h_pct",
          "oi_to_mcap", "long_short_ratio", "oi_price_divergence",
          # Module 2 — the daily bar. Recorded so ATR and the choppiness index can be
          # computed from accumulated history; CoinGecko has no daily-granularity OHLC
          # endpoint, but high_24h/low_24h are already in the markets response that
          # every run makes, so this costs nothing and back-fills nothing.
          "high_24h", "low_24h"]

# Dune Analytics (Module B: vesting / emission-vs-adoption ERA).
# Key is read ONLY from env DUNE_API_KEY (supplied by the CI secret). Never hardcoded.
# A public unlock-schedule query is configured via DUNE_UNLOCK_QUERY_ID.
DUNE_BASE = "https://api.dune.com/api/v1"
# CoinGecko free markets endpoint (separate host from Dune).
CG_BASE = "https://api.coingecko.com/api/v3"

STABLES = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD", "USDE",
           "USD1", "USDS", "PYUSD", "GUSD", "USDG", "FRAX", "USDD", "TUSD",
           "XAUT", "PAXG"}




# ---------------------------------------------------------------------------
# specification identity
# ---------------------------------------------------------------------------
# Every function whose text can change a published score. Named here rather than
# inferred, so adding a scoring function is a deliberate act that shows up in review.
SPEC_FUNCTIONS = ("score", "_lavl_regime", "lavl_perp_mult", "_tier_for")
SPEC_CONSTANTS = ("TIER_CUTS", "STABLES")


def spec() -> dict:
    """A canonical form of everything that can change a score.

    The sibling equity project enumerates named constants because its thresholds *are*
    named constants. Here they are literals inside the scoring curves — ``turnover <=
    0.30``, ``era < 0.7`` — so there is nothing to list. The specification is therefore
    derived from the code itself: each scoring function is parsed, its docstrings
    stripped, and unparsed back to canonical source. Editing a threshold changes the
    result; editing a comment or reflowing a line does not.

    Without this the history is not segmentable, and an Information Coefficient or a
    track record computed across a silent threshold change is a number about two
    different models.
    """
    import ast

    # Read this file directly rather than via sys.modules: the validator and the tests
    # load this module through importlib under names that are never registered there,
    # and a specification that only computes when imported normally is not a
    # specification.
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    wanted = set(SPEC_FUNCTIONS)
    parts = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            body = node.body
            # Drop the docstring so prose edits do not falsely segment the series.
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            stripped = ast.FunctionDef(
                name=node.name, args=node.args, body=body or [ast.Pass()],
                decorator_list=[], returns=None, type_comment=None,
                type_params=getattr(node, "type_params", []))
            ast.fix_missing_locations(stripped)
            parts[node.name] = ast.unparse(stripped)

    missing = wanted - set(parts)
    if missing:
        # A renamed scoring function must not silently drop out of the specification.
        raise RuntimeError(f"spec() cannot find scoring function(s): {sorted(missing)}")

    consts = {}
    for name in SPEC_CONSTANTS:
        value = globals().get(name)
        consts[name] = sorted(value) if isinstance(value, set) else value
    return {"functions": parts, "constants": consts}


def spec_hash() -> str:
    """Short stable digest of spec(), recorded on every row.

    Tied to the interpreter's ``ast.unparse`` output, so a Python upgrade can in
    principle shift it without the model changing. That is visible rather than silent —
    the monitor reports the hash per day and a spurious change shows up as a boundary
    with no accompanying code change.
    """
    import hashlib
    blob = json.dumps(spec(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


# Computed once at import. spec() parses this module's source, which is not something
# to do per row.
SPEC_HASH = spec_hash()


def _get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "conviction-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec
        return json.loads(resp.read().decode())


# Column-name tolerance. A Dune query is written by a human in a web editor and the
# column will be called whatever they called it; a fetch that only accepts one spelling
# silently returns nothing and looks identical to "no data".
DUNE_ALIASES = {
    "symbol": ("symbol", "token", "ticker", "asset"),
    "unlocks_usd": ("unlocks_usd", "unlocks", "unlock_usd", "unlock_value_usd",
                    "upcoming_unlocks_usd"),
    "supply_increase_pct": ("supply_increase_pct", "supply_increase", "emission_pct",
                            "inflation_pct", "supply_growth_pct"),
    "addr_growth_pct": ("addr_growth_pct", "address_growth", "address_growth_pct",
                        "active_address_growth_pct", "adoption_pct"),
    "era": ("era", "erosion_ratio", "emission_adoption_ratio"),
}


def _pick(row: dict, field: str):
    """First alias present in the row, matched case-insensitively."""
    lower = {str(k).lower(): v for k, v in row.items()}
    for alias in DUNE_ALIASES[field]:
        if alias in lower and lower[alias] not in (None, ""):
            return lower[alias]
    return None


def fetch_dune_report(query_id: str, api_key: str) -> dict:
    """The Module B fetch, plus why it produced what it produced.

    ``{"data": {SYMBOL: {...}}, "status": ..., "detail": ..., "columns": [...]}``.

    The status matters because four different situations all end in a table of nulls,
    and a single "no data" message cannot tell a reader which one they are in. The one
    that actually happened here: a valid key and a real query id pointing at a query
    about something else entirely — it returned ``cryptocurrency`` and
    ``volume_24h_usd``, every row was dropped for want of a symbol column, and the
    dashboard would have said "unconfigured or down" while being configured and up.

      unconfigured  no key or no query id
      unreachable   the call failed — wrong key, missing query, never executed
      unusable      rows came back but nothing in them was recognisable
      partial       symbols recognised, some fields absent (the expected steady state:
                    unlock schedules are contractual, so that column is mostly null)
      live          symbols and every expected field present

    Nothing here reaches ``score()``, which computes its own ERA proxy from 24-hour
    stability and has never read these columns. Recording without scoring is deliberate:
    the fields accumulate history now, and adopting them later is a separate, hashed
    decision that will draw its own specification boundary.

    ERA = supply_increase_pct / addr_growth_pct — emission against adoption. Above 1 the
    token is diluting faster than it is being adopted.
    """
    out: dict = {}
    columns: list = []
    seen = 0
    if not (query_id and api_key):
        return {"data": out, "status": "unconfigured", "columns": columns,
                "detail": "no query id or key configured"}
    try:
        # Paged: a saved query over the full token universe exceeds one page, and a
        # silently truncated result is indistinguishable from a partial feed.
        offset, limit = 0, 1000
        for _ in range(10):
            url = f"{DUNE_BASE}/query/{query_id}/results?limit={limit}&offset={offset}"
            data = _get_json(url, headers={"X-Dune-Api-Key": api_key})
            rows = (data.get("result") or {}).get("rows") or []
            # Union across rows, not rows[0]: Dune omits keys whose value is null, so
            # any single row understates the result set. Reading the first row alone is
            # what made a feed enriching 99 tokens look like it returned nothing
            # recognisable.
            columns = sorted(set(columns) | {k for r in rows for k in r})
            seen += len(rows)
            for r in rows:
                sym = str(_pick(r, "symbol") or "").upper().strip()
                if not sym:
                    continue
                rec = {k: _num(_pick(r, k)) for k in
                       ("unlocks_usd", "supply_increase_pct", "addr_growth_pct", "era")}
                if rec["era"] is None:
                    sup, addr = rec["supply_increase_pct"], rec["addr_growth_pct"]
                    if sup is not None and addr:
                        rec["era"] = round(sup / addr, 3)
                out[sym] = rec
            if len(rows) < limit:
                break
            offset += limit
    except Exception as e:  # noqa: BLE001
        print(f"[dune] fetch failed, Module B -> null: {e}", file=__import__("sys").stderr)
        return {"data": {}, "status": "unreachable", "columns": columns,
                "detail": f"the call failed ({e}) — wrong key, no such query, or a "
                          f"query that has never been executed"}

    if not out:
        return {"data": out, "status": "unusable", "columns": columns,
                "detail": "the query returned %d row(s) but no column this feed "
                          "recognises" % seen}

    covered = {f: sum(1 for r in out.values() if r.get(f) is not None)
               for f in ("unlocks_usd", "supply_increase_pct", "addr_growth_pct")}
    missing = [f for f, n in covered.items() if n == 0]
    return {"data": out, "columns": columns,
            "status": "partial" if missing else "live",
            "detail": ("%d token(s) enriched" % len(out))
                      + ("; no values for " + ", ".join(missing) if missing else
                         "; every expected field present")}


def fetch_dune_module_b(query_id: str, api_key: str) -> dict:
    """Just the data. See :func:`fetch_dune_report` for why it is what it is."""
    return fetch_dune_report(query_id, api_key)["data"]


def dune_context(rec: dict | None, market_cap: float | None) -> dict:
    """The two derived readings, recorded alongside the raw fields.

    ``unlock_overhang_pct`` normalises the dollar unlock by market cap, which is the
    only form in which it means anything: a $10m unlock is noise for a $2t asset and an
    existential event for a $30m one.

    ``adoption_dilution`` inverts ERA so that larger is better, matching the direction
    of every other reading on the board. Both are context, not inputs — nothing here is
    read by ``score()``.
    """
    out = {"unlock_overhang_pct": None, "adoption_dilution": None}
    if not rec:
        return out
    unl = rec.get("unlocks_usd")
    if unl is not None and market_cap:
        out["unlock_overhang_pct"] = round(100.0 * unl / market_cap, 4)
    era = rec.get("era")
    if era is not None and era > 0:
        out["adoption_dilution"] = round(1.0 / era, 4)
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


def fetch_long_short(perps_map: dict, symbols: set[str] | None = None,
                     limit: int = 60) -> int:
    """Binance's global long/short account ratio, merged into `perps_map` in place.

    Keyless and public, but one request per symbol, so it is bounded to the symbols
    actually being scored and capped. Anything not fetched keeps a null ratio rather
    than a neutral 1.0 — "half the accounts are long" is a real reading and must not be
    manufactured by a failed request.

    Returns how many symbols were enriched, so the caller can log coverage instead of
    reporting a silent partial.
    """
    if not symbols:
        return 0
    got = 0
    for base in sorted(symbols)[:limit]:
        try:
            data = _get_json(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
                f"?symbol={base}USDT&period=1d&limit=1")
            row = (data or [{}])[0]
            ratio = float(row.get("longShortRatio"))
        except Exception:  # noqa: BLE001
            continue      # per-asset null; never a fabricated neutral
        perps_map.setdefault(base, {})["long_short_ratio"] = round(ratio, 4)
        got += 1
    return got


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

# Funding is quoted per 8-hour interval, so three settlements a day.
FUNDING_INTERVALS_PER_YEAR = 3 * 365
# Beyond these the carry is doing something a spot-only reader cannot see.
FUNDING_HOT_APR = 30.0
FUNDING_COLD_APR = -20.0
# Below this a "divergence" is rounding, on either axis.
DIVERGENCE_EPS_PRICE = 0.5      # percent
DIVERGENCE_EPS_OI = 1.0         # percent


def funding_ann_pct(funding_rate) -> float | None:
    """Funding as an annualised percentage carry.

    The raw 8-hour rate is unreadable at a glance — 0.0005 and 0.01 both look like
    small numbers and mean 55% and 1,095% a year. Annualising is the only form in which
    the figure can be compared against anything else on the board.
    """
    if funding_rate is None:
        return None
    try:
        return round(float(funding_rate) * FUNDING_INTERVALS_PER_YEAR * 100.0, 4)
    except (TypeError, ValueError):
        return None


def oi_price_divergence(price_chg_pct, oi_chg_pct) -> str | None:
    """Where open interest went while price went somewhere.

    The four states are the standard reading of positioning against direction:

      price up,   OI up    ACCUMULATION   new money backing the move
      price up,   OI down  SHORT_SQUEEZE  the rally is shorts closing, not buyers
      price down, OI up    SHORT_BUILD    new money positioned against it
      price down, OI down  LONG_FLUSH     leverage being unwound, not distribution

    None when either leg is missing or too small to be a direction. Guessing a quadrant
    from a 0.1% drift would produce a confident badge on noise, and a badge is read as
    a claim.
    """
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    try:
        p, o = float(price_chg_pct), float(oi_chg_pct)
    except (TypeError, ValueError):
        return None
    if abs(p) < DIVERGENCE_EPS_PRICE or abs(o) < DIVERGENCE_EPS_OI:
        return "FLAT"
    if p > 0:
        return "ACCUMULATION" if o > 0 else "SHORT_SQUEEZE"
    return "SHORT_BUILD" if o > 0 else "LONG_FLUSH"


def _prev_oi_by_symbol() -> dict:
    """Yesterday's recorded open interest, for the 24h delta.

    Read from the ledger rather than fetched, because Bybit's tickers endpoint reports
    a point-in-time value with no history. On the first night after this column lands
    there is nothing to compare against and the delta is null — which is the honest
    reading, and specifically not zero, since zero is a claim that OI did not move.
    """
    rows = _read_signals_rows()
    if not rows:
        return {}
    dates = sorted({r.get("date") for r in rows if r.get("date")})
    if len(dates) < 2:
        return {}
    prev = dates[-1]
    out = {}
    for r in rows:
        if r.get("date") != prev:
            continue
        v = _num(r.get("oi_usd"))
        if v:
            out[(r.get("symbol") or "").upper()] = v
    return out


def perp_context(ticker: str, perps_map: dict, market_cap, price_chg_pct,
                 prev_oi: dict) -> dict:
    """The recorded derivatives columns for one asset. Observational throughout."""
    info = perps_map.get(ticker) or {}
    fr = info.get("funding_rate")
    oi = info.get("oi_usd")
    p0 = prev_oi.get(ticker)
    oi_chg = round(100.0 * (oi / p0 - 1.0), 4) if (oi and p0) else None
    return {
        "funding_rate": fr,
        "funding_ann_pct": funding_ann_pct(fr),
        "oi_usd": oi,
        "oi_chg_24h_pct": oi_chg,
        # Leverage relative to the size of the asset. A $1bn book is enormous on a
        # $2bn token and unremarkable on a $200bn one.
        "oi_to_mcap": round(oi / market_cap, 6) if (oi and market_cap) else None,
        "long_short_ratio": info.get("long_short_ratio"),
        "oi_price_divergence": oi_price_divergence(price_chg_pct, oi_chg),
    }


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


def _risk_stats(daily_returns: list[float]) -> dict | None:
    """Risk statistics from a series of OVERNIGHT returns.

    Takes the returns explicitly rather than reading index.csv, because that file's
    return columns are cumulative since the basket's cost basis, not daily. Feeding them
    to a Sharpe ratio computes the dispersion of a running total, which rises with the
    length of the series rather than with the volatility of anything. Gated at 30
    observations, so it had never fired — a landmine rather than a live wrong number.

    Sharpe: rf=0, mean(daily) / std(daily) * sqrt(365).
    Max drawdown: largest peak-to-trough decline of the compounded curve.
    """
    if len(daily_returns) < 30:
        return None
    rets = list(daily_returns)
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


# Snapshot days required before the paper curve is drawn. Below this it would be one or
# two segments, a shape the eye reads as a trend and which is nothing of the kind.
PERF_MIN_DAYS = 5

# Basket size and weighting mirror build_basket(): top 10 by conviction, weighted in
# proportion to it. The hysteresis buffer is deliberately *not* replicated — this curve
# answers "what did the score say to hold", and reconstructing the ejection rules from a
# ledger that never recorded which names were actually held would be a guess dressed as
# a record.
PERF_TOP_N = 10

# A leg that loses more than this share of the book to unpriceable or departed names is
# not a return, it is a data outage wearing one.
PERF_MAX_WEIGHT_LOSS = 0.10

# The benchmark. Present on every recorded day, and the closest crypto analogue to the
# index the equity terminal measures against.
PERF_BENCHMARK = "BTC"

# ---------------------------------------------------------------------------
# Selection edge
# ---------------------------------------------------------------------------
# Does conviction predict the next day's return? This is the only question that decides
# whether the score is worth acting on, and it is a different question from "is the
# basket beating the benchmark".
#
# The distinction matters because the basket IS losing to equal weight — about -283bp
# over the six legs since the 2026-08-05 boundary — and the obvious reading of that is
# "the selection is subtracting value". The measurement does not support that reading.
# The information coefficient over the same legs is +0.006 with a 95% interval of
# roughly [-0.09, +0.10]: indistinguishable from zero in either direction. A
# concentrated book with no measurable edge underperforms an equal-weight control as a
# matter of course, because concentration adds variance without adding expected return.
# That is the honest description of the current state, and it is neither "the model
# works" nor "the model is broken".
#
# So this panel leads with the interval and the sample size, not with a ranked list of
# which names hurt. Ranking six observations by contribution produces a confident-looking
# table of noise, and acting on it is how a model gets fitted to its own sampling error.
# The per-name accounting is computed and shown, clearly labelled as arithmetic rather
# than evidence.
EDGE_MIN_NAMES = 10        # below this a rank correlation is not worth computing
EDGE_QUINTILE = 5
# Targets used to state how much history is still needed. 0.03 is a respectable
# cross-sectional signal; 0.05 would be a strong one.
EDGE_TARGET_ICS = (0.02, 0.03, 0.05)
# Legs needed before the mean IC is worth reading at all. Chosen so the standard error
# of the mean is at most about half a plausible true signal.
EDGE_MIN_LEGS = 40


def _edge_legs(by_date: dict, boundary: str | None) -> list[dict]:
    """Per-leg information coefficient and quintile spread, after the boundary.

    Legs before a specification change are excluded rather than blended: an IC averaged
    across two different scoring functions is a number about a model that never existed.
    """
    out = []
    dates = sorted(by_date)
    for a, b in zip(dates, dates[1:]):
        if boundary and a < boundary:
            continue
        prev, curr = by_date[a], by_date[b]
        pairs = []
        for sym, row in prev.items():
            nxt = curr.get(sym)
            conv, p0 = _mon_float(row, "conviction"), _mon_float(row, "price")
            p1 = _mon_float(nxt, "price") if nxt else None
            if conv is None or not p0 or not p1:
                continue
            pairs.append((conv, p1 / p0 - 1.0))
        if len(pairs) < EDGE_MIN_NAMES:
            continue
        rho = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
        ranked = sorted(pairs, key=lambda p: -p[0])
        k = max(3, len(ranked) // EDGE_QUINTILE)
        top = sum(p[1] for p in ranked[:k]) / k
        bot = sum(p[1] for p in ranked[-k:]) / k
        out.append({"from": a, "to": b, "ic": rho, "names": len(pairs),
                    "top_quintile": round(top * 100, 4),
                    "bottom_quintile": round(bot * 100, 4),
                    "spread_bp": round((top - bot) * 1e4, 1)})
    return out


def _active_contributions(by_date: dict, boundary: str | None, limit: int = 8) -> dict:
    """Where the basket-minus-equal-weight gap came from, name by name.

    Pure accounting: contribution = active weight x (return - universe mean), summed
    over legs. It always adds up to the realised gap, which is exactly why it is
    seductive and exactly why it is labelled. Over six legs the ranking is dominated by
    sampling noise, and treating it as a list of names to drop is how a model gets
    fitted to its own error.

    Split by whether the position was over- or under-weight, because the two are
    different mistakes: an overweight name that fell is a selection error, an
    underweight name that rose is an omission, and they have different remedies.
    """
    dates = sorted(by_date)
    contrib: dict[str, float] = {}
    held: dict[str, float] = {}
    legs = 0
    for a, b in zip(dates, dates[1:]):
        if boundary and a < boundary:
            continue
        prev, curr = by_date[a], by_date[b]
        rets = {}
        for sym, row in prev.items():
            nxt = curr.get(sym)
            p0 = _mon_float(row, "price")
            p1 = _mon_float(nxt, "price") if nxt else None
            if p0 and p1:
                rets[sym] = p1 / p0 - 1.0
        if len(rets) < EDGE_MIN_NAMES:
            continue
        legs += 1
        w = _perf_weights(prev)
        tw = sum(v for s, v in w.items() if s in rets) or 1.0
        mean_r = sum(rets.values()) / len(rets)
        for sym, r in rets.items():
            active = (w.get(sym, 0.0) / tw) - 1.0 / len(rets)
            contrib[sym] = contrib.get(sym, 0.0) + active * (r - mean_r)
            held[sym] = held.get(sym, 0.0) + active
    ranked = sorted(contrib.items(), key=lambda kv: kv[1])
    def row(sym, v):
        return {"symbol": sym, "bp": round(v * 1e4, 1),
                "stance": "overweight" if held.get(sym, 0.0) > 0 else "underweight"}
    return {
        "legs": legs,
        "total_bp": round(sum(contrib.values()) * 1e4, 1),
        "detractors": [row(s, v) for s, v in ranked[:limit]],
        "contributors": [row(s, v) for s, v in ranked[-limit:][::-1]],
        "basis": ("Arithmetic, not evidence. Contribution = active weight x (return - "
                  "universe mean), summed over legs; it reconciles to the realised gap "
                  "by construction. Over this many legs the ordering is mostly sampling "
                  "noise, so it is a description of what happened and not a list of "
                  "names to act on. An overweight name that fell is a selection error; "
                  "an underweight name that rose is an omission."),
    }


def _compute_edge() -> dict:
    """Whether the conviction score has demonstrable predictive power yet.

    Reports the interval, not just the point estimate. A mean IC quoted alone invites
    the reader to treat +0.006 as "slightly positive" when the honest statement is
    "cannot be distinguished from nothing with the data on hand".
    """
    by_date, _ = _perf_by_date()
    perf = _compute_performance()
    boundary = perf.get("spec_boundary")
    legs = _edge_legs(by_date, boundary)
    attribution = _active_contributions(by_date, boundary)
    ics = [l["ic"] for l in legs if l["ic"] is not None]
    spreads = [l["spread_bp"] for l in legs]
    base = {"legs": len(legs), "min_legs": EDGE_MIN_LEGS, "boundary": boundary,
            "spec_hash": SPEC_HASH, "series": legs, "attribution": attribution,
            # The realised gap the attribution reconciles to. Carried here so the panel
            # can state the underperformance and the null result side by side, which is
            # the pairing that stops either being misread on its own.
            "book_total": perf.get("book_total"),
            "equal_weight_total": perf.get("equal_weight_total"),
            "benchmark_total": perf.get("benchmark_total"),
            "basis": ("Information coefficient = rank correlation between tonight's "
                      "conviction and tomorrow's return, across the assets scored on "
                      "both nights. It answers whether the ordering is informative, "
                      "which is a different question from whether the basket beat the "
                      "benchmark — a concentrated book with no edge underperforms an "
                      "equal-weight control as a matter of course.")}
    if len(ics) < 2:
        return {**base, "measurable": False, "mean_ic": None, "ci": None,
                "verdict": "Not enough legs to measure anything."}

    mean = sum(ics) / len(ics)
    var = sum((i - mean) ** 2 for i in ics) / (len(ics) - 1)
    se = (var / len(ics)) ** 0.5
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    measurable = len(ics) >= EDGE_MIN_LEGS and (lo > 0 or hi < 0)
    # Per-leg IC noise is about 1/sqrt(n-3); the legs needed to resolve a given true
    # signal follows from wanting a standard error of half that signal.
    nbar = sum(l["names"] for l in legs) / len(legs)
    per_leg = 1.0 / max(1.0, (nbar - 3)) ** 0.5
    needed = {f"{t:.2f}": int(round((per_leg / (t / 2.0)) ** 2)) for t in EDGE_TARGET_ICS}
    return {
        **base,
        "measurable": measurable,
        "mean_ic": round(mean, 4),
        "median_ic": round(sorted(ics)[len(ics) // 2], 4),
        "ic_sd": round(var ** 0.5, 4),
        "ic_se": round(se, 4),
        "ci": [round(lo, 4), round(hi, 4)],
        "t_stat": round(mean / se, 3) if se else None,
        "legs_positive": sum(1 for i in ics if i > 0),
        "mean_spread_bp": round(sum(spreads) / len(spreads), 1) if spreads else None,
        "spreads_positive": sum(1 for s in spreads if s > 0),
        "per_leg_noise": round(per_leg, 3),
        "legs_needed": needed,
        "verdict": (
            "Conviction orders the universe informatively."
            if measurable and mean > 0 else
            "Conviction orders the universe backwards — the ranking is inverted."
            if measurable else
            "No measurable relationship between conviction and next-day return. The "
            "interval spans zero, so this is neither evidence the score works nor "
            "evidence it does not — there is simply not enough history yet."),
    }


def _perf_by_date() -> tuple[dict, int]:
    """signals.csv as {date: {symbol: row}}, latest run per (date, symbol) winning.

    The ledger has been appended to more than once on some days — 2026-08-02 carries
    nine runs — so a naive read counts one day's board nine times and computes returns
    between a day and itself. Collapsing on (date, symbol) is the only reading that
    makes a daily series daily; the count of collapsed rows is reported rather than
    swallowed, because a rising number means the workflow is firing more than once.
    """
    if not LEDGER_CSV.exists():
        return {}, 0
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    by_date: dict[str, dict[str, dict]] = {}
    collapsed = 0
    for r in rows:
        d, sym = r.get("date"), (r.get("symbol") or "").upper()
        if not d or not sym:
            continue
        try:
            price = float(r.get("price") or 0)
            conv = float(r.get("conviction") or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        day = by_date.setdefault(d, {})
        if sym in day:
            collapsed += 1
        day[sym] = {"price": price, "conviction": conv}
    return by_date, collapsed


def _perf_weights(day: dict) -> dict:
    """Conviction-weighted top N, as build_basket() would have published it."""
    ranked = sorted(day.items(), key=lambda kv: -kv[1]["conviction"])[:PERF_TOP_N]
    total = sum(v["conviction"] for _, v in ranked) or 1.0
    return {sym: v["conviction"] / total for sym, v in ranked if v["conviction"] > 0}


def _compute_performance() -> dict:
    """Paper return of the published basket, chained across recorded days.

    The same three rules as the equity terminal, for the same reasons:

    * **Weights from the earlier night, prices from both.** Using tonight's weights
      against tonight's prices prints alpha every day forever and looks entirely
      plausible doing it.
    * **Only recorded dates.** No back-fill. A day that was not recorded is gone.
    * **A missing benchmark is a gap, not a flat segment.** Flat reads as "the market
      did not move" where the truth is "it was not recorded".

    Deliberately NOT built on ledger/index.json. That file's row series is unusable: its
    header declares seven columns while eight of ten rows carry thirteen, so a DictReader
    silently files the alpha figure under n_holdings and a dollar amount under
    rebalanced; several dates repeat; and benchmark_return is identically 0.0 on every
    row, which makes its "alpha" the raw return under another name. signals.csv is
    structurally sound and is the source here.
    """
    by_date, collapsed = _perf_by_date()
    dates = sorted(by_date)
    if len(dates) < 2:
        return {"days": len(dates), "legs": 0, "min_days": PERF_MIN_DAYS,
                "renderable": False, "series": [], "duplicates_collapsed": collapsed,
                "benchmark": PERF_BENCHMARK}

    legs = []
    for a, b in zip(dates, dates[1:]):
        prev, curr = by_date[a], by_date[b]
        weights = _perf_weights(prev)
        num = held = missing = 0.0
        names = 0
        for sym, w in weights.items():
            held += w
            p1 = (curr.get(sym) or {}).get("price")
            if not p1:
                missing += w      # left the universe, or unpriceable tonight
                continue
            num += w * (p1 / prev[sym]["price"] - 1.0)
            names += 1
        kept = held - missing
        if held <= 0 or kept <= 0:
            continue

        shared = [s for s in prev if s in curr]
        eq = (sum(curr[s]["price"] / prev[s]["price"] - 1.0 for s in shared) / len(shared)
              if shared else None)
        bp0 = (prev.get(PERF_BENCHMARK) or {}).get("price")
        bp1 = (curr.get(PERF_BENCHMARK) or {}).get("price")
        legs.append({
            "from": a, "to": b,
            "book": num / kept,
            "benchmark": (bp1 / bp0 - 1.0) if (bp0 and bp1) else None,
            "equal_weight": eq,
            "names": names,
            "weight_lost": missing / held,
            "usable": (missing / held) <= PERF_MAX_WEIGHT_LOSS,
        })

    usable = [l for l in legs if l["usable"]]

    # Start after the most recent specification boundary. A leg that straddles one
    # chains a book chosen by one model onto returns scored by another, and averaging
    # across that is not a track record for either — it is a number about a model that
    # never existed. The legs before the boundary are still real measurements of the
    # model that produced them; they are excluded from *this* curve, not deleted, and
    # the count is reported so the exclusion is visible rather than implied.
    breaks = sorted(b["to"] for b in _spec_breaks())
    boundary = breaks[-1] if breaks else None
    dropped_pre_break = 0
    if boundary:
        before = [l for l in usable if l["from"] < boundary]
        dropped_pre_break = len(before)
        usable = [l for l in usable if l["from"] >= boundary]

    series, book, bench, eqc = [], 1.0, 1.0, 1.0
    bench_live = False
    # The origin is where measurement starts, which is the first *usable* leg's earlier
    # date — not the first date on file. Anchoring at dates[0] when the opening legs
    # were dropped draws the first segment from 08-01 to 08-05 and attributes one
    # night's return to four days of chart.
    if usable:
        series.append({"date": usable[0]["from"], "book": 0.0,
                       "benchmark": 0.0, "equal_weight": 0.0})
    for l in usable:
        book *= (1.0 + l["book"])
        if l["benchmark"] is not None:
            bench *= (1.0 + l["benchmark"]); bench_live = True
        if l["equal_weight"] is not None:
            eqc *= (1.0 + l["equal_weight"])
        series.append({
            "date": l["to"],
            "book": round((book - 1.0) * 100, 4),
            "benchmark": round((bench - 1.0) * 100, 4) if l["benchmark"] is not None else None,
            "equal_weight": round((eqc - 1.0) * 100, 4) if l["equal_weight"] is not None else None,
        })
    # The origin is only a real point on a line that has real points.
    if series and not bench_live:
        series[0]["benchmark"] = None
    if series and not any(p["equal_weight"] is not None for p in series[1:]):
        series[0]["equal_weight"] = None

    # A leg that straddles a specification boundary chains a book chosen by one model
    # onto returns scored by another. Reported rather than hidden, exactly as the equity
    # terminal reports a curve spanning two spec hashes: it is two series drawn end to
    # end, and the reader has to know which.
    crossed = sorted({l["to"] for l in usable if l["to"] in set(breaks)})

    return {
        "days": len(dates),
        "min_days": PERF_MIN_DAYS,
        "spec_breaks_crossed": crossed,
        "spec_stable": not crossed,
        "spec_boundary": boundary,
        "legs_before_boundary": dropped_pre_break,
        "renderable": len(usable) >= PERF_MIN_DAYS - 1,
        "legs": len(usable),
        "legs_dropped": len(legs) - len(usable),
        "duplicates_collapsed": collapsed,
        "from": usable[0]["from"] if usable else dates[0],
        "to": usable[-1]["to"] if usable else dates[-1],
        "recorded_from": dates[0], "recorded_to": dates[-1],
        "benchmark": PERF_BENCHMARK,
        "benchmark_available": bench_live,
        "top_n": PERF_TOP_N,
        "book_total": round((book - 1.0) * 100, 4) if usable else None,
        "benchmark_total": round((bench - 1.0) * 100, 4) if bench_live else None,
        "equal_weight_total": round((eqc - 1.0) * 100, 4) if usable else None,
        "series": series,
    }


# ---------------------------------------------------------------------------
# model monitoring
# ---------------------------------------------------------------------------
# Every threshold below is tuned against the days actually on file, not carried over
# from the equity project. Two of its panels do not transfer at all and are deliberately
# replaced rather than copied:
#
#   * Equity ranks a fixed index constituent list, so its rank correlation is a
#     statement about scoring stability. This universe is "top N by market cap from
#     CoinGecko" and turns over 8-20% a night by construction, so the same statistic
#     across all names would mostly report which coins were large that morning.
#     Stability is therefore measured on the surviving cohort only, with the churn
#     reported beside it so the number is never read without its context.
#
#   * Equity's coverage panel measures the share of inputs that came from a filing
#     rather than a sector-median substitution. Nothing here imputes, so that has no
#     analogue. The equivalent early warning is field presence: if CoinGecko stops
#     returning rs200, or Dune's Module B flatlines, a factor quietly goes to zero and
#     the only symptom is a column of nulls.
#
# Dispersion is a warn, never a fail. Equity's percentiles guarantee dispersion by
# construction, so a collapse there is always a pipeline defect. These are absolute
# thresholds over raw turnover and momentum, so a collapse can be a genuine market —
# everything correlated, or liquidity gone. It is a regime reading, not a bug report.
MON_MIN_ASSETS = 25
MON_DISPERSION_WARN = 8.0        # observed 17.9 on the latest recorded board
MON_STABILITY_WARN = 0.70
MON_CHURN_WARN = 0.35            # settled nights run 8-20%; the first two were 44-63%
MON_STALE_HOURS = 36
# Fields whose disappearance would silently zero a factor rather than raise anything.
# These are model inputs: an absence here is a defect, and it sets the check's status.
MON_TRACKED_FIELDS = ("price", "market_cap", "turnover_pct", "conviction",
                      "rs7", "rs14", "rs30", "rs200", "perp_mult",
                      # Genuine model inputs, not context: _lavl_regime — a captured
                      # SPEC_FUNCTION — reads high_24h/low_24h off the live payload and
                      # always has. Recording them as columns did not put them into the
                      # specification; they were already in it. If CoinGecko stops
                      # returning them the LAVL band silently changes for every asset,
                      # which is precisely the dropout this panel exists to catch.
                      "high_24h", "low_24h")
# Recorded-but-not-scored context: the Dune feed, and the emission/adoption ratio beside
# it — score() computes its own internal adoption proxy and never reads any of these.
# Coverage is reported so a dead feed stays visible, but it must not set the status.
# `unlocks_usd` is null for most tokens *by construction* — unlock schedules are not
# on-chain in the general case — so folding it into the warn would pin this panel to a
# permanent amber and hide a real dropout in price or rs200 behind it.
MON_CONTEXT_FIELDS = ("era", "unlocks_usd", "supply_increase_pct", "addr_growth_pct",
                      # Derivatives and the daily bar. Observational on the same terms:
                      # score() reads none of them. funding_rate is the one exception
                      # worth naming — it has always reached score() via lavl_perp_mult,
                      # but through perps_map at run time, never through this column.
                      "funding_rate", "oi_usd", "long_short_ratio")
MON_FIELD_PRESENCE_WARN = 0.90


def _spearman(a: list, b: list):
    """Rank correlation, ties averaged. None below three pairs."""
    n = len(a)
    if n < 3:
        return None

    def ranks(xs):
        order = sorted(range(n), key=lambda i: xs[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(num / (da * db), 4) if da and db else None


def _mon_check(name, status, detail, value=None) -> dict:
    return {"name": name, "status": status, "detail": detail, "value": value}


def _mon_float(r, k):
    try:
        v = r.get(k)
        return float(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


# A suspected specification change, detected from the data rather than declared.
# The hash makes a change visible going forward; this catches one that predates the
# hash, or one shipped by someone who edited a threshold without the hash noticing —
# and it is the backstop that found the 2026-08-05 break in this ledger, where the
# median conviction moved 36 points on a night the median price moved 0.00%.
BREAK_SCORE_MOVE = 10.0      # median |delta conviction|, points
BREAK_PRICE_MOVE = 0.02      # median |price move| below which the market cannot explain it


def _spec_breaks() -> list:
    """Nights where the scores moved and the market did not.

    Conviction is a function of price and liquidity inputs. If the median asset's score
    moves double digits while the median asset's price barely moves, the function
    changed — that is a specification boundary regardless of whether anything recorded
    one, and any return study or Information Coefficient spanning it is a number about
    two different models.
    """
    rows = _read_signals_rows()
    by_date = {}
    for r in rows:
        d, sym = r.get("date"), (r.get("symbol") or "").upper()
        if d and sym:
            by_date.setdefault(d, {})[sym] = r
    dates = sorted(by_date)
    out = []
    for a, b in zip(dates, dates[1:]):
        prev, curr = by_date[a], by_date[b]
        shared = sorted(set(prev) & set(curr))
        dconv, dpx = [], []
        for s in shared:
            c0, c1 = _mon_float(prev[s], "conviction"), _mon_float(curr[s], "conviction")
            p0, p1 = _mon_float(prev[s], "price"), _mon_float(curr[s], "price")
            if c0 is not None and c1 is not None:
                dconv.append(abs(c1 - c0))
            if p0 and p1:
                dpx.append(abs(p1 / p0 - 1.0))
        if len(dconv) < 5 or len(dpx) < 5:
            continue
        med_c = sorted(dconv)[len(dconv) // 2]
        med_p = sorted(dpx)[len(dpx) // 2]
        if med_c >= BREAK_SCORE_MOVE and med_p <= BREAK_PRICE_MOVE:
            out.append({
                "from": a, "to": b, "shared": len(shared),
                "median_score_move": round(med_c, 1),
                "median_price_move": round(med_p * 100, 2),
                "detail": ("the median asset's score moved %.0f points on a night its "
                           "price moved %.2f%% — the scoring function changed"
                           % (med_c, med_p * 100)),
            })
    return out


def _compute_monitor(dune_report: dict | None = None) -> dict:
    """Operational condition of the pipeline. Never a claim about predictive power.

    A board can be fresh, dispersed, fully covered and perfectly stable while
    forecasting nothing at all. Whether high-conviction assets outperform is a separate
    measurement needing months inside one specification hash — which is what the
    spec_hash column exists to make possible, and why it is reported here.

    ``dune_report`` is this run's fetch report, when there was one. Without it the
    contextual feed can only be described from the columns on disk, which cannot tell a
    feed that is switched off from one that is switched on and pointing at the wrong
    query.
    """
    rows = _read_signals_rows()
    if not rows:
        return {}

    by_date = {}
    for r in rows:
        d, sym = r.get("date"), (r.get("symbol") or "").upper()
        if d and sym:
            by_date.setdefault(d, {})[sym] = r
    dates = sorted(by_date)
    if not dates:
        return {}
    latest = by_date[dates[-1]]

    # --- stability, on the surviving cohort ---------------------------------
    stability = None
    if len(dates) >= 2:
        prev, curr = by_date[dates[-2]], by_date[dates[-1]]
        shared = sorted(set(prev) & set(curr))
        pairs = [(x, y) for x, y in
                 ((_mon_float(prev[s], "conviction"), _mon_float(curr[s], "conviction"))
                  for s in shared)
                 if x is not None and y is not None]
        entered, left = sorted(set(curr) - set(prev)), sorted(set(prev) - set(curr))
        moves = [abs(y - x) for x, y in pairs]
        stability = {
            "from": dates[-2], "to": dates[-1],
            "shared": len(pairs),
            "rank_correlation": _spearman([p[0] for p in pairs], [p[1] for p in pairs]),
            "mean_abs_move": round(sum(moves) / len(moves), 2) if moves else None,
            "max_abs_move": round(max(moves), 1) if moves else None,
            # Reported with the correlation, always. A stability reading on a population
            # that replaced a fifth of itself overnight is not a statement about scoring.
            "entered": entered, "left": left,
            "churn": round(len(entered) / len(curr), 3) if curr else None,
            "universe_prev": len(prev), "universe_curr": len(curr),
        }

    # --- field presence, and its trend --------------------------------------
    def presence(day, fields=MON_TRACKED_FIELDS):
        return {f: (round(sum(1 for r in day.values() if _mon_float(r, f) is not None)
                          / len(day), 4) if day else 0.0)
                for f in fields}

    coverage_series = [dict(date=d, n=len(by_date[d]), **presence(by_date[d])) for d in dates]
    latest_presence = presence(latest)
    context_presence = presence(latest, MON_CONTEXT_FIELDS)
    context_series = [dict(date=d, **presence(by_date[d], MON_CONTEXT_FIELDS)) for d in dates]

    # --- dispersion, as a regime reading ------------------------------------
    def sigma(day):
        cs = [c for c in (_mon_float(r, "conviction") for r in day.values()) if c is not None]
        if len(cs) < 2:
            return None, None
        m = sum(cs) / len(cs)
        return (round((sum((c - m) ** 2 for c in cs) / (len(cs) - 1)) ** 0.5, 2),
                round(sorted(cs)[len(cs) // 2], 1))

    dispersion, _median = sigma(latest)
    disp_series = []
    for d in dates:
        s, med = sigma(by_date[d])
        if s is not None:
            disp_series.append({"date": d, "dispersion": s, "median": med})
    convs = [c for c in (_mon_float(r, "conviction") for r in latest.values()) if c is not None]

    # --- specification continuity -------------------------------------------
    spec_spans, unknown_days = [], 0
    for d in dates:
        hashes = {r.get("spec_hash") for r in by_date[d].values() if r.get("spec_hash")}
        h = sorted(hashes)[0] if hashes else None
        if not h:
            unknown_days += 1
        if spec_spans and spec_spans[-1]["spec_hash"] == h:
            spec_spans[-1]["to"] = d
            spec_spans[-1]["days"] += 1
        else:
            spec_spans.append({"spec_hash": h, "from": d, "to": d, "days": 1})

    # --- health -------------------------------------------------------------
    checks = []
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(dates[-1] + "T00:00:00+00:00")).total_seconds() / 3600
        checks.append(_mon_check(
            "Data freshness", "pass" if age_h <= MON_STALE_HOURS else "fail",
            "latest board %s (%.0fh old)" % (dates[-1], age_h), round(age_h, 1)))
    except ValueError:
        checks.append(_mon_check("Data freshness", "fail", "unparseable latest date"))

    checks.append(_mon_check(
        "Universe size", "pass" if len(latest) >= MON_MIN_ASSETS else "fail",
        "%d assets scored on %s" % (len(latest), dates[-1]), len(latest)))

    tiers = {r.get("signal") for r in latest.values() if r.get("signal")}
    checks.append(_mon_check(
        "Signal tiers populated", "pass" if len(tiers) >= 3 else "warn",
        "%d of 5 tiers in use" % len(tiers), len(tiers)))

    # Warn, never fail — see the note above the thresholds.
    ok = (dispersion or 0) >= MON_DISPERSION_WARN
    checks.append(_mon_check(
        "Score dispersion", "pass" if ok else "warn",
        "sigma = %s across %d assets" % (dispersion, len(convs))
        + ("" if ok else " — compressed. On absolute thresholds this can be the market, "
                         "not the pipeline"),
        dispersion))

    if latest_presence:
        worst = min(latest_presence.items(), key=lambda kv: kv[1])
        checks.append(_mon_check(
            "Field presence", "pass" if worst[1] >= MON_FIELD_PRESENCE_WARN else "warn",
            "weakest input `%s` present for %.0f%% of the board" % (worst[0], worst[1] * 100),
            round(worst[1], 4)))

    if context_presence:
        live = [f for f, v in context_presence.items() if v > 0]
        # Informational by design — see MON_CONTEXT_FIELDS. Never graded: a null here is
        # the expected state, not a defect. But *why* it is null is worth saying, which
        # is what the fetch report supplies — a query id pointing at the wrong query
        # produces exactly the same empty columns as no configuration at all.
        why = {
            "unconfigured": "no Dune query configured",
            "unreachable": "the Dune call failed — wrong key, no such query, or a query "
                           "that has never been executed",
            "unusable": "the configured Dune query returned rows this feed cannot read "
                        "— it is answering a different question",
        }.get((dune_report or {}).get("status"))
        # Only where the reason needs evidence. "no Dune query configured" is complete
        # on its own; the other two are claims about a remote system and have to show
        # what came back. Columns come from the structured field rather than being read
        # back out of the prose, so the two cannot drift apart.
        if why and (dune_report or {}).get("status") != "unconfigured":
            bits = [b for b in (dune_report.get("detail"),
                                "returned: " + ", ".join(dune_report["columns"])
                                if dune_report.get("columns") else None) if b]
            if bits:
                why += " (%s)" % "; ".join(bits)
        checks.append(_mon_check(
            "Contextual feeds", "info",
            ("%d of %d recorded-not-scored fields carry values (%s) — observational only, "
             "nothing here reaches score()"
             % (len(live), len(context_presence), ", ".join(live)))
            if live else
            (why or "no contextual fields carry values") +
            ". Nothing scored depends on it",
            round(len(live) / len(context_presence), 4)))

    if stability and stability["rank_correlation"] is not None:
        rc, churn = stability["rank_correlation"], stability["churn"] or 0
        checks.append(_mon_check(
            "Ranking stability", "pass" if rc >= MON_STABILITY_WARN else "warn",
            "rank correlation %s across %d names held in common" % (rc, stability["shared"]),
            rc))
        checks.append(_mon_check(
            "Universe churn", "pass" if churn <= MON_CHURN_WARN else "warn",
            "%d of %d assets are new (%.0f%%) — the ranking above is measured only on the rest"
            % (len(stability["entered"]), stability["universe_curr"], churn * 100),
            round(churn, 3)))
    else:
        checks.append(_mon_check("Ranking stability", "pending",
                                 "needs two recorded days; nothing to compare against yet"))

    real_spans = [s for s in spec_spans if s["spec_hash"]]
    if unknown_days and not real_spans:
        checks.append(_mon_check(
            "Specification history", "pending",
            "%d day(s) recorded before the specification was hashed — unknown, not "
            "assumed to match today's" % unknown_days, 0))
    else:
        multi = len(real_spans) > 1
        checks.append(_mon_check(
            "Specification history", "warn" if multi else "pass",
            "%d specification(s) across %d recorded day(s)"
            % (len(real_spans), sum(s["days"] for s in real_spans))
            + ("; %d earlier day(s) unhashed" % unknown_days if unknown_days else "")
            + ("  — a series spanning two hashes is two datasets" if multi else ""),
            len(real_spans)))

    breaks = _spec_breaks()
    if breaks:
        # Fails only when this run introduced one. A boundary in the recorded past is an
        # immutable fact about the history — failing on it forever would block every
        # future deploy and train everyone to ignore the gate, which is worse than not
        # having it. A fresh one means tonight's run changed the scoring, and that
        # should stop the build.
        fresh = [b for b in breaks if b["to"] == dates[-1]]
        checks.append(_mon_check(
            "Undeclared specification change", "fail" if fresh else "warn",
            ("this run changed the scoring: " + fresh[0]["detail"]) if fresh else
            ("%d boundary in the recorded past (%s) — history either side of it is two "
             "datasets, not one, and any return study has to segment there"
             % (len(breaks), ", ".join(b["to"] for b in breaks))),
            len(breaks)))
    else:
        checks.append(_mon_check(
            "Undeclared specification change", "pass",
            "no night where the median score moved without the median price", 0))

    dupes = len(rows) - len({(r.get("date"), r.get("symbol")) for r in rows})
    checks.append(_mon_check(
        "Ledger integrity", "pass" if dupes == 0 else "fail",
        "one row per (date, symbol)" if dupes == 0
        else "%d duplicate row(s) — the daily series is not daily" % dupes, dupes))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_hash": SPEC_HASH,
        "observations": len(dates),
        "from": dates[0], "to": dates[-1],
        "health": checks,
        "stability": stability,
        "dispersion": {"latest": dispersion, "series": disp_series,
                       "warn_below": MON_DISPERSION_WARN},
        "coverage": {"latest": latest_presence, "series": coverage_series,
                     "warn_below": MON_FIELD_PRESENCE_WARN,
                     "basis": ("Share of the board carrying a usable value for each input. "
                               "Nothing here is imputed, so this measures feed health rather "
                               "than substitution: a field going to zero is an upstream "
                               "dropout, and the factor it feeds quietly stops contributing."),
                     "context": {"latest": context_presence, "series": context_series,
                                 "feed": {k: v for k, v in (dune_report or {}).items()
                                          if k != "data"} or None,
                                 "basis": ("Recorded, not scored. These columns are logged "
                                           "beside every board so they can be studied "
                                           "against realised returns before anyone argues "
                                           "for adopting them; adoption would change the "
                                           "specification hash and start a new track "
                                           "record. Nulls here are expected — unlock "
                                           "schedules are contractual, not on-chain, and "
                                           "are recorded only for tokens whose vesting "
                                           "contracts are enumerated in the query.")}},
        "specification": {"spans": spec_spans, "unknown_days": unknown_days,
                          "suspected_breaks": breaks},
        "scope": ("Operational condition only. Whether these scores predict returns is a "
                  "separate question needing months of history inside one specification "
                  "hash. Nothing here is evidence that the conviction score forecasts "
                  "anything; it is evidence that the pipeline producing it is behaving."),
    }


CHOP_PERIOD = 14
CHOP_TRENDING = 38.2
CHOP_CHOPPY = 61.8


def choppiness(bars: list) -> float | None:
    """The 14-period Choppiness Index over accumulated daily bars.

        CHOP = 100 * log10(sum(ATR1) / (maxHigh - minLow)) / log10(period)

    Low means directional, high means range-bound. It is deliberately computed from
    bars this pipeline recorded itself: CoinGecko's /ohlc endpoint has no daily
    granularity at all (30-minute at 1 day, 4-hour to 30 days, 4-day beyond), so a
    "14-day" CHOP taken from it would be a 14-bar CHOP over 56 hours wearing the wrong
    label. high_24h/low_24h are already in the markets response, so the honest version
    costs no extra request and simply needs fourteen nights to exist.

    Returns None below `CHOP_PERIOD + 1` bars — the panel says how many it has rather
    than showing a number computed from a shorter window.
    """
    if len(bars) < CHOP_PERIOD + 1:
        return None
    window = bars[-(CHOP_PERIOD + 1):]
    trs = []
    for prev, cur in zip(window, window[1:]):
        h, lo, pc = cur.get("high"), cur.get("low"), prev.get("close")
        if h is None or lo is None or pc is None:
            return None
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    recent = window[1:]
    highs = [b["high"] for b in recent if b.get("high") is not None]
    lows = [b["low"] for b in recent if b.get("low") is not None]
    if len(highs) < CHOP_PERIOD or len(lows) < CHOP_PERIOD:
        return None
    rng = max(highs) - min(lows)
    total = sum(trs)
    if rng <= 0 or total <= 0:
        return None
    return round(100.0 * math.log10(total / rng) / math.log10(CHOP_PERIOD), 2)


def chop_regime(chop) -> str | None:
    if chop is None:
        return None
    return ("TRENDING" if chop < CHOP_TRENDING
            else "RANGE-BOUND" if chop > CHOP_CHOPPY
            else "TRANSITIONAL")


def _chop_by_symbol() -> dict:
    """Choppiness per asset from the recorded bars, plus how far off it is otherwise.

    ``{SYMBOL: {"chop": float|None, "regime": str|None, "bars": int}}`` — the bar count
    travels with the value so the terminal can render "accumulating (10/15)" instead of
    an empty cell that reads as a broken column.
    """
    rows = _read_signals_rows()
    bars: dict[str, list] = {}
    for r in sorted(rows, key=lambda x: (x.get("date") or "")):
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        h, lo, c = _num(r.get("high_24h")), _num(r.get("low_24h")), _num(r.get("price"))
        if h is None or lo is None or c is None:
            continue
        bars.setdefault(sym, []).append({"high": h, "low": lo, "close": c})
    out = {}
    for sym, seq in bars.items():
        ch = choppiness(seq)
        out[sym] = {"chop": ch, "regime": chop_regime(ch), "bars": len(seq)}
    return out


# Horizons the change feed will use, longest first, with the recorded days each needs.
# A delta over N days needs N+1 recorded boards to have two endpoints.
FEED_HORIZONS = (("d30", 31), ("d10", 11), ("d7", 8), ("d1", 2))
FEED_LIMIT = 8


def _change_feed(trend: dict, days_recorded: int) -> dict:
    """Largest conviction gains and losses, over the longest horizon that has data.

    This used to be hardwired to a 10-day delta, which needs eleven recorded boards.
    The ledger has never had that many, so `d10` was None for every asset on every run
    and the feed serialised as {"gains": [], "losses": []} — rendering as an empty
    panel that looks exactly like a broken one. It would have started working on its
    own eventually, silently, which is its own problem: nobody would have known whether
    it was fixed or still faulty.

    So it degrades instead. It reports the horizon it actually used and what the longer
    ones are still waiting for, and the panel says so rather than showing nothing.
    """
    pending = {h: {"needs": need, "have": days_recorded}
               for h, need in FEED_HORIZONS if days_recorded < need}
    for horizon, need in FEED_HORIZONS:
        if days_recorded < need:
            continue
        movers = [(s, t[horizon]) for s, t in trend.items() if t.get(horizon) is not None]
        if not movers:
            continue
        movers.sort(key=lambda x: x[1], reverse=True)
        return {
            "horizon": horizon,
            "days": int(horizon[1:]),
            "gains": [{"symbol": s, "delta": round(d, 1), horizon: round(d, 1)}
                      for s, d in movers[:FEED_LIMIT] if d > 0],
            "losses": [{"symbol": s, "delta": round(d, 1), horizon: round(d, 1)}
                       for s, d in reversed(movers[-FEED_LIMIT:]) if d < 0],
            "pending": pending,
        }
    return {"horizon": None, "days": None, "gains": [], "losses": [], "pending": pending}


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
            # d1 and d7 exist because d10 needs eleven recorded days and the ledger has
            # had fewer for its entire life so far. The feed was serialising as
            # {"gains": [], "losses": []} every night and rendering as nothing, which
            # is indistinguishable from a broken panel — see _change_feed below.
            "d1": _at(seq, 1),
            "d7": _at(seq, 7),
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

    change_feed = _change_feed(trend, len(set(all_dates)))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_assets": n,  # latest-day investable universe (not the historical union)
        "breadth_above70": above70,
        "breadth_above80": above80,
        "breadth_pct_above70": round(100 * above70 / n, 1),
        "dispersion": round(dispersion, 2),
        "persistent_30d": persistent30,
        "persistent_90d": persistent90,
        "conviction_change_feed": change_feed,
        # Regime per asset, plus the bar count so the terminal can say what it is still
        # waiting for rather than rendering an empty cell.
        "chop": _chop_by_symbol(),
        # Whether the ordering is informative at all — the question that decides
        # whether any of the rest is worth acting on.
        "edge": _compute_edge(),
        "chop_period": CHOP_PERIOD,
        "trend": {s: {"conviction": round(t["conviction"], 1),
                      **{h: (round(t[h], 1) if t[h] is not None else None)
                         for h in ("d1", "d7", "d10", "d30")}}
                 for s, t in sorted(trend.items(), key=lambda x: -x[1]["conviction"])},
    }


def _macro_regime_from_ledger() -> str:
    """Macro Liquidity Gate (D) — PASSIVE / display-only.

    Reads the stored index.csv and compares today's global mcap to the value
    ~7 days ago. RISK-OFF if global mcap is down > 8% over that window.
    Returns "RISK-OFF" / "RISK-ON" / "N/A" (no history). This is a logged
    context signal only — it NEVER changes basket holdings.
    """
    try:
        # Through the schema-checked reader: a mismatched header yields no rows rather
        # than a column of misread values.
        rows = read_index_rows()
        if not rows:
            return "N/A"
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
# Where a file whose header cannot be reconciled with INDEX_FIELDS is set aside. Kept
# rather than deleted: it is the only record of what the pipeline actually emitted, and
# the malformed rows are evidence even though they are not data.
INDEX_LEGACY_CSV = LEDGER_DIR / "index.legacy.csv"

# The schema, in one place. Previously the header was `list(row.keys())` written only
# when the file did not yet exist, so when six columns were added to `row` the existing
# file kept its seven-column header while every new line appended thirteen values. A
# DictReader then filed the alpha figure under n_holdings and a dollar amount under
# rebalanced, with no error and no visible symptom — the same class of failure the
# equity ledger's append-only column check exists to catch.
INDEX_FIELDS = [
    "date", "global_market_cap",
    "basket_return_since_entry", "benchmark_return_since_entry", "alpha_since_entry",
    "exec_adjusted_return_since_entry", "turnover_bps", "macro_regime",
    "eject_delta", "ejected_syms", "entrants_syms", "n_holdings", "rebalanced",
]


def _read_signals_rows() -> list[dict]:
    """Existing signals rows, normalised onto the current FIELDS."""
    if not LEDGER_CSV.exists():
        return []
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        return [{k: r.get(k) for k in FIELDS} for r in csv.DictReader(f)]


def dedupe_signals() -> int:
    """Collapse duplicate (date, symbol) rows, latest occurrence winning.

    A one-shot repair for the history a blind append already produced. Returns the
    number of rows removed. Not run automatically — a caller decides, because silently
    rewriting a ledger is the kind of thing this project is meant to be careful about.
    """
    rows = _read_signals_rows()
    if not rows:
        return 0
    seen: dict[tuple, dict] = {}
    for r in rows:
        seen[(r.get("date"), r.get("symbol"))] = r
    out = sorted(seen.values(), key=lambda r: (r.get("date") or "", r.get("symbol") or ""))
    removed = len(rows) - len(out)
    if removed:
        with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(out)
    return removed


def read_index_rows(path=None) -> list[dict]:
    """Rows from index.csv, or [] if its header does not match the current schema.

    Refusing to read a mismatched file is the point. Parsing it anyway is what produced
    a published chart of fabricated alpha.
    """
    path = path or INDEX_CSV
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return []
        if header != INDEX_FIELDS:
            return []
        return [dict(zip(header, r)) for r in reader if len(r) == len(header)]


def _persist_index_row(row: dict, path=None) -> list[dict]:
    """Write today's row and return the full series.

    Rewrites the whole file every run rather than appending, which fixes three things at
    once: the header can never drift from the rows, a re-run on the same date replaces
    that date instead of adding a duplicate, and a file whose header no longer matches
    the schema is moved aside instead of being appended to. At ledger scale — one row a
    day — a full rewrite costs nothing.
    """
    path = path or INDEX_CSV
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != INDEX_FIELDS:
            legacy = INDEX_LEGACY_CSV
            path.replace(legacy)
            print(f"[index] header did not match the schema ({len(header)} columns vs "
                  f"{len(INDEX_FIELDS)}); moved the old file to {legacy.name} and started "
                  f"a clean series. Those rows are not recoverable: their values are "
                  f"positionally misaligned against the header they were written under.")

    rows = [r for r in read_index_rows(path) if r.get("date") != row["date"]]
    rows.append({k: ("" if row.get(k) is None else row.get(k)) for k in INDEX_FIELDS})
    rows.sort(key=lambda r: r.get("date") or "")

    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)
    return rows
INDEX_JSON = LEDGER_DIR / "index.json"
MARKET_BREADTH_JSON = LEDGER_DIR / "market_breadth.json"
MONITOR_JSON = LEDGER_DIR / "monitor.json"
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
    for h in kept_holdings:
        sym = h["symbol"]
        # refresh conviction from current scoring
        nh = next((x for x in top if x[0] == sym), None)
        conv = nh[2] if nh else h.get("conviction", 0)
        holdings.append({
            "symbol": sym, "conviction": conv, "weight": 0.0,
            "entry_price": h.get("entry_price") or 0,
            "current_price": (next((t.get("current_price") for _, t, _ in top if _ and (t.get("symbol") or "").upper() == sym), None)) or h.get("current_price") or 0,
        })
    for sym, t, conv in new_top:
        holdings.append({
            "symbol": sym, "conviction": conv, "weight": 0.0,
            "entry_price": t.get("current_price") or 0,
            "current_price": t.get("current_price") or 0,
        })

    # Normalise over the holdings that actually exist, not over the top N.
    #
    # The denominator used to be the sum of the top ten's convictions while the weights
    # were applied to kept + new entrants — a superset whenever the hysteresis buffer
    # retains a name that has slipped out of the top ten. The two sets only coincide
    # when nothing is being held over, so the weights stopped summing to 1 the moment
    # the buffer did its job. On 2026-08-08 the denominator was 9 (a single name in
    # `top`) against eleven holdings, and the weights summed to 76.6 — so every
    # `wret = sum(weight * return)` in the index was inflated roughly seventy-six-fold,
    # which is where the published "daily" returns of 87% and 123% came from.
    total_conv = sum(h["conviction"] for h in holdings) or 1
    for h in holdings:
        h["weight"] = round(h["conviction"] / total_conv, 4)

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
        # The benchmark baseline is carried over, not re-snapshotted. Resetting it here
        # while kept holdings keep their original entry_price measured the two legs over
        # different horizons: the basket accumulated from its first entry while the
        # benchmark restarted from zero on every rebalance. Since `rebalanced` was true
        # on nine of the first ten runs, benchmark_return was 0.0 on every row and the
        # reported alpha was the basket's raw return under another name. Only a genuinely
        # new basket — no prior baseline — takes today's reading.
        gmc = prev.get("entry_global_mcap") or fetch_global_market_cap()
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
        # Renamed from basket_return / benchmark_return. Both are cumulative since the
        # basket's cost basis, never overnight, and the old names invited exactly the
        # mistake the consumer made: compounding ten since-entry figures as if they were
        # ten daily ones. Overnight return lives in market_breadth.json's `performance`
        # block, which chains it from the signals ledger.
        "basket_return_since_entry": round(wret * 100, 3),
        "benchmark_return_since_entry": round((bench_total - 1) * 100, 3),
        "alpha_since_entry": round((wret - (bench_total - 1)) * 100, 3),
        "exec_adjusted_return_since_entry": round(exec_adjusted_return * 100, 3),
        "turnover_bps": round(turnover_bps, 1),
        "macro_regime": macro_regime,
        "eject_delta": round(eject_delta * 100, 3) if rebalanced else "",
        "ejected_syms": ",".join(ejected_syms) if (rebalanced and ejected_syms) else "",
        "entrants_syms": ",".join(entrants_syms) if (rebalanced and entrants_syms) else "",
        "n_holdings": len(audit),
        "rebalanced": rebalanced,
    }
    idx_rows = _persist_index_row(row)
    # The latest reading, not a product of every reading. These columns are cumulative
    # since the basket's cost basis, so compounding the series multiplied one number by
    # its own history — ten rows each already up 80-120% chained into a total return of
    # 4.53x. A cumulative figure is read, not accumulated.
    def _last(field):
        vals = [r.get(field) for r in idx_rows if r.get(field) not in (None, "", "None")]
        return 1 + float(vals[-1]) / 100.0 if vals else 1.0
    cum_basket = _last("basket_return_since_entry")
    cum_exec = _last("exec_adjusted_return_since_entry")
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
    # From the overnight series in market_breadth.json, the only daily one we have.
    perf_legs = _compute_performance()
    daily = [l for l in (perf_legs.get("series") or [])]
    dailies = []
    prev_cum = 0.0
    for pt in daily:
        cum = (pt.get("book") or 0.0) / 100.0
        dailies.append((1 + cum) / (1 + prev_cum) - 1)
        prev_cum = cum
    risk = _risk_stats(dailies[1:])
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

    # Module B (Dune). The report, not just the data — a configured query pointing at
    # the wrong thing produces the same empty table as no configuration at all, and the
    # dashboard has to be able to say which.
    dune_report = fetch_dune_report(os.environ.get("DUNE_UNLOCK_QUERY_ID"),
                                    os.environ.get("DUNE_API_KEY"))
    dune_b = dune_report["data"]
    print(f"[dune] {dune_report['status']}: {dune_report['detail']}"
          + (" | returned: " + ", ".join(dune_report["columns"])
             if dune_report.get("columns") else ""),
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
    # Long/short is a separate, per-symbol endpoint, so it is fetched only for the
    # names being scored and its coverage is reported rather than assumed.
    n_ls = fetch_long_short(perps_map, scored_syms)
    print(f"[perp] long/short ratio for {n_ls}/{len(scored_syms)} symbols.",
          file=__import__("sys").stderr)
    prev_oi = _prev_oi_by_symbol()
    print(f"[perp] prior-night open interest for {len(prev_oi)} symbols"
          + ("" if prev_oi else " — the 24h OI delta is null tonight, not zero"),
          file=__import__("sys").stderr)
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
            **dune_context(b, t.get("market_cap")),
            "roi_30d": None, "roi_90d": None, "survived": None,
            "perp_mult": round(pm, 3),
            "spec_hash": SPEC_HASH,
            **perp_context(sym, perps_map, t.get("market_cap"),
                           t.get("price_change_percentage_24h"), prev_oi),
            # The daily bar, for ATR and choppiness once enough of them exist.
            "high_24h": t.get("high_24h"),
            "low_24h": t.get("low_24h"),
        })
    rows.sort(key=lambda r: r["conviction"], reverse=True)

    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    # Replace today's rows rather than appending them. A blind append made a second run
    # on the same day a second copy of that day's board: 2026-08-02 was recorded nine
    # times and 2026-08-03 twice, 460 duplicate (date, symbol) pairs in all. Anything
    # reading this as a daily series then counts one day nine times and computes returns
    # between a day and itself. Prior days are untouched — this replaces a re-run, it
    # does not rewrite history.
    kept = [r for r in _read_signals_rows() if r.get("date") != today]
    fresh = [{k: r.get(k) for k in FIELDS} for r in rows[:50]]
    with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(kept + fresh)

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
        # Paper return of the published basket, chained across recorded days. Built from
        # signals.csv rather than index.json — see _compute_performance for why.
        breadth["performance"] = _compute_performance()
        MARKET_BREADTH_JSON.write_text(json.dumps(breadth, indent=2))
        # Operational condition of the pipeline, in its own file: it is a monitoring
        # artifact rather than a market view, and pinning it to breadth would couple the
        # two.
        mon = _compute_monitor(dune_report)
        if mon:
            MONITOR_JSON.write_text(json.dumps(mon, indent=2))
            counts = {}
            for c in mon["health"]:
                counts[c["status"]] = counts.get(c["status"], 0) + 1
            print("[monitor] " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                  + f"  (observations={mon['observations']})")
            for c in mon["health"]:
                if c["status"] in ("fail", "warn"):
                    print(f"  {c['status'].upper()}: {c['name']} — {c['detail']}")

        pf = breadth["performance"]
        if pf.get("legs"):
            print(f"[perf] {pf['legs']} leg(s), basket {pf['book_total']:+.2f}%"
                  + (f", {pf['benchmark']} {pf['benchmark_total']:+.2f}%"
                     if pf["benchmark_available"] else ", benchmark missing")
                  + (f", equal-weight {pf['equal_weight_total']:+.2f}%"
                     if pf.get("equal_weight_total") is not None else "")
                  + ("" if pf["renderable"] else
                     f" — below the {pf['min_days']}-day render threshold"))
            if pf.get("duplicates_collapsed"):
                print(f"[perf] collapsed {pf['duplicates_collapsed']} duplicate "
                      f"(date, symbol) rows — the workflow ran more than once on some days")
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
