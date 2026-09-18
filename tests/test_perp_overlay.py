"""The funding-overlay selection rule — AUDIT-PHASE1.5, boundary 1a4ea6e4d77e -> d237139d81a1.

On 2026-09-17 the published board ranked HBAR first at a conviction of 100 on a chain
that multiplied out to 242.9 before the clamp. The factor responsible was FUNDING at
x17.400. It was not a funding reading. `index.html` built its overlay from every row of
`signals.json` with no date filter, last write in file order winning, and the ledger
persists fifty names a night out of ~235 scored — so HBAR, absent from that cut since
2026-08-03, kept the value its row carried on that night. That row is one of four in the
seeded portion of the ledger whose tail columns are misaligned: `survived` holds `20`,
and `perp_mult` holds a number `funding.regime_modifier` cannot return, its envelope
being [0.85, 1.15].

The replacement has TWO independent defences, and the tests below prove each one stops
that row with the other disabled. A single defence is a single point of failure, and the
thing being defended against is a value that already got past everything once.

The rule is also now inside index.html's ported scoring block and captured in
SPEC_FUNCTIONS. That is half the fix: the old rule lived outside the markers the parity
gate extracts, so the gate was green for the entire six weeks the board was wrong.
"""
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("ov_nightly", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

# The four rows this whole boundary is about, as they sit in ledger/signals.csv.
MALFORMED = {"BEAT": 15.1, "HBAR": 17.4, "MON": 12.0, "UAI": 10.4}
TODAY = "2026-09-17"


def _ledger():
    """A ledger in the shape that produced the defect: the malformed night, then six
    weeks of nights none of those four symbols reappear in."""
    rows = [{"date": "2026-08-03", "symbol": s, "perp_mult": str(v)}
            for s, v in MALFORMED.items()]
    rows += [{"date": "2026-09-17", "symbol": s, "perp_mult": v}
             for s, v in [("ZEC", "1.071"), ("ETH", "1.0"), ("UNI", "0.94")]]
    return rows


# ------------------------------------------------------- the defect, reproduced

def test_the_retired_rule_still_serves_hbar_17_4():
    """The regression's control. If this stops failing, the tests below prove nothing."""
    old = nightly.board_perp_map(_ledger())
    assert old["HBAR"] == {"value": 17.4, "date": "2026-08-03"}
    for sym, v in MALFORMED.items():
        assert old[sym]["value"] == v
    # And it served that value on a night six weeks later, which is the defect.
    assert old["HBAR"]["date"] < TODAY


def test_the_current_rule_gives_hbar_a_neutral_one():
    rows = _ledger()
    ov = nightly.perp_overlay(rows, nightly.overlay_as_of(rows, TODAY))
    assert "HBAR" not in ov["mults"], "HBAR must not be in the overlay at all"
    # The call site's own fallback is what supplies the neutral, which is the same
    # `PERP[sym] || 1` / `.get(sym, 1.0)` the board and the nightly already had.
    assert ov["mults"].get("HBAR", nightly.PERP_NEUTRAL) == 1.0
    assert ov["mults"]["ZEC"] == 1.071


def test_the_published_chain_no_longer_reaches_242_9():
    """HBAR's own factors, with the two multipliers, end to end."""
    depth = 0.8798432251415851      # log10 mcap $3.25B
    confirm = 0.39807255196275293   # rs_blend -8.5 vs BTC
    liquidity = 0.40                # turnover 1.8%, below the depth>=0.90 bypass
    supply = 0.9965                 # FDV/MC 1.14x
    base = 100.0 * depth * confirm * liquidity * supply
    assert round(base * 17.4, 1) == 242.9              # what was published, clamped to 100
    assert round(base * nightly.PERP_NEUTRAL) == 14    # what the rest of the chain says
    # And the clamp is what hid the difference: both published as 100 before the fix.
    assert max(0, min(100, round(base * 17.4))) == 100


# ------------------------------------- each defence, with the other one disabled

def test_the_date_filter_alone_stops_it():
    """Envelope check neutralised by widening it to admit 17.4."""
    rows = _ledger()
    lo, hi = nightly.PERP_ENVELOPE_LO, nightly.PERP_ENVELOPE_HI
    try:
        nightly.PERP_ENVELOPE_LO, nightly.PERP_ENVELOPE_HI = 0.0, 1e9
        ov = nightly.perp_overlay(rows, nightly.overlay_as_of(rows, TODAY))
        assert "HBAR" not in ov["mults"]
    finally:
        nightly.PERP_ENVELOPE_LO, nightly.PERP_ENVELOPE_HI = lo, hi


def test_the_envelope_alone_stops_it():
    """Date filter neutralised by dating the malformed row to the snapshot itself."""
    rows = [{"date": TODAY, "symbol": "HBAR", "perp_mult": "17.4"},
            {"date": TODAY, "symbol": "ZEC", "perp_mult": "1.071"}]
    ov = nightly.perp_overlay(rows, nightly.overlay_as_of(rows, TODAY))
    assert ov["mults"]["HBAR"] == 1.0
    assert ov["states"]["HBAR"] == "rejected"
    assert ov["rejected"] == [{"symbol": "HBAR", "value": 17.4, "date": TODAY}]
    assert ov["mults"]["ZEC"] == 1.071          # a good row beside it is untouched


def test_every_malformed_legacy_value_is_refused_on_its_own_night():
    """Not just HBAR. All four, scored as if each were current."""
    for sym, v in MALFORMED.items():
        rows = [{"date": TODAY, "symbol": sym, "perp_mult": str(v)}]
        ov = nightly.perp_overlay(rows, nightly.overlay_as_of(rows, TODAY))
        assert ov["states"][sym] == "rejected", sym
        assert ov["mults"][sym] == 1.0, sym


# ------------------------------------------------------------- the envelope itself

def test_the_envelope_is_the_one_the_funding_model_can_produce():
    """Literals on both sides of the port; this is what stops them drifting apart."""
    assert nightly.PERP_ENVELOPE_LO == nightly.funding.MOD_MAX_PENALTY == 0.85
    assert nightly.PERP_ENVELOPE_HI == nightly.funding.MOD_MAX_BOOST == 1.15
    port = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "const PERP_ENVELOPE_LO = 0.85, PERP_ENVELOPE_HI = 1.15;" in port
    assert "const PERP_MAX_AGE_DAYS = 1;" in port
    assert nightly.PERP_MAX_AGE_DAYS == 1


def test_the_boundary_values_themselves_are_admitted():
    """0.85 and 1.15 are producible, so refusing them would be refusing the model."""
    rows = [{"date": TODAY, "symbol": "LO", "perp_mult": "0.85"},
            {"date": TODAY, "symbol": "HI", "perp_mult": "1.15"},
            {"date": TODAY, "symbol": "UNDER", "perp_mult": "0.8499"},
            {"date": TODAY, "symbol": "OVER", "perp_mult": "1.1501"}]
    ov = nightly.perp_overlay(rows, nightly.overlay_as_of(rows, TODAY))
    assert ov["states"] == {"LO": "current", "HI": "current",
                            "UNDER": "rejected", "OVER": "rejected"}


def test_an_unparseable_cell_is_refused_not_coerced():
    rows = [{"date": TODAY, "symbol": "A", "perp_mult": "1.07 garbage"},
            {"date": TODAY, "symbol": "B", "perp_mult": "nan"},
            {"date": TODAY, "symbol": "C", "perp_mult": "inf"},
            {"date": TODAY, "symbol": "D", "perp_mult": "None"},
            {"date": TODAY, "symbol": "E", "perp_mult": ""},
            {"date": TODAY, "symbol": "F", "perp_mult": None}]
    ov = nightly.perp_overlay(rows, nightly.overlay_as_of(rows, TODAY))
    # "1.07 garbage" is 1.07 to parseFloat and an error to float(). Both sides refuse it.
    assert ov["states"]["A"] == "rejected"
    assert ov["states"]["B"] == ov["states"]["C"] == "rejected"
    # Empty and the literal "None" are an ABSENT reading, not a malformed one.
    assert ov["states"]["D"] == ov["states"]["E"] == ov["states"]["F"] == "absent"
    assert all(v == 1.0 for k, v in ov["mults"].items())


# --------------------------------------------------------------- snapshot freshness

def test_a_snapshot_older_than_the_window_withholds_the_whole_overlay():
    rows = _ledger()
    assert nightly.overlay_as_of(rows, "2026-09-17") == "2026-09-17"
    assert nightly.overlay_as_of(rows, "2026-09-18") == "2026-09-17"   # nightly not run yet
    assert nightly.overlay_as_of(rows, "2026-09-19") is None           # two days: withheld
    assert nightly.overlay_as_of(rows, "2026-10-30") is None
    ov = nightly.perp_overlay(rows, nightly.overlay_as_of(rows, "2026-09-19"))
    assert ov == {"mults": {}, "states": {}, "as_of": None, "rejected": [], "n": 0}


def test_a_ledger_dated_ahead_of_the_caller_is_refused():
    """Clock skew or a hand-edited file. It cannot be what it says, so it is not used."""
    rows = [{"date": "2026-09-20", "symbol": "X", "perp_mult": "1.1"}]
    assert nightly.overlay_as_of(rows, TODAY) is None


def test_an_empty_or_undated_ledger_yields_no_overlay():
    assert nightly.overlay_as_of([], TODAY) is None
    assert nightly.overlay_as_of([{"symbol": "X", "perp_mult": "1.1"}], TODAY) is None
    assert nightly.ledger_latest_date([{"date": "not-a-date"}]) is None


# ------------------------------------------------------- the history is left alone

def test_the_malformed_rows_are_still_in_the_ledger_exactly_as_recorded():
    """The fix refuses the values. It does not edit the record of them.

    A ledger corrected to remove the evidence of a defect is worth less than one that
    carries it, and this is the row every number in AUDIT-PHASE1 is reconstructed from.
    """
    rows = json.loads((ROOT / "ledger" / "signals.json").read_text(encoding="utf-8"))["rows"]
    found = {r["symbol"]: r for r in rows
             if r.get("date") == "2026-08-03" and r.get("symbol") in MALFORMED}
    assert set(found) == set(MALFORMED)
    for sym, v in MALFORMED.items():
        assert float(found[sym]["perp_mult"]) == v
        assert found[sym]["survived"] == "20"     # the misalignment, still visible


def test_no_row_in_the_real_ledger_survives_the_current_rule_out_of_envelope():
    """Whatever the ledger holds, nothing outside the envelope can reach a score."""
    rows = json.loads((ROOT / "ledger" / "signals.json").read_text(encoding="utf-8"))["rows"]
    dates = sorted({r["date"] for r in rows if r.get("date")})
    for day in dates:
        ov = nightly.perp_overlay(rows, day)
        for sym, v in ov["mults"].items():
            assert nightly.PERP_ENVELOPE_LO - 1e-9 <= v <= nightly.PERP_ENVELOPE_HI + 1e-9, \
                f"{day}/{sym} = {v}"


def test_the_overlay_never_reaches_back_past_its_own_snapshot():
    """The property in one sentence: every value applied carries the snapshot's date."""
    rows = json.loads((ROOT / "ledger" / "signals.json").read_text(encoding="utf-8"))["rows"]
    by_day = {}
    for r in rows:
        by_day.setdefault(r.get("date"), set()).add((r.get("symbol") or "").upper())
    for day, syms in by_day.items():
        if not day:
            continue
        assert set(nightly.perp_overlay(rows, day)["mults"]) <= syms, day


# --------------------------------------------------------------- the capture

def test_the_selection_rule_is_in_the_specification():
    """The lesson of AUDIT-2026-09 1.8, applied to the layer above venue selection.

    The captured set is exactly what can move a published multiplier, and it shrank at
    1.6 as well as grew: three functions left it with the signals.json path they served.
    Capturing dead code means an edit to dead code re-segments the track record, which
    is the mirror image of the hole 1.8 closed and costs just as much.
    """
    captured = nightly.spec()["functions"]
    for name in ("iso_day_diff", "perp_entry", "perp_feed"):
        assert name in captured, name
    consts = nightly.spec()["constants"]
    for name in ("PERP_NEUTRAL", "PERP_ENVELOPE_LO", "PERP_ENVELOPE_HI",
                 "PERP_MAX_AGE_DAYS"):
        assert name in consts, name
    # Retired rules reach no score and must NOT be captured. board_perp_map is the
    # pre-1.5 rule; the other three are the 1.5 rule, retired by the 1.6 transport.
    for name in ("board_perp_map", "ledger_latest_date", "overlay_as_of",
                 "perp_overlay"):
        assert name not in captured, name
    # Writers are never captured either: recording a number is not valuing one.
    for name in ("perp_artifact", "write_perp_artifact"):
        assert name not in captured, name


def test_the_specification_is_the_size_the_notes_say_it_is():
    """Counted, not asserted in prose.

    docs/ARCHITECTURE-NOTES.md states the size of the capture. A sentence in a document
    cannot notice a function being added to or dropped from SPEC_FUNCTIONS; this can,
    and a deliberate change fails here and gets the document updated with it.
    """
    sp = nightly.spec()
    assert len(sp["functions"]) == 19, sorted(sp["functions"])
    assert len(sp["constants"]) == 29, sorted(sp["constants"])
    assert len(nightly.SPEC_FUNCTIONS) == 10
    assert len(nightly.SPEC_CONSTANTS) == 11
    assert len(nightly.SPEC_FUNDING_FUNCTIONS) == 9
    assert len(nightly.SPEC_FUNDING_CONSTANTS) == 18
    # Whitespace-normalised: the document is hard-wrapped, so a phrase that spans a
    # line break is still the phrase. Asserting on the raw text would fail on a reflow,
    # which is the kind of brittleness that gets a useful test deleted.
    notes = " ".join(
        (ROOT / "docs" / "ARCHITECTURE-NOTES.md").read_text(encoding="utf-8").split())
    assert "nineteen functions and twenty-nine constants" in notes
    assert f"**Current hash: `{nightly.SPEC_HASH}`**" in notes


def test_the_boundary_is_a_re_valuation_and_not_an_equivalence():
    """No SPEC_EQUIVALENT entry, deliberately.

    The other two entries in that table are corrections to the ruler: same arithmetic,
    different digest. This one changes which input arrives, and therefore changes
    published scores. Collapsing it onto the old digest would claim the board said the
    same thing either side of it, and the board did not.
    """
    # ab16684ad5c1 -> 91bbc2a7e466 (AUDIT-PHASE2A): every factor threshold was collected into one captured SCORING object and asserted against the behaviour of the functions that already applied them. No scoring arithmetic was edited and no published score moved, so unlike 1.5 and 1.6 this one IS an instrumentation equivalence and the track record does not segment.
    assert nightly.SPEC_HASH == "91bbc2a7e466"
    # Neither 1.5 nor 1.6 folds onto anything: both changed which input arrives and
    # both moved published scores. The table's fixed point is still 1a4ea6e4d77e.
    # The two re-valuations stay out of the table. ab16684ad5c1 LEFT this list at
    # AUDIT-PHASE2A: collecting the thresholds into SCORING moved the digest and no
    # number, which is an instrumentation equivalence and belongs in the table.
    for h in ("1a4ea6e4d77e", "8e750228e15a"):
        assert h not in nightly.SPEC_EQUIVALENT, h
        assert nightly.canonical_spec_hash(h) == h, h
    assert nightly.canonical_spec_hash("ab16684ad5c1") == nightly.SPEC_HASH
