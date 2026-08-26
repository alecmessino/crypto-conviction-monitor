#!/usr/bin/env python3
"""Quantitative readings derived from what this repository already recorded.

Why these live here and not in nightly.py
-----------------------------------------
``nightly.py`` is the ingestion and persistence layer: it fetches, scores, writes and
gates. Everything in this file is a *pure function over recorded data* — no network, no
files, no clock. That is the whole design constraint and it is what makes these
testable at all. ``choppiness()`` set the precedent in nightly.py and outgrew it: once
ADX, a correlation matrix, drawdown archaeology and an impact model all want the same
recorded bar series, they belong together and they belong somewhere a test can call
them with a list.

The other half of the reason is honesty about provenance. Every reading below is
computed from bars *this pipeline recorded itself*, night by night, from
``high_24h``/``low_24h``/``price`` in the markets payload. None of it is back-filled.
CoinGecko's ``/ohlc`` endpoint has no daily granularity (30-minute at 1 day, 4-hour to
30 days, 4-day beyond), so a "14-day ADX" taken from it would be a 14-bar ADX over 56
hours wearing the wrong label — the same trap ``choppiness()`` documents, and the same
answer: compute it from bars whose spacing is known, and report how many exist.

The universal rule, inherited from funding.py
---------------------------------------------
``None`` means *not observed* and is never a zero, a neutral, or a midpoint. An ADX
that has 19 of the 29 bars it needs returns None and the count; it does not return a
14-period ADX computed over 19. A correlation over four overlapping nights returns
None; it does not return a number that happens to be computable. Every function here
that can decline to answer, does, and every one of them reports what it would need.

Nothing in this module reaches score(). The conviction number is produced by
``nightly.score`` from the markets payload, the funding modifier and the emission
modifier, and by nothing here. These readings drive the terminal's regime column, its
correlation and rotation panels, and the position sizer's caps — which shape a book
after the ranking exists, and are a different decision from the ranking itself.
"""
from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# ADX / directional movement (Wilder)
# ---------------------------------------------------------------------------
ADX_PERIOD = 14
# Wilder's ADX is a smoothed average of a smoothed ratio, so it consumes its period
# twice: `period` bars to establish the directional indicators, and `period` more to
# average the DX into an ADX. Plus one bar to have a previous close at all. This is why
# a 14-period ADX needs 29 recorded nights and not 15 — a distinction that a library
# call hides and that decides whether this column is real or is a shorter reading
# wearing a longer name.
ADX_MIN_BARS = 2 * ADX_PERIOD + 1
# Wilder's own boundaries, unchanged. 25 is the conventional line between a trending market
# and a directionless one; 20 is where a trend that was there is no longer.
ADX_TRENDING = 25.0
ADX_WEAK = 20.0


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder's smoothing: seed with a sum, then decay by 1/period.

    Not an EMA and not an SMA. Wilder's own recursion is
    ``S_t = S_{t-1} - S_{t-1}/period + v_t``, which is what every published ADX is
    computed with; substituting a standard EMA gives numbers close enough to look
    right and different enough to disagree with any other terminal on the desk.
    """
    if len(values) < period:
        return []
    out = [sum(values[:period])]
    for v in values[period:]:
        out.append(out[-1] - out[-1] / period + v)
    return out


def adx(bars: list, period: int = ADX_PERIOD) -> dict:
    """Average Directional Index and the two directional indicators.

    ``bars`` is ``[{"high":, "low":, "close":}, ...]`` oldest first — the same shape
    ``nightly.choppiness`` consumes, so both read one accumulated series.

    Returns ``{"adx":, "plus_di":, "minus_di":, "bars":, "needed":, "regime":}`` with
    the three numbers None until there are enough bars. ``bars`` and ``needed`` travel
    with the reading so the terminal can render "accumulating (19/29)" rather than an
    empty cell, which reads as a broken column and gets ignored rather than waited for.

    ADX measures trend *strength* without direction; +DI over -DI carries the sign. A
    high ADX in a downtrend is a strong downtrend, and a screen that reads ADX alone as
    bullish is reading half the indicator.
    """
    n = len(bars)
    need = 2 * period + 1
    blank = {"adx": None, "plus_di": None, "minus_di": None,
             "bars": n, "needed": need, "regime": None}
    if n < need:
        return blank
    trs, plus_dm, minus_dm = [], [], []
    for prev, cur in zip(bars, bars[1:]):
        h, lo, pc = cur.get("high"), cur.get("low"), prev.get("close")
        ph, pl = prev.get("high"), prev.get("low")
        if None in (h, lo, pc, ph, pl):
            return blank
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
        up, down = h - ph, pl - lo
        # Only the larger of the two directional moves counts, and only if positive.
        # An "inside day" contributes nothing to either side rather than a small amount
        # to both, which is the difference between DI that oscillates and DI that drifts.
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    str_ = _wilder_smooth(trs, period)
    sp = _wilder_smooth(plus_dm, period)
    sm = _wilder_smooth(minus_dm, period)
    if not str_ or len(sp) != len(str_) or len(sm) != len(str_):
        return blank
    dxs = []
    for tr, p, m in zip(str_, sp, sm):
        if tr <= 0:
            continue
        pdi = 100.0 * p / tr
        mdi = 100.0 * m / tr
        denom = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    if len(dxs) < period:
        return blank
    # The ADX is the Wilder average of DX, which for the first value is a plain mean.
    adx_val = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    tr_last, p_last, m_last = str_[-1], sp[-1], sm[-1]
    pdi = round(100.0 * p_last / tr_last, 2) if tr_last > 0 else None
    mdi = round(100.0 * m_last / tr_last, 2) if tr_last > 0 else None
    val = round(adx_val, 2)
    return {"adx": val, "plus_di": pdi, "minus_di": mdi,
            "bars": n, "needed": need, "regime": adx_regime(val, pdi, mdi)}


def adx_regime(adx_val, plus_di=None, minus_di=None) -> str | None:
    """The label, with direction folded in. None when ADX is not known."""
    if adx_val is None:
        return None
    if adx_val < ADX_WEAK:
        return "NO TREND"
    if adx_val < ADX_TRENDING:
        return "EMERGING"
    if plus_di is None or minus_di is None:
        return "TRENDING"
    return "TRENDING UP" if plus_di >= minus_di else "TRENDING DOWN"


# ---------------------------------------------------------------------------
# strategy selection
# ---------------------------------------------------------------------------
# The Choppiness Index and ADX answer overlapping but not identical questions: CHOP
# measures how much of the range was travelled, ADX how persistently one side won. They
# disagree most usefully at the edges — a market can be low-CHOP (directional bars) and
# low-ADX (no net progress), which is a whipsaw and is the single worst state to run
# either a trend or a grid in. That state has its own label rather than being resolved
# to whichever indicator was read first.
def strategy_for(chop_reg: str | None, adx_reading: dict | None) -> dict:
    """Which book an asset's current regime is suited to, and how confident that is.

    Returns ``{"strategy":, "basis":, "confidence":}``. ``confidence`` is "confirmed"
    only when both indicators are present and agree; "partial" on one; and the strategy
    is None when neither has enough history, because "no data" and "stand aside" are
    different instructions and only one of them is a view.
    """
    a = (adx_reading or {}).get("adx")
    a_reg = (adx_reading or {}).get("regime")
    if chop_reg is None and a is None:
        return {"strategy": None, "basis": "neither index has enough recorded bars yet",
                "confidence": None}
    trending = (chop_reg == "TRENDING") or (a is not None and a >= ADX_TRENDING)
    ranging = (chop_reg == "RANGE-BOUND") or (a is not None and a < ADX_WEAK)
    both = chop_reg is not None and a is not None
    if trending and ranging:
        # CHOP says one thing, ADX the other. Named rather than arbitrated.
        return {"strategy": "STAND ASIDE",
                "basis": f"CHOP says {chop_reg} and ADX {a:.0f} disagrees — whipsaw",
                "confidence": "conflicted"}
    if trending:
        return {"strategy": "TREND / ALPHA BASKET",
                "basis": (f"ADX {a:.0f} ({a_reg})" if a is not None else f"CHOP {chop_reg}"),
                "confidence": "confirmed" if both else "partial"}
    if ranging:
        return {"strategy": "GRID / RANGE HARVEST",
                "basis": (f"ADX {a:.0f} below {ADX_WEAK:.0f}" if a is not None
                          else f"CHOP {chop_reg}"),
                "confidence": "confirmed" if both else "partial"}
    return {"strategy": "COMPRESSING",
            "basis": (f"ADX {a:.0f} between {ADX_WEAK:.0f} and {ADX_TRENDING:.0f}"
                      if a is not None else f"CHOP {chop_reg}"),
            "confidence": "confirmed" if both else "partial"}


# ---------------------------------------------------------------------------
# correlation, beta, and how many bets a book actually is
# ---------------------------------------------------------------------------
CORR_MIN_OBS = 8          # below this a Pearson r is a coincidence with a decimal point
CORR_WINDOW = 30          # trailing nights considered, when that many exist
CORR_CLUSTER = 0.90       # at or above, two names are one bet for risk purposes


def log_returns(closes: list) -> list[float]:
    """Log returns from a close series. Skips any pair a return cannot be taken over.

    Log rather than simple because these are summed and compared across assets of very
    different volatility, and simple returns are not additive across time. The
    difference is small at daily horizons and is not zero.
    """
    out = []
    for a, b in zip(closes, closes[1:]):
        try:
            a, b = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson r over the overlapping prefix. None below CORR_MIN_OBS or on zero variance.

    Zero variance returns None rather than 0.0. A constant series is not uncorrelated
    with anything — it is a series a correlation is undefined over, most often because
    a stale price was recorded, and reporting that as "independent" would make the
    stalest name in the book look like its best diversifier.
    """
    n = min(len(a), len(b))
    if n < CORR_MIN_OBS:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return round(cov / math.sqrt(va * vb), 4)


def beta(asset_returns: list[float], bench_returns: list[float]) -> float | None:
    """Beta to the benchmark: cov(a, b) / var(b). None when the benchmark is flat."""
    n = min(len(asset_returns), len(bench_returns))
    if n < CORR_MIN_OBS:
        return None
    a, b = asset_returns[-n:], bench_returns[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    vb = sum((y - mb) ** 2 for y in b)
    if vb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return round(cov / vb, 4)


def correlation_report(closes_by_symbol: dict, benchmark: str = "BTC",
                       window: int = CORR_WINDOW) -> dict:
    """Pairwise correlation, beta to the benchmark, and the book's effective breadth.

    ``closes_by_symbol`` is ``{SYMBOL: [close, ...]}`` oldest first.

    The number this exists to produce is ``effective_n``. A fifteen-name book whose
    members are 0.95 correlated to each other is not fifteen positions, and every risk
    statistic computed as though it were is wrong in the same direction. The standard
    identity is

        N_eff = N^2 / sum_ij(rho_ij)

    which returns N for a perfectly uncorrelated book and 1 for a perfectly correlated
    one, and is the honest denominator for anything per-position. It is reported
    alongside the raw mean correlation rather than instead of it, because a mean of 0.6
    with one 0.99 pair and a mean of 0.6 spread evenly are different books.

    Pairs whose overlap is too short are excluded from the mean rather than imputed to
    it. The count of pairs actually measured is reported, so a mean over three pairs
    cannot be mistaken for a mean over a hundred.
    """
    rets = {s: log_returns(list(c)[-(window + 1):])
            for s, c in closes_by_symbol.items()}
    rets = {s: r for s, r in rets.items() if len(r) >= CORR_MIN_OBS}
    syms = sorted(rets)
    bench = rets.get(benchmark)
    matrix, betas = {}, {}
    for s in syms:
        if bench is not None and s != benchmark:
            betas[s] = beta(rets[s], bench)
        row = {}
        for o in syms:
            row[o] = 1.0 if o == s else pearson(rets[s], rets[o])
        matrix[s] = row

    vals, clusters = [], {}
    for i, s in enumerate(syms):
        hot = []
        for o in syms[i + 1:]:
            r = matrix[s][o]
            if r is None:
                continue
            vals.append(r)
        for o in syms:
            if o == s:
                continue
            r = matrix[s][o]
            if r is not None and r >= CORR_CLUSTER:
                hot.append(o)
        if hot:
            clusters[s] = sorted(hot)

    n = len(syms)
    mean_r = round(sum(vals) / len(vals), 4) if vals else None
    eff_n = None
    if n >= 2 and mean_r is not None:
        # sum_ij(rho) = N (the diagonal) + 2 * sum over the measured upper triangle,
        # with the mean standing in for any pair too short to measure. Stated rather
        # than silently substituted: with every pair measured the two are identical.
        total = n + 2 * mean_r * (n * (n - 1) / 2)
        eff_n = round((n * n) / total, 2) if total > 0 else None
    return {
        "symbols": syms, "n": n, "window": window,
        "min_obs": CORR_MIN_OBS, "observations": {s: len(rets[s]) for s in syms},
        "matrix": matrix, "beta_to_" + benchmark.lower(): betas,
        "mean_correlation": mean_r, "pairs_measured": len(vals),
        "effective_n": eff_n,
        "cluster_threshold": CORR_CLUSTER, "clusters": clusters,
        "benchmark": benchmark,
    }


# ---------------------------------------------------------------------------
# trending momentum divergence
# ---------------------------------------------------------------------------
# The two rankings measure different populations. Search rank counts people who do not
# own the asset yet; conviction counts liquidity, depth and relative strength. Where
# they agree there is nothing to say. Where they disagree, the direction of the
# disagreement is the whole reading, and it has two very different signs.
TMD_CROWDED_GAP = 20      # trending far above conviction: attention without structure
TMD_QUIET_GAP = 20        # conviction far above trending: structure without attention


def trending_divergence(trending: dict, conviction_by_symbol: dict) -> dict:
    """Rank the board by conviction, compare against the search ranking, name the gap.

    The comparison is made *within the overlap* — the names that are both trending and
    scored — and this is the whole correctness of the thing.

    The obvious version, taking each side's percentile against its own population, is
    wrong and looks right. The trending list is a top-15 SLICE, so its percentiles span
    6.7% to 100% densely; the conviction ranking spans the whole 50-name board. A
    trending coin at position 14 scores a 93rd trending percentile against a conviction
    percentile that can be anything, and the arithmetic tips almost every name into one
    label. Run against a live 234-name board that produced eleven QUIET_ACCUMULATIONs
    out of fifteen — a classifier that puts three quarters of its input in one bucket is
    not classifying.

    Ranking both sides *among the overlapping names only* puts them on one scale by
    construction. ``conviction_pct`` is still reported against the whole board, because
    "this is the 4th most searched coin and the model ranks it 88th of 234" is worth
    seeing — it is just not what the label is computed from.

    ``FOMO_CROWDED`` is a warning and ``QUIET_ACCUMULATION`` is a candidate; neither is
    an instruction. A crowded name is not automatically a short and a quiet one is not
    automatically early — the label says the two rankings disagree and by how much, and
    what to do about that is a decision made with the rest of the board in view.
    """
    coins = (trending or {}).get("coins") or {}
    conv = {s: v for s, v in (conviction_by_symbol or {}).items() if v is not None}
    if not coins or not conv:
        return {"assets": [], "n_trending": len(coins), "n_scored": len(conv),
                "detail": ("no overlap to compute: "
                           + ("no trending feed" if not coins else "no scored board"))}
    ranked = sorted(conv.items(), key=lambda kv: -kv[1])
    conv_pct = {s: 100.0 * (i + 1) / len(ranked) for i, (s, _) in enumerate(ranked)}
    n_tr = len(coins)

    # The overlap, ranked twice: once the way the crowd ordered it, once the way the
    # model does. Both percentiles are then over the same n, so their difference is a
    # disagreement about ordering rather than an artefact of two population sizes.
    overlap = [s for s in coins if s in conv]
    n_ov = len(overlap)
    by_trend = sorted(overlap, key=lambda s: coins[s]["rank"])
    by_conv = sorted(overlap, key=lambda s: -conv[s])
    ov_trend_pct = {s: 100.0 * (i + 1) / n_ov for i, s in enumerate(by_trend)} if n_ov else {}
    ov_conv_pct = {s: 100.0 * (i + 1) / n_ov for i, s in enumerate(by_conv)} if n_ov else {}

    out = []
    for sym, rec in coins.items():
        tr_pct = 100.0 * rec["rank"] / n_tr
        c_pct = conv_pct.get(sym)
        if c_pct is None:
            # Trending but not on the board at all. That is a real and common state —
            # most trending coins are below the universe's market-cap cut — and it is
            # reported as such rather than dropped, because "the crowd is looking at
            # something this model does not rank" is itself the answer to a question.
            out.append({"symbol": sym, "name": rec.get("name"),
                        "trending_rank": rec["rank"], "trending_pct": round(tr_pct, 1),
                        "conviction": None, "conviction_pct": None,
                        # Carried as None rather than omitted, so every row in this list
                        # has the same shape and a consumer never has to test for the
                        # presence of a key to decide what a row means.
                        "overlap_trend_pct": None, "overlap_conv_pct": None,
                        "divergence": None, "label": "UNRANKED",
                        "mcap": rec.get("mcap"), "chg24h": rec.get("chg24h")})
            continue
        # Positive = the crowd ranks it higher than the model does, among the names
        # both of them rank. A single-name overlap has no ordering to disagree about,
        # so the divergence is None rather than 0.0 — which would read as "the two
        # rankings agree" on evidence that cannot support either answer.
        div = (round(ov_conv_pct[sym] - ov_trend_pct[sym], 1) if n_ov >= 2 else None)
        label = ("ALIGNED" if div is None
                 else "FOMO_CROWDED" if div >= TMD_CROWDED_GAP
                 else "QUIET_ACCUMULATION" if div <= -TMD_QUIET_GAP
                 else "ALIGNED")
        out.append({"symbol": sym, "name": rec.get("name"),
                    "trending_rank": rec["rank"], "trending_pct": round(tr_pct, 1),
                    "conviction": conv.get(sym), "conviction_pct": round(c_pct, 1),
                    # The two percentiles the label was actually computed from, published
                    # so the number on screen can be checked rather than trusted.
                    "overlap_trend_pct": (round(ov_trend_pct[sym], 1) if n_ov >= 2 else None),
                    "overlap_conv_pct": (round(ov_conv_pct[sym], 1) if n_ov >= 2 else None),
                    "divergence": div, "label": label,
                    "mcap": rec.get("mcap"), "chg24h": rec.get("chg24h")})
    out.sort(key=lambda r: (r["divergence"] is None, -(r["divergence"] or 0)))
    # The other direction: names the model backs that nobody is searching for. Only
    # meaningful over the scored board, so it is computed here rather than inferred
    # from the list above, which is keyed on what is trending.
    quiet = [{"symbol": s, "conviction": conv[s],
              "conviction_pct": round(conv_pct[s], 1)}
             for s, _ in ranked[:10] if s not in coins]
    return {"assets": out, "n_trending": n_tr, "n_scored": len(conv),
            "n_overlap": n_ov,
            "crowded_gap": TMD_CROWDED_GAP, "quiet_gap": TMD_QUIET_GAP,
            "backed_but_unsearched": quiet,
            "basis": ("ranks compared within the overlapping names only, so both "
                      "percentiles are over the same population"),
            "detail": (f"{len(coins)} trending, {len(conv)} scored, {n_ov} in both")}


# ---------------------------------------------------------------------------
# sector rotation
# ---------------------------------------------------------------------------
def sector_rotation(today: dict, history: list, market_chg_24h=None,
                    lookback_days: int = 7) -> dict:
    """Sector relative strength now, and the multi-day flow once history exists.

    ``today`` is ``{category_id: {...}}`` from ``coingecko.fetch_categories``.
    ``history`` is the recorded rows of ``ledger/sectors.csv``, oldest first.

    Relative strength is the sector's 24h change *minus the whole market's*. The
    absolute number answers "did this go up", which on most days is a question about
    Bitcoin; the relative one answers "did capital move here", which is the question a
    rotation matrix is for. When the market change is unavailable the relative column is
    None and the absolute one still renders — a sector matrix with no benchmark is
    degraded, not wrong.

    The ``lookback_days`` flow is computed only from rows actually on disk, and
    ``flow_days`` reports how many that was. On night one every multi-day column is None
    and the panel says "accumulating", which is the truth; a 7d flow computed from one
    night is a 24h flow with a longer label on it.
    """
    by_cat: dict[str, list] = {}
    for row in history or []:
        cid = row.get("category_id") or row.get("id")
        if cid:
            by_cat.setdefault(cid, []).append(row)
    for seq in by_cat.values():
        seq.sort(key=lambda r: r.get("date") or "")

    rows = []
    for cid, rec in (today or {}).items():
        chg = rec.get("chg24h")
        rs = round(chg - market_chg_24h, 2) if (chg is not None
                                                and market_chg_24h is not None) else None
        prior = by_cat.get(cid) or []
        flow_pct = None
        flow_days = 0
        if prior:
            # The oldest row still inside the window, not simply the oldest row: a gap
            # in the ledger must shorten the window rather than silently lengthen it.
            window = prior[-lookback_days:]
            base = None
            for r in window:
                b = r.get("mcap")
                try:
                    b = float(b)
                except (TypeError, ValueError):
                    b = None
                if b and b > 0:
                    base = b
                    break
            flow_days = len(window)
            if base and rec.get("mcap"):
                flow_pct = round((rec["mcap"] / base - 1.0) * 100, 2)
        rows.append({
            "id": cid, "name": rec.get("name"), "mcap": rec.get("mcap"),
            "volume_24h": rec.get("volume_24h"), "turnover": rec.get("turnover"),
            "chg24h": chg, "rs24h": rs,
            "flow_pct": flow_pct, "flow_days": flow_days,
            "coins_count": rec.get("coins_count"), "top3": rec.get("top3") or [],
        })
    key = (lambda r: (r["rs24h"] is None, -(r["rs24h"] if r["rs24h"] is not None
                                            else (r["chg24h"] or 0))))
    rows.sort(key=key)
    measured = [r for r in rows if r["rs24h"] is not None or r["chg24h"] is not None]
    return {
        "sectors": rows, "n": len(rows),
        "market_chg_24h": market_chg_24h,
        "lookback_days": lookback_days,
        "leaders": measured[:8], "laggards": measured[-8:][::-1] if measured else [],
        "basis": ("relative to the total market cap change"
                  if market_chg_24h is not None
                  else "absolute — no market benchmark was available tonight"),
    }


# ---------------------------------------------------------------------------
# stablecoin velocity / the fiat bridge
# ---------------------------------------------------------------------------
# Two independent readings, and the regime is only claimed when they agree.
STABLE_VELOCITY_HOT = 0.25     # 24h volume above a quarter of the float
STABLE_FLOAT_EXPANDING = 0.5   # percent growth over the lookback that counts as inflow
STABLE_FLOAT_CONTRACTING = -0.5


def stablecoin_regime(velocity, float_chg_pct, lookback_days: int = 0) -> dict:
    """Risk-on / risk-off from the fiat bridge, claimed only on two confirmations.

    Velocity alone cannot distinguish new dollars from the same dollars moving faster,
    and float growth alone cannot distinguish deployed capital from capital parked. The
    regime is named only when both point the same way; one without the other is
    ``MIXED``, which is a reading and not a failure.

    ``float_chg_pct`` is None until the ledger has a prior night to compare against, and
    the function says so rather than treating "no history" as "no growth" — the second
    would print RISK-OFF on the first night of every deployment.
    """
    if velocity is None:
        return {"regime": None, "basis": "no stablecoin feed tonight",
                "velocity": None, "float_chg_pct": float_chg_pct,
                "lookback_days": lookback_days}
    if float_chg_pct is None:
        return {"regime": "UNCONFIRMED",
                "basis": (f"velocity {velocity:.3f} observed, but no prior night to "
                          "measure float growth against"),
                "velocity": velocity, "float_chg_pct": None,
                "lookback_days": lookback_days}
    hot = velocity >= STABLE_VELOCITY_HOT
    expanding = float_chg_pct >= STABLE_FLOAT_EXPANDING
    contracting = float_chg_pct <= STABLE_FLOAT_CONTRACTING
    if hot and expanding:
        reg, why = "RISK-ON", "float expanding and turning over"
    elif contracting and not hot:
        reg, why = "RISK-OFF", "float contracting on low turnover"
    elif expanding and not hot:
        reg, why = "CAPITAL PARKED", "float expanding but sitting still"
    elif hot and contracting:
        reg, why = "ROTATION", "float shrinking while turnover stays high"
    else:
        reg, why = "MIXED", "the two legs do not agree"
    return {"regime": reg,
            "basis": (f"{why} — velocity {velocity:.3f} vs {STABLE_VELOCITY_HOT}, "
                      f"float {float_chg_pct:+.2f}% over {lookback_days}d"),
            "velocity": velocity, "float_chg_pct": float_chg_pct,
            "lookback_days": lookback_days}


# ---------------------------------------------------------------------------
# fallen quality kings
# ---------------------------------------------------------------------------
# A drawdown screen that does not first establish quality is a screen for things that
# are going down. The quality test here is structural and boring on purpose: the asset
# has to have been ranked large for most of the recorded history, not merely today.
FALLEN_MIN_DD = 10.0       # shallower than this is noise, not a dislocation
FALLEN_MAX_DD = 40.0       # deeper than this is not a drawdown, it is a re-rating
FALLEN_RSI_OVERSOLD = 40.0
FALLEN_MIN_BARS = 10
FALLEN_QUALITY_RANK = 25   # must have sat inside the top N by market cap...
FALLEN_QUALITY_SHARE = 0.7  # ...on at least this share of its recorded nights


def fallen_kings(series: dict, quality_rank: int = FALLEN_QUALITY_RANK) -> list:
    """High-durability names in a drawdown deep enough to matter and shallow enough to mean-revert.

    ``series`` is ``{SYMBOL: [{"date":, "close":, "rank":, "rsi7":}, ...]}`` oldest
    first, built from the recorded ledger.

    Three conditions, all required, and the reason each is there:

      quality     ranked inside the top ``quality_rank`` by market cap on most of its
                  recorded nights. Today's rank is not enough — a token that fell INTO
                  the top 25 this week is not a fallen king, it is a mover.
      drawdown    between FALLEN_MIN_DD and FALLEN_MAX_DD off the recorded peak. Both
                  bounds matter: below the floor there is nothing dislocated, and past
                  the ceiling the market is usually repricing something real and
                  "mean reversion" is a bet against information.
      exhaustion  RSI7 oversold, OR a bullish momentum divergence — price making a
                  lower low while RSI does not. The divergence is the stronger of the
                  two and is reported separately rather than pooled, because an
                  oversold reading is a condition and a divergence is an event.

    The peak is the highest close *this pipeline recorded*, which on a short ledger is
    not the all-time high. That is stated on every row as ``peak_from_bars`` so a 12%
    drawdown off a 19-night peak is never read as a 12% drawdown off the cycle high.
    """
    out = []
    for sym, seq in (series or {}).items():
        seq = [r for r in seq if r.get("close") is not None]
        if len(seq) < FALLEN_MIN_BARS:
            continue
        ranks = [r["rank"] for r in seq if r.get("rank") is not None]
        if not ranks:
            continue
        share = sum(1 for r in ranks if r <= quality_rank) / len(ranks)
        if share < FALLEN_QUALITY_SHARE:
            continue
        closes = [float(r["close"]) for r in seq]
        peak = max(closes)
        last = closes[-1]
        if peak <= 0:
            continue
        dd = (last / peak - 1.0) * 100.0
        if not (-FALLEN_MAX_DD <= dd <= -FALLEN_MIN_DD):
            continue
        peak_i = closes.index(peak)
        rsis = [r.get("rsi7") for r in seq]
        rsi_now = rsis[-1]
        # Bullish divergence: this close is the lowest since the peak, and RSI at the
        # prior low was lower than RSI is now. Selling is making new lows on less force.
        divergence = False
        post = closes[peak_i:]
        post_rsi = rsis[peak_i:]
        if len(post) >= 4 and rsi_now is not None:
            prior_lows = [(c, r) for c, r in zip(post[:-1], post_rsi[:-1]) if r is not None]
            if prior_lows:
                low_close, low_rsi = min(prior_lows, key=lambda p: p[0])
                if last <= low_close and rsi_now > low_rsi:
                    divergence = True
        oversold = rsi_now is not None and rsi_now <= FALLEN_RSI_OVERSOLD
        if not (oversold or divergence):
            continue
        out.append({
            "symbol": sym, "drawdown_pct": round(dd, 2),
            "peak": round(peak, 8), "last": round(last, 8),
            "peak_date": seq[peak_i].get("date"),
            "days_since_peak": len(seq) - 1 - peak_i,
            "rsi7": rsi_now, "oversold": oversold, "divergence": divergence,
            "quality_share": round(share, 2), "bars": len(seq),
            "peak_from_bars": len(seq),
            "trigger": ("divergence + oversold" if (divergence and oversold)
                        else "divergence" if divergence else "oversold"),
        })
    out.sort(key=lambda r: (not r["divergence"], r["drawdown_pct"]))
    return out


# ---------------------------------------------------------------------------
# execution: impact, drag, and how long an exit takes
# ---------------------------------------------------------------------------
# Square-root market impact. The coefficient is the one thing here that is a choice
# rather than an identity, and it is stated as such: published estimates for the
# Almgren-Chriss temporary-impact constant cluster around 1 for liquid equities, and
# nothing in this repository has calibrated it for crypto. It is therefore a plausible
# scale, not a measurement, and every number derived from it is labelled an estimate on
# the terminal. What it is NOT is arbitrary in shape: impact growing with the square
# root of participation is the one part of this that is well established.
IMPACT_COEFF = 1.0
# Half the quoted spread, paid on entry and again on exit. Same figure funding.py uses
# for its carry screen, kept in sync deliberately rather than duplicated with a
# different value in a second place.
DEFAULT_SPREAD_BPS = 5.0


def execution_drag(notional: float, adv_usd, daily_vol_pct=None,
                   spread_bps: float = DEFAULT_SPREAD_BPS,
                   participation_pct: float = 20.0) -> dict:
    """The cost of getting into a position of this size, in basis points.

    Returns ``{"impact_bps":, "spread_bps":, "total_bps":, "days_to_exit":,
    "participation":, "estimate":}``. Everything is None when ADV is unknown; a
    position sizer that silently assumes infinite liquidity for a token it has no volume
    for will size the least liquid names on the board the largest, which is precisely
    backwards.

    ``days_to_exit`` is the honest constraint and often the binding one. A position that
    costs 30bp to enter and takes nine days to exit at 20% of volume is not a 30bp
    position — the exit is where illiquidity is actually paid, and it is paid in market
    moves rather than in spread.
    """
    try:
        adv = float(adv_usd)
    except (TypeError, ValueError):
        adv = 0.0
    if adv <= 0 or notional is None or notional <= 0:
        return {"impact_bps": None, "spread_bps": spread_bps, "total_bps": None,
                "days_to_exit": None, "participation": None, "estimate": False,
                "basis": "no 24h volume recorded for this asset — cost is unknown, "
                         "not zero"}
    part = notional / adv
    sigma = (float(daily_vol_pct) / 100.0) if daily_vol_pct not in (None, "") else None
    # Without a volatility reading the impact term has no scale. Rather than substitute
    # a market-average sigma — which would put a number on the screen that no input
    # supports — the impact is reported as None and only the spread is charged, with
    # `estimate` False so the terminal can say the figure is a floor.
    if sigma is None or sigma <= 0:
        return {"impact_bps": None, "spread_bps": spread_bps, "total_bps": None,
                "days_to_exit": round(part / (participation_pct / 100.0), 2),
                "participation": round(part * 100, 3), "estimate": False,
                "basis": "no volatility reading — impact cannot be scaled, only the "
                         "spread is known"}
    impact_bps = 10000.0 * IMPACT_COEFF * sigma * math.sqrt(part)
    return {
        "impact_bps": round(impact_bps, 1),
        "spread_bps": spread_bps,
        "total_bps": round(impact_bps + spread_bps, 1),
        "days_to_exit": round(part / (participation_pct / 100.0), 2),
        "participation": round(part * 100, 3),
        "estimate": True,
        "basis": (f"sqrt-impact at coefficient {IMPACT_COEFF} on {part*100:.2f}% of ADV, "
                  f"sigma {sigma*100:.1f}%/day, plus {spread_bps:.0f}bp spread"),
    }


ATR_PERIOD = 14


def atr_min_bars(period: int = ATR_PERIOD) -> int:
    """Bars needed before an ATR exists at all.

    Named rather than left implicit as ``period + 1`` at three call sites, because the
    eligibility gate has to ask this question of the recorded series without restating
    the window. One true range consumes two bars, so a fourteen-period ATR needs fifteen.
    """
    return period + 1


def atr(bars: list, period: int = ATR_PERIOD) -> float | None:
    """Average True Range over recorded bars. None below ``period + 1`` of them.

    Used by the sizer for volatility parity, which is why it is here rather than folded
    into the ADX computation that also needs true ranges: they take different windows,
    and sharing the intermediate would tie the sizer's window to the trend indicator's.
    """
    if len(bars) < atr_min_bars(period):
        return None
    trs = []
    for prev, cur in zip(bars, bars[1:]):
        h, lo, pc = cur.get("high"), cur.get("low"), prev.get("close")
        if None in (h, lo, pc):
            return None
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    window = trs[-period:]
    return round(sum(window) / len(window), 10) if window else None


# ---------------------------------------------------------------------------
# liquidity shock
# ---------------------------------------------------------------------------
LIQ_SHOCK_Z = -2.0        # turnover this far below its own baseline is a freeze
LIQ_SHOCK_MIN_OBS = 7


def liquidity_shock(turnover_series: list, current=None) -> dict:
    """Is this asset's turnover collapsing relative to its own recent baseline?

    A cross-sectional liquidity screen compares assets to each other and therefore flags
    the same illiquid names every night, which is a list of facts about the universe
    rather than news. This compares an asset to *itself*: the z-score of today's
    turnover against its own trailing mean and standard deviation. A name that normally
    turns over 40% of its cap and today turns over 4% is the event, whatever its
    absolute rank.

    None below ``LIQ_SHOCK_MIN_OBS`` observations, and None when the trailing series has
    no dispersion — a constant history gives a z-score of infinity and reports every
    small change as a crisis.
    """
    vals = []
    for v in turnover_series or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        vals.append(f)
    cur = current
    if cur is None and vals:
        cur, vals = vals[-1], vals[:-1]
    else:
        try:
            cur = float(cur)
        except (TypeError, ValueError):
            cur = None
    if cur is None or len(vals) < LIQ_SHOCK_MIN_OBS:
        return {"z": None, "shock": False, "n": len(vals),
                "needed": LIQ_SHOCK_MIN_OBS, "mean": None,
                "basis": f"{len(vals)} of {LIQ_SHOCK_MIN_OBS} baseline nights recorded"}
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = math.sqrt(var)
    if sd <= 0:
        return {"z": None, "shock": False, "n": len(vals),
                "needed": LIQ_SHOCK_MIN_OBS, "mean": round(mean, 4),
                "basis": "trailing turnover has no dispersion — a z-score would be "
                         "undefined, not extreme"}
    z = (cur - mean) / sd
    return {"z": round(z, 2), "shock": z <= LIQ_SHOCK_Z, "n": len(vals),
            "needed": LIQ_SHOCK_MIN_OBS, "mean": round(mean, 4),
            "threshold": LIQ_SHOCK_Z,
            "basis": (f"turnover {cur:.2f} against a {len(vals)}-night baseline of "
                      f"{mean:.2f} +/- {sd:.2f}")}
