"""The RWA workspace: the net-issuance identity, the wrapper join, and the refusals.

The properties this file exists to hold:

  * The residual IS the change in IMPLIED units, exactly. Not approximately, not up to
    a scaling factor. The arithmetic is what is tested here, against supply directly
    rather than against a stored expectation — and nothing in this file asserts that the
    implied change is a real mint, because no available feed could corroborate that.
  * The join is by symbol and the candidate ORDER is load-bearing. 32 underlyings have
    tickers ending in 'x', so a strip-first implementation mis-joins Dinari's BITX to
    'bit' silently and permanently. The ordering is pinned here because nothing else
    would catch its reversal — the code still runs, the graph still builds, and the
    prices are simply about a different company.
  * Absence is never a zero. A missing previous price is not a flat day, an unknown
    timestamp is not fresh, an unpriced component is not a component scoring nought,
    and a single wrapper is not a wrapper in perfect agreement with itself.
  * The two models never touch. The RWA label vocabulary and the crypto tier vocabulary
    must not intersect, and the two specification hashes must move independently.
  * No network. Every test here injects its own transport and its own clock.
"""
import csv
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rwa = _load("rwa_under_test", "rwa.py")

NOW = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)          # Tue 23:00 ET Monday
WEEKEND = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)      # Sun 23:00 ET


def _stamp(hours_ago, now=NOW):
    return (now - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 1 — the identity
# ---------------------------------------------------------------------------
def test_the_residual_is_exactly_the_change_in_implied_units():
    """The whole module rests on this cancellation. Test it against supply, not against a
    remembered number: market cap is price times units, so dividing out the price-implied
    cap must leave the unit ratio and nothing else. What the test does NOT assert — and
    what the module must never claim — is that the implied change is an observed mint."""
    for p0, u0, p1, u1 in [(100.0, 1_000.0, 137.5, 1_000.0),
                           (100.0, 1_000.0, 137.5, 1_240.0),
                           (250.0, 8_000.0, 61.25, 7_100.0),
                           (0.37, 4_250_000.0, 0.41, 4_250_001.0)]:
        r = rwa.flow_residual(p0, p0 * u0, p1, p1 * u1)
        expected_pct = (u1 / u0 - 1.0) * 100.0
        assert r["residual_pct"] == pytest.approx(expected_pct, rel=1e-9, abs=1e-9), (
            f"residual must equal the implied unit change for P {p0}->{p1}, U {u0}->{u1}")
        assert r["residual_usd"] == pytest.approx(p1 * u1 - p1 * u0, rel=1e-9)


def test_the_price_leg_cancels_completely():
    """Same units, any price move at all, must read as zero issuance. If a price term
    survived the cancellation this is where it would show up."""
    for p1 in (1.0, 50.0, 99.99, 1_000_000.0):
        r = rwa.flow_residual(100.0, 100.0 * 500.0, p1, p1 * 500.0)
        assert r["residual_pct"] == pytest.approx(0.0, abs=1e-9)


def test_the_briefs_four_worked_readings():
    """The four cases the product brief states, reproduced from the arithmetic rather
    than from a table of expected labels."""
    cases = [((100, 1000, 105, 1050), rwa.IMPULSE_NEUTRAL),
             ((100, 1000, 105, 1180), rwa.IMPULSE_MINTING),
             ((100, 1000, 100, 1120), rwa.IMPULSE_STRONG),
             ((100, 1000, 108, 970), rwa.IMPULSE_REDEMPTION)]
    for (p0, m0, p1, m1), want in cases:
        r = rwa.flow_residual(p0, m0, p1, m1)
        assert rwa.impulse_label(r["residual_pct"], r["price_chg_pct"]) == want


def test_a_degenerate_pair_returns_none_and_never_a_flat_day():
    """A previous price of zero makes the ratio undefined, not flat. A residual of 0.0
    published where the arithmetic did not resolve is a fabricated 'no adoption'."""
    for args in [(0.0, 1000, 105, 1050), (None, 1000, 105, 1050),
                 (100, 0.0, 105, 1050), (100, 1000, None, 1050),
                 (100, 1000, 105, None), (100, 1000, -5, 1050)]:
        r = rwa.flow_residual(*args)
        assert r["residual_pct"] is None and r["residual_usd"] is None
        assert rwa.impulse_label(r["residual_pct"], r["price_chg_pct"]) == rwa.IMPULSE_UNREADABLE


def test_an_outage_does_not_manufacture_an_issuance_signal():
    """3% of net minting across a seven-night gap is 0.42% a night. Labelling it MINTING
    because the raw figure cleared a daily threshold would turn a pipeline outage into
    a signal."""
    assert rwa.impulse_label(rwa._daily_rate(3.0, 7), 0.2) == rwa.IMPULSE_NEUTRAL
    assert rwa.impulse_label(rwa._daily_rate(3.0, 1), 0.2) == rwa.IMPULSE_STRONG
    # And the raw residual, which the supply index compounds, is untouched by the span.
    assert rwa._daily_rate(3.0, 7) == pytest.approx((1.03 ** (1 / 7) - 1) * 100)


# ---------------------------------------------------------------------------
# 2 — the join
# ---------------------------------------------------------------------------
# A curated slice of the LIVE universe: one underlying per join family, plus every trap
# the full graph actually contains. Real ids, real tickers, real names.
UNIVERSE = [
    {"id": "alphabet-class-a", "symbol": "googl", "name": "Alphabet Class A", "asset_type": "stock"},
    {"id": "alphabet-class-c", "symbol": "goog", "name": "Alphabet Class C", "asset_type": "stock"},
    {"id": "meta-platforms", "symbol": "meta", "name": "Meta Platforms", "asset_type": "stock"},
    {"id": "nvidia", "symbol": "nvda", "name": "Nvidia", "asset_type": "stock"},
    {"id": "abbott", "symbol": "abt", "name": "Abbott", "asset_type": "stock"},
    {"id": "aci-worldwide", "symbol": "aciw", "name": "ACI Worldwide", "asset_type": "stock"},
    {"id": "invesco-qqq-etf", "symbol": "qqq", "name": "Invesco QQQ", "asset_type": "etf"},
    {"id": "spdr-s-p-500-etf-trust", "symbol": "spy", "name": "SPDR S&P 500", "asset_type": "etf"},
    {"id": "spdr-gold-shares", "symbol": "gld", "name": "SPDR Gold Shares", "asset_type": "etf"},
    {"id": "ishares-silver-trust", "symbol": "slv", "name": "iShares Silver Trust", "asset_type": "etf"},
    {"id": "goldman-sachs", "symbol": "gs", "name": "Goldman Sachs", "asset_type": "stock"},
    {"id": "b2gold", "symbol": "btg", "name": "B2Gold", "asset_type": "stock"},
    {"id": "2x-bitcoin-strategy-etf", "symbol": "bitx", "name": "2x Bitcoin Strategy ETF", "asset_type": "etf"},
    {"id": "some-other-thing", "symbol": "bit", "name": "Bit", "asset_type": "stock"},
    {"id": "ishares-treasury-bond-0-1yr", "symbol": "ib01.l", "name": "iShares Treasury 0-1yr", "asset_type": "etf"},
    {"id": "gold", "symbol": "xau", "name": "Gold", "asset_type": "commodity"},
    {"id": "silver", "symbol": "xag", "name": "Silver", "asset_type": "commodity"},
    {"id": "invesco-nasdaq-100", "symbol": "qqqm", "name": "Invesco NASDAQ 100", "asset_type": "etf"},
    {"id": "att", "symbol": "t", "name": "AT&T", "asset_type": "stock"},
    {"id": "ishares-0-3-month-treasury-bond", "symbol": "sgov",
     "name": "iShares 0-3 Month Treasury Bond", "asset_type": "etf"},
]
INDEX = rwa.build_index(UNIVERSE)
SYM_INDEX = INDEX["by_symbol"]


def _tok(tid, symbol, name=None):
    return {"id": tid, "symbol": symbol, "name": name or tid}


@pytest.mark.parametrize("tid,symbol,underlying,rule", [
    # ticker side — one per convention, all measured live
    ("aci-worldwide-dinari-tokenized-stock", "aciw", "aci-worldwide", rwa.JOIN_EXACT),
    ("alphabet-xstock", "GOOGLX", "alphabet-class-a", rwa.JOIN_X_SUFFIX),
    ("meta-xstock", "metax", "meta-platforms", rwa.JOIN_X_SUFFIX),
    ("nasdaq-xstock", "qqqx", "invesco-qqq-etf", rwa.JOIN_X_SUFFIX),
    ("wrapped-nvidia-xstock", "wnvdax", "nvidia", rwa.JOIN_W_X),
    ("backed-ib01-treasury-bond-0-1yr", "BIB01", "ishares-treasury-bond-0-1yr", rwa.JOIN_B_PREFIX),
    # id side — the underlying id prefixes the token id
    ("abbott-ondo-tokenized-stock", "abton", "abbott", rwa.JOIN_ID_PREFIX),
    ("invesco-nasdaq-100-st0x-tokenized-etf", "wtqqqm", "invesco-nasdaq-100", rwa.JOIN_ID_PREFIX),
    # Both sides fire and agree. The ticker's rule is the one reported, because it is the
    # more specific evidence — the id rule would have matched any token on that slug.
    ("nvidia-xstock", "nvdax", "nvidia", rwa.JOIN_X_SUFFIX),
    # shelf affixes, each gated on the id suffix that identifies its issuer
    ("alphabet-astock", "agoogl", "alphabet-class-a", rwa.JOIN_SHELF),
    ("meta-astock", "ameta", "meta-platforms", rwa.JOIN_SHELF),
    ("blackrock-ishares-0-3-month-treasury-bond-st0x-tokenized-etf", "wtsgov",
     "ishares-0-3-month-treasury-bond", rwa.JOIN_SHELF),
    ("atnt-ondo-tokenized-stock", "ton", "att", rwa.JOIN_SHELF),
    ("spdr-s-p-500-etf-ondo-tokenized-etf", "spyon", "spdr-s-p-500-etf-trust", rwa.JOIN_SHELF),
    # the metals, by name, last
    ("pax-gold", "paxg", "gold", rwa.JOIN_COMMODITY),
    ("kinesis-silver", "kag", "silver", rwa.JOIN_COMMODITY),
    ("tether-gold", "xaut", "gold", rwa.JOIN_COMMODITY),
])
def test_the_join_resolves_every_convention_measured_live(tid, symbol, underlying, rule):
    """One case per family. The ticker rules alone resolve 43% of the live graph; these
    are what take it to 99.3%, and each family below is 40 to 440 real tokens."""
    assert rwa.join_wrapper(_tok(tid, symbol), INDEX) == (underlying, rule)


def test_the_exact_symbol_is_always_tried_before_the_stripped_one():
    """The first of two orderings this module rests on. 32 underlyings have tickers that
    themselves end in 'x'. Dinari lists a BITX token whose underlying really IS BITX;
    strip first and it silently becomes 'bit' forever, and nothing anywhere raises."""
    assert "bit" in SYM_INDEX, "the trap only exists when the stripped key also resolves"
    assert rwa.join_by_symbol("bitx", SYM_INDEX) == ("2x-bitcoin-strategy-etf", rwa.JOIN_EXACT)


def test_the_commodity_name_rule_runs_last_and_cannot_steal_an_etf():
    """The second ordering, and the more dangerous one. Sixteen tokens in the live graph
    would be captured by the metal-name rule if it ran early — "SPDR Gold Shares aStock",
    "iShares Silver Trust (Dinari Tokenized ETF)", "VanEck Gold Miners ETF aStock" — and
    every one is a wrapper of an ETF, not of the metal. Pricing those against spot gold
    would put two different instruments in one dispersion and call the gap a
    dislocation."""
    # Which rule reaches them first does not matter and is not asserted. What matters is
    # that none of them lands on the metal.
    for tid, sym, name, want in [
            ("spdr-gold-shares-astock", "agld", "SPDR Gold Shares aStock", "spdr-gold-shares"),
            ("ishares-silver-trust-dinari-tokenized-etf", "slv",
             "iShares Silver Trust (Dinari Tokenized ETF)", "ishares-silver-trust"),
            ("spdr-gold-shares-xstock", "gldx2", "SPDR Gold Shares xStock", "spdr-gold-shares")]:
        got, rule = rwa.join_wrapper(_tok(tid, sym, name), INDEX)
        assert got == want, f"{sym} resolved to {got}, not {want}"
        assert rule != rwa.JOIN_COMMODITY, f"{sym} was captured by the metal-name rule"


def test_the_metal_name_rule_is_word_anchored():
    """"Goldman Sachs" and "B2Gold" contain the letters. Neither is a gold wrapper."""
    assert rwa.join_by_commodity_name("Goldman Sachs aStock", INDEX["ids"])[0] is None
    assert rwa.join_by_commodity_name("B2Gold Tokenized", INDEX["ids"])[0] is None
    assert rwa.join_by_commodity_name("Kinesis Gold", INDEX["ids"])[0] == "gold"


def test_a_shelf_affix_cannot_fire_outside_its_own_shelf():
    """The gate is what makes a one-letter affix safe at all. Stripping 'a' from any
    ticker that begins with one would wreck the graph; stripping it only from a token
    whose id ends '-astock' cannot."""
    assert rwa.join_by_shelf("meta-astock", "ameta", SYM_INDEX)[0] == "meta-platforms"
    assert rwa.join_by_shelf("ameta-unrelated-token", "ameta", SYM_INDEX)[0] is None


def test_the_id_rule_never_guesses_a_share_class():
    """Plain 'alphabet' is not an underlying — only Class A and Class C are — so the id
    rule declines and leaves the answer to the ticker, which reads aGOOGL and gets Class
    A. A loose match would have assigned every Alphabet wrapper to whichever class sorted
    first, and the two are different instruments."""
    assert rwa.join_by_id("alphabet-astock", INDEX["ids_longest_first"])[0] is None
    assert rwa.join_by_id("alphabet-class-a-xstock",
                          INDEX["ids_longest_first"])[0] == "alphabet-class-a"


def test_both_sides_must_agree_or_the_edge_is_flagged():
    """Where ticker and id disagree the edge is kept and marked, never picked. Two such
    cases exist in the live graph and both are genuinely ambiguous rather than defects:
    GLDX reads as the SPDR ETF by ticker and as plain gold by id."""
    got, rule = rwa.join_wrapper(_tok("gold-xstock", "gldx", "Gold xStock"), INDEX)
    assert rule == rwa.JOIN_CONFLICT
    assert got == "spdr-gold-shares", "the edge is kept; only the reading is withheld"


def test_an_exchange_suffix_is_stripped_as_a_rule():
    """/rwas/list carries London lines as 'ib01.l' while the token spells them BIB01.
    This is a rule and not a two-row patch: the next non-US listing arrives the same
    way."""
    assert rwa.normalise_symbol("IB01.L") == "ib01"
    assert rwa.normalise_symbol("NVDA") == "nvda"


def test_an_underlying_that_is_simply_absent_is_reported_and_never_guessed():
    """Eight tokens in the live graph resolve to nothing, and every one wraps something
    /rwas/list does not carry — OpenAI, SpaceX and Kalshi are pre-IPO. That is an absence
    to report, not a join to force."""
    assert rwa.join_wrapper(_tok("openai-tessera-pre-ipo", "topenai", "OpenAI"),
                            INDEX) == (None, rwa.JOIN_UNRESOLVED)
    assert rwa.join_wrapper(_tok("", ""), INDEX) == (None, rwa.JOIN_UNRESOLVED)


def _graph_fixture(issuer_markets=None):
    underlyings = [{"id": "nvidia", "symbol": "nvda", "name": "Nvidia", "asset_type": "stock"},
                   {"id": "alphabet-class-a", "symbol": "googl", "name": "Alphabet",
                    "asset_type": "stock"}]
    issuers = [{"id": "xstocks-ecosystem", "name": "xStocks", "market_cap": 4e8,
                "volume_24h": 2e7, "market_cap_change_24h": 1e5, "updated_at": _stamp(2),
                "tokens": [
                    {"id": "nvidia-xstock", "symbol": "nvdax", "name": "NVIDIA xStock",
                     "platforms": {"solana": "0x1", "base": "0x2"}},
                    {"id": "wrapped-nvidia-xstock", "symbol": "wnvdax",
                     "name": "Wrapped NVIDIA xStock", "platforms": {"solana": "0x3"}},
                    {"id": "alphabet-xstock", "symbol": "googlx", "name": "Alphabet xStock",
                     "platforms": {"solana": "0x4"}}]}]
    return rwa.build_graph(underlyings, issuers, issuer_markets)


def test_the_wrapper_graph_is_many_to_one():
    """NVIDIA, Tesla, IBM and Meta each carry both a plain and a 'wrapped-' xStock. A
    schema assuming one wrapper per underlying drops half the tradeable set."""
    g = _graph_fixture()
    assert len(g["by_underlying"]["nvidia"]) == 2
    assert {w["token_id"] for w in g["by_underlying"]["nvidia"]} == {
        "nvidia-xstock", "wrapped-nvidia-xstock"}
    assert g["unresolved"] == []


def test_an_edge_coingecko_contradicts_is_flagged_and_never_picked():
    """IEMGX resolves to IEMG by ticker while markets?issuer= lists EEM. The ticker is
    probably right; 'probably' is not enough to price a basis against."""
    g = _graph_fixture(issuer_markets={"xstocks-ecosystem": ["nvidia"]})
    alpha = g["by_underlying"]["alphabet-class-a"][0]
    assert alpha["join_rule"] == rwa.JOIN_CONFLICT
    assert alpha["underlying_id"] == "alphabet-class-a", "the edge is kept, only distrusted"


# ---------------------------------------------------------------------------
# 3 — liveness, dislocation, and what is not executable
# ---------------------------------------------------------------------------
def test_an_unknown_timestamp_is_stale_rather_than_fresh():
    """A feed that stopped stamping its rows is exactly the feed whose price should not
    be trusted. Absence must fail the gate, not pass it by default."""
    w = {"volume_24h": 1e6, "last_updated": None}
    assert rwa.wrapper_liveness(w, NOW)["live"] is False
    assert "no timestamp" in rwa.wrapper_liveness(w, NOW)["reason"]


def test_a_dead_shelf_is_excluded_from_pricing_and_kept_in_the_graph():
    """Dinari's inventory is real, listed, priced, and three weeks stale against $8.59 of
    daily volume. It belongs in the graph — a dead shelf is a finding about an issuer —
    and it must never price a basis."""
    dead = {"volume_24h": 8.59, "last_updated": _stamp(21 * 24)}
    lv = rwa.wrapper_liveness(dead, NOW)
    assert lv["live"] is False and lv["traded"] is False and lv["fresh"] is False


def _priced(token_id, price, vol=1e6, hours=1.0, rule=rwa.JOIN_EXACT):
    w = {"token_id": token_id, "symbol": token_id, "price": price, "volume_24h": vol,
         "last_updated": _stamp(hours), "join_rule": rule, "chains": ["solana"]}
    w["liveness"] = rwa.wrapper_liveness(w, NOW)
    return w


def test_one_wrapper_has_no_cross_section_and_the_engine_says_so():
    """One wrapper is its own median and its dispersion is 0.00% by construction — the
    same defect the published aggregate has. Refuse rather than print a zero."""
    d = rwa.dislocations([_priced("a", 100.0)], NOW)
    assert d["status"] == "insufficient" and d["dispersion_bps"] is None
    assert d["legs"] == []


def test_the_basis_is_measured_against_the_live_median_not_the_aggregate():
    """IBM's published aggregate read 282.91 while both live wrappers sat at 233.43 and
    238.57 — dragged by stale members of the blend. Pricing against it would make the
    basis partly a basis against itself."""
    legs = rwa.dislocations([_priced("a", 233.43), _priced("b", 238.57)], NOW)
    assert legs["median_price"] == pytest.approx((233.43 + 238.57) / 2)
    assert {l["token_id"] for l in legs["legs"]} == {"a", "b"}
    assert legs["legs"][0]["basis_bps"] * legs["legs"][1]["basis_bps"] < 0, \
        "two legs around a median must sit on opposite sides of it"


def test_every_row_is_pre_execution_and_says_what_is_missing():
    """Executable means after bid/ask, depth, cost-to-move and the trust fields. All of
    them live behind /rwas/{id}/tickers, which answers 401, so no row may be staged past
    PRE_EXECUTION and none may carry a `confidence` — a confidence beside an absent
    execution leg reads as confidence in a trade, and it reached 100."""
    d = rwa.dislocations([_priced("a", 100.0), _priced("b", 104.0)], NOW)
    assert d["kind"] == "wrapper_price_divergence"
    assert d["stage"] == rwa.DIVERGENCE_STAGE
    assert d["legs"], "a 4% spread must produce legs"
    for leg in d["legs"]:
        assert leg["stage"] == "PRE_EXECUTION"
        assert leg["execution_evidence"].startswith("UNAVAILABLE")
        # None, not False: False asserts a friction test ran and failed.
        assert leg["executable_after_friction"] is None
        assert "confidence" not in leg and "executable" not in leg
        assert 0 <= leg["observation_evidence"] <= 100


def test_no_score_can_report_complete_coverage_without_execution():
    """The redistribution guard. An earlier version set execution to weight 0 and left it
    out of the weight dict, so four priced components reported 100% coverage on a board
    with no execution evidence at all."""
    full = rwa.rwa_conviction({"liquidity": 24.0, "distribution": 15.0,
                               "integrity": 18.0, "impulse": 20.0})
    assert full["absent"] == ["execution"]
    assert full["coverage"] < 100.0
    assert full["coverage"] == full["max_coverage_on_this_plan"]
    assert "execution" in rwa.DECLARED_WEIGHTS and rwa.W_EXECUTION > 0
    assert "execution" not in rwa.COMPONENT_WEIGHTS
    w = rwa.wrapper_score(_priced("a", 100.0), {"total_volume": 1e7, "median_price": 101.0}, {})
    assert w["coverage"] < 100.0 and w["absent"] == ["execution"]
    assert w["execution_evidence"].startswith("UNAVAILABLE")


def test_a_distrusted_join_is_kept_out_of_the_tape():
    d = rwa.dislocations([_priced("a", 100.0), _priced("b", 104.0),
                          _priced("bad", 180.0, rule=rwa.JOIN_CONFLICT)], NOW)
    assert "bad" not in {l["token_id"] for l in d["legs"]}


def test_a_basis_inside_the_quantisation_is_not_a_row():
    d = rwa.dislocations([_priced("a", 100.00), _priced("b", 100.05)], NOW)
    assert d["legs"] == [], "25bp is the floor; 2.5bp is two ticks on a $100 share"


# ---------------------------------------------------------------------------
# 4 — the model, coverage, and the two vocabularies
# ---------------------------------------------------------------------------
def test_the_rwa_labels_cannot_be_confused_with_the_crypto_tiers():
    """The product decision is that these are two models. Two vocabularies that share
    even one word invite the reading that they share a scale."""
    nightly = _load("nightly_for_labels", "nightly.py")
    crypto = {"STRONG", "BUY", "HOLD", "WATCH", "AVOID"}
    assert set(rwa.RWA_LABELS) & crypto == set()
    assert set(rwa.IMPULSE_LABELS) & crypto == set()
    for conv in (95, 75, 60, 45, 10):
        assert nightly._tier_for(conv) not in rwa.RWA_LABELS


def test_the_two_specification_hashes_are_independent():
    """A crypto threshold change must not segment the RWA track record, and the reverse.
    A shared digest would make every edit to either invalidate the history of both."""
    nightly = _load("nightly_for_spec", "nightly.py")
    assert rwa.spec_hash() != nightly.SPEC_HASH
    assert set(rwa.spec()["functions"]) & set(nightly.spec()["functions"]) == set()


def test_the_specification_captures_every_scoring_function():
    """A renamed scoring function must not silently drop out of the specification."""
    captured = rwa.spec()["functions"]
    assert set(captured) == set(rwa.RWA_SPEC_FUNCTIONS)
    assert all(rwa.spec()["constants"][c] is not None for c in rwa.RWA_SPEC_CONSTANTS)
    assert rwa.spec_hash() == rwa.spec_hash(), "the digest must be stable within a run"


def test_night_one_still_grades_and_states_what_it_could_not_price():
    """On the first night the impulse component cannot exist for any row. Rescaling over
    the weight that remains keeps the board internally comparable; scoring the absent
    component as zero would publish a board on which everything looked unadopted."""
    comps = {"liquidity": 24.0, "distribution": 15.0, "integrity": 18.0, "impulse": None}
    got = rwa.rwa_conviction(comps)
    assert got["absent"] == ["execution", "impulse"]
    assert got["coverage"] == pytest.approx(100 * 75.0 / 120.0, rel=1e-6)
    assert got["score"] == pytest.approx(100 * 57.0 / 75.0, rel=1e-6)
    assert got["label"] != rwa.RWA_UNRATED


def test_below_the_coverage_floor_the_model_refuses_rather_than_guesses():
    got = rwa.rwa_conviction({"liquidity": 20.0})
    assert got["score"] is None and got["label"] == rwa.RWA_UNRATED
    assert "below the" in got["reason"]


def test_unrated_is_not_a_grade_of_zero():
    assert rwa.rwa_label(None) == rwa.RWA_UNRATED
    assert rwa.rwa_label(0.0) == rwa.RWA_DORMANT, "zero is a grade; None is a refusal"


def test_a_lone_wrapper_scores_the_middle_on_agreement_not_full_marks():
    """A single wrapper has nothing to disagree with. Scoring that as perfect integrity
    would rank the least distributed assets highest on the component that measures
    agreement."""
    alone = rwa.score_integrity(None, 1.0, 0)
    tight = rwa.score_integrity(5.0, 1.0, 0)
    assert alone is not None and tight > alone


def test_a_contradicted_join_costs_integrity():
    assert rwa.score_integrity(30.0, 1.0, 2) < rwa.score_integrity(30.0, 1.0, 0)


def test_impulse_reads_the_recorded_series_and_not_one_night():
    """Supply is a stock and issuance is its flow. The same 1% on twelve nights is the
    finding; one night's print is close to nothing."""
    assert rwa.score_impulse([]) is None
    one = rwa.score_impulse([1.0])
    many = rwa.score_impulse([1.0] * 12)
    assert many > one
    assert rwa.score_impulse([-2.0] * 6) < rwa.score_impulse([0.0] * 6), \
        "redemption must pull the score down, not merely fail to raise it"


# ---------------------------------------------------------------------------
# 5 — the ledger: the one artifact that cannot be rebuilt
# ---------------------------------------------------------------------------
def test_a_second_run_on_one_day_replaces_rather_than_appends(tmp_path):
    """The failure this prevents is the one the crypto ledger already had: rows appended
    unconditionally, a re-run duplicating a date, and 460 duplicate (date, symbol) pairs
    accumulating. A supply chain reading that file would compound one night nine times."""
    p = tmp_path / "rwa_flow.csv"
    row = {"date": "2026-09-01", "underlying_id": "nvidia", "residual_pct": 1.0}
    rwa.append_daily_rows(p, rwa.RWA_FLOW_FIELDS, "2026-09-01", [row])
    rwa.append_daily_rows(p, rwa.RWA_FLOW_FIELDS, "2026-09-01", [{**row, "residual_pct": 2.0}])
    rows = rwa.read_rows(p, rwa.RWA_FLOW_FIELDS)
    assert len(rows) == 1 and rows[0]["residual_pct"] == "2.0"


def test_prior_days_are_never_touched_by_a_rerun(tmp_path):
    p = tmp_path / "rwa_flow.csv"
    rwa.append_daily_rows(p, rwa.RWA_FLOW_FIELDS, "2026-08-31",
                          [{"date": "2026-08-31", "underlying_id": "nvidia", "supply_index": 100}])
    rwa.append_daily_rows(p, rwa.RWA_FLOW_FIELDS, "2026-09-01",
                          [{"date": "2026-09-01", "underlying_id": "nvidia", "supply_index": 101}])
    rwa.append_daily_rows(p, rwa.RWA_FLOW_FIELDS, "2026-09-01",
                          [{"date": "2026-09-01", "underlying_id": "nvidia", "supply_index": 102}])
    rows = rwa.read_rows(p, rwa.RWA_FLOW_FIELDS)
    assert [r["date"] for r in rows] == ["2026-08-31", "2026-09-01"]
    assert rows[0]["supply_index"] == "100"


def test_the_two_append_helpers_agree(tmp_path):
    """rwa.append_daily_rows restates nightly._append_context_rows because nightly.py
    imports this module and borrowing its helper would be a cycle. The duplication is
    only acceptable while it is a CHECKED invariant, which is what this is."""
    nightly = _load("nightly_for_append", "nightly.py")
    fields = ["date", "underlying_id", "residual_pct"]
    day1 = [{"date": "2026-08-31", "underlying_id": "a", "residual_pct": "1"}]
    day2 = [{"date": "2026-09-01", "underlying_id": "a", "residual_pct": "2"}]
    day2b = [{"date": "2026-09-01", "underlying_id": "a", "residual_pct": "9"}]
    out = {}
    for name, fn in (("rwa", rwa.append_daily_rows),
                     ("nightly", nightly._append_context_rows)):
        p = tmp_path / f"{name}.csv"
        for today, rows in (("2026-08-31", day1), ("2026-09-01", day2),
                            ("2026-09-01", day2b)):
            fn(p, fields, today, rows)
        out[name] = p.read_text()
    assert out["rwa"] == out["nightly"]


def test_the_supply_index_compounds_the_raw_residual_not_the_daily_rate(tmp_path):
    """The index is a level in units. It must compound what actually happened between
    the two observations, while the LABEL reads the per-day rate — otherwise a gap
    either inflates the level or suppresses the signal, and only one of those is right."""
    graph = {"by_underlying": {}}
    prior = {"nvidia": {"date": "2026-08-25", "price": "100", "market_cap": "1000",
                        "supply_index": "100", "span_days": "1"}}
    rows = [{"id": "nvidia", "symbol": "nvda", "name": "Nvidia", "asset_type": "stock",
             "tokenized_market_data": {"current_price": 100.0, "market_cap": 1030.0,
                                       "last_updated": _stamp(1)}}]
    built = rwa.assemble(rows, graph, {}, prior, "2026-09-01", NOW)
    flow = built["flow_rows"][0]
    assert flow["span_days"] == 7
    assert flow["supply_index"] == pytest.approx(103.0)          # the raw 3%
    assert flow["residual_pct_daily"] == pytest.approx((1.03 ** (1 / 7) - 1) * 100)
    assert flow["impulse"] == rwa.IMPULSE_NEUTRAL                 # 0.42%/day is not a mint


# ---------------------------------------------------------------------------
# 6 — the board gate and the assembled artifact
# ---------------------------------------------------------------------------
def _market_row(uid="nvidia", sym="nvda", price=200.0, mcap=4.0e8, vol=6.0e6,
                asset_type="stock", spark=None):
    return {"id": uid, "symbol": sym, "name": uid.title(), "asset_type": asset_type,
            "tokenized_market_data": {
                "current_price": price, "market_cap": mcap, "total_volume": vol,
                "price_change_percentage_24h": 1.2,
                "market_cap_change_percentage_24h": 1.2,
                "last_updated": _stamp(0.2),
                "sparkline_in_7d": {"price": spark or [price] * 168}}}


def _full_graph():
    g = _graph_fixture()
    return g


def test_an_underlying_with_no_live_wrapper_is_in_the_ledger_and_off_the_board():
    """'A ranked board of underlyings, not a flat token dump.' Without the gate the top
    of a 642-row ranking is decided by tokens with three digits of daily volume."""
    graph = _full_graph()
    prices = {"nvidia-xstock": {"price": 200.0, "volume_24h": 5.0, "market_cap": 1e5,
                                "last_updated": _stamp(500)},
              "wrapped-nvidia-xstock": {"price": 201.0, "volume_24h": 3.0,
                                        "market_cap": 1e5, "last_updated": _stamp(500)}}
    built = rwa.assemble([_market_row()], graph, prices, {}, "2026-09-01", NOW)
    assert built["board"] == []
    assert len(built["flow_rows"]) == 1, "it stays in the irreplaceable ledger"
    assert built["flow_rows"][0]["wrappers_live"] == 0


def test_a_live_underlying_is_ranked_scored_and_carries_its_wrappers():
    graph = _full_graph()
    prices = {"nvidia-xstock": {"price": 200.0, "volume_24h": 5.0e6, "market_cap": 2e8,
                                "last_updated": _stamp(0.5), "price_change_pct_24h": 1.4},
              "wrapped-nvidia-xstock": {"price": 202.0, "volume_24h": 1.2e7,
                                        "market_cap": 1e8, "last_updated": _stamp(0.5),
                                        "price_change_pct_24h": 1.1}}
    built = rwa.assemble([_market_row()], graph, prices, {}, "2026-09-01", NOW)
    assert len(built["board"]) == 1
    rec = built["board"][0]
    assert rec["wrappers_live"] == 2 and rec["wrappers_n"] == 2
    assert rec["conviction"] is not None and rec["label"] in rwa.RWA_LABELS
    assert rec["absent"] == ["execution", "impulse"], "night one, and execution always"
    assert [w["token_id"] for w in rec["wrappers"]][0] == "wrapped-nvidia-xstock", \
        "wrappers are ordered by the volume that makes one of them the real market"
    assert built["tape"], "a 100bp spread between two wrappers is a tape row"
    assert all(l["stage"] == "PRE_EXECUTION" for l in built["tape"])


def test_the_board_is_ranked_and_ungraded_rows_sort_last():
    graph = {"by_underlying": {}}
    rows = [_market_row("a", "aa", vol=5e6), _market_row("b", "bb", vol=9e6)]
    built = rwa.assemble(rows, graph, {}, {}, "2026-09-01", NOW)
    # No wrappers at all -> nothing clears the board gate. The ledger still records both.
    assert built["board"] == [] and len(built["flow_rows"]) == 2


# ---------------------------------------------------------------------------
# 7 — the session calendar and the off-hours reading
# ---------------------------------------------------------------------------
def test_the_weekend_window_runs_from_friday_close_to_now():
    close = rwa.last_close(WEEKEND)
    assert close == datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc), "Fri 16:00 ET"
    assert (WEEKEND - close).total_seconds() / 3600 == pytest.approx(55.0)


def test_a_holiday_extends_the_window_rather_than_being_treated_as_a_session():
    """Labor Day 2026 is Monday 7 September. A calendar that assumed every weekday was a
    session would measure Tuesday's off-hours move from a close that never happened."""
    after = datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc)
    close = rwa.last_close(after)
    assert close == datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc), "Friday, not Monday"


def test_an_early_close_is_honoured():
    """Black Friday 2026 closes at 13:00 ET, not 16:00."""
    after = datetime(2026, 11, 28, 2, 0, tzinfo=timezone.utc)
    assert rwa.last_close(after) == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)


def test_past_its_expiry_the_calendar_refuses_instead_of_assuming():
    """A hand-maintained list with no expiry is a list that is silently wrong from the
    first January nobody reviewed it. The refusal IS the maintenance trigger."""
    far = datetime(2029, 3, 1, 2, 0, tzinfo=timezone.utc)
    assert rwa.session_calendar_status(far)["ok"] is False
    reading = rwa.offhours_reading(_market_row(), [], None, far)
    assert reading["status"] == "unavailable" and "calendar" in reading["detail"]


def test_a_commodity_has_no_cash_session_to_be_shut():
    r = rwa.offhours_reading(_market_row("gold", "xau", asset_type="commodity"), [], None, WEEKEND)
    assert r["status"] == "n/a"


def test_no_offhours_reading_while_the_cash_market_is_trading():
    midday = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)   # 14:00 ET Tuesday
    assert rwa.market_open_now(midday) is True
    assert rwa.offhours_reading(_market_row(), [], None, midday)["status"] == "session_open"


def test_the_offhours_move_is_measured_from_the_close_and_the_gap_is_withheld():
    """The tokenized tape ran all weekend and the cash market did not, so this return is
    the only price discovery that happened anywhere. Converting it into an implied
    Monday gap needs the cash prints as the other half — and CoinGecko's 'underlying'
    price is a blend of these same wrappers, so publishing a gap would be inventing the
    side that was never measured."""
    # The window is 55h, the array is 168 hourly points ending at last_updated, so the
    # Friday close lands at index 168 - 1 - 55 = 112. Everything at or before it is the
    # pre-close price; everything after is the weekend drift.
    spark = [100.0] * 113 + [103.8] * 55
    row = _market_row(price=103.8, spark=spark)
    row["tokenized_market_data"]["last_updated"] = _stamp(0.2, WEEKEND)
    wrappers = [{"price_change_pct_24h": 2.0}, {"price_change_pct_24h": 1.4},
                {"price_change_pct_24h": -0.1}]
    r = rwa.offhours_reading(row, wrappers, 18.0, WEEKEND, volume_ratio=2.4)
    assert r["status"] == "live"
    assert r["window"]["kind"] == "weekend"
    assert r["offhours_return_pct"] == pytest.approx(3.8, abs=0.01)
    assert r["wrappers_agree"] == 2 and r["wrappers_live"] == 3
    assert r["dispersion_bps"] == 18.0 and r["volume_ratio"] == 2.4
    assert r["implied_gap_pct"] is None and r["implied_gap_confidence"] is None
    assert r["implied_gap_state"] == rwa.EQUITY_PENDING
    assert "equity prints" in r["implied_gap_blocked_by"]
    assert "no vendor is being added" in r["implied_gap_blocked_by"]


def test_the_sparkline_hour_mapping_is_declared_an_inference():
    """CoinGecko publishes the array with no timestamps. The mapping is inferred, and
    every reading built on it has to say so rather than presenting an hour as a fact."""
    got = rwa.sparkline_hours([1.0, 2.0, 3.0], "2026-09-01T03:00:00Z", NOW)
    assert got["inferred"] is True and got["n"] == 3
    assert got["points"][-1]["t"] == datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    assert got["points"][0]["t"] == datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
    assert "cadence may not be hourly" in got["detail"], \
        "an off-cadence array must be called out"
    # 168 and 169 were BOTH observed live on the same day. A hard equality check here
    # would fire on a feed that is behaving.
    for n in (168, 169):
        ok = rwa.sparkline_hours([1.0] * n, "2026-09-01T03:00:00Z", NOW)
        assert "cadence may not be hourly" not in ok["detail"]


def test_a_sparkline_with_an_unparseable_point_is_refused_whole():
    assert rwa.sparkline_hours([1.0, None, 3.0], "2026-09-01T03:00:00Z", NOW)["points"] == []


# ---------------------------------------------------------------------------
# 8 — degradation: a failed feed must never be able to stop the crypto ledger
# ---------------------------------------------------------------------------
def _routed_getter(routes, default=("unreachable", {}, 0)):
    """A stand-in for cg.get. Routes by path prefix; never touches the network."""
    def _g(session, path, params=None, **kw):
        for prefix, (status, data, http) in routes.items():
            if path.startswith(prefix):
                return {"status": status, "detail": f"stub {status} for {path}",
                        "data": data, "http_status": http}
        status, data, http = default
        return {"status": status, "detail": f"stub {status} for {path}",
                "data": data, "http_status": http}
    return _g


_LIST = [{"id": "nvidia", "symbol": "nvda", "name": "Nvidia", "asset_type": "stock"}]
_ISSUER_LIST = [{"id": "xstocks-ecosystem", "name": "xStocks"}]
_ISSUER = {"id": "xstocks-ecosystem", "name": "xStocks", "market_cap": 4e8,
           "volume_24h": 2e7, "market_cap_change_24h": 1e5,
           "updated_at": "2026-09-01T01:00:00Z",
           "tokens": [{"id": "nvidia-xstock", "symbol": "nvdax", "name": "NVIDIA xStock",
                       "platforms": {"solana": "0x1"}}]}


def test_a_dead_universe_feed_degrades_the_artifact_and_does_not_raise(tmp_path):
    """main() is a straight-line function with no exception handler around the feeds.
    Anything this module throws would stop a ledger that has been committing since
    August, for a board that did not exist yesterday."""
    art = rwa.snapshot(session={"plan": "keyless", "status": "unconfigured"},
                       getter=_routed_getter({}, default=("rate_limited", {}, 429)),
                       sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path, write=False)
    assert art["status"] == "unavailable"
    assert art["board"] == [] and art["feeds"]["list"]["status"] == "rate_limited"
    assert "unavailable" in art["detail"]


def test_the_paid_endpoints_are_recorded_as_declared_absences(tmp_path):
    """A reader of the artifact must be able to see what this model could not ask for.
    An absent input that is invisible is a model nobody can audit."""
    art = rwa.snapshot(
        session={"plan": "keyless", "status": "unconfigured"},
        getter=_routed_getter({"/rwas/list": ("live", _LIST, 200),
                               "/rwas/markets": ("live", [_market_row()], 200),
                               "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
                               "/rwas/issuers/": ("live", _ISSUER, 200),
                               "/coins/markets": ("live", [], 200)}),
        sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path, write=False)
    for name in ("tickers", "market_chart"):
        assert art["feeds"][name]["status"] == "unavailable"
        assert art["feeds"][name]["http_status"] == 401
    assert art["model"]["execution_weight"] > 0, "a declared component cannot weigh nothing"
    assert art["model"]["max_coverage_on_this_plan"] < 100.0
    assert art["execution"]["status"] == "UNAVAILABLE"
    assert "bid/ask" in art["execution"]["missing_fields"]
    assert "blend" in art["tape_note"]


def test_a_full_night_writes_all_three_ledgers_and_the_artifact(tmp_path):
    art = rwa.snapshot(
        session={"plan": "keyless", "status": "unconfigured"},
        getter=_routed_getter({
            "/rwas/list": ("live", _LIST, 200),
            "/rwas/markets": ("live", [_market_row()], 200),
            "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
            "/rwas/issuers/": ("live", _ISSUER, 200),
            "/coins/markets": ("live", [{"id": "nvidia-xstock", "symbol": "nvdax",
                                         "current_price": 200.0, "market_cap": 2e8,
                                         "total_volume": 6.0e6,
                                         "price_change_percentage_24h": 1.4,
                                         "last_updated": _stamp(0.5)}], 200)}),
        sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path, write=True)
    assert art["status"] == "live"
    for name in ("rwa_flow.csv", "rwa_issuers.csv", "rwa_wrappers.csv", "rwa.json"):
        assert (tmp_path / name).exists(), f"{name} was not written"
    assert art["written"]["rwa_flow.csv"] == 1
    flow = rwa.read_rows(tmp_path / "rwa_flow.csv", rwa.RWA_FLOW_FIELDS)
    assert flow[0]["spec_hash"] == rwa.spec_hash(), "every row carries its model's identity"
    assert flow[0]["impulse"] == rwa.IMPULSE_UNREADABLE, "night one has no prior to compare"
    assert art["graph"]["list_only_n"] >= 0 and "named absence" in art["graph"]["list_only_note"]


def test_the_chain_extends_across_two_nights(tmp_path):
    """The end-to-end property the whole module exists for: night two must produce a
    real issuance reading against night one's recorded row."""
    routes = {
        "/rwas/list": ("live", _LIST, 200),
        "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
        "/rwas/issuers/": ("live", _ISSUER, 200),
        "/coins/markets": ("live", [{"id": "nvidia-xstock", "symbol": "nvdax",
                                     "current_price": 200.0, "market_cap": 2e8,
                                     "total_volume": 6.0e6, "last_updated": _stamp(0.5)}], 200),
    }
    sess = {"plan": "keyless", "status": "unconfigured"}
    rwa.snapshot(session=sess, sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path,
                 getter=_routed_getter({**routes,
                                        "/rwas/markets": ("live", [_market_row(price=200.0,
                                                                               mcap=1.0e9)], 200)}))
    night2 = NOW + timedelta(days=1)
    art = rwa.snapshot(session=sess, sleep=lambda *_: None, now=night2, ledger_dir=tmp_path,
                       getter=_routed_getter({**routes,
                                              "/rwas/markets": ("live", [_market_row(price=210.0,
                                                                                     mcap=1.18e9)], 200)}))
    rec = art["board"][0]
    # P +5%, MC +18% -> units +12.38%. The brief's second worked example, end to end.
    assert rec["flow"]["residual_pct"] == pytest.approx(12.381, abs=0.01)
    assert rec["flow"]["impulse"] == rwa.IMPULSE_MINTING
    assert rec["flow"]["supply_index"] == pytest.approx(112.381, abs=0.01)
    assert rec["absent"] == ["execution"], "the impulse component exists now"
    assert rec["coverage"] < 100.0, "execution can never be priced on this plan"
    rows = rwa.read_rows(tmp_path / "rwa_flow.csv", rwa.RWA_FLOW_FIELDS)
    assert len(rows) == 2 and [r["date"] for r in rows] == sorted(r["date"] for r in rows)


# ---------------------------------------------------------------------------
# standalone entrypoint
# ---------------------------------------------------------------------------
# Same dual-mode shape as tests/test_parity.py and tests/test_atr_eligibility.py, and
# for the same reason: the nightly workflow must be able to gate on this without pytest
# installed, because a missing package must never be able to break the ledger commit —
# and, run that way, a missing package must be a FAILURE here rather than a silent skip.
def _standalone() -> int:
    """Run every check without pytest.

    Same dual-mode shape as tests/test_parity.py and tests/test_atr_eligibility.py, and
    for the same reason: the nightly must be able to gate on this without pytest
    installed, because a missing package must never break the ledger commit.

    The expected count is computed up front and asserted at the end. The first version of
    this runner reported 68 checks where pytest ran 73 — it was skipping five and saying
    nothing, which is precisely the shape of failure this repository has already had to
    remove once: a gate reporting a pass it did not earn.
    """
    import shutil
    import tempfile
    import traceback

    fns = [(n, o) for n, o in sorted(globals().items())
           if n.startswith("test_") and callable(o) and getattr(o, "__module__", None)
           == __name__]

    def cases_for(fn):
        for mark in getattr(fn, "pytestmark", []):
            if mark.name == "parametrize":
                return [c if isinstance(c, (tuple, list)) else (c,) for c in mark.args[1]]
        return None

    expected = sum(len(cases_for(fn) or [None]) for _, fn in fns)

    # Cross-checked against the FILE, not just against what is in scope. The first
    # version of this gate ran 68 of 73 checks because the entrypoint sat above five
    # tests that had not been defined yet when it fired — and it happily reported
    # "68 expected", because it counted the same truncated namespace it ran. A gate that
    # derives its own target from its own blind spot cannot see one.
    import re as _re
    in_source = _re.findall(r"^def (test_\w+)", Path(__file__).read_text(encoding="utf-8"),
                            _re.M)
    missing = sorted(set(in_source) - {n for n, _ in fns})
    if missing:
        print(f"[rwa-gate] FAIL {len(missing)} test(s) are defined in this file but were "
              f"not in scope when the runner started: {missing}")
        return 1

    failed, ran = [], 0
    for name, fn in fns:
        cases = cases_for(fn)
        wants_tmp = "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]
        for args in (cases if cases else [()]):
            tmp = Path(tempfile.mkdtemp()) if wants_tmp else None
            try:
                fn(*(args if not wants_tmp else (tmp,)))
                ran += 1
            except Exception:  # noqa: BLE001
                failed.append((name + (f"{args}" if cases else ""), traceback.format_exc()))
            finally:
                if tmp is not None:
                    shutil.rmtree(tmp, ignore_errors=True)

    for name, tb in failed:
        print(f"FAIL {name}\n{tb}")
    print(f"[rwa-gate] {ran} check(s) run, {len(failed)} failed, {expected} expected")
    if ran + len(failed) != expected:
        print(f"[rwa-gate] FAIL the runner reached {ran + len(failed)} of {expected} "
              f"checks — a check was silently skipped, which is worse than one failing")
        return 1
    return 1 if failed else 0




# ---------------------------------------------------------------------------
# 9 — the whole graph, against a capture of the live one
# ---------------------------------------------------------------------------
# Everything above tests one case per family. This tests the population, because the
# defect this file exists to prevent was measured on a sample: the ticker rules resolve
# 260 of 262 tokens across two issuers and 460 of 1,073 across all thirty-four, and
# nothing raises in between. The graph still builds; 613 wrappers are simply not in it.
FIXTURE = HERE / "fixtures" / "rwa_graph_2026-09-01.json"
# Measured on the capture. A floor rather than the exact figure, so a rule that resolves
# MORE is not a failure — but a rule change that loses coverage is.
MIN_RESOLUTION_PCT = 99.0


def _live_graph():
    fx = json.loads(FIXTURE.read_text())
    issuers = [{"id": i["id"], "name": i["name"],
                "tokens": [{**t, "platforms": {}} for t in i["tokens"]]}
               for i in fx["issuers"]]
    return fx, rwa.build_graph(fx["underlyings"], issuers)


def test_the_join_resolves_the_whole_live_graph():
    fx, g = _live_graph()
    total = len(g["wrappers"]) + len(g["unresolved"])
    assert total == 1073, f"the capture changed shape: {total} tokens"
    pct = 100.0 * len(g["wrappers"]) / total
    assert pct >= MIN_RESOLUTION_PCT, (
        f"join resolution fell to {pct:.1f}% over the captured graph "
        f"({len(g['unresolved'])} unresolved) — a naming convention rule was lost")


def test_every_unresolved_token_wraps_something_the_universe_does_not_carry():
    """The residue must be an absence in CoinGecko's own universe, not a gap in these
    rules. All eight are pre-IPO or otherwise unlisted — OpenAI, SpaceX, Kalshi."""
    fx, g = _live_graph()
    known = {u["symbol"].lower() for u in fx["underlyings"]}
    for u in g["unresolved"]:
        assert (u["symbol"] or "").lower() not in known, (
            f"{u['symbol']} is unresolved but its ticker IS in the universe — that is a "
            f"rule gap, not an absence")


def test_no_issuer_shelf_is_silently_missing_from_the_graph():
    """The failure mode that a headline percentage hides. Ondo is 438 of the 1,073
    tokens: lose its rule and the graph still reports 59% resolution and looks merely
    imperfect, while the single largest issuer is entirely absent."""
    fx, g = _live_graph()
    got = {w["issuer_id"] for w in g["wrappers"]}
    for iss in fx["issuers"]:
        # Only shelves large enough that losing one is a rule failure rather than an
        # absence. Several issuers list a single token whose underlying CoinGecko does
        # not carry at all — Figure's FGRS, Tessera's Kalshi — and those resolve to
        # nothing correctly.
        if len(iss["tokens"]) < 5:
            continue
        assert iss["id"] in got, (
            f"not one of {iss['id']}'s {len(iss['tokens'])} token(s) resolved — an entire "
            f"shelf is missing from the graph")


def test_the_share_classes_stay_apart():
    """Alphabet has two listed classes and they are different instruments. Every wrapper
    that resolves to one must not also be reachable from the other, or a dispersion is
    computed across a spread that is real and permanent."""
    fx, g = _live_graph()
    a = {w["token_id"] for w in g["by_underlying"].get("alphabet-class-a", [])}
    c = {w["token_id"] for w in g["by_underlying"].get("alphabet-class-c", [])}
    assert a and not (a & c), "a wrapper is attached to both Alphabet share classes"


def test_no_etf_wrapper_is_attached_to_a_metal():
    """The metal-name rule ordering, checked over the population rather than over the
    three cases someone thought of. Sixteen ETF wrappers carry 'Gold' or 'Silver' in
    their name."""
    fx, g = _live_graph()
    by_id = {u["id"]: u for u in fx["underlyings"]}
    for metal in ("gold", "silver"):
        for w in g["by_underlying"].get(metal, []):
            name = (w["name"] or "").lower()
            assert "etf" not in name and "trust" not in name and "shares" not in name, (
                f"{w['symbol']} ({w['name']}) is attached to spot {metal}; it wraps a fund")




def test_a_degraded_run_may_never_replace_a_complete_one(tmp_path):
    """THE invariant. Three real failures motivated it: a 429 lost an issuer, a 414 lost
    250 wrappers, and a same-day re-run overwrote a real impulse with a fabricated zero.
    A run that saw less than the run before it does not get to publish over it."""
    routes = {
        "/rwas/list": ("live", _LIST, 200),
        "/rwas/markets": ("live", [_market_row()], 200),
        "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
        "/rwas/issuers/": ("live", _ISSUER, 200),
        "/coins/markets": ("live", [{"id": "nvidia-xstock", "symbol": "nvdax",
                                     "current_price": 200.0, "market_cap": 2e8,
                                     "total_volume": 6.0e6, "last_updated": _stamp(0.5)}], 200),
    }
    sess = {"plan": "keyless", "status": "unconfigured"}
    good = rwa.snapshot(session=sess, getter=_routed_getter(routes), sleep=lambda *_: None,
                        now=NOW, ledger_dir=tmp_path)
    assert good["run"]["status"] == rwa.RUN_COMPLETE and good["run"]["promoted"]
    assert good["written"]["rwa_flow.csv"] == 1
    before = (tmp_path / "rwa_flow.csv").read_text()

    # Same day, same directory. The wrapper batch is now rate-limited — every other feed
    # is live, which is exactly how the 414 presented: a run that looks fine and is not.
    bad = rwa.snapshot(session=sess, sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path,
                       getter=_routed_getter({**routes,
                                              "/coins/markets": ("rate_limited", {}, 429)}))
    assert bad["run"]["status"] == rwa.RUN_DEGRADED
    assert bad["run"]["promoted"] is False
    assert "rwa_flow.csv" not in bad["written"]
    assert (tmp_path / "rwa_flow.csv").read_text() == before, (
        "a degraded run published over a complete canonical observation")

    # Retained as evidence, in a file nothing derives from.
    assert bad["quarantined"]["prior_status"] == rwa.RUN_COMPLETE
    q = rwa.read_rows(tmp_path / "rwa_quarantine.csv", rwa.RWA_RUN_FIELDS)
    assert len(q) == 1 and q[0]["run_status"] == rwa.RUN_DEGRADED
    # And the manifest records BOTH attempts, promoted flag and all.
    runs = rwa.read_rows(tmp_path / "rwa_runs.csv", rwa.RWA_RUN_FIELDS)
    assert [r["promoted"] for r in runs] == ["1", "0"]
    # The degraded board is inspectable but not canonical.
    assert (tmp_path / "rwa.degraded.json").exists()
    assert json.loads((tmp_path / "rwa.json").read_text())["run"]["status"] == rwa.RUN_COMPLETE


def test_a_complete_run_may_replace_a_degraded_one(tmp_path):
    """The rule is a ranking, not a lock. A night that recovers must be able to publish
    over the night that did not."""
    routes = {
        "/rwas/list": ("live", _LIST, 200),
        "/rwas/markets": ("live", [_market_row()], 200),
        "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
        "/rwas/issuers/": ("live", _ISSUER, 200),
    }
    px = ("live", [{"id": "nvidia-xstock", "symbol": "nvdax", "current_price": 200.0,
                    "market_cap": 2e8, "total_volume": 6.0e6, "last_updated": _stamp(0.5)}], 200)
    sess = {"plan": "keyless", "status": "unconfigured"}
    first = rwa.snapshot(session=sess, sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path,
                         getter=_routed_getter({**routes,
                                                "/coins/markets": ("rate_limited", {}, 429)}))
    assert first["run"]["status"] == rwa.RUN_DEGRADED and first["run"]["promoted"] is True
    second = rwa.snapshot(session=sess, sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path,
                          getter=_routed_getter({**routes, "/coins/markets": px}))
    assert second["run"]["status"] == rwa.RUN_COMPLETE and second["run"]["promoted"] is True
    assert second["written"]["rwa_flow.csv"] == 1


def test_the_promotion_rule_in_one_place():
    """Stated as a function so it can be tested without a filesystem."""
    assert rwa.may_promote(rwa.RUN_COMPLETE, None) is True
    assert rwa.may_promote(rwa.RUN_FAILED, None) is False
    assert rwa.may_promote(rwa.RUN_DEGRADED, rwa.RUN_COMPLETE) is False
    assert rwa.may_promote(rwa.RUN_COMPLETE, rwa.RUN_DEGRADED) is True
    # Equal ranks replace, which is what keeps an identical re-run idempotent.
    assert rwa.may_promote(rwa.RUN_DEGRADED, rwa.RUN_DEGRADED) is True


def test_coverage_breaks_the_tie_inside_one_status():
    """Status alone was not enough, and the live run showed it: a night where one issuer
    429s is DEGRADED, and a retry that also lost a whole wrapper batch is DEGRADED too.
    Equal rank — so under a rank-only rule the thinner run replaced the fuller one."""
    fuller = (rwa.RUN_RANK[rwa.RUN_DEGRADED], 66.7)
    assert rwa.may_promote(rwa.RUN_DEGRADED, rwa.RUN_DEGRADED, 33.3, fuller) is False
    assert rwa.may_promote(rwa.RUN_DEGRADED, rwa.RUN_DEGRADED, 66.7, fuller) is True
    assert rwa.may_promote(rwa.RUN_DEGRADED, rwa.RUN_DEGRADED, 100.0, fuller) is True
    # ...and rank still dominates coverage.
    assert rwa.may_promote(rwa.RUN_DEGRADED, rwa.RUN_COMPLETE, 100.0,
                           (rwa.RUN_RANK[rwa.RUN_COMPLETE], 66.7)) is False


def test_observations_are_recorded_before_anything_is_derived_from_them(tmp_path):
    """The vendor's own fields and its own timestamp, in their own file. A calculation
    whose inputs were never written down is not auditable later."""
    routes = {
        "/rwas/list": ("live", _LIST, 200),
        "/rwas/markets": ("live", [_market_row()], 200),
        "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
        "/rwas/issuers/": ("live", _ISSUER, 200),
        "/coins/markets": ("live", [{"id": "nvidia-xstock", "symbol": "nvdax",
                                     "current_price": 200.0, "market_cap": 2e8,
                                     "total_volume": 6.0e6, "last_updated": _stamp(0.5)}], 200),
    }
    rwa.snapshot(session={"plan": "keyless"}, getter=_routed_getter(routes),
                 sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path)
    obs = rwa.read_rows(tmp_path / "rwa_observed.csv", rwa.RWA_OBSERVED_FIELDS)
    assert len(obs) == 1
    row = obs[0]
    assert row["source_last_updated"], "the vendor's own timestamp must be recorded"
    assert row["run_ts"] and row["run_ts"] != row["source_last_updated"], (
        "our run time is not the observation time and must not stand in for it")
    # OBSERVED only: no derived column may appear in this file.
    for derived in ("residual_pct", "conviction", "label", "impulse", "supply_index"):
        assert derived not in rwa.RWA_OBSERVED_FIELDS


def test_a_row_from_an_incomplete_peer_set_is_marked_degraded(tmp_path):
    """An incomplete cross-section does not merely hide signal, it manufactures it — the
    414 published medians computed from whichever peers survived, and those rows looked
    exactly like rows computed over a whole set."""
    graph = _graph_fixture()          # nvidia has two wrappers
    prices = {"nvidia-xstock": {"price": 200.0, "volume_24h": 5.0e6, "market_cap": 2e8,
                                "last_updated": _stamp(0.5)}}   # ...only one priced
    rec = rwa.assemble([_market_row()], graph, prices, {}, "2026-09-01", NOW)
    assert rec["flow_rows"][0]["peer_set_complete"] == 0
    assert rec["flow_rows"][0]["degraded"] == 1
    both = {**prices, "wrapped-nvidia-xstock": {"price": 201.0, "volume_24h": 1e6,
                                                "market_cap": 1e8, "last_updated": _stamp(0.5)}}
    clean = rwa.assemble([_market_row()], graph, both, {}, "2026-09-01", NOW)
    assert clean["flow_rows"][0]["peer_set_complete"] == 1
    assert clean["flow_rows"][0]["degraded"] == 0


def test_publication_is_atomic(tmp_path):
    """A process killed midway through a direct write leaves a truncated file that still
    parses as CSV — a shorter ledger that looks like a real one."""
    target = tmp_path / "x.csv"
    rwa._atomic_write(target, "a,b\r\n1,2\r\n")
    # Bytes, not read_text(): universal-newline translation on READ would hide whether
    # the CRLF the csv module writes actually reached the disk, and the existing ledgers
    # in this repository are CRLF.
    assert target.read_bytes() == b"a,b\r\n1,2\r\n"
    assert not list(tmp_path.glob("*.tmp")), "the temp file must not survive the rename"


def test_wrapper_batches_fit_a_query_string():
    """Wrapper ids are slugs, not tickers — a mean of 33 characters and a max of 83 — so
    250 of them is a 9,200-character URI and the server answers `414 Request-URI Too
    Large` in HTML rather than JSON. That happened: a batch of 250 wrappers vanished from
    a run that reported itself as merely "partial", and every underlying they belonged to
    left the board with nothing saying the cause was a URL length."""
    fx = json.loads(FIXTURE.read_text())
    ids = [t["id"] for i in fx["issuers"] for t in i["tokens"]]
    batches = rwa.chunk_ids(ids)
    assert sum(len(b) for b in batches) == len(ids), "ids were lost in the split"
    assert all(b for b in batches), "an empty batch would send ids= with no ids"
    for b in batches:
        assert len(",".join(b)) <= rwa.WRAPPER_QUERY_BUDGET
        assert len(b) <= rwa.WRAPPER_CHUNK_MAX
    # And the naive count-based split is genuinely over the line, so this is not academic.
    assert len(",".join(ids[:250])) > 4000


def test_a_single_oversized_id_still_gets_its_own_batch():
    assert rwa.chunk_ids(["x" * 5000, "y"]) == [["x" * 5000], ["y"]]


# ---------------------------------------------------------------------------
# restored: denomination handling and the adversarial-review regressions
# ---------------------------------------------------------------------------
# These were deleted by an over-wide edit while the evidence contract was being written,
# and the standalone runner's source cross-check is what caught it. They are the pins on
# the +300,311bp denomination defect and on the twenty-two findings an adversarial review
# confirmed; losing them silently would have been worse than never writing them.
GOLD_LIVE = [("paxg", 4435.90, 121_523_395),    # one troy ounce
             ("xgz", 142.70, 430_391),          # one gram (4435.90 / 31.1035 = 142.62)
             ("xaum", 4430.30, 388_637),        # one troy ounce
             ("ggbr", 4.43, 121_294),           # one thousandth of an ounce
             ("kau", 142.95, 33_967)]           # one gram


NETFLIX_LIVE = [("nflxb", 80.73, 1_817_556),    # one tenth of a share
                ("nflxon", 810.92, 1_372_682)]  # one share


def _live_wrappers(rows, now=NOW):
    out = []
    for sym, px, vol in rows:
        w = {"token_id": sym, "symbol": sym, "price": px, "volume_24h": vol,
             "last_updated": _stamp(1, now), "join_rule": rwa.JOIN_EXACT, "chains": ["eth"]}
        w["liveness"] = rwa.wrapper_liveness(w, now)
        out.append(w)
    return out


def test_a_unit_difference_is_never_reported_as_a_basis():
    """PAXG is a troy ounce and KAU is a gram. Their ratio is 31.1035 because that is how
    many grams are in an ounce, not because one of them is mispriced by 3000%."""
    d = rwa.dislocations(_live_wrappers(GOLD_LIVE), NOW)
    assert d["status"] == "live"
    assert d["comparable_n"] == 2 and d["live_n"] == 5
    assert d["median_price"] == pytest.approx(4433.10, abs=0.01)
    assert d["dispersion_bps"] < 50, (
        f"dispersion is {d['dispersion_bps']}bp across two ounce-denominated wrappers")
    assert all(abs(l["basis_bps"]) < 500 for l in d["legs"])


def test_the_other_denominations_are_reported_rather_than_dropped():
    """A market quoted in both ounces and grams is a real fact about that market. An
    engine that simply showed fewer rows would be hiding it."""
    d = rwa.dislocations(_live_wrappers(GOLD_LIVE), NOW)
    others = {o["symbol"]: o["ratio_to_reference"] for o in d["other_denominations"]}
    assert set(others) == {"xgz", "kau", "ggbr"}
    assert others["kau"] == pytest.approx(1 / 31.1035, rel=0.02), "one gram per troy ounce"
    assert others["ggbr"] == pytest.approx(0.001, rel=0.02)
    for o in d["other_denominations"]:
        assert "no unit metadata" in o["reason"]


def test_the_anchor_is_the_deepest_wrapper_and_not_the_median():
    """Gold's median across five live wrappers is $142.95 — a gram — while 99.6% of the
    dollar volume is in ounces. A median over a mixed set describes no instrument."""
    live = _live_wrappers(GOLD_LIVE)
    assert rwa._median([w["price"] for w in live]) == pytest.approx(142.95)
    comp = rwa.comparable_set(live)
    assert comp["reference"]["symbol"] == "paxg", "the anchor must follow the volume"


def test_two_wrappers_at_different_units_yield_no_basis_at_all():
    """Netflix has a one-share wrapper and a tenth-share wrapper and nothing else live.
    Which one is 'right' cannot be determined without the denomination, so no basis is
    asserted — rather than a 8,189bp reading in each direction, which is what the naive
    median produced."""
    d = rwa.dislocations(_live_wrappers(NETFLIX_LIVE), NOW)
    assert d["status"] == "insufficient" and d["legs"] == []
    assert d["comparable_n"] == 1 and d["live_n"] == 2
    assert len(d["other_denominations"]) == 1


def test_a_real_dispersion_inside_one_denomination_still_surfaces():
    """The guard must not swallow the signal it was built beside. Silver's two live
    wrappers are both one ounce and genuinely 237bp apart."""
    silver = [("kag", 66.88, 193_804), ("silv", 65.33, 103_521)]
    d = rwa.dislocations(_live_wrappers(silver), NOW)
    assert d["status"] == "live" and d["comparable_n"] == 2
    assert d["dispersion_bps"] == pytest.approx(237.3, abs=1.0)
    assert len(d["legs"]) == 2


def test_a_broken_cross_section_does_not_reach_the_integrity_score(tmp_path):
    """The same defect fed the model. Gold's naive dispersion was 10,003,318bp, which
    would drive its integrity component to zero and label a healthy market FRAGILE."""
    d = rwa.dislocations(_live_wrappers(GOLD_LIVE), NOW)
    healthy = rwa.score_integrity(d["dispersion_bps"], 1.0, 0)
    broken = rwa.score_integrity(10_003_318.3, 1.0, 0)
    assert healthy > broken
    assert healthy > 0.75 * rwa.W_INTEGRITY, "a 13bp cross-section is not a broken one"


def test_a_different_denomination_does_not_cost_a_wrapper_its_integrity_score():
    """The same defect in a second place. KAU is a gram and the ounce median is $4,433:
    scoring its distance from that median as disagreement would rate a perfectly coherent
    token zero on the component named integrity."""
    live = _live_wrappers(GOLD_LIVE)
    d = rwa.dislocations(live, NOW)
    peer = {"total_volume": 1e8, "median_price": d["median_price"],
            "other_denominations": {o["token_id"] for o in d["other_denominations"]}}
    kau = next(w for w in live if w["symbol"] == "kau")
    paxg = next(w for w in live if w["symbol"] == "paxg")
    gram = rwa.wrapper_score(kau, peer, {})
    ounce = rwa.wrapper_score(paxg, peer, {})
    assert gram["components"]["integrity"] > 0.5 * rwa.W_INTEGRITY, (
        "a gram-denominated token was scored as if it disagreed with the ounce median")
    assert "denomination" in gram["reason"]
    assert ounce["components"]["integrity"] >= gram["components"]["integrity"]


def test_a_lost_wrapper_batch_is_retried_once():
    """These are 250-id calls, the heaviest thing this module asks for, so they meet a
    429 first. Losing one costs 250 wrappers — and every underlying they belonged to
    drops off the board even though its own tape was fetched fine. Observed keyless: two
    lost batches took the board from 247 ranked to 48."""
    calls = {"n": 0}

    def flaky(session, path, params=None, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"status": "rate_limited", "detail": "429", "data": {}, "http_status": 429}
        ids = [i for i in (params or {}).get("ids", "").split(",") if i]
        return {"status": "live", "detail": "ok", "http_status": 200,
                "data": [{"id": i, "symbol": i, "current_price": 1.0, "total_volume": 2e6,
                          "last_updated": _stamp(1)} for i in ids]}

    rep = rwa.fetch_wrapper_coins({"plan": "keyless"}, [f"t{i}" for i in range(500)],
                                  getter=flaky, sleep=lambda *_: None, chunk=250)
    assert rep["status"] == "live" and len(rep["data"]) == 500
    assert calls["n"] == 3, "the failed batch must be retried exactly once, not looped"


def test_a_persistent_failure_still_ends_and_is_reported():
    """One retry, not a loop. A genuine outage must not spin."""
    calls = {"n": 0}

    def dead(session, path, params=None, **kw):
        calls["n"] += 1
        return {"status": "rate_limited", "detail": "429", "data": {}, "http_status": 429}

    rep = rwa.fetch_wrapper_coins({"plan": "keyless"}, [f"t{i}" for i in range(300)],
                                  getter=dead, sleep=lambda *_: None, chunk=250)
    assert rep["status"] == "unreachable" and calls["n"] == 4  # 2 batches, each tried twice


def test_the_fetch_spacing_follows_the_probed_plan():
    """Keyless tolerates 10-15 requests a rolling minute and a Demo key 30. One constant
    for both means the keyed run wastes four minutes or the keyless run spends it in
    backoff, and this module makes about forty-five calls a night."""
    assert rwa.fetch_delay({"plan": "demo"}) < rwa.fetch_delay({"plan": "keyless"})
    assert rwa.fetch_delay(None) == rwa.FETCH_DELAY_KEYLESS, "unknown must take the safe side"


def test_a_same_day_rerun_does_not_overwrite_a_real_impulse_reading(tmp_path):
    """The worst defect this module could have had. append_daily_rows replaces today's
    rows and the nightly carries workflow_dispatch, so a re-run is an expected mode — and
    the second run was reading the row the first had just written, comparing tonight's
    price and cap against themselves, and recording a residual of exactly 0.0 labelled
    NEUTRAL over a real minting night. In a ledger market_chart cannot backfill, that is
    unrecoverable."""
    routes = {
        "/rwas/list": ("live", _LIST, 200),
        "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
        "/rwas/issuers/": ("live", _ISSUER, 200),
        "/coins/markets": ("live", [{"id": "nvidia-xstock", "symbol": "nvdax",
                                     "current_price": 200.0, "market_cap": 2e8,
                                     "total_volume": 6.0e6, "last_updated": _stamp(0.5)}], 200),
    }
    sess = {"plan": "keyless", "status": "unconfigured"}

    def run(now, price, mcap):
        return rwa.snapshot(session=sess, sleep=lambda *_: None, now=now,
                            ledger_dir=tmp_path,
                            getter=_routed_getter({**routes, "/rwas/markets":
                                                   ("live", [_market_row(price=price,
                                                                         mcap=mcap)], 200)}))

    run(NOW, 200.0, 1.0e9)                              # night one
    n2 = NOW + timedelta(days=1)
    first = run(n2, 200.0, 1.2e9)["board"][0]["flow"]    # price flat, units +20%
    assert first["residual_pct"] == pytest.approx(20.0, abs=0.01)
    assert first["impulse"] == rwa.IMPULSE_STRONG

    again = run(n2, 200.0, 1.2e9)["board"][0]["flow"]    # the retry
    assert again["residual_pct"] == pytest.approx(20.0, abs=0.01), (
        "a same-day re-run recomputed the residual against its own output")
    assert again["impulse"] == rwa.IMPULSE_STRONG
    rows = [r for r in rwa.read_rows(tmp_path / "rwa_flow.csv", rwa.RWA_FLOW_FIELDS)
            if r["date"] == n2.strftime("%Y-%m-%d")]
    assert len(rows) == 1 and float(rows[0]["residual_pct"]) == pytest.approx(20.0, abs=0.01)
    assert again["chain_days"] == first["chain_days"], "the re-run double-counted the day"


def test_a_contradicted_join_never_sets_the_median_or_the_dispersion():
    """build_graph keeps a contradicted edge and withholds the reading. A wrapper that
    might be a different company setting the median — and therefore the dispersion that
    reaches score_integrity and the flow ledger — IS that reading by another route."""
    good = _live_wrappers([("nvda-x", 180.00, 5_000_000), ("nvda-a", 180.10, 2_000_000)])
    bad = _live_wrappers([("nvda-c", 160.00, 1_000_000)])
    bad[0]["join_rule"] = rwa.JOIN_CONFLICT
    clean = rwa.dislocations(good, NOW)
    mixed = rwa.dislocations(good + bad, NOW)
    assert mixed["median_price"] == clean["median_price"]
    assert mixed["dispersion_bps"] == clean["dispersion_bps"]
    assert mixed["contradicted_n"] == 1, "the exclusion must still be reported"
    assert rwa.score_integrity(mixed["dispersion_bps"], 1.0, 0) == \
        rwa.score_integrity(clean["dispersion_bps"], 1.0, 0)


def test_a_contradicted_wrapper_is_not_scored_at_all():
    w = _live_wrappers([("nvda-c", 160.0, 1_000_000)])[0]
    w["join_rule"] = rwa.JOIN_CONFLICT
    got = rwa.wrapper_score(w, {"total_volume": 1e7, "median_price": 180.0}, {})
    assert got["score"] is None and got["label"] == rwa.RWA_UNRATED
    assert "disagree" in got["reason"]


def test_a_broken_peg_is_not_filed_as_a_denomination():
    """The guard against a fabricated signal must not quietly suppress a real one. A
    wrapper 40% off is not a unit convention, and price alone cannot prove which it is —
    so it is neither compared nor dismissed."""
    depeg = _live_wrappers([("ok-a", 100.0, 5_000_000), ("ok-b", 100.1, 4_000_000),
                            ("broke", 60.0, 3_000_000)])
    d = rwa.dislocations(depeg, NOW)
    other = {o["symbol"]: o for o in d["other_denominations"]}
    assert other["broke"]["kind"] == "unexplained"
    assert other["broke"]["unit"] is None
    assert "off its peg" in other["broke"]["reason"]
    # ...while a real unit ratio still reads as one.
    gold = rwa.dislocations(_live_wrappers(GOLD_LIVE), NOW)
    kinds = {o["symbol"]: o["kind"] for o in gold["other_denominations"]}
    assert kinds == {"xgz": "denomination", "kau": "denomination", "ggbr": "denomination"}
    assert "grams per troy ounce" in next(
        o["unit"] for o in gold["other_denominations"] if o["symbol"] == "kau")


def test_a_market_cap_of_zero_is_a_dead_feed_not_a_total_redemption():
    r = rwa.flow_residual(100.0, 1_000_000.0, 100.0, 0.0)
    assert r["residual_pct"] is None, "a published zero became a fabricated -100%"


def test_a_gap_does_not_flip_the_impulse_label(tmp_path):
    """Both legs are rebased for the span. Rebasing only the residual made a 6% move
    across seven nights read as 6% 'today', pushing quiet supply builds out of
    STRONG_ADOPTION into MINTING because of an outage."""
    prior = {"nvidia": {"date": "2026-08-25", "price": "100", "market_cap": "1000",
                        "supply_index": "100", "span_days": "1"}}
    rows = [{"id": "nvidia", "symbol": "nvda", "name": "Nvidia", "asset_type": "stock",
             "tokenized_market_data": {"current_price": 106.0, "market_cap": 1180.0,
                                       "last_updated": _stamp(1)}}]
    flow = rwa.assemble(rows, {"by_underlying": {}}, {}, prior, "2026-09-01",
                        NOW)["flow_rows"][0]
    assert flow["span_days"] == 7
    assert flow["impulse"] == rwa.IMPULSE_STRONG, (
        "a 6% price move over seven nights is 0.8% a day, which is flat")


def test_distribution_counts_only_the_wrappers_that_are_markets():
    """Dinari lists 132 tokens across five chains and one of them trades. Counting
    issuers and chains over every wrapper while counting only live ones for the wrapper
    term scored that dead shelf as broad distribution."""
    broad = rwa.score_distribution(1, 5, 5, 1.0)
    narrow = rwa.score_distribution(1, 1, 1, 1.0)
    assert broad > narrow          # the function itself is fine...
    # ...so the fix is in assemble: issuers and chains come from `live`.
    src = (ROOT / "rwa.py").read_text()
    body = src[src.index("issuers_here = {"):src.index("conflicts = sum(")]
    assert "for w in live" in body and "for w in priced" not in body.split("issuers_listed")[0]


def test_the_calendar_is_bounded_at_both_ends():
    """A missing lower bound measures a backfilled 2025 date against a table in which
    every 2025 holiday is absent, so each one reads as a trading day."""
    early = datetime(2025, 7, 4, 2, 0, tzinfo=timezone.utc)
    assert rwa.session_calendar_status(early)["ok"] is False
    assert rwa.offhours_reading(_market_row(), [], None, early)["status"] == "unavailable"
    assert rwa.session_calendar_status(NOW)["ok"] is True


def test_off_hours_agreement_counts_only_wrappers_that_spoke():
    """Including silent wrappers in the denominator reports '0 of 5 agree' from a market
    where five wrappers said nothing, which reads as disagreement rather than absence."""
    spark = [100.0] * 113 + [103.8] * 55
    row = _market_row(price=103.8, spark=spark)
    row["tokenized_market_data"]["last_updated"] = _stamp(0.2, WEEKEND)
    silent = [{"price_change_pct_24h": None}, {"price_change_pct_24h": 0.0}]
    r = rwa.offhours_reading(row, silent, None, WEEKEND)
    assert r["wrappers_voting"] == 0 and r["agreement"] is None
    loud = rwa.offhours_reading(row, [{"price_change_pct_24h": 2.0}] + silent, None, WEEKEND)
    assert loud["wrappers_voting"] == 1 and loud["agreement"] == 1.0


def test_every_wrapper_on_the_board_carries_the_basis_the_table_renders():
    graph = _graph_fixture()
    prices = {"nvidia-xstock": {"price": 200.0, "volume_24h": 5.0e6, "market_cap": 2e8,
                                "last_updated": _stamp(0.5)},
              "wrapped-nvidia-xstock": {"price": 210.0, "volume_24h": 1.2e7,
                                        "market_cap": 1e8, "last_updated": _stamp(0.5)}}
    rec = rwa.assemble([_market_row()], graph, prices, {}, "2026-09-01", NOW)["board"][0]
    assert any(w.get("basis_bps") is not None for w in rec["wrappers"]), (
        "the artifact's wrapper entries carry no basis, so the column is always a dash")


def test_the_equity_leg_is_pending_and_no_vendor_was_added(tmp_path):
    """Audited before any of this was written: there is no cash-equity data in this
    repository. The sibling equity project the older modules mention appears only in their
    prose, no ledger carries a session close or an official opening print, and no equity
    provider is configured in any workflow. So the gap is PENDING and the scope stays
    where it was."""
    rep = rwa.equity_prints(tmp_path)
    assert rep["status"] == "pending" and rep["rows"] == {}
    assert rwa.EQUITY_ARTIFACT.endswith(".csv")
    assert set(rwa.EQUITY_REQUIRED_FIELDS) >= {"session_date", "official_open", "prior_close"}


def test_the_equity_interface_is_a_read_only_artifact_not_a_runtime_call(tmp_path):
    """A direct call into another project's code couples two nightlies at runtime and
    makes each one's failure the other's. A file that either exists or does not is a
    boundary that survives either side changing."""
    src = (ROOT / "rwa.py").read_text()
    for forbidden in ("import equity", "from equity", "equity_project", "requests.get"):
        assert forbidden not in src, f"rwa.py reaches for {forbidden}"
    # And when the artifact does appear, it is simply read.
    path = tmp_path / "equity_sessions.csv"
    path.write_text("symbol,session_date,prior_close,official_open\r\n"
                    "nvda,2026-08-28,180.0,182.5\r\n")
    rep = rwa.equity_prints(tmp_path)
    assert rep["status"] == "live" and "nvda" in rep["rows"]


def test_the_calendar_declares_its_own_replacement_deadline():
    """A hand-maintained calendar that refuses past its horizon is acceptable as an
    interim; becoming the permanent architecture by default is not."""
    assert rwa.CALENDAR_REPLACEMENT_DUE.startswith(str(rwa.CALENDAR_LAST_YEAR))
    src = (ROOT / "rwa.py").read_text()
    assert "MERGE PREREQUISITE" in src
    # Comment markers stripped, because the sentence wraps across two comment lines.
    prose = " ".join(l.lstrip("# ") for l in src.splitlines())
    assert "inferring that a weekday is a trading day" in " ".join(prose.split())


def test_an_age_is_never_negative_and_real_skew_is_still_reported():
    """`now` is captured when a run starts and the run takes minutes, so a wrapper priced
    at the end of it carries a vendor timestamp ahead of the run clock. Ninety-five of
    ninety-six tape legs on the first full run read -0.1h — not a price from the future,
    just the ordering of a multi-minute fetch."""
    ahead = (NOW + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    assert rwa._age_hours(ahead, NOW) == 0.0
    assert rwa._age_hours(_stamp(3), NOW) == pytest.approx(3.0)
    assert rwa._age_hours(None, NOW) is None, "unknown is not fresh"
    # ...and the lead is still measured, so a genuinely wrong clock stays visible.
    assert rwa._clock_skew_hours([ahead], NOW) == pytest.approx(0.1, abs=0.01)
    assert rwa._clock_skew_hours([_stamp(3)], NOW) == 0.0


def test_coverage_is_continuous_so_a_thinner_run_scores_lower():
    """It began as three booleans, and a live pair showed why that is not enough: a run
    that fetched 31 of 34 issuers and one that fetched 33 both scored 66.7, so the
    promotion rule could not tell them apart and the thinner one published over the
    fuller. Fractions, not flags."""
    feeds = {k: {"status": "live"} for k in ("list", "markets", "wrappers")}
    feeds["issuers"] = {"status": "partial"}
    graph = {"wrappers": [{}] * 100, "wrappers_priced": 100}
    fuller = rwa.run_completeness(feeds, graph, 640, 641, issuers_received=33, issuers_listed=34)
    thinner = rwa.run_completeness(feeds, graph, 640, 641, issuers_received=31, issuers_listed=34)
    assert fuller["status"] == thinner["status"] == rwa.RUN_DEGRADED
    assert fuller["coverage_pct"] > thinner["coverage_pct"], (
        "two runs with the same status and different data must not score the same")
    assert rwa.may_promote(rwa.RUN_DEGRADED, rwa.RUN_DEGRADED, thinner["coverage_pct"],
                           (rwa.RUN_RANK[rwa.RUN_DEGRADED], fuller["coverage_pct"])) is False


# ---------------------------------------------------------------------------
# 12 — the score / coverage contract
# ---------------------------------------------------------------------------
# A 94 beside "63% coverage" beside "max 83.3%" reads as a contradiction unless every
# denominator is stated. These pin what each number IS, so the surfaces cannot drift.
def test_the_score_is_available_evidence_normalized_and_says_so():
    comps = {"liquidity": 28.0, "distribution": 22.0, "integrity": 18.0, "impulse": None}
    c = rwa.rwa_conviction(comps)
    priced = rwa.W_LIQUIDITY + rwa.W_DISTRIBUTION + rwa.W_INTEGRITY
    assert c["score_basis"] == rwa.SCORE_BASIS == "available_evidence_normalized"
    assert c["score"] == pytest.approx(100.0 * (28 + 22 + 18) / priced, abs=0.05)
    assert c["evidence_weight_priced"] == priced
    assert c["evidence_weight_declared"] == sum(rwa.DECLARED_WEIGHTS.values())


def test_coverage_is_priced_over_declared_and_execution_is_in_the_denominator():
    c = rwa.rwa_conviction({"liquidity": 28.0, "distribution": 22.0, "integrity": 18.0,
                            "impulse": 20.0})
    declared = sum(rwa.DECLARED_WEIGHTS.values())
    assert c["coverage"] == pytest.approx(100.0 * (declared - rwa.W_EXECUTION) / declared, abs=0.05)
    assert c["coverage"] == c["max_coverage_on_this_plan"] < 100.0
    night_one = rwa.rwa_conviction({"liquidity": 28.0, "distribution": 22.0, "integrity": 18.0})
    assert night_one["coverage"] == pytest.approx(62.5, abs=0.05)


def test_effective_is_a_plain_product_and_not_what_ranks_the_board():
    """Coverage-adjusted, for anyone who wants absent evidence to count against the
    number. A product, not a new formula — and the label follows the normalized score,
    because the signal band and the evidence share are two concepts."""
    c = rwa.rwa_conviction({"liquidity": 28.0, "distribution": 22.0, "integrity": 18.0})
    assert c["effective"] == pytest.approx(c["score"] * c["coverage"] / 100.0, abs=0.1)
    assert c["label"] == rwa.rwa_label(c["score"])
    assert c["label"] != rwa.rwa_label(c["effective"]) or c["effective"] >= rwa.RWA_T_DEEP, (
        "the check is meaningful only where the two bands differ; adjust the fixture")


def test_one_concept_per_label():
    """DEEP is the RWA SIGNAL band of the normalized score — the final signal — and not
    a liquidity sub-classification. The liquidity COMPONENT is a number, never a word."""
    d = rwa.SCORE_DEFINITION
    assert "final" in d["label"] and "signal" in d["label"].lower()
    assert "not a liquidity" in d["label"]
    c = rwa.rwa_conviction({"liquidity": 5.0, "distribution": 25.0, "integrity": 20.0,
                            "impulse": 25.0})
    assert isinstance(c["label"], str) and c["label"] in rwa.RWA_LABELS
    assert not any(isinstance(v, str) for v in
                   {"liquidity": 5.0, "distribution": 25.0}.values())


def test_the_wrapper_score_carries_the_same_contract():
    w = rwa.wrapper_score(_priced("a", 100.0), {"total_volume": 1e7, "median_price": 101.0}, {})
    assert w["score_basis"] == rwa.SCORE_BASIS
    assert w["effective"] == pytest.approx(w["score"] * w["coverage"] / 100.0, abs=0.1)
    assert w["coverage"] < 100.0 and "execution" in w["absent"]


def test_the_artifact_publishes_the_definition_and_both_denominators(tmp_path):
    routes = {
        "/rwas/list": ("live", _LIST, 200),
        "/rwas/markets": ("live", [_market_row()], 200),
        "/rwas/issuers/list": ("live", _ISSUER_LIST, 200),
        "/rwas/issuers/": ("live", _ISSUER, 200),
        "/coins/markets": ("live", [{"id": "nvidia-xstock", "symbol": "nvdax",
                                     "current_price": 200.0, "market_cap": 2e8,
                                     "total_volume": 6.0e6, "last_updated": _stamp(0.5)}], 200),
    }
    art = rwa.snapshot(session={"plan": "keyless"}, getter=_routed_getter(routes),
                       sleep=lambda *_: None, now=NOW, ledger_dir=tmp_path, write=False)
    d = art["model"]["score_definition"]
    for key in ("score", "coverage", "effective", "label", "wrapper_price_coverage",
                "execution_evidence"):
        assert key in d, f"the definition block omits {key}"
    row = art["board"][0]
    assert row["conviction_basis"] == rwa.SCORE_BASIS
    assert row["conviction_effective"] == pytest.approx(
        row["conviction"] * row["coverage"] / 100.0, abs=0.1)
    # Two different denominators, reported separately.
    assert art["run"]["wrappers_priced"] == 1 and art["run"]["wrappers_in_graph"] == 1
    assert row["evidence_weight_declared"] == sum(rwa.DECLARED_WEIGHTS.values())


# LAST in the file, deliberately. _standalone() reads the module namespace as it stands
# when it fires, so an entrypoint placed above a later test block runs without it and
# reports a pass over a smaller suite than exists. That is not hypothetical — it is what
# this file did until the source cross-check above was added.
if __name__ == "__main__":
    raise SystemExit(_standalone())
