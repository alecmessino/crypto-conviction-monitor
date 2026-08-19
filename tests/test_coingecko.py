"""The credentialed CoinGecko session, and the four feeds behind Modules F-J.

The properties this file exists to hold:

  * A Demo key and a Pro key differ in HOST and in HEADER, and the key's own text says
    which it is nowhere — both are issued in the same ``CG-...`` shape. The plan is
    therefore probed, and sending the wrong header must not degrade silently to keyless
    while a working credential sits in the environment.
  * A failed fetch produces nothing, and the status says WHICH failure. 429 is a budget
    a key fixes, 401 is a credential that was refused, 404 is an endpoint the plan does
    not serve, and a column that cannot tell them apart cannot be used to diagnose any
    of them.
  * The response shapes here came from responses this repository actually received.
    ``/search/trending`` returns market cap as the string ``"$15,982,922,471"`` while
    every other endpoint returns a number, and every GeckoTerminal numeric is a string.
    A naive float() raises on both, and under a bare except that becomes a zero — which
    sorts a trending coin to the bottom of every liquidity screen it appears in. These
    are pinned against the real shapes, not against the documentation.
  * No network. Every test here injects its own transport.
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cg = _load("cg_client", "coingecko.py")


def _getter(status="live", data=None, detail="stub", http=200):
    """A stand-in for cg.get that never touches the network."""
    def _g(session, path, params=None, **kw):
        return {"status": status, "detail": detail, "data": data if data is not None else {},
                "http_status": http}
    return _g


# ---------------------------------------------------------------------------
# credential
# ---------------------------------------------------------------------------
def test_the_key_comes_only_from_the_environment():
    assert cg.api_key_from_env({}) is None
    assert cg.api_key_from_env({"COINGECKO_API_KEY": "CG-abc"}) == "CG-abc"


def test_either_documented_spelling_is_accepted():
    """CoinGecko's own documentation has called the secret both things. A pipeline that
    silently ignores one runs keyless while a key is configured."""
    assert cg.api_key_from_env({"COINGECKO_DEMO_API_KEY": "CG-d"}) == "CG-d"
    assert cg.api_key_from_env({"COINGECKO_PRO_API_KEY": "CG-p"}) == "CG-p"


def test_a_blank_secret_is_not_a_key():
    """An unset GitHub secret interpolates to an empty string, not to an absent
    variable. Treating that as configured sends an empty header on every call."""
    assert cg.api_key_from_env({"COINGECKO_API_KEY": "   "}) is None


def test_no_key_resolves_to_keyless_and_says_why():
    s = cg.open_session(key=None)
    assert s["plan"] == "keyless" and s["status"] == "unconfigured"
    assert s["headers"] == {}
    assert s["host"] == cg.PUBLIC_HOST
    assert "rate limit" in s["detail"]


def test_a_demo_key_is_probed_onto_the_public_host(monkeypatch):
    seen = []

    def fake(url, headers):
        seen.append((url, dict(headers)))
        return (200, {"gecko_says": "ok"}) if url.startswith(cg.PUBLIC_HOST) else (401, "")
    monkeypatch.setattr(cg, "_raw_get", fake)
    s = cg.open_session(key="CG-demo")
    assert s["plan"] == "demo" and s["status"] == "live"
    assert s["host"] == cg.PUBLIC_HOST
    assert s["headers"] == {"x-cg-demo-api-key": "CG-demo"}
    assert seen[0][0].endswith("/ping")


def test_a_pro_key_is_found_on_the_second_probe(monkeypatch):
    """The failure this whole function exists to remove: a Pro key sent to the public
    host comes back as a 4xx that looks exactly like an unavailable endpoint, and the
    pipeline degrades to keyless while a paid credential sits unused."""
    def fake(url, headers):
        if url.startswith(cg.PRO_HOST):
            return 200, {"gecko_says": "ok"}
        return 400, '{"error":"missing api key"}'
    monkeypatch.setattr(cg, "_raw_get", fake)
    s = cg.open_session(key="CG-pro")
    assert s["plan"] == "pro" and s["host"] == cg.PRO_HOST
    assert s["headers"] == {"x-cg-pro-api-key": "CG-pro"}


def test_a_key_neither_host_accepts_falls_back_loudly(monkeypatch):
    """Worse than no key: it would be sent on every call and refused on every call. The
    night must read as "the key does not work" rather than as an unexplained 429."""
    monkeypatch.setattr(cg, "_raw_get", lambda url, headers: (401, "nope"))
    s = cg.open_session(key="CG-bad")
    assert s["plan"] == "keyless" and s["status"] == "rejected"
    assert s["headers"] == {}, "a refused credential must not keep being sent"
    assert "demo=401" in s["detail"] and "pro=401" in s["detail"]


def test_the_probe_can_be_skipped_but_says_it_was_assumed():
    s = cg.open_session(key="CG-x", probe=False)
    assert s["status"] == "assumed" and s["plan"] == "demo"


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------
def test_a_rate_limit_is_retried_then_named(monkeypatch):
    calls = []
    monkeypatch.setattr(cg, "_raw_get", lambda u, h: (calls.append(u), (429, "slow down"))[1])
    rep = cg.get({"host": cg.PUBLIC_HOST, "headers": {}, "plan": "keyless"},
                 "/coins/markets", retries=3, sleep=lambda s: None)
    assert rep["status"] == "rate_limited" and rep["http_status"] == 429
    assert len(calls) == 3, "the backoff loop did not use all its retries"
    assert "keyless" in rep["detail"], "the likeliest cause is not reported"


def test_each_failure_keeps_its_own_identity(monkeypatch):
    """401 is a refused credential, 404 is an endpoint the plan does not serve, and a
    connection error is neither. Collapsing them loses the whole diagnosis."""
    for code, status in ((401, "unauthorized"), (403, "unauthorized"),
                         (404, "unavailable"), (500, "unreachable")):
        monkeypatch.setattr(cg, "_raw_get", lambda u, h, c=code: (c, "body"))
        rep = cg.get({"host": cg.PUBLIC_HOST, "headers": {}}, "/x", sleep=lambda s: None)
        assert rep["status"] == status, f"HTTP {code} reported as {rep['status']}"
        assert rep["data"] == {}


def test_a_network_error_is_unreachable_with_no_status(monkeypatch):
    monkeypatch.setattr(cg, "_raw_get", lambda u, h: (0, "name resolution failed"))
    rep = cg.get({"host": cg.PUBLIC_HOST, "headers": {}}, "/x", sleep=lambda s: None)
    assert rep["status"] == "unreachable" and rep["http_status"] is None


def test_a_retry_stops_as_soon_as_it_succeeds(monkeypatch):
    seq = [(429, ""), (200, {"ok": 1})]
    monkeypatch.setattr(cg, "_raw_get", lambda u, h: seq.pop(0))
    rep = cg.get({"host": cg.PUBLIC_HOST, "headers": {}}, "/x", sleep=lambda s: None)
    assert rep["status"] == "live" and rep["data"] == {"ok": 1}


# ---------------------------------------------------------------------------
# parsing — against the shapes the API actually sends
# ---------------------------------------------------------------------------
def test_a_formatted_dollar_string_is_a_number():
    """The /search/trending shape. Not in the documentation; this is what it sends."""
    assert cg._num("$15,982,922,471") == pytest.approx(15982922471.0)
    assert cg._num("5583322.509") == pytest.approx(5583322.509)   # GeckoTerminal
    assert cg._num(1234.5) == 1234.5


def test_an_unparseable_value_is_none_not_zero():
    """Under a bare except this becomes 0.0, and a trending coin with a zero market cap
    sorts to the bottom of every liquidity screen it appears in."""
    for bad in (None, "", "n/a", "null", "not a number", []):
        assert cg._num(bad) is None


def test_the_usd_member_is_read_not_the_btc_one():
    """price_change_percentage_24h on the trending payload is a dict of ~60
    denominations. The BTC-denominated member is a RELATIVE return and changes sign in a
    Bitcoin drawdown; reading it as the dollar move is plausible and wrong."""
    d = {"usd": 22.53, "btc": 13.61, "eth": 2.64}
    assert cg._usd(d) == 22.53


# ---------------------------------------------------------------------------
# feeds
# ---------------------------------------------------------------------------
TRENDING_PAYLOAD = {
    "coins": [
        {"item": {"id": "hyperliquid", "name": "Hyperliquid", "symbol": "HYPE",
                  "market_cap_rank": 10,
                  "data": {"price": 71.85, "market_cap": "$15,982,922,471",
                           "total_volume": "$1,110,948,258",
                           "price_change_percentage_24h": {"usd": 22.53, "btc": 13.61}}}},
        {"item": {"id": "dupe", "name": "Dupe", "symbol": "HYPE", "data": {}}},
    ],
    "categories": [{"id": 328, "name": "PolitiFi", "slug": "politifi",
                    "coins_count": "89",
                    "data": {"market_cap": 557266072.0,
                             "market_cap_change_percentage_24h": {"usd": 18.78}}}],
}


def test_trending_parses_the_real_shape():
    rep = cg.fetch_trending({}, _getter(data=TRENDING_PAYLOAD))
    hype = rep["data"]["coins"]["HYPE"]
    assert rep["status"] == "live"
    assert hype["rank"] == 1
    assert hype["mcap"] == pytest.approx(15982922471.0)
    assert hype["volume"] == pytest.approx(1110948258.0)
    assert hype["chg24h"] == pytest.approx(22.53)
    assert rep["data"]["categories"][0]["mcap"] == pytest.approx(557266072.0)


def test_a_duplicate_ticker_keeps_the_higher_ranked_listing():
    """Two listings can share a ticker. The one being searched for is the one ranked
    higher, and the map is keyed on symbol because the rest of the pipeline is."""
    rep = cg.fetch_trending({}, _getter(data=TRENDING_PAYLOAD))
    assert rep["data"]["coins"]["HYPE"]["id"] == "hyperliquid"


def test_a_failed_trending_fetch_is_not_an_empty_market():
    rep = cg.fetch_trending({}, _getter(status="rate_limited", detail="429", http=429))
    assert rep["status"] == "rate_limited" and rep["data"] == {}


CATEGORIES_PAYLOAD = [
    {"id": "decentralized-derivatives", "name": "Derivatives",
     "market_cap": 1.78e10, "market_cap_change_24h": 20.6, "volume_24h": 1.34e9,
     "top_3_coins_id": ["a", "b", "c"]},
    {"id": "usd-stablecoin", "name": "USD Stablecoin",
     "market_cap": 2.7e11, "market_cap_change_24h": 0.01, "volume_24h": 8e10},
    {"id": "smart-contract-platform", "name": "Smart Contract Platform",
     "market_cap": 2.03e12, "market_cap_change_24h": 9.4, "volume_24h": 8e10},
    {"id": "tiny-thing", "name": "Tiny", "market_cap": 1e6,
     "market_cap_change_24h": 90.0, "volume_24h": 1e5},
]


def test_categories_excludes_what_a_rotation_matrix_cannot_mean():
    """Pegged assets do not rotate — a stablecoin category moving is a depeg or a data
    error. And "Smart Contract Platform" holds BTC, ETH and BNB, so it is a
    two-trillion-dollar restatement of the market rather than a sector within it."""
    rep = cg.fetch_categories({}, _getter(data=CATEGORIES_PAYLOAD))
    assert set(rep["data"]) == {"decentralized-derivatives"}


def test_a_category_below_the_size_floor_is_dropped():
    """Without it, a four-coin category with a nine-million-dollar cap tops the
    leaderboard every night."""
    rep = cg.fetch_categories({}, _getter(data=CATEGORIES_PAYLOAD))
    assert "tiny-thing" not in rep["data"]


def test_sector_turnover_is_computed_where_both_legs_exist():
    rep = cg.fetch_categories({}, _getter(data=CATEGORIES_PAYLOAD))
    d = rep["data"]["decentralized-derivatives"]
    assert d["turnover"] == pytest.approx(1.34e9 / 1.78e10, rel=1e-3)


def test_everything_filtered_out_is_empty_not_live():
    rep = cg.fetch_categories({}, _getter(data=[CATEGORIES_PAYLOAD[1]]))
    assert rep["status"] == "empty"
    assert "floor" in rep["detail"]


GLOBAL_PAYLOAD = {"data": {
    "total_market_cap": {"usd": 2.479e12}, "total_volume": {"usd": 1.02e11},
    "market_cap_percentage": {"btc": 56.58, "eth": 11.16},
    "market_cap_change_percentage_24h_usd": 8.37}}


def test_global_derives_the_two_anchors_it_was_added_for():
    rep = cg.fetch_global({}, _getter(data=GLOBAL_PAYLOAD))
    d = rep["data"]
    assert d["btc_dominance"] == pytest.approx(56.58)
    assert d["total_mcap_ex_btc"] == pytest.approx(2.479e12 * (1 - 0.5658), rel=1e-4)
    assert d["eth_btc_dominance_ratio"] == pytest.approx(11.16 / 56.58, rel=1e-4)


def test_global_without_a_total_is_empty_rather_than_zero():
    rep = cg.fetch_global({}, _getter(data={"data": {}}))
    assert rep["status"] == "empty" and rep["data"] == {}


def test_ex_btc_is_none_when_dominance_is_missing():
    """Deriving it from a missing dominance would silently report the whole market cap
    as the alt market cap."""
    payload = {"data": {"total_market_cap": {"usd": 1e12}, "market_cap_percentage": {}}}
    rep = cg.fetch_global({}, _getter(data=payload))
    assert rep["data"]["total_mcap_ex_btc"] is None


STABLE_PAYLOAD = [
    {"symbol": "usdt", "market_cap": 1.4e11, "total_volume": 6e10,
     "market_cap_change_percentage_24h": 0.2},
    {"symbol": "usdc", "market_cap": 6e10, "total_volume": 2e10,
     "market_cap_change_percentage_24h": 0.1},
    {"symbol": "broken", "market_cap": None, "total_volume": 1e9},
]


def test_stablecoin_velocity_is_volume_over_float():
    rep = cg.fetch_stablecoins({}, _getter(data=STABLE_PAYLOAD))
    d = rep["data"]
    assert d["total_mcap"] == pytest.approx(2.0e11)
    assert d["velocity"] == pytest.approx(8e10 / 2.0e11)
    assert [i["symbol"] for i in d["issuers"]] == ["USDT", "USDC"]


def test_an_issuer_with_no_cap_is_skipped_rather_than_counted_as_zero():
    rep = cg.fetch_stablecoins({}, _getter(data=STABLE_PAYLOAD))
    assert all(i["symbol"] != "BROKEN" for i in rep["data"]["issuers"])


# ---------------------------------------------------------------------------
# on-chain
# ---------------------------------------------------------------------------
POOL_PAYLOAD = {"data": [
    {"attributes": {"reserve_in_usd": "5583322.509",
                    "volume_usd": {"h24": "75406715.177689"}}},
    {"attributes": {"reserve_in_usd": "1000000", "volume_usd": {"h24": "2000000"}}},
]}


def test_dex_parses_string_numerics_and_computes_vlr():
    """Every numeric on this API is a string. VLR is the reading it exists for: a pool
    turning over many multiples of its own depth daily is a thin pool being churned, not
    deep liquidity being used, and the two look identical in a volume column."""
    rep = cg.fetch_dex_networks(["eth"], raw_get=lambda u, h: (200, POOL_PAYLOAD),
                                sleep=lambda s: None)
    d = rep["data"]["eth"]
    assert d["pools"] == 2
    assert d["reserve_usd"] == pytest.approx(6583322.509, rel=1e-6)
    assert d["vlr"] == pytest.approx(77406715.18 / 6583322.509, rel=1e-4)


def test_a_partly_failing_sweep_reports_which_networks_are_missing():
    def fake(url, headers):
        return (200, POOL_PAYLOAD) if "/eth/" in url else (503, "down")
    rep = cg.fetch_dex_networks(["eth", "solana"], raw_get=fake, sleep=lambda s: None)
    assert rep["status"] == "partial"
    assert "solana" in rep["detail"] and "eth" in rep["data"]


def test_a_wholly_failed_sweep_is_unreachable():
    rep = cg.fetch_dex_networks(["eth"], raw_get=lambda u, h: (503, "down"),
                                sleep=lambda s: None)
    assert rep["status"] == "unreachable" and rep["data"] == {}


def test_the_dex_call_does_not_carry_the_coingecko_credential():
    """A different host that never asked for it. Sending the key there is a credential
    leaked to a third party, however friendly the vendor relationship."""
    seen = []
    cg.fetch_dex_networks(["eth"], raw_get=lambda u, h: (seen.append(h), (200, POOL_PAYLOAD))[1],
                          sleep=lambda s: None)
    assert all("api-key" not in k for h in seen for k in h)
    assert seen[0] == cg.GT_HEADERS


def test_the_geckoterminal_version_header_is_pinned():
    """Without it the API serves whatever its current default is, and a schema change
    arrives as a silent reshape of the fields above rather than a version bump."""
    assert "version=" in cg.GT_HEADERS["Accept"]


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------
def test_fetch_all_records_the_plan_beside_what_each_feed_did(monkeypatch):
    monkeypatch.setattr(cg, "fetch_dex_networks", lambda *a, **k: cg._report("live", "d", {"eth": {}}))
    sess = {"host": cg.PUBLIC_HOST, "headers": {}, "plan": "keyless",
            "status": "unconfigured", "detail": "-"}
    rep = cg.fetch_all(sess, _getter(data=GLOBAL_PAYLOAD))
    assert rep["session"]["plan"] == "keyless"
    assert set(rep["feeds"]) == {"trending", "categories", "global", "stablecoins", "dex"}
    assert "keyless" in rep["detail"]


def test_no_feed_ever_raises_on_a_dead_transport(monkeypatch):
    """The nightly writes a ledger. A context feed must never be the reason it does
    not — this is the rule cryptometer.py and the Dune split already established.

    This also pins that substituting the module's transport actually reaches every
    feed. It did not: fetch_dex_networks bound _raw_get as a default argument, which
    Python evaluates once at definition, so this patch missed it entirely and the
    "offline" test made eight real requests.
    """
    monkeypatch.setattr(cg, "_raw_get", lambda u, h: (0, "connection refused"))
    monkeypatch.setattr(cg.time, "sleep", lambda s: None)
    # If either resolution regresses to a default argument this test starts making real
    # requests and taking twenty seconds, which is how the bug was found in the first
    # place. The wall-clock assertion below is the tripwire for that.
    import time as _t
    _t0 = _t.monotonic()
    sess = {"host": cg.PUBLIC_HOST, "headers": {}, "plan": "keyless"}
    rep = cg.fetch_all(sess, lambda *a, **k: cg.get(*a, sleep=lambda s: None, **k))
    for name, r in rep["feeds"].items():
        assert r["status"] != "live", name
        assert r["data"] == {}, name
    assert rep["live"] == []
    assert _t.monotonic() - _t0 < 5.0, (
        "a feed slept or reached the network despite both being substituted — check for "
        "a transport or sleep captured as a default argument")
