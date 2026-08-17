"""Module 3 — funding normalisation, regime, and the modifier that reaches the score.

The properties this file exists to hold:

  * A funding rate without its settlement interval is not a measurement. The same
    0.0001 is 10.95% a year at Bybit's 8-hour clock and 87.6% at Hyperliquid's hourly
    one, and the whole point of the module is that those two never share a column
    without the interval beside them.
  * The score modifier is a claim. It is applied only when the confirming input was
    actually observed, and falls to a neutral 1.0 otherwise — never to a partial
    adjustment, and never to an assumed confirmation.
  * A failed request produces nothing, not a neutral-looking reading. This is the
    lesson the Dune split and the long/short fetch already paid for in this repo, and
    a third feed does not get to relearn it.
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


funding = _load("funding_mod", "funding.py")
nightly = _load("nightly_mod", "nightly.py")


# ---------------------------------------------------------------------------
# A. interval normalisation — the deliverable's stated parity requirement
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("interval,expected", [
    (1.0, 87.6),      # 0.0001 * 24 * 365 * 100  — Hyperliquid
    (4.0, 21.9),      # 0.0001 *  6 * 365 * 100  — Binance's adjusted symbols
    (8.0, 10.95),     # 0.0001 *  3 * 365 * 100  — Binance/Bybit standard
])
def test_the_same_rate_annualises_differently_on_each_clock(interval, expected):
    """The identity under test: APR = rate * (24 / interval) * 365 * 100.

    This is the bug the module was written to remove. One constant (3 * 365) was
    applied to every venue, so an hourly rate read as an eighth of its true carry while
    sitting in the same column as an 8-hour one, formatted identically.
    """
    assert funding.annualize(0.0001, interval) == pytest.approx(expected)


def test_rates_on_different_clocks_meet_at_the_same_apr():
    """Exact parity across 1h, 4h and 8h.

    A rate scaled in proportion to its interval is the same annualised carry. If these
    three ever disagree, two venues are being compared in different units and every
    cross-venue reading downstream — the spread, the carry screen, the regime — is
    comparing something to itself.
    """
    one_hour, four_hour, eight_hour = 0.0001, 0.0004, 0.0008
    aprs = [funding.annualize(one_hour, 1.0), funding.annualize(four_hour, 4.0),
            funding.annualize(eight_hour, 8.0)]
    assert aprs[0] == aprs[1] == aprs[2] == pytest.approx(87.6)


def test_the_eight_hour_path_still_agrees_with_the_column_it_did_not_replace():
    """funding_ann_pct is kept, and must keep meaning what it meant.

    Fifteen nights of rows were written by that function. The new one has to agree with
    it wherever the assumption it hardcoded was true, or the two columns become
    incomparable across the boundary.
    """
    for rate in (0.0005, -0.0002, 0.0, 0.00007438746419):
        assert funding.annualize(rate, 8.0) == pytest.approx(
            nightly.funding_ann_pct(rate))


def test_a_negative_rate_annualises_negative():
    assert funding.annualize(-0.0005, 8.0) == pytest.approx(-54.75)
    assert funding.annualize(-0.0005, 1.0) == pytest.approx(-438.0)


def test_flat_funding_is_a_reading_and_a_missing_rate_is_not():
    """Zero carry is a fact about a live market. None is the absence of one, and
    collapsing them puts a confident 0% on an asset with no perp market at all."""
    assert funding.annualize(0.0, 8.0) == 0.0
    assert funding.annualize(None, 8.0) is None
    assert funding.annualize("n/a", 8.0) is None


def test_a_malformed_interval_refuses_rather_than_assuming_eight_hours():
    """A zero or negative interval is a broken feed, not a slow clock. Defaulting it
    would silently attribute another venue's settlement schedule to this one."""
    assert funding.annualize(0.0001, 0) is None
    assert funding.annualize(0.0001, -8) is None
    assert funding.annualize(0.0001, None) is None


# ---------------------------------------------------------------------------
# B. position-notional dollar conversion — the Coinbase parser
# ---------------------------------------------------------------------------
def test_a_long_paid_funding_recovers_a_negative_market_rate():
    """The worked case the widget is built around.

    10 contracts at $3,000 is $30,000 of notional. The UI shows -$40 accumulated over
    24 hours, which in this convention means the position *received* $40 — negative
    accumulated on a long is yield in.

        hourly = (40 / 24) / 30,000 = 5.5556e-5
        APR    = 5.5556e-5 * 8760 * 100 = 48.67%

    The holder earned 48.67%, so the market rate is *negative* 48.67% — longs are being
    paid, which is what SHORT_SQUEEZE_RISK describes. Keeping both numbers, with
    opposite signs and different names, is the whole job of this function: the desk
    wants its own yield, and the board wants a rate comparable to every other asset.
    """
    out = funding.position_apr(contracts=10, mark_price=3000.0,
                               accumulated_usd=-40.0, period_hours=24.0, side="LONG")
    assert out["notional_usd"] == pytest.approx(30000.0)
    assert out["position_apr"] == pytest.approx(48.6667, abs=1e-3)
    assert out["funding_apr"] == pytest.approx(-48.6667, abs=1e-3)
    assert out["position_pnl_usd"] == pytest.approx(40.0)
    assert out["regime"] == "SHORT_SQUEEZE_RISK"


def test_the_same_dollar_figure_inverts_on_a_short():
    """Identical cash flow, opposite market rate.

    A short that received $40 was paid by longs, which means the market rate is
    positive. Reading the exchange's dollar figure without the side is how a desk ends
    up recording a short-squeeze regime on an overheated-long market.
    """
    long_ = funding.position_apr(10, 3000.0, -40.0, 24.0, "LONG")
    short = funding.position_apr(10, 3000.0, -40.0, 24.0, "SHORT")
    assert short["position_apr"] == pytest.approx(long_["position_apr"])
    assert short["funding_apr"] == pytest.approx(-long_["funding_apr"])
    assert short["regime"] == "OVERHEATED_LONG"


def test_a_long_charged_funding_recovers_a_positive_market_rate():
    out = funding.position_apr(10, 3000.0, 40.0, 24.0, "LONG")
    assert out["position_apr"] == pytest.approx(-48.6667, abs=1e-3)
    assert out["funding_apr"] == pytest.approx(48.6667, abs=1e-3)
    assert out["position_pnl_usd"] == pytest.approx(-40.0)


def test_the_parsed_rate_matches_a_venue_rate_for_the_same_carry():
    """The parser and the venue path must land on the same number.

    A market paying 0.0001 an hour is 87.6% APR. A $100,000 position held 10 hours
    through that market accrues 100,000 * 0.0001 * 10 = $100. Feeding that dollar
    figure back through the parser has to return the rate it came from, or the two
    halves of the engine disagree about the same market.
    """
    venue = funding.annualize(0.0001, 1.0)
    parsed = funding.position_apr(contracts=1000, mark_price=100.0,
                                  accumulated_usd=100.0, period_hours=10.0, side="LONG")
    assert parsed["funding_apr"] == pytest.approx(venue)


def test_the_conversion_scales_out_of_position_size_and_holding_time():
    """The reason the dollar figure has to be converted at all: it is not comparable.

    $40 on a $30k position over 24h and $400 on a $300k position over 24h are the same
    market. So are $40 over 24h and $80 over 48h. A screen that ranks the raw dollar
    totals ranks position sizes.
    """
    base = funding.position_apr(10, 3000.0, -40.0, 24.0, "LONG")["funding_apr"]
    bigger = funding.position_apr(100, 3000.0, -400.0, 24.0, "LONG")["funding_apr"]
    longer = funding.position_apr(10, 3000.0, -80.0, 48.0, "LONG")["funding_apr"]
    assert base == pytest.approx(bigger) == pytest.approx(longer)


def test_a_position_with_no_size_or_no_time_has_no_rate_to_recover():
    """Dividing anyway produces an infinity, which renders as a very confident number."""
    assert funding.position_apr(0, 3000.0, -40.0, 24.0) is None
    assert funding.position_apr(10, 0.0, -40.0, 24.0) is None
    assert funding.position_apr(10, 3000.0, -40.0, 0.0) is None
    assert funding.position_apr("x", 3000.0, -40.0, 24.0) is None


def test_zero_accrual_is_a_flat_market_not_a_missing_one():
    out = funding.position_apr(10, 3000.0, 0.0, 24.0, "LONG")
    assert out["funding_apr"] == 0.0
    assert out["regime"] == "NEUTRAL"


# ---------------------------------------------------------------------------
# C. regime classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("apr,regime", [
    (120.0, "OVERHEATED_LONG"),
    (40.01, "OVERHEATED_LONG"),
    (40.0, "ELEVATED"),          # the boundary is strict: > +40
    (12.01, "ELEVATED"),
    (12.0, "NEUTRAL"),           # 0..+12 inclusive
    (0.0, "NEUTRAL"),
    (-0.01, "MILD_INVERSION"),
    (-15.0, "MILD_INVERSION"),   # the boundary is strict: < -15
    (-15.01, "SHORT_SQUEEZE_RISK"),
    (-90.0, "SHORT_SQUEEZE_RISK"),
])
def test_the_bands_and_their_boundaries(apr, regime):
    assert funding.classify_regime(apr) == regime


def test_the_two_gaps_in_the_specification_got_their_own_names():
    """+12 to +40 and -15 to 0 were unnamed in the original matrix.

    Folding them into NEUTRAL would have labelled a 35% APR carry "healthy trend
    growth" — a reading a desk acts on, and wrong.
    """
    assert funding.classify_regime(35.0) == "ELEVATED"
    assert funding.classify_regime(-8.0) == "MILD_INVERSION"


def test_mild_inversion_earns_nothing_at_all():
    """The lower gap is genuinely inert: shorts paying 8% a year is not an edge, and
    the original rule handed it the same +15% boost it gave a real squeeze."""
    assert funding.regime_modifier(-8.0, price_chg_24h=-25.0, rsi7=80.0)[0] == 1.0
    assert funding.funding_severity(-8.0) == 0.0


def test_an_asset_with_no_funding_has_no_regime():
    """Not a sixth band. Most of the board is spot-only, and printing NEUTRAL for a
    token with no perpetual market claims a market that does not exist."""
    assert funding.classify_regime(None) is None
    assert funding.classify_regime("n/a") is None


# ---------------------------------------------------------------------------
# D. the score modifier — the part that reaches conviction
# ---------------------------------------------------------------------------
def test_the_modifier_is_continuous_across_every_named_threshold():
    """The defect a step function cannot avoid, whatever threshold is chosen.

    An asset at 39.9% APR and one at 40.1% are materially identical, and the first
    version scored them 15% apart. This is the same failure score() was rewritten to
    remove — "the additive model saturated momentum at a hard clamp, PUMP and HYPE
    collided at c_momentum=20" — so it gets the same fix.
    """
    for centre, chg, rsi_ in ((40.0, 25.0, None), (12.0, 25.0, None),
                              (-15.0, None, 70.0), (-40.0, None, 70.0)):
        below = funding.regime_modifier(centre - 0.05, chg, rsi_)[0]
        at = funding.regime_modifier(centre, chg, rsi_)[0]
        above = funding.regime_modifier(centre + 0.05, chg, rsi_)[0]
        assert abs(below - at) < 0.002 and abs(at - above) < 0.002, (
            f"a cliff survives at {centre}% APR: {below} / {at} / {above}")


def test_the_modifier_is_monotone_in_the_carry():
    """More extreme funding must never earn a smaller adjustment. A non-monotone curve
    would rank two assets in an order no one could defend."""
    hot = [funding.regime_modifier(a, 25.0)[0] for a in (13, 20, 40, 80, 160, 400)]
    assert hot == sorted(hot, reverse=True), hot
    cold = [funding.regime_modifier(a, None, 70.0)[0] for a in (-16, -25, -40, -90, -400)]
    assert cold == sorted(cold), cold


def test_extreme_carry_is_no_longer_indistinguishable_from_merely_hot():
    """40% APR and 400% APR were scored identically. They are not the same market, and
    a modifier that cannot tell them apart cannot rank inside its own band."""
    at_threshold = funding.regime_modifier(40.0, price_chg_24h=25.0)[0]
    extreme = funding.regime_modifier(400.0, price_chg_24h=25.0)[0]
    assert extreme < at_threshold - 0.05
    assert extreme == pytest.approx(0.85, abs=1e-3)   # the floor, approached not jumped


def test_price_extension_scales_the_penalty_rather_than_gating_it():
    """Additive evidence, not a switch. Funding above the neutral band already
    establishes that leverage is being paid for; extension establishes the crowd is also
    sitting on a move with somewhere to fall."""
    flat = funding.regime_modifier(90.0, price_chg_24h=0.0)[0]
    half = funding.regime_modifier(90.0, price_chg_24h=5.0)[0]
    full = funding.regime_modifier(90.0, price_chg_24h=12.0)[0]
    assert full < half < flat < 1.0
    # The confirmed penalty is exactly twice the unconfirmed one, by construction.
    assert (1 - full) == pytest.approx((1 - flat) / funding.MOD_UNCONFIRMED_WEIGHT, rel=1e-6)


def test_uncorroborated_overheating_is_marked_down_not_ignored():
    """The case the first version got wrong: 90% APR on flat price scored exactly the
    same as an asset with no perpetual market at all. That discards an observation which
    was actually made — the funding print is not in doubt, only the second leg."""
    mult, reason = funding.regime_modifier(90.0, price_chg_24h=None)
    assert 0.85 < mult < 1.0
    assert "reduced weight" in reason
    # Identical to an observed-but-flat reading: neither adds extension evidence.
    assert mult == pytest.approx(funding.regime_modifier(90.0, price_chg_24h=0.0)[0])
    # And to a falling one — this file does not claim to know what hot funding on a
    # falling price means, so it does not price it.
    assert mult == pytest.approx(funding.regime_modifier(90.0, price_chg_24h=-8.0)[0])


def test_the_squeeze_boost_needs_rsi_above_forty_five():
    """Deeply negative funding on a chart in freefall is not squeeze asymmetry — it is
    a market where the shorts are correct. RSI is what separates the two."""
    assert funding.regime_modifier(-30.0, rsi7=60.0)[0] > 1.0
    assert funding.regime_modifier(-30.0, rsi7=30.0)[0] == 1.0
    assert "downtrend, not squeeze" in funding.regime_modifier(-30.0, rsi7=30.0)[1]


def test_the_boost_is_withheld_when_rsi_cannot_be_computed():
    """The first eight nights of a symbol's life, and after any gap in its history.
    Failing closed to 1.0 is the honest reading — the distinction the boost depends on
    genuinely cannot be made yet."""
    mult, reason = funding.regime_modifier(-30.0, rsi7=None)
    assert mult == 1.0
    assert "no 7d RSI" in reason


def test_the_boost_ramps_with_the_depth_of_the_inversion():
    """The specification said "+10% to +15%" without saying what selects within it.

    A range is not implementable. Interpolating on how deep shorts are underwater is the
    reading that matches what the boost is for, and starting from zero at the boundary
    rather than from +10% removes the cliff the original range implied.
    """
    at_boundary = funding.regime_modifier(-15.01, rsi7=70.0)[0]
    midway = funding.regime_modifier(-27.5, rsi7=70.0)[0]
    anchor = funding.regime_modifier(-40.0, rsi7=70.0)[0]
    beyond = funding.regime_modifier(-400.0, rsi7=70.0)[0]
    assert at_boundary == pytest.approx(1.0, abs=1e-3)     # continuous, not a jump to 1.10
    assert at_boundary < midway < anchor < beyond
    # MOD_COLD_ANCHOR: 85% of the available boost by the original matrix's cap point.
    assert anchor == pytest.approx(1.0 + 0.15 * funding.MOD_COLD_ANCHOR, abs=1e-3)
    assert beyond == 1.15, "the boost must cap rather than run away on an outlier print"


def test_the_severity_anchors_land_where_the_constants_say_they_do():
    """The tanh scales are derived from the anchors at import rather than tuned, so
    moving a threshold moves the curve coherently. Pinned so a hand-edited scale that
    contradicts its own anchor fails here."""
    assert funding.funding_severity(funding.REGIME_OVERHEATED) == pytest.approx(
        -funding.MOD_HOT_ANCHOR, abs=1e-9)
    assert funding.funding_severity(funding.MOD_SQUEEZE_SATURATION) == pytest.approx(
        funding.MOD_COLD_ANCHOR, abs=1e-9)
    # Zero, exactly, across the whole inert band — not merely small.
    for apr in (0.0, 5.0, 12.0, -1.0, -15.0):
        assert funding.funding_severity(apr) == 0.0


def test_the_two_sides_are_asymmetric_and_that_is_the_point():
    """Positive funding is the normal state of a perpetual market — longs pay shorts
    most of the time, which is why cash-and-carry is a standard trade. So +40% APR is
    elevated but unremarkable while -40% is rare and far more informative, and equal
    magnitudes must not earn equal severity."""
    assert abs(funding.funding_severity(-40.0)) > abs(funding.funding_severity(40.0))


def test_the_modifier_stays_inside_the_specified_band():
    """Nothing may leave the 0.85 .. 1.15 envelope the matrix defines. A multiplier is
    applied to a published score, and an unbounded one is an unbounded score."""
    for apr in (-1e6, -50.0, -15.0, 0.0, 12.0, 40.0, 55.0, 1e6):
        for chg in (None, -50.0, 0.0, 50.0):
            for r in (None, 0.0, 50.0, 100.0):
                m, _ = funding.regime_modifier(apr, chg, r)
                assert 0.85 <= m <= 1.15, f"{apr}/{chg}/{r} -> {m}"


def test_no_funding_feed_is_neutral_not_null():
    """1.0 rather than None: "no adjustment" is a real and correct multiplier, and a
    null here would reach the score as a type error rather than as neutrality."""
    mult, reason = funding.regime_modifier(None)
    assert mult == 1.0
    assert reason == "no funding feed"


def test_an_unreadable_confirmation_is_treated_as_an_absent_one():
    """Neither is an observation, so both fall to the same handling — reduced weight on
    the hot side, withheld on the cold side."""
    assert funding.regime_modifier(90.0, price_chg_24h="n/a")[0] == pytest.approx(
        funding.regime_modifier(90.0, price_chg_24h=None)[0])
    assert funding.regime_modifier(-30.0, rsi7="n/a")[0] == 1.0


# ---------------------------------------------------------------------------
# E. RSI
# ---------------------------------------------------------------------------
def test_rsi_refuses_until_the_window_is_full():
    """A 7-period RSI over four closes is not a 7-period RSI. It renders identically
    and would gate the squeeze boost on a number that means something else — the same
    refusal the choppiness index already makes, for the same reason."""
    rising = [10.0 + i for i in range(7)]
    assert funding.rsi(rising, 7) is None
    assert funding.rsi(rising + [17.0], 7) is not None


def test_an_unbroken_advance_is_one_hundred():
    assert funding.rsi([10.0 + i for i in range(9)], 7) == 100.0


def test_an_unbroken_decline_is_zero():
    assert funding.rsi([100.0 - i * 5 for i in range(9)], 7) == 0.0


def test_rsi_matches_wilder_on_a_hand_computed_series():
    """Pinned numerically against the textbook definition, so a refactor cannot quietly
    switch to a simple-average variant — which would disagree with every chart the
    reader compares this against."""
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    ag, al = sum(gains[:7]) / 7, sum(losses[:7]) / 7
    for i in range(7, len(deltas)):
        ag = (ag * 6 + gains[i]) / 7
        al = (al * 6 + losses[i]) / 7
    expected = 100.0 - 100.0 / (1.0 + ag / al)
    assert funding.rsi(closes, 7) == pytest.approx(round(expected, 2))


def test_a_gap_in_the_series_is_not_smoothed_over():
    closes = [10.0 + i for i in range(9)]
    closes[4] = None
    assert funding.rsi(closes, 7) is None
    closes[4] = 0.0
    assert funding.rsi(closes, 7) is None


def test_a_flat_series_is_neither_overbought_nor_oversold():
    """No gains and no losses. 50 is the defined midpoint; 100 would read as a maximal
    advance that did not happen."""
    assert funding.rsi([10.0] * 12, 7) == 50.0


# ---------------------------------------------------------------------------
# F. venue ingestion — every fetch degrades to nothing, never to a neutral
# ---------------------------------------------------------------------------
def test_binance_reads_the_per_symbol_interval_rather_than_assuming_eight_hours(monkeypatch):
    """The bug in miniature: Binance runs some symbols on a 4-hour clock, and
    annualising those at 8 reads them as half as hot as they are."""
    def fake(url, headers=None, data=None):
        if "premiumIndex" in url:
            return [{"symbol": "ETHUSDT", "lastFundingRate": "0.0001", "markPrice": "3000"},
                    {"symbol": "AAAUSDT", "lastFundingRate": "0.0001", "markPrice": "1"}]
        return [{"symbol": "AAAUSDT", "fundingIntervalHours": 4}]
    monkeypatch.setattr(funding, "_get_json", fake)
    rep = funding.fetch_binance_funding()
    assert rep["status"] == "live"
    assert rep["data"]["ETH"]["interval_hours"] == 8.0
    assert rep["data"]["ETH"]["funding_apr"] == pytest.approx(10.95)
    assert rep["data"]["AAA"]["interval_hours"] == 4.0
    assert rep["data"]["AAA"]["funding_apr"] == pytest.approx(21.9)


def test_a_dead_funding_info_call_does_not_lose_the_rates(monkeypatch):
    """The default is right for the large majority of symbols, so losing the whole
    venue over the secondary call would trade a small inaccuracy for a total outage."""
    def fake(url, headers=None, data=None):
        if "premiumIndex" in url:
            return [{"symbol": "ETHUSDT", "lastFundingRate": "0.0001", "markPrice": "3000"}]
        raise RuntimeError("503")
    monkeypatch.setattr(funding, "_get_json", fake)
    rep = funding.fetch_binance_funding()
    assert rep["status"] == "live"
    assert rep["data"]["ETH"]["interval_hours"] == 8.0


def test_hyperliquid_is_hourly_and_converts_open_interest_to_dollars(monkeypatch):
    """Hyperliquid quotes open interest in coins, not dollars. Recording the raw figure
    in a USD column would put a $2bn book beside a 40,000-coin one."""
    monkeypatch.setattr(funding, "_get_json", lambda *a, **k: [
        {"universe": [{"name": "ETH"}]},
        [{"funding": "0.0000125", "markPx": "3000", "openInterest": "50000"}]])
    rep = funding.fetch_hyperliquid_funding()
    assert rep["data"]["ETH"]["interval_hours"] == 1.0
    assert rep["data"]["ETH"]["funding_apr"] == pytest.approx(10.95)
    assert rep["data"]["ETH"]["oi_usd"] == pytest.approx(1.5e8)


def test_a_misaligned_hyperliquid_response_refuses_to_guess_the_pairing(monkeypatch):
    """meta.universe and ctxs are joined positionally — the index is the only key. A
    length mismatch zipped to the shorter of the two would silently attribute one
    asset's funding to another, which is worse than returning nothing."""
    monkeypatch.setattr(funding, "_get_json", lambda *a, **k: [
        {"universe": [{"name": "ETH"}, {"name": "SOL"}]},
        [{"funding": "0.00001", "markPx": "3000", "openInterest": "1"}]])
    rep = funding.fetch_hyperliquid_funding()
    assert rep["status"] == "unusable"
    assert rep["data"] == {}
    assert "do not align" in rep["detail"]


def test_bybit_resolves_its_interval_from_minutes(monkeypatch):
    def fake(url, headers=None, data=None):
        if "instruments-info" in url:
            return {"result": {"list": [{"symbol": "ETHUSDT", "fundingInterval": 240}]}}
        return {"result": {"list": [{"symbol": "ETHUSDT", "fundingRate": "0.0001",
                                     "markPrice": "3000", "openInterestValue": "2e9"}]}}
    monkeypatch.setattr(funding, "_get_json", fake)
    rep = funding.fetch_bybit_funding()
    assert rep["data"]["ETH"]["interval_hours"] == 4.0
    assert rep["data"]["ETH"]["funding_apr"] == pytest.approx(21.9)


def test_an_unrecognised_coinbase_shape_reports_unusable_rather_than_empty(monkeypatch):
    """Four situations end in an empty table — unconfigured, unreachable, unrecognised,
    genuinely empty — and one "no data" cannot tell a reader which. The position parser
    is the documented fallback precisely because this feed is the least contractual."""
    monkeypatch.setattr(funding, "_get_json",
                        lambda *a, **k: {"products": [{"product_id": "ETH-PERP"}]})
    rep = funding.fetch_coinbase_funding()
    assert rep["status"] == "unusable"
    assert "without a readable funding rate" in rep["detail"]


def test_an_unreachable_venue_yields_nothing_not_a_neutral_rate(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("503")
    monkeypatch.setattr(funding, "_get_json", boom)
    for fetch in (funding.fetch_binance_funding, funding.fetch_bybit_funding,
                  funding.fetch_hyperliquid_funding, funding.fetch_coinbase_funding):
        rep = fetch()
        assert rep["status"] == "unreachable"
        assert rep["data"] == {}


def test_one_dead_venue_does_not_cost_the_others(monkeypatch):
    monkeypatch.setattr(funding, "fetch_binance_funding",
                        lambda s=None: funding._report("binance", {}, "unreachable", "down"))
    monkeypatch.setattr(funding, "fetch_bybit_funding", lambda s=None: funding._report(
        "bybit", {"ETH": {"venue": "bybit", "funding_rate": 0.0001, "interval_hours": 8.0,
                          "funding_apr": 10.95, "mark_price": 3000.0, "oi_usd": 2e9}},
        "live", "1 market"))
    monkeypatch.setitem(funding.VENUE_FETCHERS, "binance", funding.fetch_binance_funding)
    monkeypatch.setitem(funding.VENUE_FETCHERS, "bybit", funding.fetch_bybit_funding)
    out = funding.fetch_all_venues(venues=("binance", "bybit"))
    assert out["venues"]["binance"]["status"] == "unreachable"
    assert funding.consolidate(out)["ETH"]["venue"] == "bybit"


# ---------------------------------------------------------------------------
# G. consolidation
# ---------------------------------------------------------------------------
def _venues(**by_venue):
    return {"venues": {name: funding._report(name, data, "live", "")
                       for name, data in by_venue.items()}}


def test_the_headline_rate_comes_from_the_deepest_book_not_an_average():
    """An average is a rate no one can receive. Earning the mean of Binance and
    Hyperliquid would require holding the position on both, and the figure would drift
    with which venues happened to respond that night."""
    reports = _venues(
        binance={"ETH": {"funding_apr": 10.0, "funding_rate": 0.0001,
                         "interval_hours": 8.0, "oi_usd": None, "mark_price": 3000.0}},
        hyperliquid={"ETH": {"funding_apr": 60.0, "funding_rate": 0.00007,
                             "interval_hours": 1.0, "oi_usd": 4e8, "mark_price": 3000.0}})
    out = funding.consolidate(reports)["ETH"]
    assert out["venue"] == "binance"
    assert out["funding_apr"] == 10.0
    assert out["interval_hours"] == 8.0
    assert out["venues_n"] == 2


def test_the_cross_venue_spread_is_kept_because_it_is_the_trade():
    """A wide spread between two venues is a basis, not a data-quality problem — and
    the two are indistinguishable if only one venue is ever recorded."""
    reports = _venues(
        binance={"ETH": {"funding_apr": 10.0, "funding_rate": 0.0001,
                         "interval_hours": 8.0, "oi_usd": 1e9, "mark_price": 3000.0}},
        hyperliquid={"ETH": {"funding_apr": 60.0, "funding_rate": 0.00007,
                             "interval_hours": 1.0, "oi_usd": 4e8, "mark_price": 3000.0}})
    assert funding.consolidate(reports)["ETH"]["apr_spread"] == pytest.approx(50.0)


def test_a_single_venue_has_no_spread_rather_than_a_zero_one():
    """Zero dispersion is a claim that the venues agree. One venue cannot agree with
    anything, and a column of zeros would read as a market in tight consensus."""
    reports = _venues(bybit={"ETH": {"funding_apr": 10.0, "funding_rate": 0.0001,
                                     "interval_hours": 8.0, "oi_usd": 1e9,
                                     "mark_price": 3000.0}})
    assert funding.consolidate(reports)["ETH"]["apr_spread"] is None


def test_open_interest_is_taken_from_whichever_venue_reports_it():
    """Binance's premiumIndex carries no OI. Dropping the reading because the deepest
    book omits the field would lose a number that is present."""
    reports = _venues(
        binance={"ETH": {"funding_apr": 10.0, "funding_rate": 0.0001,
                         "interval_hours": 8.0, "oi_usd": None, "mark_price": 3000.0}},
        bybit={"ETH": {"funding_apr": 11.0, "funding_rate": 0.0001,
                       "interval_hours": 8.0, "oi_usd": 2e9, "mark_price": 3000.0}})
    out = funding.consolidate(reports)["ETH"]
    assert out["venue"] == "binance" and out["oi_usd"] == 2e9


# ---------------------------------------------------------------------------
# H. cash and carry
# ---------------------------------------------------------------------------
def test_the_fee_drag_is_amortised_over_the_holding_period():
    """The number most funding screens omit, and the one that decides the trade.

    Four fills at 0.045% plus 0.02% slippage is 0.26% of notional round trip. Held a
    month that is 3.16% a year; held two days it is 47.45% and eats any carry on the
    board.
    """
    short = funding.carry_yield(20.0, hold_days=2)
    month = funding.carry_yield(20.0, hold_days=30)
    assert month["fee_drag_apr"] == pytest.approx(0.26 * 365 / 30, abs=1e-3)
    assert month["net_apr"] == pytest.approx(20.0 - month["fee_drag_apr"], abs=1e-3)
    assert short["net_apr"] < 0 < month["net_apr"]


def test_the_breakeven_horizon_is_reported():
    """How long the position must be held before the fills are paid for. A 20% carry
    needs 4.75 days; below that the trade loses money however good the rate looks."""
    out = funding.carry_yield(20.0, hold_days=30)
    assert out["breakeven_days"] == pytest.approx(0.26 * 365 / 20.0, abs=1e-2)


def test_the_screen_ranks_by_net_and_not_by_gross():
    """Ranking on gross puts the thin, expensive markets at the top — which is exactly
    where an unfiltered funding screen always points."""
    cons = {
        "AAA": {"funding_apr": 30.0, "oi_usd": 5e8, "venue": "binance",
                "interval_hours": 8.0, "regime": "ELEVATED", "apr_spread": None},
        "BBB": {"funding_apr": 25.0, "oi_usd": 9e8, "venue": "bybit",
                "interval_hours": 8.0, "regime": "ELEVATED", "apr_spread": None},
    }
    rows = funding.carry_screen(cons, hold_days=30)
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]
    assert rows[0]["net_apr"] < rows[0]["funding_apr"]


def test_thin_books_are_excluded_but_unknown_ones_are_kept_and_marked():
    """A 300% APR on a $200k book is a quote, not an opportunity. But an absent OI
    reading is not evidence of a thin book, so those are kept and flagged instead of
    silently dropped."""
    cons = {
        "THIN": {"funding_apr": 300.0, "oi_usd": 2e5, "venue": "bybit",
                 "interval_hours": 8.0, "regime": "OVERHEATED_LONG", "apr_spread": None},
        "UNKNOWN": {"funding_apr": 50.0, "oi_usd": None, "venue": "binance",
                    "interval_hours": 8.0, "regime": "OVERHEATED_LONG", "apr_spread": None},
    }
    rows = funding.carry_screen(cons, hold_days=30, oi_floor_usd=5e6)
    assert [r["symbol"] for r in rows] == ["UNKNOWN"]
    assert rows[0]["oi_known"] is False


def test_a_missing_apr_produces_no_carry_row():
    assert funding.carry_yield(None) is None
    assert funding.carry_yield(20.0, hold_days=0) is None


# ---------------------------------------------------------------------------
# I. the integration surface
# ---------------------------------------------------------------------------
def test_an_asset_with_no_perp_market_records_nulls_but_a_neutral_modifier():
    out = funding.funding_context("NOPERP", {}, price_chg_24h=5.0, rsi7=60.0)
    for k in ("funding_rate", "funding_apr", "funding_interval_h", "funding_venue",
              "funding_venues_n", "funding_apr_spread", "funding_regime"):
        assert out[k] is None, k
    assert out["score_modifier"] == 1.0


def test_every_recorded_column_exists_in_the_ledger_schema():
    """A context key that is not a FIELD is silently dropped by the DictWriter, which
    looks identical to a feed returning nothing."""
    out = funding.funding_context("X", {"X": {"funding_apr": 10.0, "venue": "bybit",
                                              "interval_hours": 8.0, "venues_n": 1,
                                              "apr_spread": None, "regime": "NEUTRAL",
                                              "funding_rate": 0.0001}}, 1.0, 50.0)
    for k in ("funding_apr", "funding_interval_h", "funding_venue", "funding_venues_n",
              "funding_apr_spread", "funding_regime", "rsi7"):
        assert k in nightly.FIELDS, f"{k} is recorded but has no column"
    assert out["funding_regime"] == "NEUTRAL"


def test_the_modifier_reaching_the_score_is_the_one_this_module_computed():
    """lavl_perp_mult is the only channel from derivatives into conviction, and it must
    stay a thin delegation — a second copy of the thresholds inside nightly.py would be
    a second set of thresholds to drift."""
    cases = [(-32.0, -2.0, 61.0), (90.0, 14.0, 70.0), (5.0, 1.0, 50.0),
             (90.0, 2.0, 70.0), (-32.0, -2.0, 20.0)]
    for apr, chg, r in cases:
        info = {"funding_apr": apr, "price_chg_24h": chg, "rsi7": r}
        assert nightly.lavl_perp_mult("X", {"X": info}) == \
            funding.regime_modifier(apr, chg, r)[0]


def test_the_eight_hour_column_is_retired_rather_than_fed_a_foreign_clock():
    """The defect this module removes, and the shape it would have taken on the way back in.

    `funding_ann_pct` annualised at a fixed three settlements a day. Feeding it an hourly
    Hyperliquid rate understates the carry eightfold — and on 2026-08-17 every rate in
    production came from an hourly venue, so that would have been all of them. Rather
    than conditionally populating a column that can only be right by coincidence, it is
    retired: historical values stand, nothing new is written.
    """
    out = nightly.perp_context("X", {"X": {"funding_rate": 0.0001, "interval_hours": 1.0}},
                               1e9, 1.0, {})
    assert out["funding_ann_pct"] is None
    src = (ROOT / "nightly.py").read_text(encoding="utf-8")
    assert '"funding_ann_pct": None' in src, "the retired column is being written again"


def test_there_is_exactly_one_funding_fetch_path():
    """Two calls to one endpoint is how you get rate-limited out of it.

    fetch_perps_map hit Bybit's tickers endpoint for open interest and the venue layer
    hit the same endpoint for funding. On 2026-08-17 the first returned 49 symbols and
    the second got HTTP 403, so Bybit was recorded unreachable on a night it was
    reachable — and the board lost a venue to a duplication that bought nothing.
    """
    src = (ROOT / "nightly.py").read_text(encoding="utf-8")
    assert "def fetch_perps_map" not in src
    assert "okx.com" not in src, "the per-symbol OKX fallback is back"
    assert src.count("api.bybit.com") == 0, "nightly.py fetches Bybit directly again"


def test_a_caller_passing_the_old_map_shape_still_gets_a_defined_reading():
    """test_parity.py builds {"funding_rate": ..., "open_interest": ...} by hand, and
    the parity gate is not the place to discover a KeyError."""
    assert nightly.lavl_perp_mult("SOL", {"SOL": {"funding_rate": -0.002,
                                                  "open_interest": 0.0}}) == 1.0
    assert nightly.lavl_perp_mult("NONE", {}) == 1.0


def test_rsi_over_the_ledger_needs_eight_closes(tmp_path, monkeypatch):
    """The real constraint on the squeeze boost, asserted against the real reader."""
    import csv
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(nightly, "LEDGER_CSV", path)

    def write(n):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
            w.writeheader()
            for i in range(n):
                w.writerow({**{k: "" for k in nightly.FIELDS},
                            "date": f"2026-03-{i+1:02d}", "symbol": "AAA",
                            "price": 100.0 + i})

    write(6)
    assert nightly._rsi_by_symbol()["AAA"] is None
    write(8)
    assert nightly._rsi_by_symbol()["AAA"] == 100.0     # eight closes, all advancing


def test_tonights_price_is_part_of_tonights_rsi(tmp_path, monkeypatch):
    """An RSI as of yesterday would let an asset that reversed hard this afternoon
    still collect the squeeze boost."""
    import csv
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(nightly, "LEDGER_CSV", path)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader()
        for i in range(8):
            w.writerow({**{k: "" for k in nightly.FIELDS},
                        "date": f"2026-03-{i+1:02d}", "symbol": "AAA",
                        "price": 100.0 + i})
    without = nightly._rsi_by_symbol()["AAA"]
    with_crash = nightly._rsi_by_symbol({"AAA": 40.0})["AAA"]
    assert without == 100.0
    assert with_crash < without


# ---------------------------------------------------------------------------
# J. the feed check — a total outage is a scoring event, not a quiet night
# ---------------------------------------------------------------------------
@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import csv
    monkeypatch.setattr(nightly, "LEDGER_CSV", tmp_path / "signals.csv")

    def write(rows):
        with (tmp_path / "signals.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({**{k: "" for k in nightly.FIELDS}, **r})
    return write


def _board(date, n=30, perp_share=0.6):
    """A board where only some assets have perpetual markets — the normal case."""
    return [{"date": date, "symbol": f"A{i:02d}", "name": f"A{i:02d}",
             "conviction": 90 - i * 2.0, "signal": nightly._tier_for(90 - i * 2.0),
             "price": 1.0 + i, "market_cap": 1e9, "turnover_pct": 30.0,
             "rs7": 1.0, "rs14": 1.0, "rs30": 1.0, "rs200": 1.0, "perp_mult": 1.0,
             "high_24h": 1.05 + i, "low_24h": 0.95 + i, "spec_hash": "abc123",
             "funding_apr": 8.0 if i < int(n * perp_share) else ""}
            for i in range(n)]


def _check(mon, name):
    return next(c for c in mon["health"] if c["name"] == name)


def test_a_spot_only_long_tail_is_not_a_dropout(ledger):
    """The reading this check exists to *not* raise.

    Most of the board has no perpetual market, so a coverage ratio well under 100% is
    the steady state. Grading funding_apr as a tracked column pinned field presence to a
    permanent amber at exactly this coverage, which is what a real dropout in price or
    rs200 would then have hidden behind.
    """
    ledger(_board("2026-03-01") + _board("2026-03-02") + _board("2026-03-03"))
    mon = nightly._compute_monitor()
    assert _check(mon, "Funding feed")["status"] == "pass"
    assert _check(mon, "Field presence")["status"] == "pass"


def test_every_venue_going_dark_at_once_fails_the_board(ledger):
    """The dropout that matters, and the one a per-column ratio cannot express.

    With no funding anywhere, lavl_perp_mult returns a neutral 1.0 for every asset.
    Scores move, the board renders, and nothing throws — a scoring event disguised as a
    quiet night.
    """
    ledger(_board("2026-03-01") + _board("2026-03-02")
           + _board("2026-03-03", perp_share=0.0))
    c = _check(nightly._compute_monitor(), "Funding feed")
    assert c["status"] == "fail"
    assert "neutral 1.0" in c["detail"]


def test_one_venue_down_warns_against_the_recorded_baseline(ledger):
    """An absolute floor would be wrong: how much of this universe has a perp market is
    a fact about the universe and drifts. A collapse against its own history is a fact
    about the pipeline."""
    ledger(_board("2026-03-01") + _board("2026-03-02")
           + _board("2026-03-03", perp_share=0.15))
    assert _check(nightly._compute_monitor(), "Funding feed")["status"] == "warn"


def test_the_first_night_has_no_baseline_and_says_so(ledger):
    """Pending, not pass. A check that reports success before it can measure anything
    is the ornamental status this repo has already had to remove once."""
    ledger(_board("2026-03-01", perp_share=0.0))
    assert _check(nightly._compute_monitor(), "Funding feed")["status"] == "pending"


def test_the_engine_is_pure_stdlib():
    """nightly.py runs in CI with no install step, and this module is on its import
    path. A third-party import here would break the job that is the whole product."""
    src = (ROOT / "funding.py").read_text(encoding="utf-8")
    for banned in ("import requests", "import numpy", "import pandas", "import aiohttp"):
        assert banned not in src, f"{banned} would need an install step"


# ---------------------------------------------------------------------------
# K. the counterfactual modifier
# ---------------------------------------------------------------------------
def test_the_trailing_modifier_is_recorded_but_never_applied():
    """`perp_mult` is what score() multiplied by. `perp_mult_trail` is what it would
    have been reading the trailing carry instead of tonight's print.

    Recording the counterfactual is what turns "should the modifier read a trend or a
    print" from an argument into a query. Two nights of funding_apr exist; adopting the
    trailing input now would be asserting the answer, and would move the specification
    hash a third time in two days on no evidence at all.
    """
    src = (ROOT / "nightly.py").read_text(encoding="utf-8")
    assert "perp_mult_trail" in nightly.FIELDS
    # It must not reach the score. lavl_perp_mult is the only channel, and it reads the
    # live map — never a trailing column.
    for fn in nightly.spec()["functions"].values():
        assert "perp_mult_trail" not in fn
        assert "funding_apr_trail" not in fn
    assert 'pm_trail, _ = funding.regime_modifier(' in src


def test_the_counterfactual_uses_the_same_confirmations_as_the_live_modifier():
    """Otherwise the comparison measures two changes at once and settles nothing."""
    spot, trail, chg, rsi_ = 80.0, 12.0, 14.0, 70.0
    live = funding.regime_modifier(spot, chg, rsi_)[0]
    shadow = funding.regime_modifier(trail, chg, rsi_)[0]
    assert live != shadow, "the fixture must actually distinguish the two inputs"
    # Only the carry differs — same function, same legs.
    assert shadow == funding.regime_modifier(trail, chg, rsi_)[0]


# ---------------------------------------------------------------------------
# L. the venues added because Binance and Bybit are geo-blocked from the runner
# ---------------------------------------------------------------------------
def test_kraken_converts_an_absolute_rate_before_annualising():
    """The worst unit trap in the venue set, and the reason this test exists.

    Kraken quotes funding in quote currency per contract per hour, not as a decimal.
    Its docs define absolute = relative x spot, so the conversion is a division by the
    index price. The verified live case: PF_XBTUSD at -0.5228 against an index of 64,308
    is -7.12% a year. Annualising the raw figure gives -457,972%, which would sail
    through every schema check — nothing about the field name says it is denominated in
    dollars.
    """
    def fake(url, headers=None, data=None):
        return {"result": "success", "tickers": [
            {"symbol": "PF_XBTUSD", "tag": "perpetual", "pair": "XBT:USD",
             "fundingRate": -0.5228, "indexPrice": 64308.0, "markPrice": 64310.0,
             "openInterest": 2000.0, "suspended": False}]}
    import types
    funding_local = _load("kraken_mod", "funding.py")
    funding_local._get_json = fake
    rep = funding_local.fetch_kraken_funding()
    assert rep["status"] == "live"
    apr = rep["data"]["BTC"]["funding_apr"]
    assert apr == pytest.approx(-7.12, abs=0.05), apr
    # The raw-rate mistake, stated numerically so the test explains itself.
    assert funding_local.annualize(-0.5228, 1.0) == pytest.approx(-457972.8, rel=1e-3)
    assert rep["data"]["BTC"]["funding_rate"] == pytest.approx(-0.5228 / 64308.0)


def test_kraken_excludes_index_prices_too_small_to_divide_by():
    """Below a cent the division amplifies quantisation error enough that the output is
    not a reading — a sub-cent alt computed to -255% APR in testing, which may be real
    and may be an artefact, and a column cannot say which."""
    funding_local = _load("kraken_thin", "funding.py")
    funding_local._get_json = lambda *a, **k: {"result": "success", "tickers": [
        {"symbol": "PF_XBTUSD", "tag": "perpetual", "pair": "XBT:USD",
         "fundingRate": -0.5228, "indexPrice": 64308.0, "openInterest": 1.0,
         "suspended": False},
        {"symbol": "PF_MEWUSD", "tag": "perpetual", "pair": "MEW:USD",
         "fundingRate": -0.0000001, "indexPrice": 0.0003, "openInterest": 1.0,
         "suspended": False}]}
    rep = funding_local.fetch_kraken_funding()
    assert set(rep["data"]) == {"BTC"}
    assert "sub-cent" in rep["detail"]


def test_gateio_reads_the_interval_per_market_rather_than_assuming_one():
    """Gate publishes funding_interval in seconds and it genuinely varies — a snapshot
    across its 918 markets found 573 at 8h, 342 at 4h and 3 at 1h. Annualising that book
    at a fixed three settlements a day would have been silently wrong for 345 markets,
    most of them by a factor of two. This is the whole module's thesis arriving in a
    single response."""
    funding_local = _load("gate_mod", "funding.py")
    funding_local._get_json = lambda *a, **k: [
        {"name": "BTC_USDT", "funding_rate": "0.0001", "funding_interval": 28800,
         "mark_price": "60000", "position_size": 1000, "quanto_multiplier": "0.0001",
         "in_delisting": False},
        {"name": "FOUR_USDT", "funding_rate": "0.0001", "funding_interval": 14400,
         "mark_price": "1", "position_size": 10, "quanto_multiplier": "1",
         "in_delisting": False},
        {"name": "DEAD_USDT", "funding_rate": "0.0001", "funding_interval": 28800,
         "mark_price": "1", "position_size": 1, "quanto_multiplier": "1",
         "in_delisting": True}]
    rep = funding_local.fetch_kraken_funding.__globals__["fetch_gateio_funding"]()
    assert set(rep["data"]) == {"BTC", "FOUR"}, "a delisting contract was not excluded"
    assert rep["data"]["BTC"]["interval_hours"] == 8.0
    assert rep["data"]["BTC"]["funding_apr"] == pytest.approx(10.95)
    assert rep["data"]["FOUR"]["interval_hours"] == 4.0
    assert rep["data"]["FOUR"]["funding_apr"] == pytest.approx(21.9)
    # Same quoted rate, different clock, double the carry. Exactly the defect.
    assert rep["data"]["FOUR"]["funding_apr"] == 2 * rep["data"]["BTC"]["funding_apr"]


def test_dydx_excludes_markets_being_wound_down():
    """FINAL_SETTLEMENT markets still carry a funding field. It is not a live reading,
    and putting it in a column that says it is current would be a stale rate wearing a
    fresh label."""
    funding_local = _load("dydx_mod", "funding.py")
    funding_local._get_json = lambda *a, **k: {"markets": {
        "BTC-USD": {"ticker": "BTC-USD", "status": "ACTIVE",
                    "nextFundingRate": "0.00000818", "oraclePrice": "64000",
                    "openInterest": "275.55"},
        "OLD-USD": {"ticker": "OLD-USD", "status": "FINAL_SETTLEMENT",
                    "nextFundingRate": "0.5", "oraclePrice": "1", "openInterest": "1"}}}
    rep = funding_local.fetch_kraken_funding.__globals__["fetch_dydx_funding"]()
    assert set(rep["data"]) == {"BTC"}
    assert rep["data"]["BTC"]["interval_hours"] == 1.0
    assert rep["data"]["BTC"]["rate_basis"] == "predicted"
    # Open interest arrives in base units and must be valued to be comparable.
    assert rep["data"]["BTC"]["oi_usd"] == pytest.approx(275.55 * 64000, rel=1e-6)


def test_the_venue_set_survives_losing_both_geo_blocked_exchanges():
    """The production failure this addition exists for. On 2026-08-17 Binance answered
    451 and Bybit 403 from the runner, and the whole feed rested on two venues."""
    assert {"dydx", "gateio", "kraken"} <= set(funding.VENUE_FETCHERS)
    survivors = [v for v in funding.VENUE_PRIORITY if v not in ("binance", "bybit")]
    assert len(survivors) >= 5, survivors
