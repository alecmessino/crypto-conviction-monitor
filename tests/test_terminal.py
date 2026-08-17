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
# Comments stripped, for assertions that must not be satisfied — or defeated — by
# prose. A comment explaining why a pattern is forbidden necessarily contains that
# pattern, and a naive substring check cannot tell the explanation from the offence.
CODE = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", SCRIPT, flags=re.S))


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
    # Asserted against the one funding cell the page now has. There used to be two
    # implementations with different thresholds — the matrix coloured off +30/-20 and
    # Module E off the regime bands — so a 35% APR asset rendered red in one table and
    # amber in the other, on one screen.
    assert "function fundingCell(sym)" in SCRIPT
    assert '\'<span class="muted">—</span>\'' in SCRIPT
    assert 'if(apr==null) return `<span class="fh none">—</span>`' in SCRIPT
    assert "const FUNDING_HOT" not in SCRIPT, "a second funding threshold set is back"


def test_the_regime_cell_reports_progress_instead_of_an_empty_cell():
    """An empty cell reads as a broken column. The bar count is the message while the
    index is still accumulating."""
    assert "c.bars" in SCRIPT and "need" in SCRIPT


# ---------------------------------------------------------------------------
# Module 5: density, keybindings, telemetry
# ---------------------------------------------------------------------------
def test_headers_are_sticky():
    """Thirteen columns, none of them self-describing — "1/15" and "-33%*" mean nothing
    without the label above them."""
    assert "thead th{position:sticky" in HTML


def test_the_board_is_not_virtualized():
    """The universe is 50 rows. Virtual scrolling below a few thousand buys nothing
    measurable and adds a scroll-position bug surface, so the decision is recorded here
    rather than left to be re-litigated."""
    assert "NOT virtualized" in HTML
    for marker in ("IntersectionObserver", "translateY(", "virtual"):
        assert marker not in CODE


def test_the_filter_searches_the_whole_universe_not_the_visible_rows():
    """A filter that only searches what is already on screen is a highlighter."""
    assert "const base = hide ? gated : STATE;" in SCRIPT
    assert "base.filter(matchesFilter)" in SCRIPT


def test_one_predicate_serves_the_text_box_and_the_chips():
    """Two implementations would let a typed 'alt' and a clicked ALT chip drift apart."""
    assert SCRIPT.count("function matchesFilter(") == 1
    assert "setFilter(chip.textContent.trim())" in SCRIPT
    assert "setFilter(fbox.value)" in SCRIPT


def test_slash_focuses_and_escape_clears():
    assert 'e.key==="/"' in SCRIPT and 'fbox.focus()' in SCRIPT
    assert 'e.key==="Escape"' in SCRIPT


def test_slash_does_not_hijack_typing_in_an_input():
    """Binding a printable character globally makes it impossible to type that
    character anywhere else on the page."""
    assert '/^(INPUT|TEXTAREA)$/' in SCRIPT and "&& !typing" in SCRIPT


def test_no_shortcuts_are_bound_to_views_that_do_not_exist():
    """The brief asked for 1-4 to switch view tabs. This terminal has no tabs, and a
    shortcut to nothing is worse than no shortcut."""
    assert 'e.key==="1"' not in SCRIPT and "data-tab" not in HTML


def test_the_parity_status_is_read_from_an_artifact_and_never_hardcoded():
    """A decorative 'PASS 8/8' is the exact class of ornamental status this project has
    already deleted once. Absent artifact must read UNKNOWN, not PASS."""
    assert "PARITY.passed" in SCRIPT and "PARITY.total" in SCRIPT
    assert "ledger/parity.json" in SCRIPT
    assert 'parity <b style="color:var(--sec)">UNKNOWN' in SCRIPT
    assert "PASS 8/8" not in CODE


def test_latency_is_measured_rather_than_asserted():
    assert "performance.now()" in SCRIPT and "LATENCY = performance.now()-t0" in SCRIPT


# ---------------------------------------------------------------------------
# position sizing
# ---------------------------------------------------------------------------
def test_sizing_refuses_to_call_itself_optimal():
    """Optimal sizing needs an expected return. The measured IC is indistinguishable
    from zero, so a Kelly-style number here would be an arbitrary constant wearing a
    formula — and would be acted on as if it were derived."""
    assert "Not an optimal" in SCRIPT
    assert "no expected return to" in SCRIPT


def test_unfilled_size_is_left_unallocated_not_pushed_into_the_next_name():
    """Redistributing a refused line concentrates the book into whatever happened to be
    liquid, and presents that as the portfolio the score recommended."""
    assert "unallocated: Math.max(0, notional - allocated)" in SCRIPT
    assert "Unallocated" in SCRIPT


def test_an_asset_with_no_volume_is_refused_rather_than_sized():
    """Sizing it anyway puts a number on a position whose exit cost is unknown."""
    assert "adv > 0 ? Math.min(wanted, cap) : 0" in SCRIPT
    assert "no volume data" in SCRIPT


def test_low_turnover_is_not_double_counted_against_the_adv_cap():
    """The cap is already a share of actual volume, so a thin name is held down by it.
    A separate thin-turnover haircut applied the same constraint twice — and calibrated
    at 15% it flagged 46 of the 50 live names, which is the same as flagging none."""
    assert "SZ_TURN_THIN" not in CODE
    assert "SZ_TURN_WASH" in CODE


def test_the_binding_constraint_is_named_per_line():
    """A size with no reason attached cannot be argued with."""
    assert '"ADV cap"' in SCRIPT and 'binding' in SCRIPT


def test_exit_time_is_computed_at_the_same_participation_rate():
    """Days-to-exit is the number that decides whether a position is a position or a
    trap, and it has to use the rate the size was built from."""
    assert "exitDays" in SCRIPT and "adv * participation" in SCRIPT


# ---------------------------------------------------------------------------
# model health ribbon
# ---------------------------------------------------------------------------
def test_the_health_ribbon_is_subordinate_to_the_price_ribbon():
    """These are statements about the model, not the market. A reader scanning for
    prices must not have to step over them."""
    assert ".ribbon.health{" in HTML
    assert "border-left:2px solid var(--blue)" in HTML
    assert HTML.index('class="ribbon glass"') < HTML.index('class="ribbon health"')


def test_flip_counters_are_clickable_and_carry_their_symbols():
    """A delta counter in a terminal exists to get you to the names, not to tell you
    how many there were."""
    assert "data-hp=" in SCRIPT and 'closest("[data-hp]")' in SCRIPT
    assert "FLIP_SET" in CODE


def test_a_pill_selection_and_a_typed_filter_cannot_both_be_live():
    """Two live filters would silently intersect, and neither could be cleared without
    guessing which was doing the work."""
    assert "if(q) FLIP_SET = null;" in SCRIPT
    assert "if(FLIP_SET) return FLIP_SET.has(t.sym);" in SCRIPT


def test_escape_clears_a_pill_selection_too():
    assert "if(FLIP_SET){ FLIP_SET=null;" in SCRIPT


def test_the_ribbon_states_its_window_rather_than_implying_thirty_days():
    """'Persistence 88.4% (30D)' is the badge a terminal naturally writes. With eleven
    nights on file it would be a fabrication, so the window is interpolated from the
    data instead of being written into the label."""
    assert "top-${h.cohort} stickiness · ${h.pairs} nights" in SCRIPT
    assert "30D" not in CODE


def test_freshness_is_an_age_not_a_manufactured_percentage():
    """There is no meaningful '99.2% fresh' to compute — the board is from a date or it
    is not."""
    assert "Age of the most recent recorded board" in SCRIPT
    assert "99.2" not in CODE
