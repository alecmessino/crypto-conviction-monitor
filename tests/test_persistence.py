"""Which names hold conviction across nights, and which clear the bar once.

This replaced persistent_30d / persistent_90d, which answered a version of this, but they
require 30 and 90 *consecutive* nights above the level and the ledger has eleven — so
both have been empty lists since the day they were written. That is the same starvation
that made the change feed look broken: a feature that reports nothing until an arbitrary
threshold is crossed is indistinguishable from a feature that does not work.
"""
import importlib.util
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("persist_mod", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

DATES = [f"2026-03-{i:02d}" for i in range(1, 12)]


def series(**names):
    """{symbol: [(date, conviction)]} from {symbol: [conv or None per date]}."""
    return {sym: [(d, c) for d, c in zip(DATES, vals) if c is not None]
            for sym, vals in names.items()}


def row(out, sym):
    return next(r for r in out["rows"] if r["symbol"] == sym)


# ---------------------------------------------------------------------------
# the distinction the panel exists to draw
# ---------------------------------------------------------------------------
def test_a_persistent_name_outranks_a_one_night_spike():
    out = nightly._persistence(series(
        STEADY=[75] * 11,
        SPIKE=[30] * 10 + [90],
    ), DATES)
    assert out["rows"][0]["symbol"] == "STEADY"
    assert row(out, "SPIKE")["spike"] is True
    assert row(out, "STEADY")["spike"] is False


def test_a_name_seen_once_cannot_outrank_one_backed_across_the_window():
    """A raw share makes 1-of-1 a perfect 1.0, which would put a name observed on a
    single night above one backed on nine of eleven. That is the opposite of what
    persistence means, and it happened on the real ledger — BONK and OP sorted above
    HYPE until the denominator was shrunk."""
    out = nightly._persistence(series(
        HYPE=[75] * 9 + [40, 40],
        ONCE=[None] * 10 + [75],
    ), DATES)
    assert row(out, "HYPE")["share_above"] < row(out, "ONCE")["share_above"]
    assert row(out, "HYPE")["persistence"] > row(out, "ONCE")["persistence"]
    assert out["rows"][0]["symbol"] == "HYPE"


# ---------------------------------------------------------------------------
# streaks
# ---------------------------------------------------------------------------
def test_a_streak_is_broken_by_a_night_off_the_board():
    """An asset that left the universe was not there to be held, so the run genuinely
    ends — this is not the same as a night the pipeline failed to run."""
    out = nightly._persistence(series(GONE=[75] * 4 + [None] + [75] * 6), DATES)
    r = row(out, "GONE")
    assert r["best_streak"] == 6
    assert r["nights"] == 10 and r["of"] == 11


def test_a_streak_is_broken_by_dropping_below_the_level():
    out = nightly._persistence(series(DIP=[75] * 3 + [60] + [75] * 7), DATES)
    assert row(out, "DIP")["best_streak"] == 7


def test_the_current_streak_is_the_run_ending_tonight():
    out = nightly._persistence(series(
        ENDED=[75] * 8 + [40, 40, 40],
        RUNNING=[40] * 8 + [75, 75, 75],
    ), DATES)
    assert row(out, "ENDED")["current_streak"] == 0
    assert row(out, "ENDED")["best_streak"] == 8
    assert row(out, "RUNNING")["current_streak"] == 3


# ---------------------------------------------------------------------------
# the heatmap row
# ---------------------------------------------------------------------------
def test_absence_is_a_distinct_cell_from_a_low_score():
    """Colouring both the same invents a low reading that was never taken."""
    out = nightly._persistence(series(PARTIAL=[None, None, 20] + [75] * 8), DATES)
    cells = row(out, "PARTIAL")["cells"]
    assert cells[0] is None and cells[1] is None
    assert cells[2] == 20
    assert len(cells) == len(DATES)


def test_cells_align_to_the_shared_date_axis():
    """Two names with different histories must line up column for column, or the grid
    is comparing different days side by side."""
    out = nightly._persistence(series(A=[50] * 11, B=[None] * 6 + [50] * 5), DATES)
    assert row(out, "A")["cells"][6] is not None
    assert row(out, "B")["cells"][5] is None and row(out, "B")["cells"][6] is not None
    assert out["dates"] == DATES


# ---------------------------------------------------------------------------
# the window is stated, not assumed
# ---------------------------------------------------------------------------
def test_the_window_travels_with_the_result():
    """Nine of eleven and nine of ninety are different claims."""
    out = nightly._persistence(series(A=[75] * 11), DATES)
    assert out["window"] == 11 and out["level"] == nightly.PERSIST_LEVEL
    assert row(out, "A")["of"] == 11


def test_a_short_history_still_reports_rather_than_returning_nothing():
    """The failure being fixed: persistent_30d needed thirty consecutive nights and was
    an empty list for the whole life of the ledger."""
    out = nightly._persistence(series(A=[75, 75]), DATES[:2])
    assert out["rows"] and out["rows"][0]["nights_above"] == 2


def test_no_history_is_empty_rather_than_an_error():
    assert nightly._persistence({}, [])["rows"] == []


def test_the_live_ledger_produces_a_populated_panel():
    """The regression: this must not be another feature that reports nothing forever."""
    p = nightly._compute_market_breadth()["persistence"]
    assert p["rows"], "persistence is empty on the real ledger"
    assert p["n_backed"] > 0
    assert any(r["best_streak"] > 1 for r in p["rows"]), "no multi-night run found"


def test_the_specification_hash_is_deterministic():
    """The import-time value must equal a later call. It did not.

    SPEC_HASH was computed near the top of nightly.py, immediately after spec_hash() was
    defined. spec() captures functions by parsing the file from disk — position
    irrelevant — but captures constants with globals().get(), which returns None for
    anything not yet executed. TIER_CUTS is defined ~750 lines below where the hash was
    taken, so it was captured as null on every row this repository has ever written, and
    a second call moments later produced a different digest.
    """
    assert nightly.SPEC_HASH == nightly.spec_hash(), (
        "the hash recorded on every row differs from the hash of the specification as it "
        "actually stands — a constant is being captured before it is defined")


def test_every_captured_constant_has_a_value():
    """The adjacent failure: a constant NAMED in the specification but never defined.

    Distinct from the ordering bug above, which the determinism check catches — this one
    catches a name added to SPEC_CONSTANTS that no assignment ever backs, or one left
    behind after a rename. Either way the entry captures as None: it is in the list, it
    is not in the hash, and editing the thing it refers to moves no digest and starts no
    new track-record segment.
    """
    consts = nightly.spec()["constants"]
    missing = sorted(k for k, v in consts.items() if v is None)
    assert not missing, (
        f"{missing} are named in the specification but captured as None — they are "
        f"defined after SPEC_HASH is computed, so a change to them moves no hash")
    expected = set(nightly.SPEC_CONSTANTS) | {
        "funding." + c for c in nightly.SPEC_FUNDING_CONSTANTS}
    assert set(consts) == expected


def test_persistence_does_not_touch_the_specification():
    # d600984ec00b -> 596d414706be: the funding regime rewrite of lavl_perp_mult.
    # 596d414706be -> 2da60f7efd7b: Module F. `emission_mult` was added to score()'s risk term
    # and `emission_drag`/`emission_mult` were captured as SPEC_FUNCTIONS, so a supply
    # overhang now multiplies the published score. That is a scoring change and it is
    # supposed to break this pin. A token with no published FDV is unaffected — the
    # neutral path is asserted in tests/test_parity.py.
    # 2da60f7efd7b -> 6f98778fa627: SPEC_HASH moved to the bottom of nightly.py.
    # It was computed before TIER_CUTS and the emission anchors were defined, so five constants were captured as None on every row ever written — editing the tier boundaries would have moved no hash.
    # Not a scoring change; a specification that was not capturing what it named.
    assert nightly.SPEC_HASH == "6f98778fa627"
    for fn in nightly.spec()["functions"].values():
        assert "_persistence" not in fn


def test_the_structurally_empty_fields_are_gone():
    """persistent_30d / persistent_90d needed 30 and 90 *consecutive* boards and the
    ledger has 13, so both were empty lists on every night they ever ran. The terminal
    rendered their lengths, which printed "Persistent 30d: 0" — a zero reading rather
    than the honest statement that the window does not exist. Deleted rather than left
    in place: a field nothing can populate is a claim, not a null."""
    breadth = nightly._compute_market_breadth()
    assert "persistent_30d" not in breadth and "persistent_90d" not in breadth
    html = (HERE.parent / "index.html").read_text()
    code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", html, flags=re.S))
    assert "persistent_30d" not in code and "persistent_90d" not in code


# ---------------------------------------------------------------------------
# specification integrity — the four properties the PR #22 review required
# ---------------------------------------------------------------------------
def test_a_cold_interpreter_produces_the_same_hash():
    """Determinism across PROCESSES, not just across calls in one.

    The in-process check above compares SPEC_HASH to spec_hash() inside a module that
    has finished loading. This runs a fresh interpreter with no bytecode cache, which is
    what CI does every night, and is the form of the check that would have caught the
    original bug from the outside.
    """
    import shutil
    import subprocess
    import sys

    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    prog = (
        "import importlib.util,sys;"
        f"sp=importlib.util.spec_from_file_location('n',r'{ROOT / 'nightly.py'}');"
        "m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);"
        "print(m.SPEC_HASH, m.spec_hash())"
    )
    seen = set()
    for _ in range(3):
        out = subprocess.run([sys.executable, "-B", "-c", prog],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        a, b = out.stdout.split()
        assert a == b, f"cold import: SPEC_HASH {a} != spec_hash() {b}"
        seen.add(a)
    assert len(seen) == 1, f"a cold interpreter produced {len(seen)} different hashes: {seen}"
    assert seen.pop() == nightly.SPEC_HASH, "the cold hash differs from the in-process one"


def _perturb(value):
    """A different value of the same broad kind, for the mutation check below."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, (list, tuple)):
        return list(value) + ["__mutation__"]
    if isinstance(value, (set, frozenset)):
        return set(value) | {"__MUTATION__"}
    if isinstance(value, str):
        return value + "_mutated"
    raise AssertionError(f"no perturbation defined for {type(value)}")


def test_changing_any_captured_constant_changes_the_hash():
    """The property the whole mechanism claims, checked one constant at a time.

    This is the check that fails loudly if a constant is named in the specification but
    is not actually reaching the digest — which is exactly the state TIER_CUTS and the
    four emission anchors were in for the entire recorded history. A name in the list is
    not the same as a value in the hash, and only this distinguishes them.
    """
    # Computed FRESH rather than read off SPEC_HASH. Using the module attribute makes
    # this test pass for the wrong reason under the very bug it guards against: with a
    # stale SPEC_HASH, every perturbation trivially differs from it and nothing is
    # actually being measured. spec_hash() reflects the module as it stands right now.
    base = nightly.spec_hash()
    funding = nightly.funding
    unmoved = []

    for name in nightly.SPEC_CONSTANTS:
        original = getattr(nightly, name)
        setattr(nightly, name, _perturb(original))
        try:
            if nightly.spec_hash() == base:
                unmoved.append(name)
        finally:
            setattr(nightly, name, original)

    for name in nightly.SPEC_FUNDING_CONSTANTS:
        original = getattr(funding, name)
        setattr(funding, name, _perturb(original))
        try:
            if nightly.spec_hash() == base:
                unmoved.append("funding." + name)
        finally:
            setattr(funding, name, original)

    assert not unmoved, (
        f"{unmoved} are named in the specification but changing them does not move the "
        f"hash — they are not reaching the digest, so an edit to any of them would "
        f"silently reinterpret the history either side of it")
    assert nightly.spec_hash() == base, "a perturbation leaked past its restore"


def test_changing_a_captured_scoring_function_changes_the_hash(tmp_path):
    """The other half: the functions, not just the constants.

    Edits the threshold inside score() on a COPY of the module and confirms the digest
    moves. Done on a copy because spec() parses the file from disk, so this cannot be
    simulated by patching an attribute.
    """
    import importlib.util
    import shutil

    for f in ("nightly.py", "funding.py", "cryptometer.py", "coingecko.py", "quant.py"):
        shutil.copy(ROOT / f, tmp_path / f)
    target = tmp_path / "nightly.py"
    src = target.read_text(encoding="utf-8")
    needle = 'sig = "STRONG" if total >= 80'
    assert needle in src, "score()'s tier threshold is no longer where this test looks"
    target.write_text(src.replace(needle, 'sig = "STRONG" if total >= 81', 1),
                      encoding="utf-8")
    sp = importlib.util.spec_from_file_location("n_mutated", target)
    mutated = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mutated)
    assert mutated.SPEC_HASH != nightly.SPEC_HASH, (
        "moving the STRONG cut inside score() did not move the specification hash")


def test_the_equivalence_table_covers_only_the_verified_correction():
    """One entry, and it must be the audited one.

    An equivalence table is a licence to treat two track-record segments as one. That is
    exactly the thing this repository refuses to do casually, so the table is pinned to
    the single pair that was audited commit by commit, and every other digest the ledger
    holds must pass through unchanged.
    """
    table = nightly.SPEC_EQUIVALENT
    assert set(table) == {"2da60f7efd7b"}, (
        f"the equivalence table holds {sorted(table)} — only the audited "
        f"instrumentation correction may be aliased")
    entry = table["2da60f7efd7b"]
    assert entry["canonical"] == "6f98778fa627"
    assert entry["reason"] == "instrumentation"

    # Identity for everything else, including the two earlier digests, which were
    # computed under the same defect but describe genuinely different scoring code.
    for other in ("d600984ec00b", "e65f7dc59d55", "596d414706be", "", None):
        assert nightly.canonical_spec_hash(other) == (other or "").strip()
    assert nightly.canonical_spec_hash("6f98778fa627") == "6f98778fa627"


def test_the_aliased_pair_is_re_derivable_from_todays_source():
    """The alias is proved on every run, not asserted once in a commit message.

    Nulling exactly the five constants the old ordering missed, in today's specification,
    must reproduce the superseded digest. If it does not, either the scoring code has
    changed or the account of what went wrong is incorrect — and in both cases the
    equivalence has to be re-justified rather than inherited.
    """
    entry = nightly.SPEC_EQUIVALENT["2da60f7efd7b"]
    if nightly.SPEC_HASH != entry["verified_against"]:
        # Scoring has moved on since the audit. The entry is then a frozen historical
        # record about two old digests and must not have been edited to follow.
        assert entry["canonical"] == "6f98778fa627"
        pytest.skip("scoring has changed since the equivalence was verified; the entry "
                    "is now a historical record and is asserted unmodified instead")
    derived = nightly.spec_hash_as_recorded_before(entry["null_constants"])
    assert derived == "2da60f7efd7b", (
        f"re-deriving the superseded hash from today's source gave {derived}, not "
        f"2da60f7efd7b — the equivalence claim no longer holds")


def test_the_monitor_folds_the_aliased_night_without_hiding_it():
    """A collapsed span must still show both digests it was recorded under."""
    spec_block = nightly._compute_monitor()["specification"]
    folded = [s for s in spec_block["spans"] if len(s.get("recorded_as", [])) > 1]
    assert folded, "the aliased night was not folded onto its canonical span"
    for span in folded:
        assert "2da60f7efd7b" in span["recorded_as"]
        assert span["spec_hash"] == "6f98778fa627"
    assert spec_block["aliased_days"] >= 1
    assert "2da60f7efd7b" in spec_block["equivalence"]
