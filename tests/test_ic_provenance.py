"""The two IC samples must never be conflated — AUDIT-CLOSURE, 2026-09-18.

There are two information-coefficient samples in this repository and they are not the
same measurement:

  LEGACY   ledger/signals.csv, via ic_by_date(). 48 nights back to 2026-08-01, but fifty
           rows a night and those fifty are the TOP FIFTY BY CONVICTION — the very
           variable whose predictive power is being measured. Deep in time, truncated in
           population. It is the only sample with enough legs to say anything, which is
           why the publication gate reads it: composite 1d IC -0.0562, 95% CI
           [-0.1065,-0.0060], 43 legs, NEGATIVE -> DIAGNOSTIC_ONLY.

  FORWARD  ledger/xsec/, via xsec_by_date(). The whole scored cross-section, 234-235 rows
           a night, begun 2026-09-15. Wide and shallow. It has measured nothing: 0 of 18
           factor x horizon cells.

The closeout for Phases 3 and 4 reported both a populated matrix and "0 of 18 measurable"
and the two looked like a contradiction. They are not — they are two samples. This file
is the guard that stops the distinction being lost again, in code, in artifacts, or on
the page.

Four mutations it must catch, each of which would previously have passed silently:
  1. pointing ic_matrix() at the wide sample while keeping the legacy label
  2. pointing walkforward_report() at the narrow sample
  3. copying a legacy leg count into the forward report
  4. letting a sample label go stale after its source changed
"""
import csv
import importlib.util
import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("prov_nightly", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


# ------------------------------------------------- the samples are what we say they are

def test_the_two_samples_are_named_once_and_only_once():
    assert set(nightly.IC_SAMPLES) == {"legacy", "forward"}
    for key, meta in nightly.IC_SAMPLES.items():
        assert meta["id"] == key
        for field in ("source", "label", "universe", "population", "caveat", "powers"):
            assert meta[field] and isinstance(meta[field], str), (key, field)
    assert nightly.IC_SAMPLES["legacy"]["source"] == "ledger/signals.csv"
    assert nightly.IC_SAMPLES["forward"]["source"] == "ledger/xsec/"
    # the labels must not be confusable with one another
    a, b = (nightly.IC_SAMPLES[k]["label"] for k in ("legacy", "forward"))
    assert a != b and "LEGACY" in a and "FORWARD" in b


def test_each_reader_reads_exactly_one_source():
    import inspect

    def code(fn):
        # Source with comments and the docstring stripped. The readers now EXPLAIN the
        # other ledger in prose, and a guard that banned naming it would forbid exactly
        # the documentation this closure exists to add.
        src = inspect.getsource(fn)
        src = src.replace(inspect.getdoc(fn) or "", "")
        return "\n".join(l for l in src.splitlines()
                          if not l.strip().startswith("#"))

    narrow, wide = code(nightly.ic_by_date), code(nightly.xsec_by_date)
    assert "LEDGER_CSV" in narrow
    assert "XSEC_DIR" not in narrow, "the legacy reader must not reach the shards"
    assert "XSEC_DIR" in wide
    assert "LEDGER_CSV" not in wide, "the forward reader must not reach signals.csv"
    assert nightly.LEDGER_CSV.name == "signals.csv"
    assert nightly.XSEC_DIR.name == "xsec"


def test_the_live_samples_have_the_shapes_the_labels_claim():
    """Not a fixture — the real ledgers, so a label that drifts from its data fails."""
    narrow, wide = nightly.ic_by_date(), nightly.xsec_by_date()
    if not narrow or not wide:
        pytest.skip("a ledger is absent")
    n_rows = [len(v) for v in narrow.values()]
    w_rows = [len(v) for v in wide.values()]
    # legacy: deep and truncated
    assert len(narrow) > 20, "the legacy sample should be many nights deep"
    assert max(n_rows) <= 60, "the legacy sample should be a truncation, not a board"
    # forward: wide, and far shallower
    assert min(w_rows) > 150, "the forward sample should be the whole cross-section"
    assert len(wide) < len(narrow), "the forward sample should be the shallower one"
    # and on every shared night the truncation is a strict subset of the cross-section
    for d in set(narrow) & set(wide):
        assert set(narrow[d]) <= set(wide[d]), d
        assert len(narrow[d]) < len(wide[d]), d


def test_a_43_leg_figure_is_arithmetically_impossible_from_the_forward_sample():
    """The single sharpest test that the two cannot be the same measurement."""
    wide = nightly.xsec_by_date()
    if not wide:
        pytest.skip("no cross-section recorded")
    max_1d_legs = max(0, len(wide) - 1)
    published = json.loads((ROOT / "ledger" / "market_breadth.json")
                           .read_text(encoding="utf-8"))
    legs = published["ic_matrix"]["cells"]["composite"]["1"]["legs"]
    assert legs > max_1d_legs, (
        f"the published matrix reports {legs} legs and the forward sample can yield at "
        f"most {max_1d_legs} — if these ever coincide the provenance has been lost")


# --------------------------------------- mutation 1: the matrix pointed at the wide set

def test_the_matrix_cannot_be_given_a_sample_it_does_not_know():
    for bad in ("made-up", "", None, "signals.csv (top fifty by conviction)"):
        with pytest.raises(ValueError):
            nightly.ic_matrix({}, None, bad)
        with pytest.raises(ValueError):
            nightly.walkforward_report({}, None, bad)


def test_a_swapped_source_cannot_keep_the_old_label():
    """The label is derived from the sample id, so it moves with the data by construction.

    There is no free-text universe string a caller can leave behind when it changes
    source. That alone does NOT make mutation 1 impossible — it only covers the caller
    who names the wrong sample. The id is still a default, so a caller who names none
    keeps the legacy label over any data at all;
    test_the_legacy_label_cannot_be_kept_by_omission covers that path, and this docstring
    claimed a guarantee the code did not have until it did.
    """
    wide = nightly.ic_matrix(nightly.xsec_by_date(), None, "forward")
    assert wide["sample"] == "forward"
    assert wide["universe"] == nightly.IC_SAMPLES["forward"]["universe"]
    assert "LEGACY" not in wide["sample_label"]
    narrow = nightly.ic_matrix(nightly.ic_by_date(), None, "legacy")
    assert narrow["sample"] == "legacy"
    assert "FORWARD" not in narrow["sample_label"]
    import inspect
    sig = inspect.signature(nightly.ic_matrix)
    assert "universe" not in sig.parameters, \
        "a free-text universe parameter would let a label outlive its source"
    assert "universe" not in inspect.signature(nightly.walkforward_report).parameters


def test_the_legacy_label_cannot_be_kept_by_omission():
    """Mutation 1's remaining path: the sample argument LEFT OFF, not passed wrong.

    `sample` defaults to "legacy", so this call once returned a fully formed matrix
    reading LEGACY SELECTION HISTORY over three nights of the full cross-section —
    nights 3, rows/night 234, composite 1d over 2 legs at names_mean 230. None of the
    three guards fired. `publication_gate()` only rejects sample != "legacy" and this
    matrix said legacy, so it accepted it and returned NOT_ESTABLISHED, moving the
    published board off DIAGNOSTIC_ONLY on two forward legs. The validator's leg ceiling
    is nights - horizon, which 2 legs from 3 nights satisfies exactly. The cross-file
    check compares "legacy" against "forward" and they differed. The counts were all
    self-consistent; the label was the false part, and no count can falsify a label.
    """
    wide = nightly.xsec_by_date()
    if not wide:
        pytest.skip("no cross-section recorded")
    with pytest.raises(ValueError, match="forward"):
        nightly.ic_matrix(wide)               # no sample argument at all
    with pytest.raises(ValueError, match="forward"):
        nightly.ic_matrix(wide, None)         # nor a boundary-only positional call
    # and the legitimate pairings are untouched
    assert nightly.ic_matrix(wide, None, "forward")["sample"] == "forward"
    assert nightly.ic_matrix(nightly.ic_by_date())["sample"] == "legacy"


def test_the_gate_cannot_be_reached_by_a_mislabelled_matrix():
    """The consequence, asserted at the surface that would have carried it.

    The gate's own refusal is necessary and not sufficient: it reads a label. The only
    thing that makes it safe is that a mislabelled matrix can no longer be constructed.
    """
    wide = nightly.xsec_by_date()
    if not wide:
        pytest.skip("no cross-section recorded")
    with pytest.raises(ValueError):
        nightly.publication_gate(nightly.ic_matrix(wide))


def test_the_published_block_is_the_legacy_sample():
    block = nightly._ic_block()
    assert block["ic_matrix"]["sample"] == "legacy"
    assert block["ic_matrix"]["universe"] == nightly.IC_SAMPLES["legacy"]["universe"]
    gate = block["publication_gate"]
    assert gate["gate"] in ("DIAGNOSTIC_ONLY", "NOT_ESTABLISHED", "PUBLISHED")


# ------------------------------- mutation 2 & 3: the forward report borrowing from legacy

def test_the_forward_report_declares_itself_incomparable():
    w = nightly.walkforward_report(nightly.xsec_by_date(), nightly.recorded_regimes())
    assert w["sample"] == "forward"
    assert w["comparable_to_published_matrix"] is False
    assert "LEGACY" in w["not_comparable_because"]
    assert "FORWARD" in w["not_comparable_because"]
    assert "never be averaged" in w["not_comparable_because"]


def test_the_insufficient_banner_is_derived_and_lifts_on_its_own():
    """Typed banners go stale. This one is a function of the cells beneath it."""
    empty = nightly.walkforward_report({})
    assert empty["sample_state"] == nightly.FORWARD_INSUFFICIENT_BANNER
    assert empty["measurable_cells"] == 0

    # a sample that HAS measured something must not still fly the banner
    from datetime import date, timedelta
    d0 = date.fromisoformat("2026-01-01")
    by = {}
    for i in range(60):
        day = (d0 + timedelta(days=i)).isoformat()
        by[day] = {}
        for k in range(25):
            prev = by.get((d0 + timedelta(days=i - 1)).isoformat())
            px = 100.0
            if prev and f"S{k}" in prev:
                px = float(prev[f"S{k}"]["price"]) * (1 + (float(prev[f"S{k}"]["conviction"]) - 36) / 1000)
            by[day][f"S{k}"] = {"price": str(px), "conviction": str(k * 3),
                                "c_depth": str(k), "c_momentum": str(k),
                                "c_liquidity": str(k), "emission_mult": "1.0",
                                "perp_mult": "1.0"}
    rich = nightly.walkforward_report(by)
    assert rich["measurable_cells"] > 0
    assert rich["sample_state"] != nightly.FORWARD_INSUFFICIENT_BANNER
    assert "measurable" in rich["sample_state"]


def test_the_forward_report_never_carries_a_legacy_leg_count():
    """Mutation 3: a legacy figure pasted into the forward report.

    Bounded by arithmetic rather than by a list of forbidden numbers: no cell in the
    forward report may claim more legs than its own night count can produce.
    """
    path = ROOT / "ledger" / "walkforward.json"
    if not path.exists():
        pytest.skip("no forward report written yet")
    w = json.loads(path.read_text(encoding="utf-8"))
    nights = w["nights"]
    for sig, byh in w["ic"].items():
        for h, cell in byh.items():
            ceiling = max(0, nights - int(h))
            assert cell["legs"] <= ceiling, (
                f"{sig}@{h}d claims {cell['legs']} legs from {nights} night(s); at most "
                f"{ceiling} are possible — this figure did not come from this sample")


# -------------------------------------- mutation 4: labels going stale in the artifacts

def test_every_published_ic_block_names_its_sample():
    doc = json.loads((ROOT / "ledger" / "market_breadth.json").read_text(encoding="utf-8"))
    for key in ("edge", "ic_matrix"):
        block = doc.get(key)
        assert block, key
        for field in ("sample", "sample_label", "universe", "population"):
            assert block.get(field), f"{key} carries an IC and does not name its {field}"
        assert block["sample"] in nightly.IC_SAMPLES, key
    # the legacy edge panel predates the distinction and must now carry it
    assert doc["edge"]["sample"] == "legacy"
    assert doc["ic_matrix"]["sample"] == "legacy"
    wf = ROOT / "ledger" / "walkforward.json"
    if wf.exists():
        w = json.loads(wf.read_text(encoding="utf-8"))
        assert w["sample"] == "forward"
        assert w["sample_label"] != doc["ic_matrix"]["sample_label"]


def test_the_artifacts_agree_with_the_ledgers_they_claim():
    """A label is only true if the data behind it still has that shape."""
    wf = ROOT / "ledger" / "walkforward.json"
    if not wf.exists():
        pytest.skip("no forward report")
    w = json.loads(wf.read_text(encoding="utf-8"))
    wide = nightly.xsec_by_date()
    assert w["nights"] == len(wide)
    doc = json.loads((ROOT / "ledger" / "market_breadth.json").read_text(encoding="utf-8"))
    narrow = nightly.ic_by_date()
    # the legacy matrix must not be able to claim more legs than its own ledger allows
    for sig, byh in doc["ic_matrix"]["cells"].items():
        for h, cell in byh.items():
            assert cell["legs"] <= max(0, len(narrow) - int(h)), (sig, h)


# ------------------------------------------------------------------ the page

def test_the_page_labels_both_samples_visibly():
    """In the visible text, not only in a title attribute."""
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "sample_label" in page, "the matrix must render its sample's label"
    assert "forwardSampleBlock" in page, "the forward study must be shown, not omitted"
    assert "not_comparable_because" in page
    assert 'fetch("ledger/walkforward.json"' in page
    # the badge that used to read PUBLISHED beside a DIAGNOSTIC ONLY gate
    assert ">GATED ON<" in page
    assert ">PUBLISHED<" not in page, \
        "a PUBLISHED badge beside a DIAGNOSTIC ONLY gate reads as its opposite"


def test_the_board_trust_strip_names_its_sample():
    """RELEASE GATE: the most prominent IC surface was the only one that did not.

    `#label-status` sits on the default route, directly above a 234-name board, and read
    "DIAGNOSTIC ONLY · 1D IC -0.056 · 95% CI [-0.106, -0.006] · ... (43 legs ...)" with no
    sample named anywhere in it. The Selection Edge panel, the IC matrix, the forward
    block, both artifacts, the validator and all six documents name it. A reader of the
    board could only conclude the coefficient was measured on the board.

    The label must be READ FROM the gate block rather than typed into the page: a typed
    one would survive the sample changing underneath it, which is the whole failure mode
    this file exists to prevent.
    """
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r"function gateChip\(\)\{(.*?)\n\}", page, re.S)
    assert m, "gateChip not found"
    body = m.group(1)
    assert "sample_label" in body, \
        "the publication-gate chip must name the sample it was measured on"
    assert "g.sample_label" in body or "gate.sample_label" in body, \
        "the label must be read from the gate block, not typed into the page"
    assert not re.search(r'"LEGACY[^"]*"|\'LEGACY[^\']*\'', body), \
        "a typed LEGACY string would outlive the sample it describes"
    # and the gate block it reads actually carries the field
    doc = json.loads((ROOT / "ledger" / "market_breadth.json").read_text(encoding="utf-8"))
    g = doc["publication_gate"]
    assert g.get("sample_label") and g.get("measured_on"), \
        "the chip reads sample_label/measured_on; the artifact must publish them"
    assert g["sample"] == "legacy"


def test_the_page_never_prints_one_samples_numbers_under_the_others_label():
    """Structural: the two render paths must read from different globals."""
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r"function renderICMatrix\(\)\{(.*?)\n\}", page, re.S)
    assert m, "renderICMatrix not found"
    body = m.group(1)
    assert "BREADTH.ic_matrix" in body
    assert "WALKFWD" not in body, \
        "the legacy matrix renderer must not reach into the forward sample"
    f = re.search(r"function forwardSampleBlock\(\)\{(.*?)\n\}", page, re.S)
    assert f, "forwardSampleBlock not found"
    fb = f.group(1)
    assert "WALKFWD" in fb
    assert "BREADTH" not in fb, \
        "the forward block must not reach into the legacy sample"


# Every document that can quote a leg count. The regression covered two of these and
# passed; run over the rest it failed at once, which is what a partial guard is worth.
_DOCS = ("docs/DEFERRED-REGISTER.md", "methodology.html",
         "docs/AUDIT-PHASE1-2026-09-17.md", "docs/PHASE2A-CALIBRATION-2026-09-17.md",
         "docs/ARCHITECTURE-NOTES.md", "docs/CLOSURE-2026-09-18.md")


def test_the_docs_name_the_sample_wherever_they_quote_a_leg_count():
    """Any document quoting the 43-leg result must say which history it came from."""
    for name in _DOCS:
        path = ROOT / name
        if not path.exists():
            continue
        text = " ".join(path.read_text(encoding="utf-8").split())
        for hit in re.finditer(r"43[ -]leg|43 legs", text):
            window = text[max(0, hit.start() - 400):hit.end() + 400]
            assert re.search(r"signals\.csv|legacy|top fifty|top-fifty|truncat", window, re.I), \
                f"{name} quotes a 43-leg figure without naming the legacy sample"


def test_no_document_says_the_persisted_universe_is_a_market_cap_cut():
    """It is a conviction sort, and the distinction is the whole point of the caveat.

    AUDIT-2026-09 1.0 settled this from the data and corrected four source comments; a
    rendered provenance string and two sentences on the Method page kept the error, and
    those are the copies a reader actually sees. Selected on conviction means selected on
    the variable any edge measured over the sample is about — which is not a detail.
    """
    pat = re.compile(r"(persisted universe|persists?|rows\[:50\]|top[ -]?50|top fifty)"
                     r"[^.]{0,120}by market cap", re.I)
    for name in _DOCS + ("nightly.py", "index.html"):
        path = ROOT / name
        if not path.exists():
            continue
        text = " ".join(path.read_text(encoding="utf-8").split())
        for hit in pat.finditer(text):
            window = text[max(0, hit.start() - 200):hit.end() + 200]
            # a sentence that NAMES the correction is allowed to quote the old wording
            assert re.search(r"corrected|was wrong|said otherwise|error|not a market.cap",
                             window, re.I), \
                f"{name}: {hit.group(0)!r} — the persisted cut is by conviction"


def test_the_architecture_map_records_both_samples():
    """The file-to-responsibility map documented one IC history and not the other."""
    notes = (ROOT / "docs" / "ARCHITECTURE-NOTES.md").read_text(encoding="utf-8")
    for token in ("ledger/xsec", "walkforward.json", "signals.csv"):
        assert token in notes, f"the architecture map omits {token}"
    flat = " ".join(notes.split())
    assert re.search(r"two\s+(information[- ]coefficient|IC)\s+samples", flat, re.I), \
        "the architecture map must state that there are two IC samples"
