"""Cryptometer: the envelope traps, and what the API does not have.

The single most important fact this file records is a negative one. The integration was
specified as "replace Module E's funding with Cryptometer's cross-exchange weighted
funding", and Cryptometer has no funding endpoint — not under that name and not under
eleven others probed against the live host. Writing that down as a test is the only way
it stays written down: the next person to read the proposal will believe it otherwise,
and will spend a day discovering the same 403.
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name="cm_mod"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "cryptometer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._throttle = lambda: None          # tests do not wait on a rate limiter
    return mod


cm = _load()


def paid(mod):
    """Liquidations are behind a paid-endpoint opt-in now. These tests exercise the
    parsing, so they enable it explicitly rather than depending on the environment."""
    mod.paid_enabled = lambda: True
    return mod


def ok(rows):
    """A success envelope, with the string booleans the API actually sends."""
    return {"success": "true", "error": "false", "data": rows}


# ---------------------------------------------------------------------------
# the envelope, which cannot be read naively
# ---------------------------------------------------------------------------
def test_a_failure_envelope_is_not_mistaken_for_a_success():
    """`success` is the STRING "false", and every non-empty string is truthy in Python.

    A client that writes `if payload["success"]:` treats every failure as a success and
    hands back the `message` key as though it were data. The failure envelope also has
    no `data` key at all, so the naive path does not even fail loudly — it raises
    somewhere further down, in a function that looks unrelated.
    """
    m = _load("cm_fail")
    m._get_json = lambda url: {"success": "false", "message": "Invalid API Key"}
    rows, err = m.call("info", "badkey")
    assert rows is None and err == "Invalid API Key"
    # The trap, stated so the test explains itself.
    assert bool("false") is True


def test_a_success_envelope_yields_the_data_list_not_the_envelope():
    m = _load("cm_ok")
    m._get_json = lambda url: ok([{"open_interest": 284233058}])
    rows, err = m.call("open-interest", "k", e="binance_futures", market_pair="BTC-USDT")
    assert err is None
    assert rows == [{"open_interest": 284233058}]


def test_a_scalar_data_payload_is_still_returned_as_a_list():
    """`data` is documented as always an array, but a single scalar object arriving bare
    must not become an iteration over its keys."""
    m = _load("cm_scalar")
    m._get_json = lambda url: {"success": "true", "error": "false", "data": {"a": 1}}
    rows, err = m.call("info", "k")
    assert rows == [{"a": 1}] and err is None


def test_success_reported_without_data_is_an_error_not_an_empty_result():
    m = _load("cm_nodata")
    m._get_json = lambda url: {"success": "true", "error": "false"}
    rows, err = m.call("info", "k")
    assert rows is None and "no data key" in err


def test_the_api_key_never_appears_in_an_error_string():
    """403 is a catch-all for a bad path AND a misnamed parameter, so the request URL is
    logged because nothing else can distinguish them. The key must not ride along."""
    m = _load("cm_leak")
    def boom(url):
        raise RuntimeError("HTTP Error 403: Forbidden")
    m._get_json = boom
    rows, err = m.call("ls-ratio", "SUPERSECRET", pair="BTC-USDT")
    assert rows is None
    assert "SUPERSECRET" not in err
    assert "***" in err and "ls-ratio" in err


def test_the_trailing_slash_is_added_rather_than_trusted_to_call_sites():
    """Omitting it produces the same 403 as a path that does not exist."""
    seen = {}
    m = _load("cm_slash")
    m._get_json = lambda url: (seen.setdefault("url", url), ok([{}]))[1]
    m.call("liquidation-data-v2", "k", symbol="btc")
    assert "/liquidation-data-v2/?" in seen["url"]


def test_a_missing_key_is_unconfigured_rather_than_unreachable():
    """Four situations end in an empty table and one 'no data' cannot say which. An
    absent secret is not a network failure and must not read as one."""
    assert cm.fetch_liquidations("", {"BTC"})["status"] == "unconfigured"
    assert cm.fetch_positioning("", {"BTC"})["status"] == "unconfigured"
    assert cm.check_quota("")["status"] == "unconfigured"


# ---------------------------------------------------------------------------
# liquidations
# ---------------------------------------------------------------------------
def test_the_exchange_map_is_iterated_rather_than_indexed():
    """The docs show three exchanges; a live account may return others. Hardcoding the
    set would silently drop whatever it did not know about."""
    m = paid(_load("cm_liq"))
    m._get_json = lambda url: ok([{
        "binance_futures": {"longs": 6556449.8, "shorts": 8546450.39},
        "bitfinex": {"longs": 177375.14, "shorts": 50304.36},
        "some_new_venue": {"longs": 1000.0, "shorts": 0.0}}])
    rep = m.fetch_liquidations("k", {"BTC"})
    assert rep["status"] == "live"
    rec = rep["data"]["BTC"]
    assert "some_new_venue" in rec["venues"] and len(rec["venues"]) == 3
    assert rec["longs_usd"] == pytest.approx(6556449.8 + 177375.14 + 1000.0)


def test_the_imbalance_is_normalised_because_dollars_are_not_comparable():
    """Raw totals scale with the size of the market and rank the large caps every time.
    +1 is longs only, -1 shorts only."""
    m = paid(_load("cm_imb"))
    m._get_json = lambda url: ok([{"a": {"longs": 300.0, "shorts": 100.0}}])
    rec = m.fetch_liquidations("k", {"BTC"})["data"]["BTC"]
    assert rec["imbalance"] == pytest.approx(0.5)


def test_a_quiet_tape_has_no_imbalance_rather_than_a_balanced_one():
    """Zero imbalance claims both sides were liquidated equally. No liquidations at all
    is not that claim."""
    m = paid(_load("cm_quiet"))
    m._get_json = lambda url: ok([{"a": {"longs": 0.0, "shorts": 0.0}}])
    rec = m.fetch_liquidations("k", {"BTC"})["data"]["BTC"]
    assert rec["total_usd"] == 0.0
    assert rec["imbalance"] is None


def test_a_partial_sweep_reports_which_symbols_failed():
    """Silent truncation reads as 'covered everything' when it did not."""
    m = paid(_load("cm_partial"))
    calls = {"n": 0}
    def flaky(url):
        calls["n"] += 1
        if "eth" in url:
            raise RuntimeError("HTTP Error 500")
        return ok([{"a": {"longs": 1.0, "shorts": 1.0}}])
    m._get_json = flaky
    rep = m.fetch_liquidations("k", {"BTC", "ETH"})
    assert rep["status"] == "partial"
    assert set(rep["data"]) == {"BTC"}
    assert "1 failed" in rep["detail"]


def test_the_symbol_fan_out_is_bounded():
    """Every endpoint is per-symbol — no bulk variant, no pagination — so a 50-name board
    is 50 requests at 3/s. The cap is the difference between a nightly job and a rate
    limit."""
    m = paid(_load("cm_bound"))
    seen = []
    m._get_json = lambda url: (seen.append(url), ok([{"a": {"longs": 1, "shorts": 1}}]))[1]
    m.fetch_liquidations("k", {f"S{i:02d}" for i in range(40)}, limit=5)
    assert len(seen) == 5


# ---------------------------------------------------------------------------
# long/short — a column being restored, not added
# ---------------------------------------------------------------------------
def _retired_ls_ratio_test():
    """/ls-ratio/ returns ratio, buy and sell as STRINGS while close and delta are
    numbers. Trusting the type is how a ratio becomes a string in a numeric column."""
    m = _load("cm_ls")
    m._get_json = lambda url: ok([{"ratio": "1.8432", "buy": "64.8", "sell": "35.2",
                                   "close": 64000.0}])
    rec = m.fetch_long_short_ratio("k", {"BTC"})["data"]["BTC"]
    assert rec["ratio"] == pytest.approx(1.8432)
    assert isinstance(rec["ratio"], float) and isinstance(rec["buy_pct"], float)


def _retired_pair_param_test():
    """The three endpoints used here take `symbol`, `pair` and `market_pair`
    respectively and they are not interchangeable — the wrong one returns the same
    generic 403 as a nonexistent path, which looks like a missing endpoint."""
    seen = {}
    m = _load("cm_param")
    m._get_json = lambda url: (seen.setdefault("url", url), ok([{"ratio": "1.0"}]))[1]
    m.fetch_long_short_ratio("k", {"BTC"})
    assert "pair=BTC-USDT" in seen["url"]
    assert "symbol=" not in seen["url"] and "market_pair=" not in seen["url"]


# ---------------------------------------------------------------------------
# the negative result, recorded so it is not rediscovered
# ---------------------------------------------------------------------------
def test_this_module_does_not_claim_to_source_funding():
    """The integration was specified as replacing Module E's funding with Cryptometer's.
    The API has no funding endpoint — funding-rates-v2, funding-rate, funding-rates,
    funding, funding-rate-v2, funding-rate-v3, funding-data, funding-rates-data,
    fundingrate, funding-rate-history, predicted-funding-rate and funding-info were all
    probed live and every one returned the not-found signature, and the documentation
    lists none.

    The geo-blocking that motivated the proposal is real and is fixed in funding.py by
    venues that answer from a US host, not here.
    """
    src = (ROOT / "cryptometer.py").read_text(encoding="utf-8")
    for banned in ("funding-rate", "funding-rates-v2", "funding_apr", "annualize"):
        assert f'call("{banned}' not in src
    assert "does not have funding" in src.lower()
    for fn in ("fetch_liquidations", "fetch_positioning", "check_quota"):
        assert hasattr(cm, fn)
    assert not any(f.startswith("fetch_funding") for f in dir(cm))


def test_the_free_positioning_endpoint_is_the_one_that_is_wired():
    """28 of 38 endpoints are paid, and the first pass here wired two of them without
    knowing. `ls-ratio` and `liquidation-data-v2` are both paid; `long-shorts-data` is
    the only FREE positioning source and measures the same thing.

    This matters because a paid endpoint on a free key returns the same opaque 403 that a
    misnamed parameter does — so the failure would have been indistinguishable from a bug
    in this file.
    """
    src = (ROOT / "cryptometer.py").read_text(encoding="utf-8")
    assert 'call("long-shorts-data"' in src
    assert 'call("ls-ratio"' not in src, "the paid positioning endpoint is wired again"
    assert "long-shorts-data" in cm.FREE_ENDPOINTS
    assert "ls-ratio" not in cm.FREE_ENDPOINTS
    assert "liquidation-data-v2" not in cm.FREE_ENDPOINTS


def test_positioning_derives_the_ratio_and_refuses_to_divide_by_an_empty_book():
    """long-shorts-data returns absolute sizes, not a ratio. None rather than 1.0 on an
    empty short book: "half the accounts are long" is a real reading and must not be
    manufactured by an absent one."""
    m = _load("cm_pos")
    m._get_json = lambda url: ok([{"longs": 423000, "shorts": 4767000,
                                    "timestamp": "2026-08-17T04:00:00.000Z"}])
    rec = m.fetch_positioning("k", {"BTC"})["data"]["BTC"]
    assert rec["ratio"] == pytest.approx(423000 / 4767000, abs=1e-4)  # stored at 4dp
    assert rec["long_pct"] == pytest.approx(8.15, abs=0.02)
    m2 = _load("cm_pos0")
    m2._get_json = lambda url: ok([{"longs": 0, "shorts": 0}])
    assert m2.fetch_positioning("k", {"BTC"})["data"]["BTC"]["ratio"] is None


def test_positioning_uses_symbol_not_pair():
    """long-shorts-data takes `symbol`; ls-ratio takes `pair`. Not interchangeable, and
    the wrong one returns the generic 403 that looks like a missing endpoint."""
    seen = {}
    m = _load("cm_symparam")
    m._get_json = lambda url: (seen.setdefault("url", url),
                               ok([{"longs": 1, "shorts": 1}]))[1]
    m.fetch_positioning("k", {"BTC"})
    assert "symbol=btc" in seen["url"] and "pair=" not in seen["url"]


def test_nothing_here_reaches_the_score():
    """Observational on the same terms as open interest and the divergence quadrant.
    Adopting any of it would move the specification hash."""
    spec = importlib.util.spec_from_file_location("n_cm", ROOT / "nightly.py")
    nightly = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nightly)
    for fn in nightly.spec()["functions"].values():
        for field in ("liquidation", "imbalance", "cryptometer", "longs_usd"):
            assert field not in fn
