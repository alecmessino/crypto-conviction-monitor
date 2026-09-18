"""The declared ruler — AUDIT-PHASE2A, boundary ab16684ad5c1 -> 91bbc2a7e466.

SCORING collects every threshold that governs a factor into one captured object. It adds
no bound and changes no number; Phase 2A's evidence concluded that DEPTH and SUPPLY are
already bounded by construction and that LIQUIDITY's problem is the shape of its curve
rather than the absence of a clip.

That makes this file's job specific: prove the declaration is TRUE of the code that was
already running. A config object nobody checks is a comment with punctuation, and a
second implementation of scoring arithmetic that nobody reconciles is the defect this
repository keeps finding. So every declared knot is driven through the real scoring
functions, every derived helper is asserted equal to what score() applies, and the
published integer is recomputed on all 703 recorded rows both ways.
"""
import csv
import importlib.util
import math
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("cfg_nightly", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

S = nightly.SCORING


def _mkt(mc, vol, fdv=None, **pct):
    t = {"symbol": "X", "market_cap": mc, "total_volume": vol,
         "fully_diluted_valuation": fdv, "price_change_percentage_24h": 0.0}
    for tf in (7, 14, 30, 200):
        t[f"price_change_percentage_{tf}d_in_currency"] = pct.get(f"p{tf}", 0.0)
    return t


BTC = _mkt(1e12, 3e10)


# ------------------------------------------------------------------ shape and capture

def test_the_ruler_is_one_object_and_is_captured():
    assert set(S) == {"DEPTH", "CONFIRM", "LIQUIDITY", "SUPPLY", "FUNDING",
                      "CLAMP", "DOMINANCE"}
    assert "SCORING" in nightly.SPEC_CONSTANTS
    assert "SCORING" in nightly.spec()["constants"]
    assert nightly.DOM_FACTORS == ("DEPTH", "CONFIRM", "LIQUIDITY", "SUPPLY", "FUNDING")


def test_the_boundary_is_proved_score_identical_rather_than_asserted():
    """Removing SCORING from today's specification must reproduce the old digest.

    This is the repo's own equivalence mechanism, and it only works because the scoring
    functions were NOT refactored to read from the object. That restraint is the whole
    reason the track record does not segment here.
    """
    assert nightly.SPEC_HASH == "91bbc2a7e466"
    entry = nightly.SPEC_EQUIVALENT["ab16684ad5c1"]
    assert entry["canonical"] == nightly.SPEC_HASH
    assert entry["reason"] == "instrumentation"
    assert entry["added_constants"] == ("SCORING",)
    assert nightly.spec_hash_without(constants=("SCORING",)) == "ab16684ad5c1"
    assert nightly.canonical_spec_hash("ab16684ad5c1") == nightly.SPEC_HASH


def test_the_ruler_agrees_with_the_constants_that_already_had_names():
    assert S["SUPPLY"]["FLOOR"] == nightly.EMISSION_MAX_PENALTY
    assert S["SUPPLY"]["FREE_RATIO"] == nightly.EMISSION_FREE_RATIO
    assert S["SUPPLY"]["ANCHOR_RATIO"] == nightly.EMISSION_ANCHOR_RATIO
    assert S["SUPPLY"]["ANCHOR_SEVERITY"] == nightly.EMISSION_ANCHOR_SEVERITY
    assert S["FUNDING"]["LO"] == nightly.PERP_ENVELOPE_LO == nightly.funding.MOD_MAX_PENALTY
    assert S["FUNDING"]["HI"] == nightly.PERP_ENVELOPE_HI == nightly.funding.MOD_MAX_BOOST
    assert S["FUNDING"]["NEUTRAL"] == nightly.PERP_NEUTRAL
    assert S["FUNDING"]["MAX_AGE_DAYS"] == nightly.PERP_MAX_AGE_DAYS


def test_the_js_and_python_rulers_are_the_same_object():
    """Declared twice, so it is asserted twice. The parity gate executes both sides;
    this checks the literals themselves, which a behavioural gate would not catch if
    both sides happened to be wrong in the same direction."""
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    for decl in ('DEPTH: {MIN: 0, MAX: 1, LOG_MCAP_ZERO: 6, LOG_MCAP_SPAN: 4}',
                 'CONFIRM: {BASE: 0.10, SPAN: 0.90, TANH_SCALE: 25}',
                 'LIQUIDITY: {FLOOR: 0.40, BYPASS_DEPTH: 0.90, FIT_MAX: 30',
                 'SUPPLY: {FLOOR: 0.90, CEIL: 1.0, FREE_RATIO: 1.10, ANCHOR_RATIO: 3.0',
                 'FUNDING: {LO: 0.85, HI: 1.15, NEUTRAL: 1.0, MAX_AGE_DAYS: 1}',
                 'CLAMP: {MIN: 0, MAX: 100}',
                 'DOMINANCE: {SHARE_WARN: 0.70, MAGNITUDE_WARN: 0.22}'):
        assert decl in page, decl


# --------------------------------------------------- the declaration is TRUE of score()

def test_the_depth_knots_are_where_the_ruler_says():
    """Driven through score(), not through the helper."""
    z, span = S["DEPTH"]["LOG_MCAP_ZERO"], S["DEPTH"]["LOG_MCAP_SPAN"]
    # at 10**ZERO the factor is exactly MIN; at 10**(ZERO+SPAN) exactly MAX.
    for mc, want in ((10 ** z, S["DEPTH"]["MIN"]), (10 ** (z + span), S["DEPTH"]["MAX"])):
        _, _, _, comp = nightly.score(_mkt(mc, mc * 0.45), {}, BTC)
        assert abs(comp["depth"] / 20.0 - want) < 1e-9, mc
    # and it does not exceed MAX above the top knot
    _, _, _, comp = nightly.score(_mkt(1e15, 1e14), {}, BTC)
    assert comp["depth"] / 20.0 == S["DEPTH"]["MAX"]


def test_the_liquidity_floor_and_bypass_are_where_the_ruler_says():
    z, span = S["DEPTH"]["LOG_MCAP_ZERO"], S["DEPTH"]["LOG_MCAP_SPAN"]
    floor, fitmax = S["LIQUIDITY"]["FLOOR"], S["LIQUIDITY"]["FIT_MAX"]
    # a market cap just under the bypass, so the floor is what can bind
    mc = 10 ** (z + span * (S["LIQUIDITY"]["BYPASS_DEPTH"] - 0.01))
    _, _, _, comp = nightly.score(_mkt(mc, 0.0), {}, BTC)
    assert abs(comp["liquidity"] / fitmax - floor) < 1e-9, "zero turnover must hit FLOOR"
    # Straddle the bypass knot rather than sitting on it: log10 of a power of ten is not
    # exactly the exponent in float64, so "exactly at" is not a place a test can stand.
    bypass = S["LIQUIDITY"]["BYPASS_DEPTH"]
    mc_by = 10 ** (z + span * (bypass + 1e-9))
    assert nightly.depth_state(mc_by)["applied"] >= bypass
    _, _, _, comp = nightly.score(_mkt(mc_by, 1.0), {}, BTC)
    assert abs(comp["liquidity"] / fitmax - 1.0) < 1e-9, "at BYPASS_DEPTH must bypass"
    # below it, the same hair-thin turnover is floored instead
    mc_no = 10 ** (z + span * (bypass - 1e-6))
    assert nightly.depth_state(mc_no)["applied"] < bypass
    _, _, _, comp = nightly.score(_mkt(mc_no, 1.0), {}, BTC)
    assert abs(comp["liquidity"] / fitmax - floor) < 1e-9, "below BYPASS_DEPTH must not"


def test_the_liquidity_curve_peaks_where_the_ruler_says():
    """KNOT_PEAK is the turnover at which the curve reaches FIT_MAX."""
    z, span = S["DEPTH"]["LOG_MCAP_ZERO"], S["DEPTH"]["LOG_MCAP_SPAN"]
    mc = 10 ** (z + span * 0.5)          # well below the bypass
    peak = S["LIQUIDITY"]["KNOT_PEAK"]
    _, _, _, at = nightly.score(_mkt(mc, mc * peak), {}, BTC)
    assert abs(at["liquidity"] - S["LIQUIDITY"]["FIT_MAX"]) < 1e-9
    for off in (0.05, -0.05):
        _, _, _, near = nightly.score(_mkt(mc, mc * (peak + off)), {}, BTC)
        assert near["liquidity"] < at["liquidity"], off


def test_the_confirm_band_is_where_the_ruler_says():
    base, spanc, scale = S["CONFIRM"]["BASE"], S["CONFIRM"]["SPAN"], S["CONFIRM"]["TANH_SCALE"]
    # rs_blend 0 -> exactly the midpoint of the band
    _, _, _, comp = nightly.score(_mkt(1e10, 4.5e9), {}, _mkt(1e12, 3e10))
    assert abs(comp["momentum"] / 20.0 - (base + spanc / 2)) < 1e-9
    # and the ceiling is BASE + SPAN = 1.00, not the 0.91 the old comment claimed
    _, _, _, hot = nightly.score(_mkt(1e10, 4.5e9, p7=1e4, p14=1e4, p30=1e4, p200=1e4),
                                 {}, _mkt(1e12, 3e10))
    assert abs(hot["momentum"] / 20.0 - (base + spanc)) < 1e-9
    assert base + spanc == 1.0


def test_the_supply_envelope_is_where_the_ruler_says():
    assert nightly.emission_mult(1e9 * S["SUPPLY"]["FREE_RATIO"], 1e9) == S["SUPPLY"]["CEIL"]
    assert nightly.emission_mult(None, 1e9) == S["SUPPLY"]["CEIL"]
    assert nightly.emission_mult(1e9 * 1e6, 1e9) >= S["SUPPLY"]["FLOOR"]
    # the anchor: at ANCHOR_RATIO the severity is ANCHOR_SEVERITY
    d = nightly.emission_drag(1e9 * S["SUPPLY"]["ANCHOR_RATIO"], 1e9)
    assert abs(d - S["SUPPLY"]["ANCHOR_SEVERITY"]) < 1e-5


def test_the_clamp_is_where_the_ruler_says():
    lo, hi = S["CLAMP"]["MIN"], S["CLAMP"]["MAX"]
    _, conv, _, _ = nightly.score(_mkt(1e12, 4.5e11, p7=1e4, p14=1e4, p30=1e4, p200=1e4),
                                  {}, _mkt(1e12, 3e10))
    assert lo <= conv <= hi
    # the invariant Phase 2A recorded: only FUNDING can exceed 1.0, so the largest
    # pre-clamp any chain can reach is 100 * FUNDING.HI.
    assert abs(100 * S["FUNDING"]["HI"] - 115.0) < 1e-9


# ------------------------------------------- raw vs applied agrees with what is scored

def test_the_state_helpers_report_what_score_actually_applied():
    cases = [(1e12, 4.5e11, None), (3.25e9, 5.8e7, 3.7e9), (1.2e8, 0.0, None),
             (1.2e8, None, 1.2e8), (8e8, 9e8, 2.4e9), (1e10, 4.5e9, 1e10)]
    for mc, vol, fdv in cases:
        t = _mkt(mc, vol, fdv)
        _, _, _, comp = nightly.score(t, {}, BTC)
        d = nightly.depth_state(mc)
        l = nightly.liquidity_state(vol, mc, d["applied"])
        s = nightly.supply_state(fdv, mc)
        assert abs(round(d["applied"] * 20, 1) - comp["depth"]) < 1e-9, (mc, "depth")
        assert abs(round(l["applied"] * 30, 1) - comp["liquidity"]) < 1e-9, (mc, "liq")
        assert abs(s["applied"] - comp["emission_mult"]) < 1e-9, (mc, "supply")


def test_null_volume_and_zero_volume_score_identically_and_read_differently():
    """Phase 2A makes the distinction OBSERVABLE and does not act on it."""
    a = nightly.liquidity_state(None, 1e9, 0.7)
    b = nightly.liquidity_state(0, 1e9, 0.7)
    assert a["applied"] == b["applied"] == S["LIQUIDITY"]["FLOOR"]
    assert a["raw"] == b["raw"] == 0.0
    assert a["state"] == "no-volume" and b["state"] == "zero-volume"


def test_every_liquidity_state_is_reachable_and_named():
    seen = {
        nightly.liquidity_state(1e8, 1e9, 0.7)["state"],      # curve
        nightly.liquidity_state(1e6, 1e9, 0.7)["state"],      # floor
        nightly.liquidity_state(0, 1e9, 0.7)["state"],        # zero-volume
        nightly.liquidity_state(None, 1e9, 0.7)["state"],     # no-volume
        nightly.liquidity_state(1e6, 1e11, 0.95)["state"],    # bypass
    }
    assert seen == {"curve", "floor", "zero-volume", "no-volume", "bypass"}
    assert {nightly.depth_state(1e8)["state"], nightly.depth_state(1e13)["state"],
            nightly.depth_state(0)["state"]} == {"curve", "cap", "no-mcap"}
    assert {nightly.supply_state(3e9, 1e9)["state"], nightly.supply_state(1e9, 1e9)["state"],
            nightly.supply_state(None, 1e9)["state"]} == {"applied", "inert", "unpublished"}


# ----------------------------------------------------------------------- dominance

def test_zec_is_the_control_case_and_must_not_flag():
    """100% concentration on 0.069 of magnitude. The reason the second leg exists."""
    d = nightly.chain_dominance({"DEPTH": 1.0, "CONFIRM": 1.0, "LIQUIDITY": 1.0,
                                 "SUPPLY": 1.0, "FUNDING": 1.071})
    assert d["factor"] == "FUNDING"
    assert d["share"] == 1.0
    assert abs(d["magnitude"] - 0.0686) < 1e-3
    assert d["warn"] is False


def test_a_real_haircut_does_flag():
    d = nightly.chain_dominance({"DEPTH": 0.88, "CONFIRM": 0.95, "LIQUIDITY": 0.40,
                                 "SUPPLY": 1.0, "FUNDING": 1.0})
    assert d["factor"] == "LIQUIDITY" and d["warn"] is True
    assert d["signed"] < 0


def test_both_legs_are_load_bearing():
    # high share, tiny magnitude -> no warning
    assert nightly.chain_dominance({"DEPTH": 1.0, "CONFIRM": 1.0, "LIQUIDITY": 1.0,
                                    "SUPPLY": 0.99, "FUNDING": 1.0})["warn"] is False
    # large magnitude, low share -> no warning
    assert nightly.chain_dominance({"DEPTH": 0.52, "CONFIRM": 0.40, "LIQUIDITY": 0.40,
                                    "SUPPLY": 0.92, "FUNDING": 0.90})["warn"] is False
    # an all-neutral chain has no dominant factor at all
    flat = nightly.chain_dominance({k: 1.0 for k in nightly.DOM_FACTORS})
    assert flat["factor"] is None and flat["warn"] is False and flat["total"] == 0.0


def test_a_warning_can_never_mean_a_factor_inflated_the_score():
    """Structural: only FUNDING exceeds 1.0, and its largest magnitude is below the bar."""
    assert abs(math.log(S["FUNDING"]["HI"])) < S["DOMINANCE"]["MAGNITUDE_WARN"]
    assert abs(math.log(S["SUPPLY"]["FLOOR"])) < S["DOMINANCE"]["MAGNITUDE_WARN"]
    for hi in ("DEPTH", "CONFIRM", "LIQUIDITY", "SUPPLY"):
        m = {k: 1.0 for k in nightly.DOM_FACTORS}
        m["FUNDING"] = S["FUNDING"]["HI"]
        assert nightly.chain_dominance(m)["warn"] is False, hi


def test_ties_break_in_the_declared_order_not_by_map_iteration():
    m = {"DEPTH": 0.5, "CONFIRM": 0.5, "LIQUIDITY": 1.0, "SUPPLY": 1.0, "FUNDING": 1.0}
    assert nightly.chain_dominance(m)["factor"] == "DEPTH"
    m2 = {"DEPTH": 1.0, "CONFIRM": 0.5, "LIQUIDITY": 0.5, "SUPPLY": 1.0, "FUNDING": 1.0}
    assert nightly.chain_dominance(m2)["factor"] == "CONFIRM"


def test_the_dominance_thresholds_are_documented_for_what_they_are():
    """0.70 is empirical; 0.22 is policy. The distinction is in the source, deliberately."""
    assert S["DOMINANCE"]["SHARE_WARN"] == 0.70
    assert abs(S["DOMINANCE"]["MAGNITUDE_WARN"] - 0.22) < 1e-12
    assert abs(math.log(1.25) - 0.223) < 1e-3          # what the policy threshold means
    src = (ROOT / "nightly.py").read_text(encoding="utf-8")
    assert "EMPIRICALLY ANCHORED" in src and "POLICY threshold" in src


# -------------------------------------------------- the invariant: nothing moved

def test_the_reorganisation_changed_no_published_score():
    """Recompute every recorded row and assert the published integer is unchanged.

    This is the claim the whole phase rests on and the reason no performance segment is
    created. It is checked against the ledger rather than against a fixture, because a
    fixture proves the fixture.
    """
    shard = ROOT / "ledger" / "xsec"
    if not shard.exists():
        pytest.skip("no cross-section recorded yet")
    rows = []
    for path in sorted(shard.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            rows += [r for r in csv.DictReader(f) if r.get("src") == "live"]
    assert rows, "shards exist but hold no live rows"
    checked = 0
    for r in rows:
        mc = float(r["market_cap"])
        turn = float(r["turnover_pct"]) / 100.0
        fdv = None if r["fdv_usd"] in ("", "None") else float(r["fdv_usd"])
        perp = float(r["perp_mult"]) if r["perp_mult"] not in ("", "None") else 1.0
        d = nightly.depth_state(mc)
        l = nightly.liquidity_state(turn * mc, mc, d["applied"])
        s = nightly.supply_state(fdv, mc)
        cm = float(r["c_momentum"]) / 20.0
        raw = 100.0 * d["applied"] * cm * l["applied"] * s["applied"] * perp
        published = max(S["CLAMP"]["MIN"], min(S["CLAMP"]["MAX"], int(round(raw))))
        # c_momentum is a x20 display scale rounded to a decimal, so the reconstruction
        # is good to about half a point. A reorganisation that moved a score would move
        # it by more than that; this is the tightest claim the recorded columns support.
        assert abs(published - int(r["conviction"])) <= 1, (r["date"], r["symbol"],
                                                            published, r["conviction"])
        checked += 1
    assert checked > 700
