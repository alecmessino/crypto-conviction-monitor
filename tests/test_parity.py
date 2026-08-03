"""Frontend<->backend parity (mandatory before every deploy) + frozen regression.

The dashboard's `launch_skew.html` `conviction()`/`build()` must produce the SAME
conviction + component attribution as the nightly `score()`. If they ever diverge,
historical results become untrustworthy and the live terminal silently disagrees
with the persisted Index. This check is enforced in CI — a parity break fails the build.

It imports the REAL nightly.score() and compares against a faithful port of the
frontend JS math (liquidityFit / depthScore / conviction with blended RS vs BTC).

Runs two ways:
  * `python -m pytest tests/test_parity.py`   (local / dev CI, needs pytest)
  * `python tests/test_parity.py`            (CI nightly gate — NO pytest needed;
                                             exits non-zero on any failure so the
                                             workflow step fails and blocks the commit)
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_spec = importlib.util.spec_from_file_location("nightly", os.path.join(os.path.dirname(_HERE), "nightly.py"))
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


# ---- faithful port of launch_skew.html frontend math ----
def liquidity_fit(turn):  # turn is a FRACTION (0.05 = 5%); mirrors nightly Module A
    if turn <= 0:
        return 0
    if turn <= 0.30:
        return 10 + (turn / 0.30) * 20
    if turn <= 0.60:
        return 30 - abs(turn - 0.45) / 0.15 * 6
    if turn <= 1.20:
        return 20 - (turn - 0.60) / 0.60 * 12
    return max(2, 8 - (turn - 1.20) * 4)


import math
_math_log10 = math.log10


def depth_score(mc):
    if not mc:
        return 0
    return max(0, min(1, (_math_log10(mc) - 6) / 4.0))


def frontend_conviction(vol, mc, chg, perp, rs_blend):
    """Exact port of launch_skew.html conviction(t, perp, rsBlend)."""
    turn = vol / mc
    a = liquidity_fit(turn)
    ag = 15 if abs(chg) < 5 else 10 if abs(chg) < 15 else 5
    era = 5 / ag
    b = 20 if era < 0.7 else 15 if era < 1.0 else 10 if era < 1.5 else 5 if era < 2.0 else 0
    cd = depth_score(mc) * 20
    cm_raw = 12.0 if rs_blend is None else max(4, min(20, 12 + (rs_blend or 0) * 0.4))
    cm = cm_raw * perp
    conv = max(0, min(100, round(a + b + cd + cm)))
    comp = {
        "liquidity": round(a, 1), "era": round(b, 1), "depth": round(cd, 1),
        "momentum": round(cm, 1), "rsBlend": round((rs_blend or 0), 2),
    }
    return conv, comp


def frontend_rs_blend(t, btc):
    def pct(tf):
        return (t.get(f"price_change_percentage_{tf}d_in_currency") or 0) - \
               (btc.get(f"price_change_percentage_{tf}d_in_currency") or 0)
    rs7, rs14, rs30, rs200 = pct(7), pct(14), pct(30), pct(200)
    return 0.30 * rs7 + 0.25 * rs14 + 0.25 * rs30 + 0.20 * rs200


# ---- shared fixture: BTC reference + fixed assets (deterministic inputs) ----
BTC = {
    "symbol": "BTC", "market_cap": 1.3e12, "total_volume": 3e10,
    "price_change_percentage_24h": 2.0,
    "price_change_percentage_7d_in_currency": 2.0,
    "price_change_percentage_14d_in_currency": 1.0,
    "price_change_percentage_30d_in_currency": 3.0,
    "price_change_percentage_200d_in_currency": -30.0,
}
FIXTURE = {
    "ETH": {"market_cap": 4e11, "total_volume": 1.5e10, "price_change_percentage_24h": -1.0,
            "price_change_percentage_7d_in_currency": 10.0, "price_change_percentage_14d_in_currency": 5.0,
            "price_change_percentage_30d_in_currency": 20.0, "price_change_percentage_200d_in_currency": 15.0},
    "SOL": {"market_cap": 8e10, "total_volume": 4e9, "price_change_percentage_24h": 3.0,
            "price_change_percentage_7d_in_currency": 18.0, "price_change_percentage_14d_in_currency": 12.0,
            "price_change_percentage_30d_in_currency": 30.0, "price_change_percentage_200d_in_currency": 40.0},
    "ADA": {"market_cap": 1.2e10, "total_volume": 6e8, "price_change_percentage_24h": 0.5,
            "price_change_percentage_7d_in_currency": 17.0, "price_change_percentage_14d_in_currency": 8.0,
            "price_change_percentage_30d_in_currency": 3.0, "price_change_percentage_200d_in_currency": -20.0},
    "LINK": {"market_cap": 9e9, "total_volume": 5e8, "price_change_percentage_24h": -2.0,
             "price_change_percentage_7d_in_currency": -5.0, "price_change_percentage_14d_in_currency": -3.0,
             "price_change_percentage_30d_in_currency": 8.0, "price_change_percentage_200d_in_currency": 25.0},
}


def _asset(sym, perp_mult=1.0):
    d = dict(FIXTURE[sym])
    d["symbol"] = sym
    return d, perp_mult


def check_frontend_backend_parity():
    """Frontend conviction + component attribution must equal nightly score()."""
    for sym in FIXTURE:
        t, _ = _asset(sym)
        rs_blend = frontend_rs_blend(t, BTC)
        fe_conv, fe_comp = frontend_conviction(t["total_volume"], t["market_cap"],
                                                t["price_change_percentage_24h"], 1.0, rs_blend)
        # backend: score(t, perps_map={}, btc=BTC) -> (era, total, sig, comp)
        era, be_conv, sig, be_comp = nightly.score(t, {}, BTC)
        assert fe_conv == be_conv, f"{sym}: frontend {fe_conv} != backend {be_conv}"
        # component attribution must match (this is what the drawer renders).
        # frontend comp uses key "rsBlend"; backend uses "rs_blend" — compare values.
        for k_fe, k_be in [("liquidity", "liquidity"), ("era", "era"),
                            ("depth", "depth"), ("momentum", "momentum")]:
            assert fe_comp[k_fe] == round(be_comp[k_be], 1), \
                f"{sym}: {k_fe} fe={fe_comp[k_fe]} be={be_comp[k_be]}"
        assert abs(fe_comp["rsBlend"] - be_comp["rs_blend"]) < 1e-9, \
            f"{sym}: rsBlend fe={fe_comp['rsBlend']} be={be_comp['rs_blend']}"


def check_parity_under_perp_overlay():
    """LAVL perp overlay must agree. Frontend reads PERP[sym] = backend's
    lavl_perp_mult() output, so derive the multiplier the same way."""
    for sym, fr in [("SOL", -0.002), ("ETH", 0.002)]:
        t, _ = _asset(sym)
        # backend-style perps_map: funding_rate drives the multiplier
        perps_map = {sym: {"funding_rate": fr, "open_interest": 0.0}}
        pm = nightly.lavl_perp_mult(sym, perps_map)  # what the frontend actually uses
        rs_blend = frontend_rs_blend(t, BTC)
        fe_conv, _ = frontend_conviction(t["total_volume"], t["market_cap"],
                                          t["price_change_percentage_24h"], pm, rs_blend)
        era, be_conv, sig, be_comp = nightly.score(t, perps_map, BTC)
        assert fe_conv == be_conv, f"{sym}@perp{pm}: frontend {fe_conv} != backend {be_conv}"


# ---- frozen regression: the scoring engine must not silently drift ----
FROZEN_CONVICTION = {
    "ETH": 71,
    "SOL": 73,
    "ADA": 69,
    "LINK": 69,
}


def check_frozen_conviction_regression():
    """Replacing the scoring engine (RS refactor) — pin exact outputs.

    If a future edit changes these, the check fails and forces a conscious
    sign-off rather than an accidental model shift.
    """
    for sym, expected in FROZEN_CONVICTION.items():
        t, _ = _asset(sym)
        era, conv, sig, comp = nightly.score(t, {}, BTC)
        assert conv == expected, f"{sym}: expected {expected}, got {conv} (comp={comp})"


# ---- dual-mode entrypoint ----
def _run_all():
    failures = []
    for name, fn in [
        ("frontend/backend parity", check_frontend_backend_parity),
        ("parity under perp overlay", check_parity_under_perp_overlay),
        ("frozen conviction regression", check_frozen_conviction_regression),
    ]:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:  # noqa: BLE001 - report any unexpected error as a failure
            print(f"  ERROR {name}: {e}")
            failures.append(name)
    return failures


if __name__ == "__main__":
    print("Parity + regression check (standalone mode, no pytest required):")
    failures = _run_all()
    if failures:
        print(f"\nFAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("\nALL PARITY + REGRESSION CHECKS PASSED")
    sys.exit(0)
else:
    # pytest mode: expose the same logic as decorated test functions.
    import pytest  # only needed when run under pytest

    def test_frontend_backend_parity():
        check_frontend_backend_parity()

    def test_parity_under_perp_overlay():
        check_parity_under_perp_overlay()

    def test_frozen_conviction_regression():
        check_frozen_conviction_regression()
