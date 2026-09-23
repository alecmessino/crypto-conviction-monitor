"""Does conviction predict the next day's return?

This is the only question that decides whether the score is worth acting on, and it is
a different question from "is the basket beating the benchmark". The distinction is the
reason this module exists: the basket IS losing to equal weight — about -283bp over the
six legs since 2026-08-05 — and the obvious reading of that is "the selection is
subtracting value". The measurement does not support that reading. The information
coefficient over the same legs is +0.006 with a 95% interval of roughly [-0.09, +0.10].

A concentrated book with no measurable edge underperforms an equal-weight control as a
matter of course, because concentration adds variance without adding expected return.
So the tests below pin two things above all: that a null result is reported as a null
rather than as a small positive, and that the per-name attribution can never be mistaken
for evidence.
"""
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("edge_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


def board(date, ranking, ret=None):
    """One night: {symbol: {conviction, price}} with an optional forward return applied."""
    out = {}
    for i, sym in enumerate(ranking):
        px = 100.0 * (1.0 + (ret or {}).get(sym, 0.0))
        out[sym] = {"symbol": sym, "conviction": float(100 - i * 2), "price": px}
    return out


def two_nights(ret):
    syms = [f"S{i:02d}" for i in range(20)]
    return {"2026-03-01": board("2026-03-01", syms),
            "2026-03-02": board("2026-03-02", syms, ret)}


# ---------------------------------------------------------------------------
# the measurement
# ---------------------------------------------------------------------------
def test_a_perfectly_predictive_score_scores_ic_one():
    """Highest conviction gets the best return, monotonically."""
    syms = [f"S{i:02d}" for i in range(20)]
    ret = {s: (20 - i) / 100.0 for i, s in enumerate(syms)}
    legs = nightly._edge_legs(two_nights(ret), None)
    assert legs[0]["ic"] == pytest.approx(1.0)
    assert legs[0]["spread_bp"] > 0


def test_an_inverted_score_scores_ic_minus_one():
    """Worth a test of its own: an inverted ranking is a usable signal read backwards,
    and it must never be reported as 'no relationship'."""
    syms = [f"S{i:02d}" for i in range(20)]
    ret = {s: i / 100.0 for i, s in enumerate(syms)}
    assert nightly._edge_legs(two_nights(ret), None)[0]["ic"] == pytest.approx(-1.0)


def test_a_thin_board_is_skipped_rather_than_measured():
    """A rank correlation over a handful of names is noise with a decimal point."""
    thin = {"2026-03-01": board("2026-03-01", ["A", "B", "C"]),
            "2026-03-02": board("2026-03-02", ["A", "B", "C"], {"A": 0.1})}
    assert nightly._edge_legs(thin, None) == []


def test_legs_before_a_specification_boundary_are_excluded():
    """An IC averaged across two scoring functions is a number about a model that never
    existed."""
    syms = [f"S{i:02d}" for i in range(20)]
    days = {d: board(d, syms) for d in ("2026-03-01", "2026-03-02", "2026-03-03")}
    assert len(nightly._edge_legs(days, None)) == 2
    assert len(nightly._edge_legs(days, "2026-03-02")) == 1


def test_a_ledger_gap_is_not_a_one_day_leg():
    """The edge is a one-day IC. Two failed nightlies (2026-09-21, -22) left 09-20 and
    09-23 adjacent in the ledger, and pairing adjacent nights made a three-day return
    into a "one-day" leg — so the Selection Edge panel and the IC matrix, which already
    required the exact offset, published different numbers for the same cell."""
    syms = [f"S{i:02d}" for i in range(20)]
    days = {d: board(d, syms) for d in ("2026-03-01", "2026-03-02", "2026-03-05",
                                         "2026-03-06")}
    legs = nightly._edge_legs(days, None)
    assert [(l["from"], l["to"]) for l in legs] == [("2026-03-01", "2026-03-02"),
                                                     ("2026-03-05", "2026-03-06")]


def test_the_edge_and_the_matrix_count_the_same_legs_over_the_real_ledger():
    """The two estimators of the composite one-day cell, recomputed from the ledger on
    disk rather than read from an artifact the next nightly will overwrite."""
    by_date, _ = nightly._perf_by_date()
    if len(by_date) < 3:
        pytest.skip("no ledger history")
    boundary = nightly._compute_performance().get("spec_boundary")
    edge = nightly._edge_legs(by_date, boundary)
    mtx = nightly._ic_legs(by_date, "conviction", 1, boundary)
    assert [(l["from"], l["to"]) for l in edge] == [(l["from"], l["to"]) for l in mtx]


# ---------------------------------------------------------------------------
# the measurement contract
# ---------------------------------------------------------------------------
# These tests used to assert last night's answer: `measurable is False` and
# `abs(t_stat) < 2`. Both were snapshots of a finding wearing the clothes of a
# regression gate, and both expired the moment the finding moved — the first proxy
# (`legs < min_legs`) died on 2026-09-14 when the ledger reached EDGE_MIN_LEGS exactly,
# and its replacement died on 2026-09-15 when the forty-first leg carried the interval
# clear of zero on the NEGATIVE side (mean IC -0.0530, 95% CI [-0.1048, -0.0012],
# t = -2.004, measurable True).
#
# Nothing was wrong with the code either time. A test that pins a measurement's current
# value fails when the measurement changes, which is the one thing a measurement is
# supposed to be allowed to do.
#
# What is asserted now is the CONTRACT: how `measurable`, the interval, the t-statistic
# and the verdict are derived from the legs, and that each verdict branch is reachable
# and says what it means. Deterministic synthetic legs, because the contract has to hold
# for data the ledger has not produced yet — including the inverted branch, which until
# yesterday no live data had ever reached. No number below is a golden value copied off
# a recent run; the live ledger is checked only for INTERNAL CONSISTENCY, whatever it
# happens to say.


def legs_with(ics, names=20):
    """Synthetic edge legs carrying exactly these per-leg information coefficients."""
    return [{"from": f"2026-03-{i + 1:02d}", "to": f"2026-03-{i + 2:02d}",
             "ic": ic, "names": names, "top_quintile": 0.0,
             "bottom_quintile": 0.0, "spread_bp": 0.0}
            for i, ic in enumerate(ics)]


def ics_for(n, mean, spread=0.1):
    """`n` coefficients with exactly this mean, alternating +/- `spread` around it.

    Sample SD is then spread * sqrt(n/(n-1)) and the standard error spread/sqrt(n-1),
    so the t-statistic is mean * sqrt(n-1) / spread — chosen, not discovered, which is
    what makes each case below a statement about the contract rather than about a fit.
    """
    assert n % 2 == 0, "an even count keeps the mean exact"
    return [mean + (spread if i % 2 == 0 else -spread) for i in range(n)]


@pytest.fixture
def edge_over(monkeypatch):
    """Run the real _compute_edge() over legs we choose.

    Only the collaborators that fetch data are stubbed. The statistics — mean, variance,
    standard error, interval, t, the measurable conjunction and the verdict — are the
    production ones, which is the point: this exercises the derivation rather than
    re-implementing it in the test.
    """
    def run(ics, names=20):
        monkeypatch.setattr(nightly, "_perf_by_date", lambda: ({}, 0))
        monkeypatch.setattr(nightly, "_compute_performance", lambda: {"spec_boundary": None})
        monkeypatch.setattr(nightly, "_perf_legs", lambda *a, **k: ([], [], None, 0))
        monkeypatch.setattr(nightly, "_active_contributions", lambda *a, **k: {})
        monkeypatch.setattr(nightly, "_edge_legs",
                            lambda *a, **k: legs_with(ics, names=names))
        return nightly._compute_edge()
    return run


MIN = nightly.EDGE_MIN_LEGS


def t_slack(mean, se, t):
    """How far a t rebuilt from PUBLISHED mean and se may sit from the published t.

    t_stat is computed from the unrounded mean and standard error; mean_ic and ic_se are
    rounded to four decimals before publication, and t_stat itself to three. Dividing
    two rounded numbers therefore cannot reproduce the published quotient exactly, and
    the gap is arithmetic rather than error. Derived from the rounding granularity so
    the tolerance stays honest if the published precision ever changes, rather than
    being a constant chosen until the test went green.
    """
    half = 5e-5                      # half a unit in the last published place, 4 dp
    return abs(t) * (half / abs(mean) + half / abs(se)) + 5e-4 + 1e-9


def test_measurable_is_the_conjunction_it_claims_to_be(edge_over):
    """Enough legs AND an interval clear of zero. Neither alone, and both required.

    The two halves fail differently and for different reasons — one is a sample-size
    statement and the other an evidence statement — so each is exercised on its own.
    """
    # Enough legs, interval far from zero: the only combination that may claim anything.
    assert edge_over(ics_for(MIN, 0.08))["measurable"] is True

    # Enough legs, interval spanning zero: a small mean is not an edge.
    spans = edge_over(ics_for(MIN, 0.002))
    assert spans["measurable"] is False
    assert spans["ci"][0] < 0 < spans["ci"][1]

    # Too few legs, however clean the separation: sample size is not evidence.
    short = edge_over(ics_for(MIN - 2, 0.08))
    assert short["legs"] == MIN - 2
    assert short["measurable"] is False
    assert short["ci"][0] > 0, "the interval excluded zero and it still must not count"

    # Neither.
    assert edge_over(ics_for(MIN - 2, 0.002))["measurable"] is False


def test_the_interval_and_the_t_statistic_are_one_statement(edge_over):
    """ci = mean +/- 1.96*se and t = mean/se, so they can never disagree.

    Published side by side, they are read as corroborating each other. They do not
    corroborate — they are the same arithmetic twice — and the failure that matters is
    one drifting from the other.
    """
    for mean in (-0.20, -0.08, -0.01, 0.0, 0.01, 0.08, 0.20):
        e = edge_over(ics_for(MIN, mean))
        lo, hi = e["ci"]
        mid, half = (lo + hi) / 2, (hi - lo) / 2
        assert mid == pytest.approx(e["mean_ic"], abs=1e-3)
        assert half == pytest.approx(1.96 * e["ic_se"], abs=1e-3)
        if e["mean_ic"]:
            assert e["t_stat"] == pytest.approx(
                e["mean_ic"] / e["ic_se"],
                abs=t_slack(e["mean_ic"], e["ic_se"], e["t_stat"]))
        # With the leg count satisfied, "interval clear of zero" and "|t| past 1.96" are
        # the same condition. Borderline cases are excluded because `measurable` is
        # computed from the unrounded bounds while `ci` is published rounded.
        if abs(e["t_stat"]) > 2.1 or abs(e["t_stat"]) < 1.8:
            assert e["measurable"] is (abs(e["t_stat"]) > 1.96)


def test_the_interval_excludes_zero_from_either_side(edge_over):
    """A ranking that is reliably backwards is as measurable as one reliably right.

    A one-sided rule would report an inverted score as "not enough history yet", which
    is the failure mode this whole panel exists to prevent.
    """
    up = edge_over(ics_for(MIN, 0.08))
    down = edge_over(ics_for(MIN, -0.08))
    assert up["measurable"] is down["measurable"] is True
    assert up["ci"][0] > 0 and down["ci"][1] < 0
    assert up["t_stat"] > 0 > down["t_stat"]


def test_every_verdict_branch_is_reachable_and_says_what_it_means(edge_over):
    """Three branches, three states, each reachable from data.

    The inverted branch had never been reached by live data until 2026-09-15, so until
    then nothing tested that it existed, that it was wired to a negative mean, or that
    it said anything sensible when it fired.
    """
    informative = edge_over(ics_for(MIN, 0.08))
    assert informative["mean_ic"] > 0
    assert "informatively" in informative["verdict"]

    inverted = edge_over(ics_for(MIN, -0.08))
    assert inverted["mean_ic"] < 0
    assert "backwards" in inverted["verdict"] and "inverted" in inverted["verdict"]

    null = edge_over(ics_for(MIN, 0.002))
    assert "neither evidence" in null["verdict"]
    assert "not enough history" in null["verdict"]

    assert len({informative["verdict"], inverted["verdict"], null["verdict"]}) == 3


def test_an_inverted_result_is_a_finding_and_not_an_instruction(edge_over):
    """A measurable negative IC is evidence about the ranking, not a trade.

    "The ranking is inverted" invites "so invert it", and a panel that printed an action
    on the strength of one interval clearing zero would be doing exactly what the label
    gate in AUDIT-2026-09 1.2 exists to stop. The verdict states the finding and stops;
    the action vocabulary is not the edge panel's to emit.
    """
    verdict = edge_over(ics_for(MIN, -0.08))["verdict"]
    for word in ("STRONG", "BUY", "AVOID", "WATCH", "HOLD", "short", "sell"):
        assert word not in verdict, f"{word!r} is action vocabulary, not a finding"


def test_the_live_ledger_is_internally_consistent_whatever_it_says(edge_over):
    """The live check, holding no opinion about the answer.

    This is what replaces `measurable is False` and `abs(t_stat) < 2`. Those asserted a
    result; this asserts that the published fields agree with each other and with the
    contract above, and it holds whether tonight's interval spans zero, sits above it or
    sits below it.
    """
    e = nightly._compute_edge()
    if not e.get("mean_ic"):
        pytest.skip("fewer than two legs recorded; nothing to be consistent about")

    lo, hi = e["ci"]
    assert lo < hi
    assert (lo + hi) / 2 == pytest.approx(e["mean_ic"], abs=1e-3)
    assert (hi - lo) / 2 == pytest.approx(1.96 * e["ic_se"], abs=1e-3)
    assert e["t_stat"] == pytest.approx(
        e["mean_ic"] / e["ic_se"], abs=t_slack(e["mean_ic"], e["ic_se"], e["t_stat"]))

    clear_of_zero = lo > 0 or hi < 0
    enough = e["legs"] >= e["min_legs"]
    assert e["measurable"] is bool(enough and clear_of_zero)

    if e["measurable"]:
        expected = "informatively" if e["mean_ic"] > 0 else "backwards"
        assert expected in e["verdict"]
    else:
        assert "neither evidence" in e["verdict"]


def test_the_sample_size_still_needed_is_stated():
    """Without it, 'not measurable yet' is indistinguishable from 'never will be'."""
    e = nightly._compute_edge()
    needed = e["legs_needed"]
    assert set(needed) == {"0.02", "0.03", "0.05"}
    # A weaker signal needs more history, always.
    assert needed["0.02"] > needed["0.03"] > needed["0.05"]


def test_too_few_legs_measures_nothing_at_all():
    by_date = {"2026-03-01": board("2026-03-01", [f"S{i:02d}" for i in range(20)])}
    assert nightly._edge_legs(by_date, None) == []


# ---------------------------------------------------------------------------
# attribution is arithmetic, not evidence
# ---------------------------------------------------------------------------
def test_the_attribution_reconciles_to_the_realised_gap():
    """It adds up exactly, and "exactly" now means to floating point.

    This bound used to be 8% relative with a 15bp floor, and it was loosened once
    already when a fixed band started failing as legs accumulated. That was treating the
    symptom. The cause was that per-name contributions were summed arithmetically while
    book_total and equal_weight_total are chained geometrically, so the residual grew
    with the number of legs by construction: 312.9bp against a -1266.0bp gap at twenty
    legs, a quarter of the number the panel claimed to explain, while calling itself
    exact "by construction".

    With Carino linking the identity is exact, so the tolerance is floating point rather
    than a percentage. If this test starts failing again, the linking is wrong; do not
    widen the band.
    """
    e = nightly._compute_edge()
    a = e["attribution"]
    gap = (e["book_total"] or 0) - (e["equal_weight_total"] or 0)
    # residual_bp is computed from the unrounded chain inside the attribution itself,
    # so it is the honest measure of whether the identity holds.
    assert a["residual_bp"] == pytest.approx(0.0, abs=1e-6)
    assert a["linking"] == "carino"
    # And against the published, rounded fields: book_total and equal_weight_total are
    # rounded to 4dp and total_bp to 1dp, so 0.2bp is the display granularity and not a
    # tolerance for error.
    assert a["total_bp"] == pytest.approx(gap * 100, abs=0.2)


def test_the_attribution_decomposes_the_same_legs_the_curve_chains():
    """An attribution of a different set of days is not an attribution of that curve.

    The two used to build their own leg sets from the same dates under different
    filters: the curve dropped legs losing more than PERF_MAX_WEIGHT_LOSS of the book,
    the attribution did not, and the attribution applied an EDGE_MIN_NAMES floor the
    curve did not. Both happened to report twenty legs on the ledger as it stood, which
    is a coincidence that would have hidden the divergence indefinitely.
    """
    perf = nightly._compute_performance()
    e = nightly._compute_edge()
    a = e["attribution"]
    assert a["legs"] + a["legs_without_benchmark"] == perf["legs"], (
        f"attribution decomposes {a['legs']} legs, the curve chains {perf['legs']}")


def test_attribution_separates_a_selection_error_from_an_omission():
    """Different mistakes with different remedies: an overweight name that fell is the
    model picking badly, an underweight name that rose is the model not looking."""
    a = nightly._compute_edge()["attribution"]
    stances = {d["stance"] for d in a["detractors"]}
    assert stances <= {"overweight", "underweight"}
    assert any(d["stance"] == "underweight" for d in a["detractors"])


def test_the_attribution_says_it_is_not_evidence():
    a = nightly._compute_edge()["attribution"]
    assert "not evidence" in a["basis"]
    assert "sampling" in a["basis"]


def test_the_edge_panel_does_not_touch_the_specification():
    """Measuring the score must not change the score. The hash is the real assertion —
    it is derived from the scoring source itself, so it catches anything a name-based
    check would miss.

    Moved d600984ec00b -> 596d414706be when lavl_perp_mult was rewritten to read the
    interval-normalised funding APR and require a confirming input before adjusting.
    Moved again 596d414706be -> 2da60f7efd7b when Module F put a supply-overhang
    multiplier into score()'s risk term. Both are scoring changes and both are supposed
    to break this line; see tests/test_perps.py for the boundary each moved and what
    stays observational.

    Moved 6f98778fa627 -> 1a4ea6e4d77e when the capture was widened to funding.consolidate,
    VENUE_PRIORITY, INTERVAL_BASIS_REAL, VENUE_DEFAULT_INTERVAL and _rsi_by_symbol. That
    one is instrumentation rather than scoring, and it is aliased in SPEC_EQUIVALENT so
    the legs either side of it stay one track record — but the pin still moves, because
    the pin asserts what the specification captures and that is what changed.
    """
    # 1a4ea6e4d77e -> 8e750228e15a (AUDIT-PHASE1.5): the capture was widened to the OVERLAY SELECTION layer — which recorded multiplier a ledger consumer may apply — and unlike the two boundaries before it this one is a re-valuation, not instrumentation: the published board changes. See tests/test_perp_overlay.py.
    # 8e750228e15a -> ab16684ad5c1 (AUDIT-PHASE1.6): the funding TRANSPORT changed. The board reads ledger/perp.json — the whole scored cross-section for the current snapshot — instead of the fifty rows signals.json persists, so 184 rows gain the multiplier score() already applied. Published scores move, so this is a re-valuation like 1.5 before it and nothing canonicalises onto it.
    # ab16684ad5c1 -> 91bbc2a7e466 (AUDIT-PHASE2A): every factor threshold was collected into one captured SCORING object and asserted against the behaviour of the functions that already applied them. No scoring arithmetic was edited and no published score moved, so unlike 1.5 and 1.6 this one IS an instrumentation equivalence and the track record does not segment.
    assert nightly.SPEC_HASH == "91bbc2a7e466"
    captured = nightly.spec()["functions"]
    for fn in captured.values():
        for name in ("_edge_legs", "_compute_edge", "_active_contributions"):
            assert name not in fn, f"{name} reached a scoring function"
