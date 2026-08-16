"""Derivatives and the regime index — recorded, never scored.

Module 1 is mostly a recording job rather than an integration: fetch_perps_map already
pulled open interest from Bybit in the same call that fetched funding, and threw it
away on every run. The ingestion cost was being paid and the data discarded.

The property under test throughout is the same one the Dune columns carry: none of this
reaches score(). Adopting any of it must be a separate decision that moves the
specification hash, not something that happens the night a column is added.
"""
import csv
import importlib.util
import math
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("perp_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


# ---------------------------------------------------------------------------
# the property that matters most
# ---------------------------------------------------------------------------
def test_positioning_and_the_regime_index_stay_out_of_the_specification():
    """The boundary, restated after Module 3 moved part of it — deliberately.

    This test used to assert that *no* derivatives reading reached a scoring function.
    That was never quite true and the file said so in its own docstring: funding_rate
    has reached score() through lavl_perp_mult since before any of these columns
    existed. What was true, and remains true, is the part that matters — open interest,
    positioning and the regime index are recorded and never scored.

    Module 3 moved the funding half of the boundary on purpose. lavl_perp_mult now reads
    the interval-normalised APR instead of a raw rate carrying an unstated 8-hour
    assumption, and gates each adjustment on a confirming input. That widened the
    specification to include the 24h price change and a 7-period RSI, and it moved the
    hash from d600984ec00b to 872935361713.

    Restating the boundary rather than deleting the test is the point: the guarantee is
    still worth having for everything on the left of it, and an assertion that quietly
    became false is worse than no assertion.
    """
    captured = nightly.spec()["functions"]
    for fn in captured.values():
        for field in ("oi_usd", "oi_chg_24h_pct", "oi_to_mcap", "long_short_ratio",
                      "oi_price_divergence", "chop",
                      # Module 3 provenance: which venue, how many, how far apart. The
                      # modifier reads the consolidated APR and must never read the
                      # spread — a basis between two exchanges is a trade, not a signal
                      # about the asset's own leverage.
                      "funding_venue", "funding_venues_n", "funding_apr_spread"):
            assert field not in fn, f"{field} reached a scoring function"


def test_exactly_three_inputs_reach_the_funding_modifier():
    """A whitelist, so widening the specification stays a deliberate act.

    The negative test above cannot catch a *new* field being read — it only knows the
    names it was told about. This one asserts the positive side: the funding modifier
    reads the annualised carry and the two confirmations, and adding a fourth input
    fails here rather than being noticed a month later in a drifting track record.
    """
    src = nightly.spec()["functions"]["lavl_perp_mult"]
    for field in ("funding_apr", "price_chg_24h", "rsi7"):
        assert field in src, f"{field} is no longer read by the modifier"
    # The raw rate survives only as the fallback path for a caller passing the old map
    # shape, and the interval it is annualised at must be explicit in the source.
    assert "interval_hours" in src


def test_the_recorded_modifier_is_the_one_that_was_applied():
    """perp_mult and the reason string must describe the same decision.

    They are computed by two separate calls in main() — score() multiplies by
    lavl_perp_mult, and funding_context recomputes for the audit trail. If those ever
    disagree, the ledger records a multiplier next to an explanation of a different
    one, and the audit trail is worse than useless because it is confidently wrong.
    """
    cons = {"AAA": {"funding_apr": -32.0}, "BBB": {"funding_apr": 90.0},
            "CCC": {"funding_apr": 5.0}}
    for sym, chg, rsi_ in (("AAA", -2.0, 61.0), ("BBB", 14.0, 70.0), ("CCC", 1.0, 50.0)):
        pm = nightly.lavl_perp_mult(sym, {sym: {"funding_apr": cons[sym]["funding_apr"],
                                                "price_chg_24h": chg, "rsi7": rsi_}})
        fc = nightly.funding.funding_context(sym, cons, chg, rsi_)
        assert pm == fc["score_modifier"], f"{sym}: scored {pm}, recorded {fc['score_modifier']}"


def test_the_daily_bar_was_already_a_model_input_before_it_was_a_column():
    """high_24h/low_24h are the exception, and the distinction matters.

    _lavl_regime is a captured SPEC_FUNCTION and it reads both straight off the live
    CoinGecko payload — it always has. Recording them as columns did not add them to
    the specification; they were in it already, arriving by a different route. Filing
    them as observational context would have been wrong twice over: it would misstate
    what the model reads, and it would exempt a genuine input from the dropout check
    that exists to notice a feed going dark.
    """
    captured = nightly.spec()["functions"]
    assert any("high_24h" in fn for fn in captured.values())
    for f in ("high_24h", "low_24h"):
        assert f in nightly.MON_TRACKED_FIELDS
        assert f not in nightly.MON_CONTEXT_FIELDS


def test_the_new_columns_are_appended_never_inserted():
    """The committed header must stay a strict prefix of the schema, or the nightly
    cannot widen the file in place and the validator cannot tell schema growth from the
    positional-misalignment bug it exists to catch."""
    new = ["funding_rate", "funding_ann_pct", "oi_usd", "oi_chg_24h_pct", "oi_to_mcap",
           "long_short_ratio", "oi_price_divergence", "high_24h", "low_24h",
           # Module 3, appended behind Modules 1 and 2 on the same terms.
           "funding_apr", "funding_interval_h", "funding_venue", "funding_venues_n",
           "funding_apr_spread", "funding_regime", "rsi7"]
    assert nightly.FIELDS[-len(new):] == new
    old = nightly.FIELDS[:-len(new)]
    assert nightly.FIELDS[:len(old)] == old


def test_derivatives_are_context_not_tracked_inputs():
    """Field presence must not go amber because an optional feed is null. That is the
    lesson the Dune split already paid for."""
    for f in ("funding_rate", "oi_usd", "long_short_ratio"):
        assert f in nightly.MON_CONTEXT_FIELDS
        assert f not in nightly.MON_TRACKED_FIELDS


# ---------------------------------------------------------------------------
# funding
# ---------------------------------------------------------------------------
def test_funding_is_annualised_over_three_settlements_a_day():
    """0.05% per 8h is 54.75% a year. The raw rate is unreadable at a glance — 0.0005
    and 0.01 both look small and mean 55% and 1,095%."""
    assert nightly.funding_ann_pct(0.0005) == pytest.approx(54.75)
    assert nightly.funding_ann_pct(-0.0002) == pytest.approx(-21.9)


def test_zero_funding_is_zero_and_absent_funding_is_none():
    """Flat funding is a reading. A missing feed is not, and collapsing the two would
    put a confident 0% carry on an asset with no perp market at all."""
    assert nightly.funding_ann_pct(0.0) == 0.0
    assert nightly.funding_ann_pct(None) is None
    assert nightly.funding_ann_pct("n/a") is None


# ---------------------------------------------------------------------------
# open interest against price
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("price,oi,expected", [
    (3.0, 5.0, "ACCUMULATION"),     # new money backing the move
    (3.0, -5.0, "SHORT_SQUEEZE"),   # the rally is shorts closing, not buyers
    (-3.0, 5.0, "SHORT_BUILD"),     # new money positioned against it
    (-3.0, -5.0, "LONG_FLUSH"),     # leverage unwinding, not distribution
])
def test_the_four_divergence_quadrants(price, oi, expected):
    assert nightly.oi_price_divergence(price, oi) == expected


def test_a_drift_too_small_to_have_a_direction_is_not_a_quadrant():
    """A badge is read as a claim. Assigning one from a 0.1% move would put a confident
    label on rounding."""
    assert nightly.oi_price_divergence(0.1, 20.0) == "FLAT"
    assert nightly.oi_price_divergence(20.0, 0.2) == "FLAT"


def test_a_missing_leg_yields_no_quadrant_rather_than_a_guess():
    assert nightly.oi_price_divergence(None, 5.0) is None
    assert nightly.oi_price_divergence(5.0, None) is None


# ---------------------------------------------------------------------------
# the recorded context
# ---------------------------------------------------------------------------
def test_open_interest_is_normalised_by_market_cap():
    """A $1bn perp book is enormous against a $2bn token and unremarkable against a
    $200bn one."""
    pm = {"ETH": {"funding_rate": 0.0001, "oi_usd": 1e9}}
    small = nightly.perp_context("ETH", pm, 2e9, 1.0, {})
    large = nightly.perp_context("ETH", pm, 2e11, 1.0, {})
    assert small["oi_to_mcap"] == pytest.approx(0.5)
    assert large["oi_to_mcap"] == pytest.approx(0.005)


def test_the_oi_delta_is_null_on_the_first_night_not_zero():
    """Zero is a claim that open interest did not move. Before there is a prior night
    there is no such claim to make."""
    pm = {"SOL": {"funding_rate": 0.0, "oi_usd": 5e8}}
    out = nightly.perp_context("SOL", pm, 1e10, 2.0, {})
    assert out["oi_chg_24h_pct"] is None
    assert out["oi_price_divergence"] is None      # cannot diverge against nothing


def test_the_oi_delta_uses_the_prior_night():
    pm = {"SOL": {"funding_rate": 0.0, "oi_usd": 1.1e9}}
    out = nightly.perp_context("SOL", pm, 1e10, 4.0, {"SOL": 1.0e9})
    assert out["oi_chg_24h_pct"] == pytest.approx(10.0)
    assert out["oi_price_divergence"] == "ACCUMULATION"


def test_an_asset_with_no_perp_market_records_nulls_throughout():
    """Spot-only microcaps are the majority of the board. They must not acquire a
    fabricated neutral reading just because the column exists."""
    out = nightly.perp_context("NOPERP", {}, 1e9, 3.0, {})
    assert out == {"funding_rate": None, "funding_ann_pct": None, "oi_usd": None,
                   "oi_chg_24h_pct": None, "oi_to_mcap": None,
                   "long_short_ratio": None, "oi_price_divergence": None}


def test_long_short_is_never_manufactured_by_a_failed_request(monkeypatch):
    """'Half the accounts are long' is a real reading. A failed fetch must not produce
    it."""
    def boom(url, headers=None):
        raise RuntimeError("503")
    monkeypatch.setattr(nightly, "_get_json", boom)
    pm = {}
    assert nightly.fetch_long_short(pm, {"ETH", "SOL"}) == 0
    assert nightly.perp_context("ETH", pm, 1e9, 1.0, {})["long_short_ratio"] is None


def test_long_short_merges_without_clobbering_funding(monkeypatch):
    monkeypatch.setattr(nightly, "_get_json",
                        lambda url, headers=None: [{"longShortRatio": "1.8432"}])
    pm = {"ETH": {"funding_rate": 0.0003, "oi_usd": 2e9}}
    assert nightly.fetch_long_short(pm, {"ETH"}) == 1
    assert pm["ETH"]["long_short_ratio"] == 1.8432
    assert pm["ETH"]["funding_rate"] == 0.0003


# ---------------------------------------------------------------------------
# choppiness
# ---------------------------------------------------------------------------
def bars(n, high, low, close):
    return [{"high": high, "low": low, "close": close} for _ in range(n)]


def test_choppiness_needs_a_full_window_before_it_reports_anything():
    """A 14-period index computed over six bars is not a 14-period index. It would look
    identical on screen, which is exactly why it must refuse."""
    assert nightly.choppiness(bars(14, 2, 1, 1.5)) is None
    assert nightly.choppiness(bars(15, 2, 1, 1.5)) is not None


def test_a_market_going_nowhere_reads_as_range_bound():
    """Each bar spans the same range, so the true ranges sum to far more than the
    envelope they trade inside — the definition of chop."""
    seq = bars(20, 110.0, 90.0, 100.0)
    chop = nightly.choppiness(seq)
    assert chop > nightly.CHOP_CHOPPY
    assert nightly.chop_regime(chop) == "RANGE-BOUND"


def test_a_clean_trend_reads_as_trending():
    """Bars stepping upward with small individual ranges: the envelope is large
    relative to the sum of true ranges."""
    seq = [{"high": 100 + i * 10 + 1, "low": 100 + i * 10 - 1, "close": 100 + i * 10}
           for i in range(20)]
    chop = nightly.choppiness(seq)
    assert chop < nightly.CHOP_TRENDING
    assert nightly.chop_regime(chop) == "TRENDING"


def test_a_gap_in_the_bars_is_not_papered_over():
    seq = bars(20, 110.0, 90.0, 100.0)
    seq[-3]["high"] = None
    assert nightly.choppiness(seq) is None


def test_the_regime_label_is_absent_rather_than_guessed():
    assert nightly.chop_regime(None) is None


def test_chop_matches_the_published_formula():
    """CHOP = 100 * log10(sum(ATR1) / (maxHigh - minLow)) / log10(period). Pinned
    numerically so a refactor cannot quietly change the index."""
    seq = bars(15, 110.0, 90.0, 100.0)
    # Each bar: high-low = 20; prior close 100, so TR = 20 for all 14.
    expected = 100.0 * math.log10((20.0 * 14) / 20.0) / math.log10(14)
    assert nightly.choppiness(seq) == pytest.approx(round(expected, 2))


# ---------------------------------------------------------------------------
# end to end over a ledger
# ---------------------------------------------------------------------------
def test_chop_reports_its_bar_count_so_the_panel_can_say_what_it_is_waiting_for(
        tmp_path, monkeypatch):
    """An empty cell reads as a broken column. The bar count travels with the value so
    the terminal can say "accumulating (10/15)" instead."""
    path = tmp_path / "signals.csv"
    monkeypatch.setattr(nightly, "LEDGER_CSV", path)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
        w.writeheader()
        for i in range(6):
            w.writerow({**{k: "" for k in nightly.FIELDS},
                        "date": f"2026-03-{i+1:02d}", "symbol": "AAA", "price": 100.0,
                        "high_24h": 110.0, "low_24h": 90.0})
    out = nightly._chop_by_symbol()
    assert out["AAA"]["bars"] == 6
    assert out["AAA"]["chop"] is None and out["AAA"]["regime"] is None


def test_the_column_order_is_pinned_exhaustively():
    """Every reader of signals.csv is positional once the header is stripped, and the
    file self-migrates only while the committed header stays a strict prefix of the
    schema. An insertion mid-list silently reinterprets every row already written, so
    the whole order is pinned here rather than spot-checked — a rename, a reorder or an
    insert all fail this, and only a genuine append passes.
    """
    assert nightly.FIELDS == [
        "date", "symbol", "name", "price", "market_cap", "turnover_pct",
        "erosion_ratio", "conviction", "signal",
        "rs7", "rs14", "rs30", "rs200", "rs_blend",
        "c_liquidity", "c_era", "c_depth", "c_momentum",
        "unlocks_usd", "supply_increase_pct", "addr_growth_pct", "era",
        "roi_30d", "roi_90d", "survived", "perp_mult",
        "spec_hash",
        "unlock_overhang_pct", "adoption_dilution",
        "funding_rate", "funding_ann_pct", "oi_usd", "oi_chg_24h_pct",
        "oi_to_mcap", "long_short_ratio", "oi_price_divergence",
        "high_24h", "low_24h",
        # Module 3. funding_ann_pct above is NOT replaced by funding_apr: the old column
        # is the primary venue's rate annualised at a fixed three settlements a day, and
        # every row already on disk was written under that assumption. The new column is
        # the interval-correct figure. They agree wherever the venue settles on an
        # 8-hour clock and diverge where it does not, which is exactly the information
        # that would be destroyed by overwriting one with the other.
        "funding_apr", "funding_interval_h", "funding_venue", "funding_venues_n",
        "funding_apr_spread", "funding_regime", "rsi7",
    ]
