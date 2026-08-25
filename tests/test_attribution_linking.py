"""Carino linking, on synthetic legs where the right answer is known in closed form.

tests/test_edge.py checks the identity against the recorded ledger, which is the real
case but only one case, and a ledger that happens to be benign can hide a linking bug
for weeks. These are constructed: legs are handed in directly, the chained totals are
computed independently in the test, and the linked contributions have to reconcile to
P - B at floating-point tolerance rather than to a band.

The property under test, for every shape below:

    sum_i C_i  ==  P - B      where  P = prod(1+p_t) - 1,  B = prod(1+b_t) - 1

An arithmetic sum satisfies this for one leg and fails for two, which is exactly the
bug this file exists to keep out. `test_an_unlinked_sum_would_fail_these` pins that:
if the naive sum ever passes the twenty-leg case, the case has gone slack.
"""
import importlib.util
import math
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location("nightly", os.path.join(_ROOT, "nightly.py"))
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

TOL = 1e-9


def leg(frm, to, prices_prev, prices_curr, weights):
    """One synthetic leg in the shape `_perf_legs` produces.

    `weights` is the book's conviction weighting over its top names; the equal-weight
    benchmark is every name present on both nights, which is deliberately a different
    set, because that difference is where a naive re-derivation goes wrong.
    """
    prev = {s: {"price": p, "conviction": 50} for s, p in prices_prev.items()}
    curr = {s: {"price": p, "conviction": 50} for s, p in prices_curr.items()}
    kept = sum(w for s, w in weights.items() if prices_curr.get(s))
    book = sum(w * (prices_curr[s] / prices_prev[s] - 1.0)
               for s, w in weights.items() if prices_curr.get(s)) / kept
    shared = [s for s in prices_prev if s in prices_curr]
    eq = sum(prices_curr[s] / prices_prev[s] - 1.0 for s in shared) / len(shared)
    return {"from": frm, "to": to, "book": book, "equal_weight": eq,
            "benchmark": None, "names": len(weights), "weight_lost": 0.0,
            "usable": True, "kept": kept, "weights": weights, "shared": shared,
            "_prev": prev, "_curr": curr}


def chained(legs):
    p = q = 1.0
    for l in legs:
        p *= (1.0 + l["book"])
        q *= (1.0 + l["equal_weight"])
    return p - 1.0, q - 1.0


def reconciles(legs):
    """Assert the identity and hand back the attribution.

    Checked on `residual_bp`, which the attribution computes from its own unrounded
    chain. `total_bp` is rounded to a tenth of a basis point for display, so comparing
    against it would cap this test's resolution at 1e-5 in return space and quietly turn
    a floating-point assertion into a display-granularity one.
    """
    a = nightly._active_contributions(legs)
    P, B = chained(legs)
    assert a["residual_bp"] == pytest.approx(0.0, abs=1e-6), (
        f"linked contributions do not reconcile: residual {a['residual_bp']}bp")
    assert a["reconciles_to_bp"] == pytest.approx((P - B) * 1e4, abs=0.05), (
        f"the attribution is reconciling to {a['reconciles_to_bp']}bp, but the legs "
        f"chain to {(P - B) * 1e4}bp")
    return a


def _mk(n, seed_moves):
    """n legs of four names, moving by a repeating deterministic pattern."""
    out, px = [], {"AAA": 100.0, "BBB": 50.0, "CCC": 10.0, "DDD": 2.0}
    for i in range(n):
        nxt = {}
        for j, (s, p) in enumerate(px.items()):
            nxt[s] = p * (1.0 + seed_moves[(i + j) % len(seed_moves)])
        w = {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}       # book holds three of four
        out.append(leg(f"d{i:02d}", f"d{i + 1:02d}", dict(px), dict(nxt), w))
        px = nxt
    return out


# ---------------------------------------------------------------------------
def test_one_leg_reconciles():
    """The case the old arithmetic sum also got right, kept so a regression is visible
    as a linking failure rather than as a total collapse."""
    a = reconciles(_mk(1, [0.05, -0.03, 0.01, 0.00]))
    assert a["legs"] == 1


def test_twenty_volatile_legs_reconcile():
    """The case that broke. Twenty legs with double-digit swings, where an unlinked sum
    is off by a visible fraction of the gap."""
    a = reconciles(_mk(20, [0.11, -0.09, 0.14, -0.12, 0.03, -0.07]))
    assert a["legs"] == 20


def test_an_unlinked_sum_would_fail_these():
    """Guards the guard.

    If a naive arithmetic sum passes the twenty-leg case, that case is too gentle to
    detect the bug it was written for and the suite is reporting safety it does not
    have.
    """
    legs = _mk(20, [0.11, -0.09, 0.14, -0.12, 0.03, -0.07])
    naive = sum(l["book"] - l["equal_weight"] for l in legs)
    P, B = chained(legs)
    assert abs(naive - (P - B)) > 1e-4, (
        "the synthetic legs are too tame: an unlinked sum reconciles on them, so they "
        "cannot catch the linking bug")


def test_a_zero_active_leg_uses_the_limit_not_an_epsilon():
    """p_t == b_t makes k_t = 0/0. The limit is 1/(1+p); a nudge would put a small
    fabricated number into every contribution on that day."""
    assert nightly._carino_k(0.05, 0.05) == pytest.approx(1.0 / 1.05, abs=1e-15)
    assert nightly._carino_k(0.0, 0.0) == pytest.approx(1.0, abs=1e-15)
    # And it is continuous: approaching equality must not jump.
    near = nightly._carino_k(0.05, 0.05 + 1e-9)
    assert near == pytest.approx(1.0 / 1.05, abs=1e-6)

    # A whole leg where the book exactly matched the benchmark, inside a real chain.
    legs = _mk(3, [0.04, -0.02, 0.06])
    flat = leg("x0", "x1", {"AAA": 100.0, "BBB": 50.0}, {"AAA": 104.0, "BBB": 52.0},
               {"AAA": 0.5, "BBB": 0.5})
    assert flat["book"] == pytest.approx(flat["equal_weight"], abs=1e-15)
    legs.insert(1, flat)
    reconciles(legs)


def test_the_equal_total_return_limit():
    """P == B over the whole period makes K = 0/0 too. The limit is 1/(1+P), and the
    decomposition must still sum to zero rather than divide by nothing."""
    assert nightly._carino_k(0.2, 0.2) == pytest.approx(1.0 / 1.2, abs=1e-15)
    up = leg("a", "b", {"AAA": 100.0, "BBB": 100.0}, {"AAA": 110.0, "BBB": 100.0},
             {"AAA": 1.0})
    down = leg("b", "c", {"AAA": 110.0, "BBB": 100.0}, {"AAA": 110.0, "BBB": 110.0},
               {"AAA": 1.0})
    legs = [up, down]
    P, B = chained(legs)
    a = reconciles(legs)
    if abs(P - B) < 1e-12:
        assert a["total_bp"] == pytest.approx(0.0, abs=1e-6)


def test_missing_names_and_renormalised_weights():
    """A held name that stops being priceable leaves the book renormalised over what
    remains, while the benchmark's own set shrinks separately. The two sets are not the
    same set, and the identity has to hold anyway."""
    l1 = leg("a", "b",
             {"AAA": 100.0, "BBB": 50.0, "CCC": 10.0, "DDD": 4.0},
             {"AAA": 108.0, "BBB": 47.0, "CCC": 11.0, "DDD": 4.4},
             {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2})
    # CCC leaves the universe: absent from curr, so it is neither held nor benchmarked.
    l2 = leg("b", "c",
             {"AAA": 108.0, "BBB": 47.0, "CCC": 11.0, "DDD": 4.4},
             {"AAA": 100.0, "BBB": 50.0, "DDD": 4.0},
             {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2})
    assert l2["kept"] < 1.0, "the fixture no longer exercises a dropped holding"
    a = reconciles([l1, l2])
    assert a["legs"] == 2


def test_a_leg_with_no_benchmark_is_excluded_and_reported():
    """It cannot be decomposed, and a silent drop would leave the attribution
    reconciling to a gap built over a different set of days."""
    good = _mk(2, [0.05, -0.03])
    blind = dict(good[0])
    blind["equal_weight"] = None
    a = nightly._active_contributions(good + [blind])
    assert a["legs"] == 2
    assert a["legs_without_benchmark"] == 1


def test_every_leg_before_the_spec_boundary_is_excluded():
    """The boundary filter lives in _perf_legs, so the attribution inherits it rather
    than reimplementing it. This pins that it is actually applied to the real ledger."""
    by_date, _ = nightly._perf_by_date()
    all_legs, usable, boundary, dropped = nightly._perf_legs(by_date, sorted(by_date))
    if not boundary:
        pytest.skip("no specification boundary in the recorded ledger")
    assert all(l["from"] >= boundary for l in usable), \
        "a leg starting before the specification boundary survived into the curve"
    assert dropped > 0
    a = nightly._active_contributions(usable)
    assert a["legs"] <= len(usable)


def test_a_total_loss_is_refused_rather_than_linked():
    """ln(0) is undefined. There is no linking coefficient, and inventing one would put
    a number under a leg that has none."""
    with pytest.raises(ValueError):
        nightly._carino_k(-1.0, 0.05)
    with pytest.raises(ValueError):
        nightly._carino_k(0.05, -1.2)


def test_the_basis_names_the_linking_method():
    """A reader who cannot tell which linking was used cannot check the claim."""
    a = nightly._compute_edge()["attribution"]
    assert "carino" in a["basis"].lower()
    assert a["linking"] == "carino"
