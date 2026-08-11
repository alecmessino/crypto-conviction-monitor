"""Structural checks on index.html that a browser would only reveal by clicking.

These exist because the drawer was throwing on every single click on the live site and
nothing said so. `openDrawer` queried `#dw-era` and `#dw-perp` twenty-nine lines before
the `innerHTML` assignment that creates them, so the lookups returned null, assigning
to them threw, and the assignment never ran. The header rendered, the body did not, and
the exception died inside a click handler where no test and no user could see it.

A DOM test would catch that, but the repo has no browser harness in CI. These are the
cheap structural invariants that would have caught it anyway.
"""
import re
from pathlib import Path

import pytest

HTML = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)


def test_no_element_is_queried_before_the_markup_that_creates_it():
    """Any id that only ever exists inside a template literal must not be read with $()
    earlier in the file than the assignment that writes it."""
    created = {}      # id -> offset where the markup creating it appears
    for m in re.finditer(r'id="(dw-[a-z-]+)"', SCRIPT):
        created.setdefault(m.group(1), m.start())
    assert "dw-era" in created, "the drawer markup no longer declares #dw-era"
    for el, made_at in created.items():
        for use in re.finditer(rf'\$\("#{re.escape(el)}"\)', SCRIPT):
            assert use.start() > made_at, (
                f"#{el} is queried at offset {use.start()} but only created at "
                f"{made_at} — on the first call the query returns null and any "
                f"assignment to it throws inside the click handler")


def test_the_drawer_writes_its_values_into_the_markup():
    """The fix, pinned: the era and perp readings are interpolated rather than queried
    and mutated after the fact."""
    assert 'id="dw-era" class="${eraCls}">${eraTxt}' in SCRIPT
    assert 'id="dw-perp" class="${perpCls}">${perpTxt}' in SCRIPT


def test_the_error_row_spans_the_whole_table():
    """A colspan narrower than the header leaves the failure message sitting under the
    first few columns with empty cells beside it, which reads as a rendering fault
    rather than as the explanation it is."""
    header = re.search(r'<table id="tbl-conv"><thead><tr>(.*?)</tr>', HTML, re.S).group(1)
    cols = len(re.findall(r"<th", header))
    span = int(re.search(r'<td colspan="(\d+)" class="err">Live fetch failed', HTML).group(1))
    assert span == cols, f"error row spans {span} of {cols} columns"


def test_the_timeframe_control_actually_reorients_the_table():
    """It used to move its own highlight and write a note while the table went on
    showing the 24h delta. A control that appears to change the view and does not is
    worse than no control at all."""
    handler = SCRIPT[SCRIPT.index('document.querySelectorAll("#tf .sw")'):]
    handler = handler[:handler.index("});") + 3]
    assert "TF = s.dataset.tf" in handler
    assert "renderTables()" in handler


def test_the_scoring_block_is_still_delimited_for_the_parity_gate():
    assert "MODEL PORT" in SCRIPT and "END MODEL PORT" in SCRIPT


@pytest.mark.parametrize("fn", ["fundingCell", "divBadge", "regimeCell", "perpBlock"])
def test_the_derivatives_renderers_exist(fn):
    assert f"function {fn}(" in SCRIPT


def test_absent_derivatives_render_as_a_dash_rather_than_a_zero():
    """No perp market and flat funding are opposite readings. Rendering both as 0%
    would put a confident carry figure on assets that have no perpetual at all."""
    assert 'if(!p || p.fundingAnn==null) return \'<span class="muted">—</span>\'' in SCRIPT


def test_the_regime_cell_reports_progress_instead_of_an_empty_cell():
    """An empty cell reads as a broken column. The bar count is the message while the
    index is still accumulating."""
    assert "c.bars" in SCRIPT and "need" in SCRIPT
