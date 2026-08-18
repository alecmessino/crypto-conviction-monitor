#!/usr/bin/env python3
"""Cryptometer: liquidations and positioning. Not funding — it does not have funding.

What this API is, and what it is not
------------------------------------
Cryptometer exposes 38 documented GET endpoints of exchange-level order-flow data behind
a single query-string key. It is genuinely useful for two things this project cannot
source anywhere else, and it is genuinely missing the thing it was proposed to fix.

**There is no funding-rate endpoint.** Not under that name and not under any other:
``funding-rates-v2``, ``funding-rate``, ``funding-rates``, ``funding``,
``funding-rate-v2/v3``, ``funding-data``, ``fundingrate``, ``funding-rate-history``,
``predicted-funding-rate`` and ``funding-info`` were all probed against the live host and
every one returned the same not-found signature as a control path that does not exist.
The published documentation contains no funding endpoint either. So the plan to "replace
Module E's rates with Cryptometer's real-time cross-exchange weighted funding" is not
something the API can do, and the geo-blocking that motivated it is fixed instead by
``funding.py`` adding venues that answer from a US host — dYdX, Gate.io and Kraken.

Of the eight endpoint names originally proposed, exactly one is real (``open-interest``).
The rest were misremembered or invented: the real paths are ``liquidation-data-v2`` (not
``liquidation-v2``), ``merged-orderbook`` (not ``orderbook-depth``, and it returns scalar
totals rather than a depth ladder), ``xtrades`` (not ``whale-trades``), and
``24h-trade-volume-v2`` / ``merged-trade-volume`` for the buy/sell split a CVD would be
computed from. There is no liquidation-heatmap endpoint at all, under any spelling.

What it is used for here
------------------------
Two readings, both of which this project currently cannot get:

  liquidations   long and short liquidation volume per exchange. Nothing else in the
                 pipeline sees forced selling, and a conviction score that cannot tell an
                 orderly decline from a cascade is missing the distinction that decides
                 whether to wait for a better fill.

  long/short     the positioning ratio. This column has existed since Module 1 and has
                 been null on every row since the runner started getting HTTP 451 from
                 Binance, which was its only source. Cryptometer is not geo-blocked from
                 US datacenter IPs — verified by the same controlled comparison that
                 reproduced the Binance 451 and Bybit 403 — so it restores a column that
                 has been dark rather than adding a new one.

Everything from this module is observational. None of it reaches ``score()``, and
adopting any of it would be a separate decision that moves the specification hash.

The envelope traps, which are not optional to handle
-----------------------------------------------------
``success`` and ``error`` come back as the STRINGS ``"true"`` and ``"false"``, not
booleans. ``if payload["success"]:`` is therefore true when the call failed, because the
string ``"false"`` is truthy. Error responses have a different shape entirely — a
``message`` key and no ``data`` key — so code that reaches for ``data`` on the failure
path raises rather than degrades. And HTTP 403 is a catch-all returned identically for a
path that does not exist and for a real path with a misnamed parameter, so the response
cannot distinguish the two: the request URL is logged on 403 because that is the only
thing that can.

Parameter names are not interchangeable between endpoints — ``open-interest`` takes
``market_pair``, ``ls-ratio`` takes ``pair``, ``liquidation-data-v2`` takes ``symbol`` —
and passing the wrong one produces that same 403.

Standard library only, matching the rest of the pipeline.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://api.cryptometer.io"

# Documented as 20 requests per 5 seconds, raised to 50 per 5 for premium accounts. No
# X-RateLimit-* or Retry-After headers appear on any response and a 429 could not be
# provoked without a valid key, so the enforcement behaviour is unknown — which argues
# for staying well under the published figure rather than probing where it bites.
MAX_REQUESTS_PER_SEC = 3.0
_MIN_INTERVAL = 1.0 / MAX_REQUESTS_PER_SEC

# Every endpoint here is per-symbol; the API publishes no bulk or multi-symbol variant
# and no pagination. Fan-out is therefore the only access pattern, and at three requests
# a second a 50-name board is already 17 seconds of wall clock. Bounded to the names that
# could plausibly act on the reading rather than the whole universe.
DEFAULT_SYMBOL_LIMIT = 25

# Cryptometer's exchange identifiers for derivatives endpoints. Not exhaustively
# documented; discovered at runtime via /coinlist/ if this ever needs widening.
DERIV_EXCHANGES = ("binance_futures", "bybit", "bitmex", "deribit")

# 28 of the 38 documented endpoints carry a "Paid" badge; only these ten are free, and
# the distinction was missed on the first pass here. It matters more than it looks:
# `ls-ratio` and `liquidation-data-v2` are BOTH paid, and both were wired as though they
# were not. `long-shorts-data` is the only free positioning source, which inverts which
# endpoint should be primary — a free endpoint that answers beats a paid one that may
# not, when they measure the same thing.
FREE_ENDPOINTS = frozenset({
    "open-interest", "long-shorts-data", "24h-trade-volume-v2", "merged-orderbook",
    "trend-indicator-v3", "rapid-movements", "ticker", "tickerlist", "coinlist", "info"})

_UA = {"User-Agent": "conviction-monitor/1.0"}
_last_call = [0.0]


def _throttle() -> None:
    """Client-side spacing. The server publishes a limit and reports nothing about how it
    enforces one, so the only safe assumption is that exceeding it is our problem."""
    gap = time.monotonic() - _last_call[0]
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call[0] = time.monotonic()


def _get_json(url: str):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec
        return json.loads(resp.read().decode())


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def call(path: str, api_key: str, **params) -> tuple:
    """One endpoint call. Returns ``(rows, error)`` — exactly one is None.

    ``rows`` is always the ``data`` list, never the envelope, because the envelope's
    truthiness cannot be trusted: ``success`` is the string ``"true"`` or ``"false"`` and
    both are truthy in Python. That is not a style point — a client that branches on
    ``payload["success"]`` treats every failure as a success and returns the ``message``
    key as though it were data.

    The trailing slash is required by the API and is added here rather than trusted to
    each call site, since omitting it produces the same 403 as a nonexistent path.
    """
    if not api_key:
        return None, "no API key configured"
    qs = urllib.parse.urlencode({**params, "api_key": api_key})
    url = f"{BASE}/{path.strip('/')}/?{qs}"
    _throttle()
    try:
        payload = _get_json(url)
    except Exception as e:  # noqa: BLE001
        # 403 is a catch-all for a bad path AND for a real path with a misnamed
        # parameter, and the body cannot tell them apart. The redacted URL is logged
        # because it is the only thing that can.
        safe = url.replace(api_key, "***") if api_key else url
        return None, f"{e} [{safe}]"
    if not isinstance(payload, dict):
        return None, f"expected an object, got {type(payload).__name__}"
    # String comparison, deliberately. See the docstring.
    if str(payload.get("success", "")).lower() != "true":
        return None, str(payload.get("message") or "request rejected without a message")
    rows = payload.get("data")
    if rows is None:
        return None, "response reported success but carried no data key"
    # `data` is always an array, even when it holds a single scalar object.
    return (rows if isinstance(rows, list) else [rows]), None


def _report(status: str, data, detail: str, **extra) -> dict:
    """The same envelope every other feed in this pipeline returns.

    Four situations end in an empty table — unconfigured, unreachable, reachable but
    returning a shape this code does not recognise, and genuinely empty — and one "no
    data" message cannot tell a reader which they are in. This project has paid for that
    confusion once already on the Dune feed.
    """
    return {"source": "cryptometer", "status": status, "data": data,
            "detail": detail, **extra}


def check_quota(api_key: str) -> dict:
    """Account status from ``/info/``. The health check, and the cheapest possible one.

    Worth its own call because the failure it detects is otherwise invisible: an expired
    plan or an exhausted quota returns the same rejection as a malformed request, so
    without this the panel would report "liquidations unavailable" on a night the only
    thing wrong was the bill.
    """
    rows, err = call("info", api_key)
    if err:
        return _report("unconfigured" if "no API key" in err else "unreachable",
                       None, err)
    return _report("live", (rows or [{}])[0], "account reachable")


def _ranked(symbols, limit: int) -> list:
    """Caller order preserved, deduped, then capped.

    These functions used to do ``sorted(symbols)[:limit]``, which throws away the one
    piece of information the caller had: which symbols matter most. With a per-symbol
    endpoint, no bulk variant and a hard cap, the ORDER is the priority — re-sorting it
    alphabetically means the cap falls on whatever the alphabet chose. That is exactly
    how a 25-call sweep ended up starting at "A7A5" and never reaching BTC.
    """
    seen, out = set(), []
    for s in symbols:
        u = str(s).upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out[:limit]


def paid_enabled() -> bool:
    """Paid endpoints are opt-in and off by default.

    ``liquidation-data-v2`` is the only source here for forced-selling volume and there
    is no free equivalent, so the code stays. It is not called unless someone sets
    CRYPTOMETER_ALLOW_PAID, because a paid call on a free plan returns the same opaque
    403 as a misnamed parameter — spending the quota to find out is a decision, not a
    default.
    """
    return os.environ.get("CRYPTOMETER_ALLOW_PAID", "").strip().lower() in ("1", "true", "yes")


def fetch_liquidations(api_key: str, symbols, limit: int = DEFAULT_SYMBOL_LIMIT) -> dict:
    """Long and short liquidation volume per exchange, per symbol.

    PAID endpoint. A free key returns the same opaque 403 that a misnamed parameter
    produces, so the smoke test below is the only way to learn which of the two happened.

    ``/liquidation-data-v2/`` takes ``symbol`` (lowercase base, e.g. ``btc``) and returns
    ``data[0]`` as a MAP of exchange name to ``{longs, shorts}``. The exchange key set is
    dynamic — the documentation shows three, a live account may return more — so it is
    iterated rather than indexed, and a hardcoded exchange list would silently drop
    whatever it did not know about.

    The derived reading is the imbalance: ``(longs - shorts) / (longs + shorts)``, which
    is +1 when only longs are being liquidated and -1 when only shorts are. That is the
    form the number is comparable in — raw dollar totals scale with the size of the
    market and rank the large caps every time.
    """
    if not api_key:
        return _report("unconfigured", {}, "no CRYPTOMETER_API_KEY in the environment")
    if not paid_enabled():
        return _report("unconfigured", {},
                       "liquidation-data-v2 is a paid endpoint and CRYPTOMETER_ALLOW_PAID "
                       "is not set — the liquidation columns stay null rather than "
                       "spending quota to discover the plan tier")
    out, errors = {}, []
    for base in _ranked(symbols, limit):
        rows, err = call("liquidation-data-v2", api_key, symbol=base.lower())
        if err:
            errors.append(f"{base}: {err}")
            continue
        by_exchange = (rows or [{}])[0]
        if not isinstance(by_exchange, dict):
            errors.append(f"{base}: expected an exchange map")
            continue
        longs = shorts = 0.0
        venues = []
        for venue, side in by_exchange.items():
            if not isinstance(side, dict):
                continue
            lo, sh = _num(side.get("longs")), _num(side.get("shorts"))
            if lo is None and sh is None:
                continue
            longs += lo or 0.0
            shorts += sh or 0.0
            venues.append(venue)
        total = longs + shorts
        if not venues:
            continue
        out[base.upper()] = {
            "longs_usd": round(longs, 2), "shorts_usd": round(shorts, 2),
            "total_usd": round(total, 2),
            # None rather than 0.0 on a quiet tape: zero imbalance is a claim that both
            # sides were liquidated equally, and no liquidations at all is not that.
            "imbalance": round((longs - shorts) / total, 4) if total else None,
            "venues": sorted(venues),
        }
    if not out:
        return _report("unreachable" if errors else "unusable", {},
                       "; ".join(errors[:3]) or "no symbol returned a liquidation map")
    detail = f"{len(out)} symbol(s)"
    if errors:
        detail += f"; {len(errors)} failed ({errors[0]})"
    return _report("partial" if errors else "live", out, detail)


def fetch_positioning(api_key: str, symbols, exchange: str = "binance_futures",
                      limit: int = DEFAULT_SYMBOL_LIMIT) -> dict:
    """Long vs short position sizes, restoring a column that has been dark.

    ``long_short_ratio`` has been in the schema since Module 1 and null on every row since
    the runner began getting HTTP 451 from Binance, its only source. This is the same
    reading from a host that answers, not a new one.

    Uses ``/long-shorts-data/`` rather than ``/ls-ratio/``, and the difference is not
    cosmetic: ls-ratio is a PAID endpoint and long-shorts-data is FREE, while both
    measure the same thing. The first version of this module wired the paid one without
    knowing, which would have failed on a free key with the same opaque 403 that a
    misnamed parameter produces — indistinguishable from a bug.

    Takes ``symbol`` (not ``pair``, which is what ls-ratio takes; the two are not
    interchangeable and the wrong one yields that same 403). Returns absolute longs and
    shorts, so the ratio is derived here rather than read.
    """
    if not api_key:
        return _report("unconfigured", {}, "no CRYPTOMETER_API_KEY in the environment")
    out, errors = {}, []
    for base in _ranked(symbols, limit):
        rows, err = call("long-shorts-data", api_key, e=exchange,
                         symbol=base.lower(), timeframe="1h")
        if err:
            errors.append(f"{base}: {err}")
            continue
        rec = (rows or [{}])[0]
        if not isinstance(rec, dict):
            continue
        longs, shorts = _num(rec.get("longs")), _num(rec.get("shorts"))
        if longs is None or shorts is None:
            continue
        total = longs + shorts
        out[base.upper()] = {
            "longs": longs, "shorts": shorts,
            # None rather than a division by zero, and None rather than 1.0 on an empty
            # book: "half the accounts are long" is a real reading and must not be
            # manufactured by an absent one.
            "ratio": round(longs / shorts, 4) if shorts else None,
            "long_pct": round(100.0 * longs / total, 2) if total else None,
        }
    if not out:
        return _report("unreachable" if errors else "unusable", {},
                       "; ".join(errors[:3]) or "no symbol returned position sizes")
    detail = f"{len(out)} symbol(s) on {exchange}"
    if errors:
        detail += f"; {len(errors)} failed ({errors[0]})"
    return _report("partial" if errors else "live", out, detail)


def api_key_from_env() -> str:
    """Read only from the environment. The key is a repository secret and belongs
    nowhere else — not in a default, not in a config file, not in a log line."""
    return os.environ.get("CRYPTOMETER_API_KEY", "").strip()


if __name__ == "__main__":
    # Smoke test, for running once from CI where the secret exists. The research that
    # specified this client was necessarily unauthenticated, so every response shape here
    # is transcribed from documentation rather than observed — this is what turns that
    # into a fact.
    key = api_key_from_env()
    if not key:
        print("CRYPTOMETER_API_KEY is not set", file=sys.stderr)
        raise SystemExit(2)
    print("[quota]", json.dumps(check_quota(key), indent=1)[:800])
    checks = [
        ("positioning (FREE long-shorts-data)", fetch_positioning(key, {"BTC", "ETH"}, limit=2)),
        ("liquidations (PAID liquidation-data-v2)", fetch_liquidations(key, {"BTC", "ETH"}, limit=2)),
    ]
    failed = []
    for name, rep in checks:
        print(f"[{name}] {rep['status']}: {rep['detail']}")
        print(json.dumps(rep["data"], indent=1)[:600])
        if rep["status"] not in ("live", "partial"):
            failed.append(name)
    # The free endpoint failing is a real problem. The paid one failing on a free plan is
    # expected, and the exit code says which so a workflow log can be read at a glance.
    if any("FREE" in f for f in failed):
        print("\nFAIL: a free endpoint did not answer", file=sys.stderr)
        raise SystemExit(1)
    if failed:
        print(f"\nNOTE: {len(failed)} paid endpoint(s) unavailable on this plan — "
              f"expected on the free tier, and the liquidation columns stay null.")
    raise SystemExit(0)
