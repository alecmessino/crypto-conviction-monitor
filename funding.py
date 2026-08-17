#!/usr/bin/env python3
"""Perpetual funding: ingestion, interval normalisation, regime, score modifier.

Why this is a module and not a few more lines in nightly.py
-----------------------------------------------------------
``nightly.py`` already fetched funding — one venue (Bybit), one hardcoded assumption
(8-hour settlement), one threshold pair inside ``lavl_perp_mult``. That was enough while
funding was a single column. It stops being enough the moment funding is read across
venues that settle on different clocks, because the annualisation constant is no longer
a constant and the raw rates are no longer comparable to each other.

The specific defect this module exists to remove: ``funding_ann_pct`` multiplied every
rate by ``3 * 365``. Bybit settles every 8 hours, so that was right. Hyperliquid settles
every hour. Feeding a Hyperliquid rate through the same constant understates its carry
by a factor of eight, and the two numbers sit in the same column looking equally
authoritative. An interval is not a detail about a feed, it is part of the unit, and a
rate recorded without one is not a measurement.

What is a claim and what is not
-------------------------------
Every function here returns ``None`` rather than a neutral-looking number when the
inputs do not support a reading. A funding APR of 0.0 means the market is flat; a
missing feed means nothing is known, and the two must never collapse. The same rule
governs the regime labels and the score modifier: an unconfirmed condition yields the
neutral 1.0, never a partial adjustment, because a multiplier is a claim about an asset
and applying one on evidence that was not gathered is a fabrication with a decimal point
on it.

Scoring: this module is the one place derivatives touch the score
-----------------------------------------------------------------
``regime_modifier`` is called from ``nightly.lavl_perp_mult``, which is a captured
SPEC_FUNCTION. Editing the thresholds in this file therefore moves the specification
hash and starts a new track record segment — that is intended and is the mechanism by
which the change is visible. Everything else here (venue spread, consolidated APR, the
carry screen, the position parser) is observational and reaches no score.

Standard library only, matching nightly.py: this runs in CI with no install step.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request

# ---------------------------------------------------------------------------
# regime classification
# ---------------------------------------------------------------------------
# Boundaries in annualised percent. The two intermediate bands are not in the original
# specification, which named only >+40, 0..+12 and <-15 and left two gaps: +12 to +40,
# and -15 to 0. Those gaps are real market states, and folding them into NEUTRAL would
# have labelled a 35% APR carry "healthy trend growth" — a reading a desk would act on,
# and wrong. They are given their own names instead, and neither carries a modifier.
REGIME_OVERHEATED = 40.0      # above: crowded longs, liquidation-cascade risk
REGIME_ELEVATED = 12.0        # 12..40: hot but not yet a cascade setup
REGIME_NEUTRAL_FLOOR = 0.0    # 0..12: longs paying a normal premium for leverage
REGIME_SQUEEZE = -15.0        # below: shorts paying longs, squeeze asymmetry
# -15..0 is MILD_INVERSION: shorts are paying, but not enough to be an edge.

# --- score modifier -------------------------------------------------------
# These are the numbers that move the specification hash.
#
# The envelope, unchanged from the original matrix: the modifier never leaves
# [0.85, 1.15] whatever the inputs do.
MOD_MAX_PENALTY = 0.85
MOD_MAX_BOOST = 1.15

# Everything inside that envelope is continuous. The first version of this was a step
# function — exactly 0.85 above +40% APR, exactly 1.0 below — which has two defects that
# no choice of threshold fixes. An asset at 39.9% and one at 40.1% are materially
# identical and were scored 15% apart; an asset at 40.1% and one at 400% are not remotely
# identical and were scored the same. This is the failure score() itself was rewritten to
# remove ("the additive model saturated momentum at a hard clamp — PUMP and HYPE collided
# at c_momentum=20"), and the fix here is the one that worked there: a soft curve with no
# clamp, so the board stays rankable inside the band.
#
# Severity is tanh over the distance past the neutral band, scaled so that a named
# threshold lands on a named severity. The scales are derived from those anchors at import
# rather than tuned by hand, so moving a threshold moves the curve coherently instead of
# requiring a second constant to be re-fitted.
MOD_SQUEEZE_SATURATION = -40.0    # the APR the original matrix called the boost cap
MOD_HOT_ANCHOR = 0.50   # severity at REGIME_OVERHEATED (+40% APR)
MOD_COLD_ANCHOR = 0.85  # severity at MOD_SQUEEZE_SATURATION (-40% APR)
#
# The two sides are deliberately not symmetric, and the asymmetry is the empirical claim
# this file makes most confidently: positive funding is the *normal* state of a perpetual
# market. Longs pay shorts most of the time on every major venue — that structural bias is
# the entire reason cash-and-carry is a standard trade. So +40% APR is elevated but
# unremarkable, while -40% APR is rare and much more informative. The hot side therefore
# reaches half severity at its anchor and approaches the floor only for genuinely extreme
# carry (~100%+ APR); the cold side is most of the way there by -40%.

# Confirmation. The two legs are not the same kind of evidence, and treating them
# identically was the mistake in the first version.
#
# Price extension is ADDITIVE evidence on the hot side. Funding above the neutral band
# already establishes that leverage is being paid for; price extension establishes that
# the position is also crowded into a move that has somewhere to fall. Absent the second
# leg the first still stands, so the penalty is applied at reduced weight rather than
# withheld — withholding it entirely, as the first version did, threw away an observation
# that was actually made.
MOD_OVERHEATED_PRICE_CHG = 10.0   # 24h percent at which price confirmation is full
MOD_UNCONFIRMED_WEIGHT = 0.50     # weight on funding evidence standing alone
#
# RSI is DISCRIMINATING evidence on the cold side, which is a different thing. Deeply
# negative funding has two opposite readings — shorts trapped under a floor, or shorts
# correctly positioned in a market that is still falling — and RSI is what separates them.
# Without it the *sign* of the correct adjustment is unknown, not merely its size, so
# there is no reduced-weight version to fall back on and the boost is withheld outright.
# Half a boost on a falling knife is worse than none.
MOD_SQUEEZE_RSI = 45.0        # below this: no boost at all
MOD_SQUEEZE_RSI_FULL = 60.0   # at or above this: full boost


def _atanh_scale(span: float, anchor_severity: float) -> float:
    """The tanh scale that puts ``anchor_severity`` exactly at ``span`` from the origin."""
    return abs(span) / math.atanh(anchor_severity)


MOD_HOT_SCALE = _atanh_scale(REGIME_OVERHEATED - REGIME_ELEVATED, MOD_HOT_ANCHOR)
MOD_COLD_SCALE = _atanh_scale(REGIME_SQUEEZE - MOD_SQUEEZE_SATURATION, MOD_COLD_ANCHOR)


def _ramp(value, lo: float, hi: float) -> float:
    """Linear 0->1 between two bounds, clamped. Kept linear rather than smoothed: the
    numbers on this board are meant to be checkable by hand."""
    if hi == lo:
        return 1.0 if value >= hi else 0.0
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))

REGIMES = ("OVERHEATED_LONG", "ELEVATED", "NEUTRAL", "MILD_INVERSION",
           "SHORT_SQUEEZE_RISK")


def annualize(rate, interval_hours) -> float | None:
    """A per-interval funding rate as an annualised percentage.

    ``rate`` is the decimal fraction charged each settlement (0.0001 = 1 basis point),
    ``interval_hours`` how often that settlement happens. The identity is

        APR% = rate * (24 / interval_hours) * 365 * 100

    and the middle term is the whole reason this function takes two arguments. Bybit at
    8h and Hyperliquid at 1h can quote the same 0.0001 and mean 10.95% and 87.6%.

    Returns None for a missing rate or a non-positive interval. A zero or negative
    interval is not a slow clock, it is a malformed feed, and defaulting it to 8 would
    silently attribute someone else's settlement schedule to the venue.
    """
    if rate is None or interval_hours is None:
        return None
    try:
        r, h = float(rate), float(interval_hours)
    except (TypeError, ValueError):
        return None
    if h <= 0:
        return None
    return round(r * (24.0 / h) * 365.0 * 100.0, 4)


def position_apr(contracts, mark_price, accumulated_usd, period_hours,
                 side: str = "LONG") -> dict | None:
    """A position's realised funding, as an APR, from the dollar figure an exchange UI shows.

    Coinbase Advanced (and most retail perp UIs) report accumulated funding as a running
    dollar total against an open position, never as a rate. That number is unusable for
    comparison — it scales with position size and with how long the position has been
    open, so $40 on one screen and $12 on another say nothing about which market is
    paying more. Dividing back through notional and time recovers the rate:

        hourly rate = (accumulated_usd / period_hours) / (contracts * mark_price)
        APR%        = hourly rate * 8760 * 100

    Sign convention, which is where this is easy to get wrong. The exchange reports the
    cash flow, not the market's rate. A negative accumulated dollar figure on a LONG
    means the long *received* funding, which happens when the market rate is negative.
    On a SHORT the same negative figure means the short paid, and the market rate was
    positive. So the returned ``funding_apr`` is always the market rate — comparable to
    every other APR on the board — while ``position_apr`` keeps the sign of the holder's
    own cash flow, positive meaning they were paid.

    Returns None if notional or period is zero: there is no rate to recover from a
    position with no size or no elapsed time, and dividing anyway produces an infinity
    that renders as a very confident number.
    """
    try:
        n, px, usd, hrs = (float(contracts), float(mark_price),
                           float(accumulated_usd), float(period_hours))
    except (TypeError, ValueError):
        return None
    notional = n * px
    if notional <= 0 or hrs <= 0:
        return None

    # The holder's own cash flow, per hour, as a fraction of notional. Negative in the
    # exchange's convention means "charged to the account", so it is flipped here: a
    # positive position_apr means yield received.
    holder_hourly = -(usd / hrs) / notional
    holder_apr = holder_hourly * 8760.0 * 100.0

    # The market rate. A long receiving funding implies a negative market rate; a short
    # receiving funding implies a positive one.
    direction = 1.0 if str(side).upper() == "LONG" else -1.0
    market_apr = -holder_apr * direction

    return {
        "notional_usd": round(notional, 2),
        "hourly_rate_pct": round(holder_hourly * -direction * 100.0, 8),
        "funding_apr": round(market_apr, 4),
        "position_apr": round(holder_apr, 4),
        "position_pnl_usd": round(-usd, 2),
        "side": str(side).upper(),
        "regime": classify_regime(market_apr),
    }


def classify_regime(funding_apr) -> str | None:
    """The five bands, or None when there is no funding to classify.

    None is not a sixth regime and must not render as one. Most of the board is
    spot-only; a token with no perpetual market has no funding regime, and printing
    NEUTRAL for it would claim a market exists.
    """
    if funding_apr is None:
        return None
    try:
        apr = float(funding_apr)
    except (TypeError, ValueError):
        return None
    if apr > REGIME_OVERHEATED:
        return "OVERHEATED_LONG"
    if apr > REGIME_ELEVATED:
        return "ELEVATED"
    if apr >= REGIME_NEUTRAL_FLOOR:
        return "NEUTRAL"
    if apr >= REGIME_SQUEEZE:
        return "MILD_INVERSION"
    return "SHORT_SQUEEZE_RISK"


def funding_severity(funding_apr) -> float:
    """How far past the neutral band the carry sits, as a signed 0..1 magnitude.

    Negative for hot funding (a penalty direction), positive for inverted funding (a
    boost direction), and exactly 0.0 anywhere inside NEUTRAL or MILD_INVERSION. Smooth
    everywhere, including across the band boundaries, so no asset is scored differently
    from a materially identical one because it fell on the other side of a round number.

    Unbounded input, bounded output: 400% APR and 40% APR produce different severities
    that both stay inside the envelope, which a clamp cannot do.
    """
    if funding_apr is None:
        return 0.0
    try:
        apr = float(funding_apr)
    except (TypeError, ValueError):
        return 0.0
    if apr > REGIME_ELEVATED:
        return -math.tanh((apr - REGIME_ELEVATED) / MOD_HOT_SCALE)
    if apr < REGIME_SQUEEZE:
        return math.tanh((REGIME_SQUEEZE - apr) / MOD_COLD_SCALE)
    return 0.0


def regime_modifier(funding_apr, price_chg_24h=None, rsi7=None) -> tuple[float, str]:
    """The conviction multiplier, and the reason it is what it is.

    Returns ``(multiplier, reason)``. The reason travels with the number because a bare
    0.87 on a dashboard is unreadable — it does not say whether the asset was marked
    down for crowding or whether the feed was simply absent, and those are opposite
    facts about an asset.

    The shape is ``1 + available_adjustment x severity x confirmation``:

      severity      how far past the neutral band the carry sits, 0..1, smooth. See
                    :func:`funding_severity`.
      confirmation  how much of the second leg was actually observed, 0..1.

    Both terms are continuous, so the modifier is continuous in every input. Nothing
    here steps, and nothing clamps until the envelope itself.

    The two sides confirm differently because the evidence is of different kinds:

      hot   price extension is additive. Funding above the neutral band already
            establishes that leverage is being paid for. Extension establishes that the
            crowd is also sitting on a move with somewhere to fall. Without it the first
            observation still stands, so the penalty applies at MOD_UNCONFIRMED_WEIGHT
            rather than being withheld. The previous version withheld it entirely, which
            threw away an observation that had actually been made: 90% APR on flat price
            scored exactly the same as no perpetual market at all.

      cold  RSI is discriminating. Deeply negative funding reads two opposite ways —
            shorts trapped, or shorts correct in a market still falling — and RSI is
            what separates them. Absent it, the *sign* of the right adjustment is
            unknown rather than its size, so there is no reduced-weight fallback and the
            boost is withheld. On the 2026-08-15 board this is not hypothetical: INJ
            printed -51% APR at RSI 2, and the rule this replaced paid it a 15% boost
            for being in freefall.
    """
    regime = classify_regime(funding_apr)
    if regime is None:
        return 1.0, "no funding feed"
    apr = float(funding_apr)
    sev = funding_severity(apr)
    if sev == 0.0:
        return 1.0, (f"{regime.lower().replace('_', ' ')} carry at {apr_str(apr)} — "
                     f"inside the band that earns no adjustment")

    if sev < 0:
        chg = _num_or_none(price_chg_24h)
        if chg is None:
            conf, why = MOD_UNCONFIRMED_WEIGHT, (
                "no 24h price change to confirm the crowding, so the funding evidence "
                "is applied at reduced weight")
        else:
            conf = MOD_UNCONFIRMED_WEIGHT + (1.0 - MOD_UNCONFIRMED_WEIGHT) * _ramp(
                chg, 0.0, MOD_OVERHEATED_PRICE_CHG)
            why = (f"on a {chg:+.1f}% 24h move"
                   if chg > 0 else f"price not extended ({chg:+.1f}% 24h)")
        mult = 1.0 - (1.0 - MOD_MAX_PENALTY) * (-sev) * conf
        return round(mult, 4), (
            f"longs paying {apr_str(apr)}, {why} — severity {-sev:.2f}, "
            f"confirmation {conf:.2f}")

    r = _num_or_none(rsi7)
    if r is None:
        return 1.0, (f"shorts paying {apr_str(apr)}, but no 7d RSI to separate a squeeze "
                     f"from a downtrend — the boost is withheld, not reduced")
    conf = _ramp(r, MOD_SQUEEZE_RSI, MOD_SQUEEZE_RSI_FULL)
    if conf == 0.0:
        return 1.0, (f"shorts paying {apr_str(apr)} but RSI {r:.0f} is at or below "
                     f"{MOD_SQUEEZE_RSI:.0f} — downtrend, not squeeze")
    mult = 1.0 + (MOD_MAX_BOOST - 1.0) * sev * conf
    return round(mult, 4), (
        f"shorts paying {apr_str(apr)} with RSI {r:.0f} — squeeze asymmetry, "
        f"severity {sev:.2f}, confirmation {conf:.2f}")


def _num_or_none(v):
    """A float, or None for anything that is not one. Unreadable and absent are the same
    thing to a confirmation term: neither is an observation."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def apr_str(apr) -> str:
    """APR for a human-readable reason string."""
    try:
        return f"{float(apr):.0f}% APR"
    except (TypeError, ValueError):
        return "unknown APR"


# ---------------------------------------------------------------------------
# relative strength index
# ---------------------------------------------------------------------------
def rsi(closes: list, period: int = 7) -> float | None:
    """Wilder's RSI over a close series, oldest first.

    None until ``period + 1`` closes exist. A 7-period RSI computed over four closes is
    not a 7-period RSI; it would render identically and gate the squeeze boost on a
    number that means something else. The same refusal the choppiness index already
    makes in nightly.py, for the same reason.

    Wilder's smoothing rather than a simple mean, because that is what "RSI" denotes
    everywhere it is quoted — a simple-average variant would disagree with every chart
    the reader compares it against.
    """
    if not closes or len(closes) < period + 1:
        return None
    vals = []
    for c in closes:
        try:
            v = float(c)
        except (TypeError, ValueError):
            return None            # a gap is not smoothed over
        if v <= 0:
            return None
        vals.append(v)

    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        # No down moves in the window. RSI is 100 by definition, not undefined.
        return 100.0 if avg_gain > 0 else 50.0
    rs_ = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs_)), 2)


# ---------------------------------------------------------------------------
# venue ingestion
# ---------------------------------------------------------------------------
# Default settlement clock per venue, in hours. Used only when the venue does not report
# its own interval for a symbol — Binance and Bybit both do, per instrument, and the
# reported value always wins. Hyperliquid is hourly for every market and does not
# publish a per-symbol interval, so the constant is the fact rather than a fallback.
VENUE_DEFAULT_INTERVAL = {
    "binance": 8.0,
    "bybit": 8.0,
    "hyperliquid": 1.0,
    "coinbase": 1.0,
    "dydx": 1.0,       # protocol property, not reported in the response
    "kraken": 1.0,     # perps fund hourly; not reported in the response
    "gateio": 8.0,     # fallback only — Gate reports the interval per market
}

# Ranked by depth of the USDT-margined perp book. The consolidated reading prefers the
# first venue that has a market, so the headline APR comes from the deepest book rather
# than from whichever request happened to return first.
# Ranked by depth of the USDT-margined perp book, with one deliberate exception:
# binance and bybit stay at the top because they are the deepest books when reachable,
# and drop out cleanly when they are not — which on a US-hosted runner is every night.
# The venues behind them are ordered to keep the surviving set diverse rather than
# merely long: dydx is an independent DEX, kraken a US-regulated CEX, gateio the widest
# coverage and the only venue that publishes its own settlement interval per market.
VENUE_PRIORITY = ("binance", "bybit", "gateio", "kraken", "dydx",
                  "hyperliquid", "coinbase")

_UA = {"User-Agent": "conviction-monitor/1.0"}


def _get_json(url: str, headers: dict | None = None, data: bytes | None = None):
    """Single egress point, so tests can substitute one function for every venue."""
    req = urllib.request.Request(url, data=data, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec
        return json.loads(resp.read().decode())


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _report(venue: str, data: dict, status: str, detail: str) -> dict:
    """Every fetch returns the same envelope.

    Four different situations all end in an empty table — not configured, unreachable,
    reachable but returning a shape this code does not recognise, and genuinely empty —
    and a single "no data" cannot tell a reader which one they are in. This is the same
    envelope ``nightly.fetch_dune_report`` settled on after exactly that confusion cost
    a day of debugging a feed that was working.
    """
    return {"venue": venue, "data": data, "status": status, "detail": detail}


def fetch_binance_funding(symbols: set | None = None) -> dict:
    """Binance USD-M futures: premium index plus the per-symbol funding interval.

    Two calls rather than one. ``/premiumIndex`` gives the rate and mark price for every
    symbol; ``/fundingInfo`` lists only the symbols whose settlement clock differs from
    the 8-hour default. Skipping the second call was the original bug in miniature — the
    4-hour symbols would be annualised at 8 and read half as hot as they are.
    """
    try:
        rows = _get_json("https://fapi.binance.com/fapi/v1/premiumIndex")
    except Exception as e:  # noqa: BLE001
        return _report("binance", {}, "unreachable", f"premiumIndex failed ({e})")
    if not isinstance(rows, list):
        return _report("binance", {}, "unusable",
                       "premiumIndex did not return a list of symbols")

    intervals = {}
    try:
        for it in _get_json("https://fapi.binance.com/fapi/v1/fundingInfo") or []:
            h = _f(it.get("fundingIntervalHours"))
            if h:
                intervals[it.get("symbol")] = h
    except Exception as e:  # noqa: BLE001
        # Non-fatal: the default is right for the large majority of symbols. Reported
        # rather than swallowed, because it means some intervals are assumed.
        print(f"[funding] binance fundingInfo unavailable ({e}); "
              f"assuming 8h for all symbols", file=sys.stderr)

    out = {}
    for it in rows:
        sym = it.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4].upper()
        if symbols and base not in symbols:
            continue
        rate = _f(it.get("lastFundingRate"))
        if rate is None:
            continue
        hours = intervals.get(sym, VENUE_DEFAULT_INTERVAL["binance"])
        out[base] = {
            "venue": "binance", "funding_rate": rate, "interval_hours": hours,
            "funding_apr": annualize(rate, hours),
            "mark_price": _f(it.get("markPrice")), "oi_usd": None,
        }
    if not out:
        return _report("binance", out, "unusable",
                       f"{len(rows)} symbol(s) returned, none matched the filter")
    return _report("binance", out, "live",
                   f"{len(out)} market(s); {len(intervals)} non-default interval(s)")


def fetch_bybit_funding(symbols: set | None = None) -> dict:
    """Bybit V5 linear tickers, with the settlement interval from instruments-info.

    The tickers call is the one nightly.py already makes; it carries funding, mark price
    and open interest in a single request. What it does not carry is the funding
    interval, which lives on the instrument rather than the ticker — so the second call
    is what makes the rate interpretable rather than merely present.
    """
    try:
        data = _get_json("https://api.bybit.com/v5/market/tickers?category=linear")
        items = (data.get("result") or {}).get("list") or []
    except Exception as e:  # noqa: BLE001
        return _report("bybit", {}, "unreachable", f"tickers failed ({e})")

    intervals = {}
    try:
        info = _get_json("https://api.bybit.com/v5/market/instruments-info"
                         "?category=linear&limit=1000")
        for it in (info.get("result") or {}).get("list") or []:
            mins = _f(it.get("fundingInterval"))     # minutes
            if mins:
                intervals[it.get("symbol")] = mins / 60.0
    except Exception as e:  # noqa: BLE001
        print(f"[funding] bybit instruments-info unavailable ({e}); "
              f"assuming 8h for all symbols", file=sys.stderr)

    out = {}
    for it in items:
        sym = it.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4].upper()
        if symbols and base not in symbols:
            continue
        rate = _f(it.get("fundingRate"))
        if rate is None:
            continue
        hours = intervals.get(sym, VENUE_DEFAULT_INTERVAL["bybit"])
        out[base] = {
            "venue": "bybit", "funding_rate": rate, "interval_hours": hours,
            "funding_apr": annualize(rate, hours),
            "mark_price": _f(it.get("markPrice")),
            "oi_usd": _f(it.get("openInterestValue")),
        }
    if not out:
        return _report("bybit", out, "unusable",
                       f"{len(items)} ticker(s) returned, none matched the filter")
    return _report("bybit", out, "live",
                   f"{len(out)} market(s); {len(intervals)} interval(s) resolved")


def fetch_hyperliquid_funding(symbols: set | None = None) -> dict:
    """Hyperliquid ``metaAndAssetCtxs``: the hourly venue, and the reason this module exists.

    Hyperliquid settles funding every hour. Its quoted rate is therefore roughly an
    eighth of an equivalent Bybit rate for the same annualised carry, and the two are
    not comparable until both have been through ``annualize`` with their own interval.
    Recording the raw rates side by side without the interval — which is what the single
    ``funding_rate`` column did — puts two different units in one column.

    The response is ``[meta, ctxs]`` positionally aligned: ``meta["universe"][i]`` names
    the asset that ``ctxs[i]`` describes. The alignment is the only join key, so a
    length mismatch is treated as an unusable response rather than zipped to the shorter
    of the two — a silent truncation here would attribute one asset's funding to another.
    """
    try:
        payload = _get_json("https://api.hyperliquid.xyz/info",
                            headers={"Content-Type": "application/json"},
                            data=json.dumps({"type": "metaAndAssetCtxs"}).encode())
    except Exception as e:  # noqa: BLE001
        return _report("hyperliquid", {}, "unreachable", f"info call failed ({e})")

    if not (isinstance(payload, list) and len(payload) == 2):
        return _report("hyperliquid", {}, "unusable",
                       "expected [meta, ctxs]; got a different shape")
    meta, ctxs = payload
    universe = (meta or {}).get("universe") or []
    if not universe or len(universe) != len(ctxs or []):
        return _report("hyperliquid", {}, "unusable",
                       f"universe ({len(universe)}) and contexts ({len(ctxs or [])}) "
                       f"do not align; refusing to guess the pairing")

    hours = VENUE_DEFAULT_INTERVAL["hyperliquid"]
    out = {}
    for asset, ctx in zip(universe, ctxs):
        base = str((asset or {}).get("name") or "").upper()
        if not base or (symbols and base not in symbols):
            continue
        rate = _f((ctx or {}).get("funding"))
        if rate is None:
            continue
        mark = _f((ctx or {}).get("markPx"))
        oi_coins = _f((ctx or {}).get("openInterest"))
        out[base] = {
            "venue": "hyperliquid", "funding_rate": rate, "interval_hours": hours,
            "funding_apr": annualize(rate, hours), "mark_price": mark,
            # Hyperliquid reports open interest in contracts (coins), not dollars.
            "oi_usd": round(oi_coins * mark, 2) if (oi_coins and mark) else None,
        }
    if not out:
        return _report("hyperliquid", out, "unusable",
                       f"{len(universe)} market(s) returned, none matched the filter")
    return _report("hyperliquid", out, "live", f"{len(out)} market(s) at {hours:g}h")


def fetch_coinbase_funding(symbols: set | None = None) -> dict:
    """Coinbase Advanced perpetual futures, best-effort and honest about it.

    Coinbase exposes perpetual products through the brokerage market endpoint, and the
    funding fields sit under a nested ``perpetual_details`` block whose shape is not
    contractual for unauthenticated callers and differs by jurisdiction. Rather than
    hardcode a path and let a shape change read as "no perps listed", an unrecognised
    response reports ``unusable`` with what actually came back.

    This is the venue the position parser exists for. When the feed is not usable,
    ``position_apr`` recovers the same rate from the accumulated-dollar figure the
    account UI already shows, which requires no API access at all.
    """
    url = ("https://api.coinbase.com/api/v3/brokerage/market/products"
           "?product_type=FUTURE&contract_expiry_type=PERPETUAL")
    try:
        payload = _get_json(url)
    except Exception as e:  # noqa: BLE001
        return _report("coinbase", {}, "unreachable",
                       f"brokerage market products failed ({e}) — use the position "
                       f"parser instead")
    products = (payload or {}).get("products")
    if not isinstance(products, list):
        return _report("coinbase", {}, "unusable",
                       f"no product list in the response (keys: "
                       f"{sorted((payload or {}).keys())})")

    hours = VENUE_DEFAULT_INTERVAL["coinbase"]
    out, unrecognised = {}, 0
    for p in products:
        detail = ((p.get("future_product_details") or {}).get("perpetual_details")
                  or {})
        rate = _f(detail.get("funding_rate"))
        if rate is None:
            unrecognised += 1
            continue
        base = str(p.get("base_currency_id") or p.get("product_id") or "").upper()
        base = base.split("-")[0]
        if not base or (symbols and base not in symbols):
            continue
        mark = _f(p.get("price"))
        out[base] = {
            "venue": "coinbase", "funding_rate": rate, "interval_hours": hours,
            "funding_apr": annualize(rate, hours), "mark_price": mark,
            "oi_usd": _f(detail.get("open_interest")),
        }
    if not out:
        return _report("coinbase", out, "unusable",
                       f"{len(products)} product(s) returned, "
                       f"{unrecognised} without a readable funding rate")
    return _report("coinbase", out, "live", f"{len(out)} perpetual market(s)")


def fetch_dydx_funding(symbols: set | None = None) -> dict:
    """dYdX v4 indexer. One keyless call, every market, and no unit traps.

    Reachable from US datacenter IPs, which is the point: Binance answers 451 and Bybit
    403 from the runners this job executes on, and a feed that depends on two venues that
    geo-block its host is a feed with a single point of failure wearing a disguise.

    ``markets`` is an OBJECT keyed by ticker, not an array. Funding is hourly and the
    interval is NOT a field in the response — it is a property of the protocol, so it is
    a constant here rather than a parse. ``defaultFundingRate1H`` is a rate despite the
    name and must not be mistaken for one.

    FINAL_SETTLEMENT markets are excluded: they are being wound down, their funding is
    not a live reading, and including them would put a stale rate in a column that says
    it is current.
    """
    try:
        payload = _get_json("https://indexer.dydx.trade/v4/perpetualMarkets")
    except Exception as e:  # noqa: BLE001
        return _report("dydx", {}, "unreachable", f"perpetualMarkets failed ({e})")
    markets = (payload or {}).get("markets")
    if not isinstance(markets, dict):
        return _report("dydx", {}, "unusable",
                       f"expected a markets object; got {type(markets).__name__}")

    hours = VENUE_DEFAULT_INTERVAL["dydx"]
    out, skipped = {}, 0
    for rec in markets.values():
        if (rec or {}).get("status") != "ACTIVE":
            skipped += 1
            continue
        ticker = str(rec.get("ticker") or "")
        if not ticker.endswith("-USD"):
            continue
        base = ticker[:-4].upper()
        if symbols and base not in symbols:
            continue
        rate = _f(rec.get("nextFundingRate"))
        if rate is None:
            continue
        px = _f(rec.get("oraclePrice"))
        oi_base = _f(rec.get("openInterest"))
        out[base] = {
            "venue": "dydx", "funding_rate": rate, "interval_hours": hours,
            "funding_apr": annualize(rate, hours),
            # dYdX has no mark price. The oracle price is the closest thing and is
            # labelled as what it is rather than aliased into a field it is not.
            "mark_price": px,
            "oi_usd": round(oi_base * px, 2) if (oi_base and px) else None,
            "rate_basis": "predicted",
        }
    if not out:
        return _report("dydx", out, "unusable",
                       f"{len(markets)} market(s) returned, none matched the filter")
    return _report("dydx", out, "live",
                   f"{len(out)} market(s) at {hours:g}h; {skipped} non-active skipped")


def fetch_gateio_funding(symbols: set | None = None) -> dict:
    """Gate.io USDT perpetuals — the venue that publishes its own settlement interval.

    ``/contracts`` rather than ``/tickers``: the tickers endpoint carries a funding rate
    and omits the interval, which is precisely the shape that caused the original defect
    in this repo. Here the interval arrives per market, in seconds, and it genuinely
    varies — a snapshot on 2026-08-17 found 573 markets at 8h, 342 at 4h and 3 at 1h.

    That distribution is the empirical case for this whole module in one response: had
    these rates been annualised at a fixed three settlements a day, 345 of 918 markets
    would have been silently wrong, most of them by a factor of two.

    Read every run rather than cached — venues move individual markets between 8h, 4h and
    1h in response to sustained funding pressure, which is exactly why the field exists.
    """
    try:
        rows = _get_json("https://api.gateio.ws/api/v4/futures/usdt/contracts")
    except Exception as e:  # noqa: BLE001
        return _report("gateio", {}, "unreachable", f"contracts failed ({e})")
    if not isinstance(rows, list):
        return _report("gateio", {}, "unusable", "contracts did not return a list")

    out, intervals = {}, {}
    for rec in rows:
        if (rec or {}).get("in_delisting"):
            continue
        name = str(rec.get("name") or "")
        if not name.endswith("_USDT"):
            continue
        base = name[:-5].upper()
        if symbols and base not in symbols:
            continue
        rate = _f(rec.get("funding_rate"))
        secs = _f(rec.get("funding_interval"))
        if rate is None or not secs:
            continue
        hours = secs / 3600.0
        intervals[hours] = intervals.get(hours, 0) + 1
        mark = _f(rec.get("mark_price"))
        # position_size is in CONTRACTS; quanto_multiplier converts to base units.
        size = _f(rec.get("position_size"))
        mult = _f(rec.get("quanto_multiplier"))
        out[base] = {
            "venue": "gateio", "funding_rate": rate, "interval_hours": hours,
            "funding_apr": annualize(rate, hours), "mark_price": mark,
            "oi_usd": (round(abs(size) * mult * mark, 2)
                       if (size and mult and mark) else None),
            "rate_basis": "current",
        }
    if not out:
        return _report("gateio", out, "unusable",
                       f"{len(rows)} contract(s) returned, none matched the filter")
    mix = ", ".join(f"{k:g}h x{v}" for k, v in sorted(intervals.items()))
    return _report("gateio", out, "live", f"{len(out)} market(s); intervals {mix}")


# Kraken quotes funding in quote currency per contract per hour, not as a decimal rate.
# Dividing by the index price recovers the rate. Below this index price the division
# amplifies quantisation error badly enough that the result is not a reading — a
# sub-cent alt computed to -255% APR in testing, which may be real and may be an
# artefact, and a column cannot say which.
KRAKEN_MIN_INDEX_PRICE = 0.01


def fetch_kraken_funding(symbols: set | None = None) -> dict:
    """Kraken Futures. Carries the worst unit trap of any venue here.

    ``fundingRate`` is the ABSOLUTE rate — quote currency per contract per hour — not a
    decimal. Kraken's own documentation defines absolute = relative x spot price, so the
    conversion is a division by the index price. Getting this wrong is not subtle:
    PF_XBTUSD at -0.5228 with an index of 64,308 is -7.12% a year, and annualising the
    raw figure yields -457,972%.

    It is the same class of defect as the fixed 8-hour constant this module was written
    to remove — a number that means something other than what the column says — and it is
    worth naming because it would have passed every schema check. Nothing about
    ``fundingRate`` says it is denominated in dollars.
    """
    try:
        payload = _get_json("https://futures.kraken.com/derivatives/api/v3/tickers")
    except Exception as e:  # noqa: BLE001
        return _report("kraken", {}, "unreachable", f"tickers failed ({e})")
    tickers = (payload or {}).get("tickers")
    if not isinstance(tickers, list):
        return _report("kraken", {}, "unusable", "tickers did not return a list")

    hours = VENUE_DEFAULT_INTERVAL["kraken"]
    out, thin = {}, 0
    for rec in tickers:
        if (rec or {}).get("tag") != "perpetual" or rec.get("suspended"):
            continue
        pair = str(rec.get("pair") or "")          # 'XBT:USD'
        base = pair.split(":")[0].upper()
        if base == "XBT":
            base = "BTC"                            # Kraken's name for bitcoin
        if not base or (symbols and base not in symbols):
            continue
        absolute = _f(rec.get("fundingRate"))
        index = _f(rec.get("indexPrice"))
        if absolute is None or not index:
            continue
        if index < KRAKEN_MIN_INDEX_PRICE:
            thin += 1
            continue
        rate = absolute / index
        out[base] = {
            "venue": "kraken", "funding_rate": rate, "interval_hours": hours,
            "funding_apr": annualize(rate, hours), "mark_price": _f(rec.get("markPrice")),
            "oi_usd": (round(_f(rec.get("openInterest")) * index, 2)
                       if _f(rec.get("openInterest")) else None),
            "rate_basis": "current",
        }
    if not out:
        return _report("kraken", out, "unusable",
                       f"{len(tickers)} ticker(s) returned, none matched the filter")
    return _report("kraken", out, "live",
                   f"{len(out)} perpetual(s) at {hours:g}h"
                   + (f"; {thin} sub-cent index price(s) excluded" if thin else ""))


VENUE_FETCHERS = {
    "binance": fetch_binance_funding,
    "bybit": fetch_bybit_funding,
    "hyperliquid": fetch_hyperliquid_funding,
    "coinbase": fetch_coinbase_funding,
    "dydx": fetch_dydx_funding,
    "gateio": fetch_gateio_funding,
    "kraken": fetch_kraken_funding,
}


def fetch_all_venues(symbols: set | None = None,
                     venues: tuple = VENUE_PRIORITY) -> dict:
    """Every venue, each independently degradable.

    One venue being down must not cost the others. Returns
    ``{"venues": {name: report}, "reports": [...]}`` so the caller can log coverage per
    venue instead of reporting a single opaque total that hides which book went dark.
    """
    out = {}
    for name in venues:
        fetcher = VENUE_FETCHERS.get(name)
        if not fetcher:
            continue
        try:
            out[name] = fetcher(symbols)
        except Exception as e:  # noqa: BLE001
            out[name] = _report(name, {}, "unreachable", f"unhandled error: {e}")
    return {"venues": out,
            "reports": [f"{n}: {r['status']} — {r['detail']}" for n, r in out.items()]}


def consolidate(venue_reports: dict, priority: tuple = VENUE_PRIORITY) -> dict:
    """Cross-venue merge, keyed by base symbol.

    The headline ``funding_apr`` is taken from the highest-priority venue that lists the
    asset, not averaged across them. An average would be a rate no one can actually
    receive: to earn the mean of Binance and Hyperliquid you would have to hold the
    position on both, and the number would drift with which venues happened to respond.

    The dispersion is kept separately as ``apr_spread``, which is the genuinely useful
    cross-venue reading — a wide spread is a basis trade, not a data-quality problem, and
    the two look identical if only one venue is recorded.
    """
    merged: dict = {}
    for name in priority:
        rep = (venue_reports.get("venues") or {}).get(name) or {}
        for base, rec in (rep.get("data") or {}).items():
            slot = merged.setdefault(base, {"by_venue": {}})
            slot["by_venue"][name] = rec

    out = {}
    for base, slot in merged.items():
        by_venue = slot["by_venue"]
        aprs = {v: r["funding_apr"] for v, r in by_venue.items()
                if r.get("funding_apr") is not None}
        primary = next((v for v in priority if v in by_venue), None)
        if primary is None:
            continue
        prec = by_venue[primary]
        apr = prec.get("funding_apr")
        # Open interest from whichever venue reports it — Binance's premiumIndex does
        # not, and dropping OI because the deepest book omits the field would lose a
        # reading that is present.
        oi = next((by_venue[v].get("oi_usd") for v in priority
                   if v in by_venue and by_venue[v].get("oi_usd")), None)
        out[base] = {
            "funding_rate": prec.get("funding_rate"),
            "interval_hours": prec.get("interval_hours"),
            "funding_apr": apr,
            "venue": primary,
            "venues_n": len(by_venue),
            # None rather than 0.0 on a single venue: zero dispersion is a claim that
            # the venues agree, and one venue cannot agree with anything.
            "apr_spread": (round(max(aprs.values()) - min(aprs.values()), 4)
                           if len(aprs) > 1 else None),
            "mark_price": prec.get("mark_price"),
            "oi_usd": oi,
            "regime": classify_regime(apr),
            "by_venue": {v: {"funding_apr": r.get("funding_apr"),
                             "interval_hours": r.get("interval_hours"),
                             "funding_rate": r.get("funding_rate")}
                         for v, r in by_venue.items()},
        }
    return out


# ---------------------------------------------------------------------------
# cash and carry
# ---------------------------------------------------------------------------
# Round-trip execution cost as a percentage of notional. A delta-neutral carry is four
# fills — buy spot, short perp, then unwind both — so a 0.045% taker fee is 0.18% of
# notional before slippage. Defaults are Binance/Bybit taker tiers; a maker-only desk
# should pass its own.
CARRY_TAKER_FEE_PCT = 0.045
CARRY_FILLS = 4
CARRY_SLIPPAGE_PCT = 0.02       # per fill, conservative for a top-50 book
CARRY_DEFAULT_HOLD_DAYS = 30


def carry_yield(funding_apr, hold_days: float = CARRY_DEFAULT_HOLD_DAYS,
                taker_fee_pct: float = CARRY_TAKER_FEE_PCT,
                slippage_pct: float = CARRY_SLIPPAGE_PCT,
                fills: int = CARRY_FILLS) -> dict | None:
    """Net annualised yield on a delta-neutral carry, after execution drag.

    Long spot, short perp: the position collects funding and is flat on direction, so
    the gross yield is the funding APR. The drag is the round trip amortised over how
    long the position is held — and that amortisation is the number most screens omit.
    A 20% APR carry is a good trade held for a quarter and a losing one held for two
    days, because the same 0.26% of fills is charged either way.

        drag APR = fills * (fee + slippage) * (365 / hold_days)

    Returns None for a missing APR. Negative funding is not filtered out here: it is a
    real carry in the other direction (short spot, long perp), and the caller decides
    whether the borrow to do that exists.
    """
    if funding_apr is None:
        return None
    try:
        gross = float(funding_apr)
        days = float(hold_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    roundtrip_pct = fills * (float(taker_fee_pct) + float(slippage_pct))
    drag_apr = roundtrip_pct * (365.0 / days)
    return {
        "gross_apr": round(gross, 4),
        "roundtrip_cost_pct": round(roundtrip_pct, 4),
        "fee_drag_apr": round(drag_apr, 4),
        "net_apr": round(gross - drag_apr, 4),
        "hold_days": days,
        # The hold at which the trade breaks even on today's carry. Below it the fills
        # cost more than the funding pays.
        "breakeven_days": (round(roundtrip_pct * 365.0 / abs(gross), 2)
                           if gross else None),
    }


def carry_screen(consolidated: dict, hold_days: float = CARRY_DEFAULT_HOLD_DAYS,
                 min_net_apr: float = 0.0, limit: int = 25,
                 oi_floor_usd: float | None = 5e6) -> list[dict]:
    """Assets ranked by net carry, highest first.

    ``oi_floor_usd`` exists because the top of an unfiltered carry screen is reliably a
    list of markets too thin to put the trade on. A 300% APR on a book with $200k of
    open interest is not an opportunity; it is a quote. Assets whose open interest is
    unknown are kept rather than dropped — absence of a reading is not evidence of a
    thin book — but they are marked so the screen can say which is which.
    """
    rows = []
    for sym, rec in consolidated.items():
        net = carry_yield(rec.get("funding_apr"), hold_days)
        if not net or net["net_apr"] < min_net_apr:
            continue
        oi = rec.get("oi_usd")
        if oi_floor_usd and oi is not None and oi < oi_floor_usd:
            continue
        rows.append({
            "symbol": sym, "venue": rec.get("venue"),
            "funding_apr": rec.get("funding_apr"),
            "interval_hours": rec.get("interval_hours"),
            "regime": rec.get("regime"), "oi_usd": oi,
            "oi_known": oi is not None,
            "apr_spread": rec.get("apr_spread"),
            **{k: net[k] for k in ("net_apr", "fee_drag_apr", "roundtrip_cost_pct",
                                   "breakeven_days")},
        })
    rows.sort(key=lambda r: r["net_apr"], reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# the row a snapshot records
# ---------------------------------------------------------------------------
def funding_context(symbol: str, consolidated: dict, price_chg_24h=None,
                    rsi7=None) -> dict:
    """The recorded funding columns for one asset, plus the modifier it earned.

    Every field is None for an asset with no perpetual market. The majority of the board
    is spot-only and must not acquire a fabricated neutral regime because the column
    exists — but ``score_modifier`` is 1.0 rather than None, because "no adjustment" is
    a real and correct multiplier, and a null there would propagate into the score as a
    type error rather than as neutrality.
    """
    rec = consolidated.get(symbol) or {}
    apr = rec.get("funding_apr")
    mult, reason = regime_modifier(apr, price_chg_24h, rsi7)
    return {
        "funding_rate": rec.get("funding_rate"),
        "funding_apr": apr,
        "funding_interval_h": rec.get("interval_hours"),
        "funding_venue": rec.get("venue"),
        "funding_venues_n": rec.get("venues_n"),
        "funding_apr_spread": rec.get("apr_spread"),
        "funding_regime": rec.get("regime"),
        "rsi7": rsi7,
        "score_modifier": mult,
        "modifier_reason": reason,
    }
