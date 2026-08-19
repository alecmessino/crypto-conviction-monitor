#!/usr/bin/env python3
"""CoinGecko: credentialed session, and the four feeds nightly.py did not have.

Why this is a module and not a few more lines in nightly.py
-----------------------------------------------------------
``nightly.py`` talked to CoinGecko in two places — ``fetch_markets`` and
``fetch_global_market_cap`` — and both were keyless. Keyless is not a configuration, it
is a rate limit: the free public tier allows roughly 10-30 calls a minute per IP and
answers HTTP 429 when it does not. ``fetch_markets`` already carries an exponential
backoff loop for exactly that reason, and on a bad night it gives up on a page and the
board is scored over half a universe.

A key removes that, but only if every call carries it, which is the first thing this
module is for. The second is that once the calls are credentialed there is budget for
feeds that were not previously worth spending a keyless request on: what is trending,
where sector capital is rotating, and whether net-new fiat is bridging in. Those are
three separate questions the monitor could not answer at all.

Demo keys and Pro keys are not the same credential
--------------------------------------------------
CoinGecko issues two kinds, and they differ in host *and* header:

  * Demo  — ``api.coingecko.com``      header ``x-cg-demo-api-key``
  * Pro   — ``pro-api.coingecko.com``  header ``x-cg-pro-api-key``

Sending a Pro key to the public host, or a Demo key to the Pro host, does not fail
loudly in a way that survives a ``try/except`` — it comes back as a 400 or a 401 that
looks exactly like "this endpoint is unavailable", and the pipeline degrades to keyless
while a paid key sits unused in the environment. The key's own text does not say which
it is: both are issued in the same ``CG-...`` shape.

So the plan is *probed*, once, against ``/ping``, and the answer is recorded on the
artifact. "The key is a Demo key" is then a fact this repository observed rather than an
assumption someone made when they pasted a secret into a settings page.

What is a claim and what is not
-------------------------------
Same rule as ``funding.py``, and for the same reason. Every fetch here returns a report
``{status, detail, data, http_status}``. ``data`` is empty on failure and the status says
which failure it was — unconfigured, rate-limited, unreachable, or empty. Nothing
substitutes a plausible value for a missing one: a sector with no reading is absent from
the matrix, not flat, and an absent trending list is not an empty market.

Nothing in this module reaches ``score()``. It supplies context, ranking and macro
regime; the conviction number is computed from the markets payload and the derivatives
feed exactly as before.

Standard library only, matching nightly.py and funding.py: this runs in CI with no
install step.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PUBLIC_HOST = "https://api.coingecko.com/api/v3"
PRO_HOST = "https://pro-api.coingecko.com/api/v3"

UA = "conviction-monitor/1.0"
TIMEOUT = 25

# ---------------------------------------------------------------------------
# sector selection
# ---------------------------------------------------------------------------
# /coins/categories returns 753 rows. Most of them are not sectors in any sense a
# rotation matrix means: "Made in USA" is a jurisdiction, "Alleged SEC Securities" is a
# legal status, and "Smart Contract Platform" holds BTC, ETH and BNB and is therefore a
# 2-trillion-dollar restatement of the market itself. Ranking all 753 by 24h change puts
# a four-coin category with a nine-million-dollar cap at the top every night.
#
# Two floors and one exclusion list, all stated here rather than tuned in place:
SECTOR_MIN_MCAP = 250_000_000.0   # below this a category moves on one thin coin
SECTOR_MIN_COINS = 5              # a "sector" of three tokens is three tokens
# Anything whose id contains one of these is excluded. Pegged assets do not rotate —
# a stablecoin category moving 3% is a depeg or a data error, never a capital flow —
# and the market-wide categories would dominate a ranking they are not informative in.
SECTOR_EXCLUDE_SUBSTRINGS = (
    "stablecoin", "tokenized-", "wrapped-", "bridged-", "-pegged",
    "cryptocurrency", "smart-contract-platform", "proof-of-work", "proof-of-stake",
    "made-in-", "alleged-", "portfolio", "index-", "-ecosystem-index",
)
# How many sectors the matrix reports at each end. Both ends, deliberately: capital
# rotating *out* of a narrative is the same observation as capital rotating in, and a
# leaderboard that only shows winners cannot tell a broad rally from a rotation.
SECTOR_REPORT_N = 12

# Stablecoins whose aggregate cap and turnover are read as the fiat bridge. Restricted
# to the ones with a genuine redemption path: velocity is meant to say that new dollars
# arrived, and an algorithmic or yield-bearing token's cap can expand without a dollar
# moving anywhere.
STABLE_IDS = ("tether", "usd-coin", "dai", "first-digital-usd", "paypal-usd",
              "ethena-usde", "usds", "true-usd", "gemini-dollar", "usdd")


def _report(status: str, detail: str, data=None, http_status: int | None = None) -> dict:
    return {"status": status, "detail": detail, "data": data if data is not None else {},
            "http_status": http_status}


def api_key_from_env(env: dict | None = None) -> str | None:
    """The key, from the environment only. Never a literal in this repository.

    Accepts either spelling because the secret has been called both things in
    CoinGecko's own documentation, and a pipeline that silently ignores the other one
    is a pipeline that runs keyless while a key is configured.
    """
    e = os.environ if env is None else env
    for name in ("COINGECKO_API_KEY", "CG_API_KEY", "COINGECKO_DEMO_API_KEY",
                 "COINGECKO_PRO_API_KEY"):
        v = (e.get(name) or "").strip()
        if v:
            return v
    return None


def _raw_get(url: str, headers: dict) -> tuple[int, object]:
    """One request. Returns ``(http_status, parsed_or_error_text)``.

    Deliberately does not raise on 4xx: which 4xx it is carries the whole diagnosis
    here — 401 is the wrong header for the key, 429 is the budget, 404 is a Pro-only
    endpoint on a Demo plan — and an exception erases the distinction.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                               **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode()[:400]
        except Exception:  # noqa: BLE001
            body = ""
        return e.code, body
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def open_session(key: str | None = None, probe: bool = True) -> dict:
    """Resolve the credential to a host and a header, by asking rather than assuming.

    Returns ``{host, headers, plan, status, detail}``. ``plan`` is one of ``demo``,
    ``pro`` or ``keyless``, and it is the *probed* answer, not a guess from the key's
    shape — CoinGecko issues both kinds in the same ``CG-...`` form, so the shape
    carries no information.

    ``probe=False`` skips the two ``/ping`` calls and assumes Demo. That exists for
    tests and for a caller who already knows; it is never what the nightly does,
    because "assume Demo" is exactly the failure this function was written to remove.
    """
    key = key if key is not None else api_key_from_env()
    if not key:
        return {"host": PUBLIC_HOST, "headers": {}, "plan": "keyless",
                "status": "unconfigured",
                "detail": ("no COINGECKO_API_KEY in the environment — every call runs on "
                           "the public rate limit, and a 429 costs a page of the "
                           "universe rather than a retry")}
    if not probe:
        return {"host": PUBLIC_HOST, "headers": {"x-cg-demo-api-key": key},
                "plan": "demo", "status": "assumed",
                "detail": "plan not probed; Demo assumed by the caller"}

    attempts = []
    for plan, host, header in (("demo", PUBLIC_HOST, "x-cg-demo-api-key"),
                               ("pro", PRO_HOST, "x-cg-pro-api-key")):
        code, body = _raw_get(f"{host}/ping", {header: key})
        attempts.append(f"{plan}={code or 'net-error'}")
        if code == 200:
            return {"host": host, "headers": {header: key}, "plan": plan,
                    "status": "live",
                    "detail": f"{plan.upper()} key accepted at {urllib.parse.urlsplit(host).netloc}"}
    # A key that answers neither is worse than no key: it will be sent on every call and
    # rejected on every call. Fall back to keyless explicitly and say so, so the night
    # reads as "the key does not work" rather than as an unexplained rate limit.
    return {"host": PUBLIC_HOST, "headers": {}, "plan": "keyless",
            "status": "rejected",
            "detail": ("a key is configured but neither the Demo nor the Pro endpoint "
                       "accepted it (" + ", ".join(attempts) + "); running keyless "
                       "rather than sending a credential that is refused")}


def get(session: dict, path: str, params: dict | None = None,
        retries: int = 3, backoff: float = 4.0, sleep=None) -> dict:
    """One credentialed GET, with the 429 backoff the free tier makes mandatory.

    Returns a report rather than a value. A 429 that exhausts its retries is
    ``rate_limited``, not ``unreachable``: the first is a budget problem that a key
    fixes and the second is an outage, and a column that cannot tell them apart cannot
    be used to decide whether the key is working.
    """
    # Same late-binding rule as fetch_dex_networks: resolved at call time so a caller
    # or a test can substitute it, and so a default cannot capture a function object
    # that a later patch never reaches.
    sleep = sleep or time.sleep
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{session['host']}{path}{qs}"
    wait = backoff
    last = None
    for attempt in range(retries):
        code, body = _raw_get(url, session.get("headers") or {})
        if code == 200:
            return _report("live", f"{path} ok", body, 200)
        last = (code, body)
        if code == 429:
            if attempt < retries - 1:
                sleep(wait)
                wait = min(wait * 2, 60.0)
            continue
        break
    code, body = last if last else (0, "no response")
    if code == 429:
        return _report("rate_limited",
                       f"{path} returned HTTP 429 on every one of {retries} attempts"
                       + ("" if session.get("plan") != "keyless" else
                          " — this run is keyless, which is the likeliest cause"),
                       {}, 429)
    if code in (401, 403):
        return _report("unauthorized",
                       f"{path} returned HTTP {code} — the key was sent and refused",
                       {}, code)
    if code == 404:
        return _report("unavailable",
                       f"{path} returned HTTP 404 — not served on the "
                       f"{session.get('plan')} plan", {}, 404)
    if code == 0:
        return _report("unreachable", f"{path} did not resolve: {str(body)[:160]}", {}, None)
    return _report("unreachable", f"{path} returned HTTP {code}: {str(body)[:160]}", {}, code)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
def _num(v):
    """Float or None. Never 0.0 for an unparseable value.

    ``/search/trending`` returns market cap and volume as *formatted strings*
    — ``"$15,982,922,471"`` — while every other endpoint returns them as numbers. That
    is not in the documentation; it is what the endpoint actually sends, and a naive
    ``float()`` on it raises, which under a bare except becomes a zero. A trending coin
    with a zero market cap sorts to the bottom of every liquidity screen it appears in.
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(",", "")
    if not s or s.lower() in ("none", "null", "n/a"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _usd(d, default=None):
    """The USD member of one of CoinGecko's per-currency dictionaries.

    ``price_change_percentage_24h`` on the trending payload is a dict of ~60 fiat and
    crypto denominations. The BTC-denominated member is a *relative* return and the USD
    one is not; reading the wrong key gives a number that is plausible, wrong, and
    silently changes sign in a BTC drawdown.
    """
    if isinstance(d, dict):
        return _num(d.get("usd"))
    return _num(d) if d is not None else default


# ---------------------------------------------------------------------------
# feeds
# ---------------------------------------------------------------------------
def fetch_trending(session: dict, getter=get) -> dict:
    """``/search/trending`` — the 15 coins with the most search traffic, ranked.

    The rank is the datum. CoinGecko orders the list by search interest over the last
    24 hours, so position 1 is not "the best coin", it is "the coin the most people who
    do not own it yet are looking up". Read against a conviction score that is computed
    from liquidity, depth and relative strength, the *disagreement* between the two
    orderings is the signal — which is what ``nightly.trending_divergence`` computes and
    what this fetch exists to supply.

    ``data`` is ``{SYMBOL: {rank, id, name, mcap_rank, price, mcap, volume, chg24h}}``.
    Keyed by symbol because that is what the rest of the pipeline keys on, and
    de-duplicated by first occurrence: two listings can share a ticker and the
    higher-ranked one is the one being searched for.
    """
    rep = getter(session, "/search/trending")
    if rep["status"] != "live":
        return _report(rep["status"], "trending: " + rep["detail"], {}, rep["http_status"])
    payload = rep["data"] if isinstance(rep["data"], dict) else {}
    out = {}
    for rank, entry in enumerate(payload.get("coins") or [], start=1):
        item = (entry or {}).get("item") or {}
        sym = (item.get("symbol") or "").upper()
        if not sym or sym in out:
            continue
        d = item.get("data") or {}
        out[sym] = {
            "rank": rank,
            "id": item.get("id"),
            "name": item.get("name"),
            "mcap_rank": item.get("market_cap_rank"),
            "price": _num(d.get("price")),
            "mcap": _num(d.get("market_cap")),
            "volume": _num(d.get("total_volume")),
            "chg24h": _usd(d.get("price_change_percentage_24h")),
        }
    # The trending *categories* ride along on the same response, so reading them costs
    # nothing and answers "is the search interest concentrated in one narrative".
    cats = []
    for rank, c in enumerate(payload.get("categories") or [], start=1):
        cats.append({"rank": rank, "id": c.get("id"), "name": c.get("name"),
                     "slug": c.get("slug"),
                     "coins_count": _num(c.get("coins_count")),
                     "mcap": _num(((c.get("data") or {}).get("market_cap"))),
                     "chg24h": _usd((c.get("data") or {}).get(
                         "market_cap_change_percentage_24h"))})
    if not out:
        return _report("empty", "trending: the endpoint answered with no coins", {}, 200)
    return _report("live", f"trending: {len(out)} coin(s), {len(cats)} category(ies)",
                   {"coins": out, "categories": cats}, 200)


def fetch_categories(session: dict, getter=get) -> dict:
    """``/coins/categories`` — every sector's market cap, volume and 24h change.

    Filtered down to the ones a rotation matrix can mean something about (see
    ``SECTOR_MIN_MCAP`` / ``SECTOR_EXCLUDE_SUBSTRINGS`` above). The endpoint has no 7d
    column and no history of any kind, so multi-day rotation is *not* taken from here:
    it is computed from ``ledger/sectors.csv``, which this feed appends one row per
    sector per night to. A 7d flow claimed on the first night would be a 24h flow
    wearing a longer label.
    """
    rep = getter(session, "/coins/categories", {"order": "market_cap_desc"})
    if rep["status"] != "live":
        return _report(rep["status"], "categories: " + rep["detail"], {}, rep["http_status"])
    rows = rep["data"] if isinstance(rep["data"], list) else []
    out = {}
    for c in rows:
        cid = c.get("id") or ""
        if not cid or any(s in cid for s in SECTOR_EXCLUDE_SUBSTRINGS):
            continue
        mcap = _num(c.get("market_cap"))
        vol = _num(c.get("volume_24h"))
        chg = _num(c.get("market_cap_change_24h"))
        if mcap is None or mcap < SECTOR_MIN_MCAP:
            continue
        # coins_count is absent from /coins/categories (it is only on the trending
        # payload), so the floor is applied where the field exists and skipped where it
        # does not, rather than defaulting the count to something.
        n = _num(c.get("coins_count"))
        if n is not None and n < SECTOR_MIN_COINS:
            continue
        out[cid] = {
            "id": cid, "name": c.get("name") or cid,
            "mcap": mcap, "volume_24h": vol, "chg24h": chg,
            "coins_count": int(n) if n is not None else None,
            "top3": list(c.get("top_3_coins_id") or [])[:3],
            # Turnover at the sector level. The same quantity Module A reads per asset,
            # and it separates a sector rising on real flow from one marked up on none.
            "turnover": round(vol / mcap, 4) if (vol and mcap) else None,
            "updated_at": c.get("updated_at"),
        }
    if not out:
        return _report("empty",
                       f"categories: {len(rows)} returned, none cleared the "
                       f"${SECTOR_MIN_MCAP:,.0f} floor and the exclusion list", {}, 200)
    return _report("live", f"categories: {len(out)} sector(s) of {len(rows)} returned",
                   out, 200)


def fetch_global(session: dict, getter=get) -> dict:
    """``/global`` — total cap, total volume, and the dominance split.

    The dominance percentages are the reason this is fetched credentialed rather than
    left as the bare ``fetch_global_market_cap`` call it replaces. BTC dominance and the
    ETH/BTC ratio are the two macro anchors that decide whether an alt board should be
    read at all, and both were being discarded from a response that already contained
    them.
    """
    rep = getter(session, "/global")
    if rep["status"] != "live":
        return _report(rep["status"], "global: " + rep["detail"], {}, rep["http_status"])
    d = (rep["data"] or {}).get("data") or {}
    total = _num((d.get("total_market_cap") or {}).get("usd"))
    vol = _num((d.get("total_volume") or {}).get("usd"))
    dom = d.get("market_cap_percentage") or {}
    btc_dom = _num(dom.get("btc"))
    eth_dom = _num(dom.get("eth"))
    if total is None:
        return _report("empty", "global: no total_market_cap.usd in the response", {}, 200)
    out = {
        "total_mcap": total,
        "total_volume": vol,
        "btc_dominance": btc_dom,
        "eth_dominance": eth_dom,
        # The alt board's own denominator. Reported rather than left to be derived,
        # because "total cap rose" and "total cap ex-BTC rose" are different facts and
        # the second is the one an alt basket is measured against.
        "total_mcap_ex_btc": (round(total * (1 - btc_dom / 100.0), 2)
                              if btc_dom is not None else None),
        # ETH/BTC expressed from the dominance split rather than from a price ratio.
        # Same information, and it needs no second request.
        "eth_btc_dominance_ratio": (round(eth_dom / btc_dom, 5)
                                    if (btc_dom and eth_dom) else None),
        "mcap_chg_24h": _num(d.get("market_cap_change_percentage_24h_usd")),
        "active_cryptocurrencies": d.get("active_cryptocurrencies"),
        "updated_at": d.get("updated_at"),
    }
    return _report("live", f"global: total ${total/1e12:.3f}T, BTC dominance "
                           f"{btc_dom if btc_dom is not None else float('nan'):.2f}%",
                   out, 200)


def fetch_stablecoins(session: dict, getter=get, ids=STABLE_IDS) -> dict:
    """Aggregate cap and 24h turnover of the redeemable stablecoins.

    One ``/coins/markets`` call with an explicit id list rather than
    ``category=stablecoins``, because that category holds ~180 tokens including
    algorithmic and yield-bearing ones whose caps expand without a dollar being wired
    anywhere. The bridge indicator is meant to say that fiat arrived; a token that mints
    against crypto collateral is not evidence of that.

    Velocity is 24h volume over aggregate cap: how many times the float turned over.
    High velocity with a *rising* float is fiat entering and being deployed; high
    velocity with a flat float is the same dollars circulating faster, which is a
    different market and must not read the same.
    """
    rep = getter(session, "/coins/markets",
                 {"vs_currency": "usd", "ids": ",".join(ids), "per_page": len(ids),
                  "page": 1})
    if rep["status"] != "live":
        return _report(rep["status"], "stablecoins: " + rep["detail"], {}, rep["http_status"])
    rows = rep["data"] if isinstance(rep["data"], list) else []
    total_cap = 0.0
    total_vol = 0.0
    seen = []
    for r in rows:
        mc = _num(r.get("market_cap"))
        v = _num(r.get("total_volume"))
        if mc is None:
            continue
        total_cap += mc
        total_vol += v or 0.0
        seen.append({"symbol": (r.get("symbol") or "").upper(), "mcap": mc, "volume": v,
                     "chg24h": _num(r.get("market_cap_change_percentage_24h"))})
    if not seen:
        return _report("empty", "stablecoins: no row carried a market cap", {}, 200)
    seen.sort(key=lambda x: x["mcap"], reverse=True)
    return _report("live",
                   f"stablecoins: {len(seen)} issuer(s), ${total_cap/1e9:.1f}B float",
                   {"total_mcap": round(total_cap, 2),
                    "total_volume": round(total_vol, 2),
                    "velocity": round(total_vol / total_cap, 5) if total_cap else None,
                    "issuers": seen}, 200)


# ---------------------------------------------------------------------------
# on-chain (GeckoTerminal)
# ---------------------------------------------------------------------------
# Same vendor, different host and a different auth story: GeckoTerminal's v2 API is
# public and keyless, so it lives here rather than in its own module but does NOT take
# the session's credential — sending a CoinGecko key to it is a credential leaked to a
# host that never asked for one.
GT_BASE = "https://api.geckoterminal.com/api/v2"
# The version header is not optional decoration. Without it the API serves whatever its
# current default is, and a schema change then arrives as a silent reshape of the fields
# below rather than as a version bump this repository chose to take.
GT_HEADERS = {"Accept": "application/json;version=20230302"}
# One request per network, so this list is a budget as much as a selection. Chosen as
# the venues where a narrative rotation actually shows up in pool volume first; adding
# one costs a request a night and is a deliberate edit, not a config.
GT_NETWORKS = ("eth", "solana", "base", "arbitrum", "bsc", "polygon_pos", "avax",
               "optimism")


def fetch_dex_networks(networks=GT_NETWORKS, raw_get=None, sleep=None,
                       pause: float = 2.5) -> dict:
    """Aggregate pool depth and 24h pool volume for the top pools on each network.

    This is the honest, affordable form of the on-chain liquidity question. Reading
    depth for the *board's* tokens would mean resolving 250 symbols to contract
    addresses — a ``/coins/{id}`` call each, 250 requests a night to enrich fifty rows —
    so it is not done. What is done instead is the network aggregate, which is one
    request per chain and answers the question that actually moves allocations: which
    chain's pools are absorbing volume this week and which are draining.

    Two readings come out of it per network:

      ``reserve_usd``  summed pool depth. What a size order can be worked against.
      ``vlr``          volume over reserve — the Volume-to-Liquidity Ratio. A pool
                       turning over many multiples of its own depth daily is not deep
                       liquidity being used, it is a thin pool being churned, and the
                       two look identical in a volume column.

    Week-over-week rotation is deliberately NOT computed here. It is derived from
    ``ledger/dex.csv``, which this appends a row per network per night to, for the same
    reason the sector matrix does not claim a 7d flow on its first night.

    Every numeric field on this API arrives as a *string* — ``"5583322.509"``, not
    ``5583322.509``. Parsed through ``_num`` accordingly.
    """
    # Resolved at CALL time, not bound as a default. A default argument is evaluated
    # once when the module is defined, so `raw_get=_raw_get` captures the original
    # function object and a later substitution of the module attribute never reaches
    # here — which made this the one feed a test could not stand in for, and the one
    # that quietly made eight real network calls inside a unit test.
    raw_get = raw_get or _raw_get
    sleep = sleep or time.sleep
    out, failures = {}, []
    for i, net in enumerate(networks):
        code, body = raw_get(f"{GT_BASE}/networks/{net}/pools?page=1", GT_HEADERS)
        if code != 200 or not isinstance(body, dict):
            failures.append(f"{net}=HTTP {code or 'net-error'}")
        else:
            reserve = vol24 = 0.0
            pools = 0
            for entry in body.get("data") or []:
                attrs = (entry or {}).get("attributes") or {}
                r = _num(attrs.get("reserve_in_usd"))
                v = _num((attrs.get("volume_usd") or {}).get("h24"))
                if r is None and v is None:
                    continue
                reserve += r or 0.0
                vol24 += v or 0.0
                pools += 1
            if pools:
                out[net] = {
                    "network": net, "pools": pools,
                    "reserve_usd": round(reserve, 2),
                    "volume_24h": round(vol24, 2),
                    "vlr": round(vol24 / reserve, 4) if reserve > 0 else None,
                }
            else:
                failures.append(f"{net}=no pool carried a reading")
        if i < len(networks) - 1:
            sleep(pause)
    if not out:
        return _report("unreachable",
                       "dex: no network returned pools (" + ", ".join(failures) + ")",
                       {}, None)
    detail = f"dex: {len(out)}/{len(networks)} network(s)"
    if failures:
        detail += " — missing: " + ", ".join(failures)
    return _report("live" if not failures else "partial", detail, out, 200)


def fetch_all(session: dict | None = None, getter=get, with_dex: bool = True) -> dict:
    """Every feed in this module, one report each, with a combined status line.

    A single call site in ``nightly.main`` so a feed added here does not need a fourth
    block of near-identical logging there, and so the artifact records the plan the
    session actually resolved to alongside what each feed did with it.
    """
    session = session if session is not None else open_session()
    feeds = {
        "trending": fetch_trending(session, getter),
        "categories": fetch_categories(session, getter),
        "global": fetch_global(session, getter),
        "stablecoins": fetch_stablecoins(session, getter),
    }
    if with_dex:
        feeds["dex"] = fetch_dex_networks()
    live = [n for n, r in feeds.items() if r["status"] == "live"]
    return {
        "session": {k: session.get(k) for k in ("plan", "status", "detail", "host")},
        "feeds": feeds,
        "live": live,
        "detail": (f"{len(live)}/{len(feeds)} feed(s) live on the "
                   f"{session.get('plan')} plan"),
    }


def _smoke() -> int:
    """Prove the shapes against the live API, the way cryptometer.py does.

    Every field name above came from a response this repository actually received, not
    from documentation — ``/search/trending`` returning ``"$15,982,922,471"`` where
    every other endpoint returns a number is the reason that distinction matters. This
    entrypoint is what keeps it true after a CoinGecko change.

    Exit code is non-zero only when a feed that should be free fails, so an unset key
    reports and returns success.
    """
    sess = open_session()
    print(f"[cg] session: {sess['plan']} / {sess['status']} — {sess['detail']}")
    rc = 0
    all_rep = fetch_all(sess)
    for name, rep in all_rep["feeds"].items():
        code = f" [HTTP {rep['http_status']}]" if rep.get("http_status") else ""
        print(f"[cg] {name}: {rep['status']} — {rep['detail']}{code}")
        if rep["status"] not in ("live", "empty"):
            rc = 1
    g = all_rep["feeds"]["global"]["data"]
    if g:
        print(f"[cg] anchors: BTC dom {g.get('btc_dominance')}%, "
              f"ex-BTC cap ${(g.get('total_mcap_ex_btc') or 0)/1e12:.3f}T")
    s = all_rep["feeds"]["stablecoins"]["data"]
    if s:
        print(f"[cg] stable float ${(s.get('total_mcap') or 0)/1e9:.1f}B, "
              f"velocity {s.get('velocity')}")
    dx = all_rep["feeds"].get("dex", {}).get("data") or {}
    for net, rec in sorted(dx.items(), key=lambda kv: -(kv[1]["volume_24h"] or 0))[:4]:
        print(f"[cg] dex {net}: ${rec['reserve_usd']/1e6:.1f}M depth, "
              f"${rec['volume_24h']/1e6:.1f}M 24h, VLR {rec['vlr']}")
    return rc


if __name__ == "__main__":
    sys.exit(_smoke())
