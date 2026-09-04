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
    # Signature widened to fundingCell(sym, row) when the rewind scrubber landed: a
    # recorded row must read the carry ITS OWN night recorded rather than joining
    # tonight's funding.json onto a three-week-old board. Still exactly one
    # implementation, which is what this line is really pinning.
    assert SCRIPT.count("function fundingCell(") == 1
    assert "function fundingCell(sym,row)" in SCRIPT
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
    """A filter that only searches what is already on screen is a highlighter.

    Reads BOARD rather than STATE since the rewind scrubber landed: BOARD is the live
    board in live mode and the recorded one when scrubbed back, so the filter searches
    whichever universe is actually on screen. The property is unchanged — it is the
    whole set, not the visible ten.
    """
    assert "const BOARD = boardRows();" in SCRIPT
    assert "const base = hide ? gated : BOARD;" in SCRIPT
    assert "base.filter(matchesFilter)" in SCRIPT
    assert "function boardRows(){ return RENDER_ROWS || STATE; }" in SCRIPT


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
    # The terminal now HAS tabs (the secondary pane), so the original form of this —
    # "no tabs exist, therefore bind no tab keys" — no longer states the property. The
    # property was always that a shortcut must not point at a view that does not exist.
    tabs = set(re.findall(r'data-tab="([a-z]+)"', HTML))
    panes = set(re.findall(r'data-pane="([a-z]+)"', HTML))
    assert tabs and tabs == panes, (
        f"tab buttons {sorted(tabs)} do not match panes {sorted(panes)} — a tab that "
        f"reveals nothing is a shortcut to a view that does not exist")
    # And no digit is bound to anything, because nothing numbers these views.
    for k in ("1", "2", "3", "4"):
        assert f'e.key==="{k}"' not in SCRIPT


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
    # The health ribbon moved inside the Status expand when the three ribbons were
    # collapsed into one strip. It is now subordinate by being closed by default, which
    # is a stronger form of the same property — but it must still come after the strip.
    assert HTML.index('class="ribbon glass statusbar"') < HTML.index('class="ribbon health"')
    assert HTML.index('id="status-detail" hidden') < HTML.index('class="ribbon health"'), \
        "the health ribbon is no longer inside the collapsed Status panel"


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


# ---------------------------------------------------------------------------
# Modules F-J: the context panels, the supply column, and the rewind scrubber
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fn", ["renderMacro", "renderSectors", "renderTMD",
                                "renderFallen", "renderCorr", "factorQuad",
                                "renderRewind", "supplyCell"])
def test_the_new_renderers_exist(fn):
    assert f"function {fn}(" in SCRIPT


@pytest.mark.parametrize("host", ["#macro", "#sectors", "#tmd", "#fallen", "#corr",
                                  "#rewind"])
def test_every_new_panel_has_markup_to_render_into(host):
    assert f'id="{host[1:]}"' in HTML


def test_every_context_panel_renders_a_pending_state():
    """A sector matrix with no rows and a market where nothing moved look identical, and
    only one of them is a reason to go and look at the pipeline. Each panel must name
    the artifact it is waiting on rather than sitting silently empty."""
    for panel in ("Sector feed", "Trending feed", "Screen pending", "Correlation pending"):
        assert panel in HTML, f"no pending state for {panel!r}"
    assert HTML.count("ledger/market_intel.json") >= 4


def test_the_supply_column_distinguishes_unknown_from_neutral():
    """The distinction Module F is built on. A dash means no FDV was published and NO
    multiplier was applied; x1.000 means the token is fully circulating and the
    multiplier was exactly neutral. Collapsing them would tell a reader that an
    undisclosed emission schedule had been checked and cleared."""
    assert "function supplyCell(" in SCRIPT
    assert "d==null" in SCRIPT
    assert "least transparent tokens" in SCRIPT


def test_the_emission_curve_lives_inside_the_parity_markers():
    """Module F multiplies the published score, so both sides must be executed by the
    gate. A copy outside the markers is a copy the gate cannot see."""
    port = SCRIPT[SCRIPT.find("MODEL PORT"):SCRIPT.find("END MODEL PORT")]
    for fn in ("function emissionDrag(", "function emissionMult("):
        assert fn in port, f"{fn} is outside the MODEL PORT markers"
    assert SCRIPT.count("function emissionMult(") == 1


def test_the_terminal_reads_its_thresholds_from_the_engine():
    """A threshold duplicated in two languages is a threshold that will disagree with
    itself. The funding bands already went through this; the Module F-J bands are
    published on the artifact and read from it."""
    assert "INTEL_TH = INTEL.thresholds || {}" in SCRIPT
    assert "INTEL_TH.liq_shock_z" in SCRIPT


def test_a_rewound_row_does_not_borrow_tonights_readings():
    """The failure this guards: joining tonight's funding.json and tonight's choppiness
    onto a three-week-old board, so a historical row asserts a carry that did not exist
    on the date it claims to show."""
    assert "row._rewound" in SCRIPT
    assert "_fundApr" in SCRIPT and "_strategy" in SCRIPT
    assert "fundingCell(t.sym,t)" in SCRIPT and "regimeCell(t.sym,t)" in SCRIPT


def test_a_rewound_row_reports_no_range_position_rather_than_zero():
    """The ledger records no ATH or ATL, so a recorded row has no range Z. Rendering
    0.00 would read as "exactly mid-range", which is a claim the row cannot support."""
    assert "ath:0, atl:0, z:null" in SCRIPT
    assert "t.z==null" in SCRIPT


def test_the_rewound_state_is_impossible_to_miss():
    """A nineteen-day-old board read as this morning's is the worst failure this control
    can cause, so the whole page is marked rather than one corner of it."""
    assert "body.rewound" in HTML
    assert 'classList.toggle("rewound"' in SCRIPT


def test_the_scrubber_has_a_live_position_past_the_last_recorded_night():
    """Live is a position on the control, not a separate mode you have to know about."""
    assert "sl.max=REWIND_DATES.length" in SCRIPT
    assert "i>=REWIND_DATES.length ? null" in SCRIPT


def test_the_rewind_reads_the_unfiltered_row_set():
    """LEDGER is filtered to rows carrying a Dune era, which is most of them absent.
    Rewinding over that subset would show a board that was never published."""
    assert "ALL_ROWS.forEach(r=>{ const d=r.date;" in SCRIPT
    assert "LEDGER = ALL_ROWS.filter(" in SCRIPT


def test_the_factor_quadrant_names_its_quadrants():
    """A quadrant chart whose quadrants are unnamed is a scatter plot."""
    for label in ("CONVICTION", "QUALITY, UNCONFIRMED", "MOMENTUM, THIN", "DE-ALLOCATE"):
        assert label in SCRIPT


def test_the_quadrant_axis_is_robust_to_an_outlier():
    """Relative strength has a long right tail. One name up 400% against BTC stretched a
    min/max axis until the other fifty-nine sat in a single column against the left
    edge, which is what this chart did on its first render."""
    assert "pct(ms,0.05)" in SCRIPT and "pct(ms,0.95)" in SCRIPT
    # Wording moved with the rewrite; the property is that the count of clamped points
    # is reported rather than silently absorbed.
    assert "clamped to the 5-95 percentile axis" in SCRIPT


def test_the_sizer_caps_a_correlated_book_and_prices_the_entry():
    """Conviction sets the shape; the caps set the ceiling. A book of fifteen names that
    move together is not fifteen positions."""
    assert "function execDrag(" in SCRIPT
    assert "Math.sqrt(part)" in SCRIPT, "impact must grow with the root of participation"
    assert "correlated with" in SCRIPT
    assert "dragMissing" in SCRIPT, "unestimatable lines must be counted, not averaged in as free"


def test_the_new_columns_did_not_break_the_error_row_span():
    """The failure row spans the table. A stale colspan leaves a ragged cell that reads
    as a rendering bug at exactly the moment the page is already reporting a problem."""
    headers = re.search(r'<table id="tbl-conv"><thead><tr>(.*?)</tr>', HTML, re.S).group(1)
    assert headers.count("<th") == 14
    assert 'colspan="14"' in HTML


# ---------------------------------------------------------------------------
# provenance: the nightly's credential and the browser's live feed are two facts
# ---------------------------------------------------------------------------
def test_the_ribbon_separates_the_nightly_credential_from_the_live_feed():
    """The contradiction this replaced: one card reading "CG PLAN KEYLESS" while the
    pipeline it documented was authenticated. The nightly runs server-side and can hold
    a secret; this page is a static file and never can. They are allowed to disagree and
    the ribbon has to be able to say so."""
    assert "function nightlyFeedState(" in SCRIPT
    assert "function liveFeedState(" in SCRIPT
    assert 'macroCard("Nightly Feed"' in SCRIPT
    assert 'macroCard("Live Prices"' in SCRIPT
    assert "CG Plan" not in SCRIPT, "the conflated single card is back"


@pytest.mark.parametrize("label", ["DEMO KEY", "PRO KEY", "KEYLESS", "KEY REJECTED"])
def test_every_nightly_credential_state_is_rendered(label):
    assert label in SCRIPT


@pytest.mark.parametrize("label", ["LIVE", "STALE", "STUBBED", "THROTTLED", "FAILED"])
def test_every_live_feed_state_is_rendered(label):
    assert label in SCRIPT


def test_stubbed_data_is_detected_from_the_payload_not_a_flag():
    """A preview flag someone has to remember to set is a flag that will be forgotten on
    the screenshot that matters. The payload dates itself: a real /coins/markets response
    carries last_updated timestamps seconds old."""
    assert "function feedProvenance(" in SCRIPT
    assert "last_updated" in SCRIPT
    assert "FEED_LIVE_MIN" in SCRIPT and "FEED_STALE_MIN" in SCRIPT


def test_the_feed_age_reads_the_newest_timestamp_not_the_oldest():
    """A delisted token legitimately carries a month-old timestamp. Reading the minimum
    would call every live fetch stale."""
    fn = SCRIPT[SCRIPT.find("function feedProvenance("):]
    fn = fn[:fn.find("\nfunction ")]
    assert "v>newest" in fn.replace(" ", ""), "provenance is not taking the maximum"


def test_a_stubbed_render_says_so_page_wide():
    """One chip in a ribbon can be cropped out of a screenshot. The disclosure is a
    full-width band plus a body class, mirroring the rewound treatment."""
    assert "function renderFeedBanner(" in SCRIPT
    assert "PREVIEW — PRICES ARE NOT LIVE" in SCRIPT
    assert "body.stubbed" in HTML
    assert ".stub-banner" in HTML
    assert 'classList.toggle("stubbed"' in SCRIPT


def test_a_failed_or_throttled_fetch_is_a_provenance_state():
    """Without this the LIVE PRICES card keeps showing its last success while the board
    underneath it renders an error."""
    assert 'FEED = {state: /rate-limit|429/i.test(e.message)' in SCRIPT
    assert "renderFeedBanner(); renderMacro();" in SCRIPT


def test_the_ribbon_is_re_rendered_after_the_live_fetch():
    """renderMacro runs on the ledger path, before any price arrives. Without a second
    call the live card is frozen on 'pending' forever."""
    live = SCRIPT[SCRIPT.find("async function load(){"):]
    assert "factorQuad(); renderSizing(); renderMacro();" in live


def test_the_page_never_holds_a_coingecko_credential():
    """This is a static file served from Pages. There is nowhere to put a secret, and an
    attempt to would publish it. Pinned so nobody 'fixes' the KEYLESS label by adding a
    key to the client."""
    for banned in ("x-cg-demo-api-key", "x-cg-pro-api-key", "COINGECKO_API_KEY"):
        assert banned not in HTML, f"{banned} appears in a public static page"


# ---------------------------------------------------------------------------
# chart legibility: budgeted labels, collision-aware placement, reachability
# ---------------------------------------------------------------------------
def _chart_body(name):
    start = SCRIPT.find(f"function {name}(){{")
    assert start != -1, f"{name}() is gone"
    end = SCRIPT.find("\n}\n", start)
    return SCRIPT[start:end]


@pytest.mark.parametrize("fn", ["quad", "alphaMap", "factorQuad"])
def test_every_chart_places_labels_through_the_shared_engine(fn):
    """Three charts each grew their own fixed +4/-4 offset and no placement logic at
    all. One engine, executed by tests/test_labels.py under node, is what keeps the
    overlap and bounds guarantees true for all of them at once."""
    body = _chart_body(fn)
    assert "placeLabels(" in body, f"{fn}() no longer uses the shared label engine"
    assert "labelPriority(" in body, f"{fn}() no longer ranks its labels"


@pytest.mark.parametrize("fn", ["quad", "alphaMap", "factorQuad"])
def test_no_chart_labels_points_unconditionally(fn):
    """The defect that produced the rejected screenshots: quad() drew a label for every
    one of 234 rows inside its forEach. A label written directly from the point loop is
    a label nothing budgeted or placed."""
    body = _chart_body(fn)
    loop_start = body.find("forEach(")
    if loop_start == -1:
        return
    loop = body[loop_start:]
    assert "fillText(t.sym" not in loop, \
        f"{fn}() still writes a ticker straight from its point loop"


@pytest.mark.parametrize("fn", ["quad", "alphaMap", "factorQuad"])
def test_every_chart_bounds_its_labels_to_a_plot_rect(fn):
    body = _chart_body(fn)
    assert "rect=" in body.replace(" ", ""), f"{fn}() defines no plot rect"
    assert "CHART_LABELS" in body, f"{fn}() does not record what it drew"


@pytest.mark.parametrize("fn", ["quad", "alphaMap", "factorQuad"])
def test_every_chart_registers_its_points_for_hover_and_keyboard(fn):
    """A budgeted label set is only honest if the dropped names stay reachable."""
    body = _chart_body(fn)
    assert "CHART_POINTS" in body, f"{fn}() registers no points for hit-testing"
    assert "attachChartInteraction(" in body, f"{fn}() wires no interaction"


@pytest.mark.parametrize("fn", ["quad", "alphaMap", "factorQuad"])
def test_every_chart_still_plots_every_point(fn):
    """Only the LABELS are budgeted. A chart that dropped points to relieve crowding
    would be solving the wrong problem — the scatter's whole value is the distribution."""
    body = _chart_body(fn)
    assert "slice(0,12)" not in body and "slice(0,10)" not in body, \
        f"{fn}() truncates its point set"
    # The budget must be applied to the LABEL candidates, not to the points. Both charts
    # that plot the full universe register every row for hit-testing, so the registered
    # count is the check: it is pushed inside the point loop, unfiltered.
    if fn in ("quad", "alphaMap"):
        assert "pts.push(" in body, f"{fn}() registers no points at all"
        assert body.count("pts.push(") >= 1
        loop = body[body.find("forEach("):body.find("placeLabels(")]
        assert "pts.push(" in loop, (
            f"{fn}() does not register its points from inside the point loop, so some "
            f"rows are plotted without becoming reachable")


def test_the_annotations_are_drawn_last_so_markers_cannot_scribble_them():
    """They used to be painted before the points, so a dense cluster was drawn straight
    through 'QUALITY, UNCONFIRMED'."""
    assert "function annotate(" in SCRIPT
    for fn in ("quad", "alphaMap", "factorQuad"):
        body = _chart_body(fn)
        # Matched on the CALL, not the bare token: alphaMap explains in a comment why
        # its rotated axis title bypasses annotate(), and that mention sits above
        # placeLabels — a substring search finds the prose and reports a false failure.
        call = "annotate(x, notes)" if fn != "factorQuad" else "annotate(g, notes)"
        assert call in body, f"{fn}() does not defer its annotations"
        assert body.find(call) > body.find("placeLabels("), \
            f"{fn}() draws annotations before its labels are placed"


def test_the_charts_are_keyboard_reachable():
    """A tooltip reachable only by mouse puts the dropped names out of reach of anyone
    navigating by keyboard, which is worse than the collisions this replaced."""
    assert 'cv.setAttribute("tabindex","0")' in SCRIPT
    assert '"ArrowRight"' in SCRIPT and '"Escape"' in SCRIPT
    assert "canvas:focus-visible" in HTML


def test_the_tooltip_cannot_eat_its_own_mouse_events():
    tip = HTML[HTML.find(".chart-tip{"):]
    tip = tip[:tip.find("}")]
    assert "pointer-events:none" in tip


def test_factor_quad_measures_its_own_canvas_not_its_parents_padding_box():
    """It read parentElement.clientWidth (398) inside a 374px content area and pinned
    that back with an inline style, so the element was 24px wider than its container —
    which is what clipped the right-hand quadrant names."""
    body = _chart_body("factorQuad")
    assert "cv.clientWidth" in body
    assert "box.clientWidth" not in body
    assert 'cv.style.width' not in body


def test_the_layout_never_scrolls_sideways():
    """Three flex rows had nowrap and one grid track was plain 1fr, so the page had a
    921px minimum and every viewport below ~1280px scrolled horizontally."""
    assert ".ribbon{grid-column:1/-1;display:flex;gap:14px;flex-wrap:wrap" in HTML
    assert "grid-template-columns:minmax(0,1fr)" in HTML
    rail = HTML[HTML.find(".rail{"):]
    assert "min-width:0" in rail[:rail.find("}")]
    # The rewind moved inside the wrapping status strip, so it no longer needs its own
    # wrap rule — but it must not reintroduce a fixed minimum. Its slider is the one
    # element with a width, and it is a small fixed one rather than a flex-basis that
    # grows the row.
    rw = HTML[HTML.find(".rewind{"):]
    rw = rw[:rw.find("}")]
    assert "grid-column" not in rw, "the rewind is a full-width row again"
    assert "min-width" not in rw


# ---------------------------------------------------------------------------
# degraded rendering: what a throttled visitor actually sees
# ---------------------------------------------------------------------------
def test_the_charts_render_an_empty_state_when_the_live_fetch_fails():
    """Found by smoke-testing the deployed bytes: the three canvases only ever ran
    inside the success path, so a rate-limited visitor got three blank rectangles beside
    a board that was reporting the error in words. Blank reads as broken."""
    assert "function emptyChart(" in SCRIPT
    # Anchored inside load(), not on the first '}catch(e){' in the file — loadLedger()
    # has seven of them above it and a naive search lands in the wrong function.
    body = SCRIPT[SCRIPT.find("async function load(){"):]
    body = body[:body.find("\n}\n")]
    catch = body[body.find("}catch(e){"):]
    assert catch, "load() no longer has a catch path"
    assert "quad(); alphaMap(); factorQuad();" in catch, \
        "the charts are still unreachable when the live fetch fails"


def test_the_empty_state_names_the_cause_not_the_symptom():
    """'no rows in view' does not distinguish a starved panel from a broken one."""
    assert "awaiting live prices" in SCRIPT
    assert "which has no recorded equivalent" in SCRIPT


def test_the_empty_state_is_still_keyboard_focusable():
    """Each chart wires its interaction at the END of its draw, and the empty path
    returns before that — so whether a canvas took tab focus depended on the feed."""
    fn = SCRIPT[SCRIPT.find("function emptyChart("):]
    fn = fn[:fn.find("\n}")]
    assert "attachChartInteraction(id)" in fn


def test_the_empty_state_still_records_a_placement():
    """Otherwise CHART_LABELS keeps whatever the last successful render left, and a
    render check would assert against a stale board."""
    fn = SCRIPT[SCRIPT.find("function emptyChart("):]
    fn = fn[:fn.find("\n}")]
    assert "CHART_LABELS[id]=" in fn and "CHART_POINTS[id]=[]" in fn


def test_the_page_declares_a_favicon():
    """Production answered 404 to every visitor's /favicon.ico. Inline, so there is no
    binary asset to keep in sync and no second request."""
    assert 'rel="icon"' in HTML
    assert "data:image/svg+xml," in HTML


def test_the_inline_favicon_is_valid_svg():
    """A malformed data URI fails silently — the browser shows the default icon and
    nothing reports it. This caught a mistyped SVG namespace on the first attempt."""
    import urllib.parse
    import xml.etree.ElementTree as ET

    m = re.search(r'<link rel="icon" href="data:image/svg\+xml,([^"]+)"', HTML)
    assert m, "no inline favicon"
    svg = urllib.parse.unquote(m.group(1))
    assert "http://www.w3.org/2000/svg" in svg, "wrong or mistyped SVG namespace"
    ET.fromstring(svg)


def test_the_charts_plot_the_live_board_only_never_the_rewound_one():
    """The rewind note promises the charts are not rewound, and it was half true:
    renderTables() ends by calling quad(), so scrubbing back moved that one chart to the
    recorded rows while the other two stayed live — three panels side by side, one
    silently on a different date."""
    assert "function liveRows(){ return STATE; }" in SCRIPT
    for fn in ("quad", "alphaMap", "factorQuad"):
        body = _chart_body(fn)
        assert "liveRows()" in body, f"{fn}() does not read the live board"
        assert "boardRows()" not in body, (
            f"{fn}() reads boardRows(), so scrubbing the rewind moves it to a different "
            f"date than the two charts beside it")


def test_only_the_conviction_matrix_follows_the_rewind():
    """boardRows() is the rewind-aware source and must stay confined to the table."""
    assert "function boardRows(){ return RENDER_ROWS || STATE; }" in SCRIPT
    assert SCRIPT.count("boardRows()") <= 3, (
        "boardRows() has spread beyond renderTables — every extra caller is a panel "
        "that can silently disagree about which night it is showing")


# ---------------------------------------------------------------------------
# density pass: one status strip, one tabbed secondary pane, relocated prose
# ---------------------------------------------------------------------------
def test_there_is_exactly_one_ribbon_above_the_board():
    """Three stacked ribbons plus a rewind bar put 225px of status above a page whose
    first job is the ranked board."""
    markup = HTML[:HTML.find("<script>")]
    strip = markup.find('class="ribbon glass statusbar"')
    matrix = markup.find("CONVICTION MATRIX")
    between = markup[strip:matrix]
    # The health and macro ribbons still exist in the markup between the two, but inside
    # the collapsed Status panel — so they are excluded here rather than counted. What
    # must not come back is a ribbon that is VISIBLE above the board by default.
    detail = between.find('id="status-detail"')
    visible = between[:detail] if detail != -1 else between
    assert visible.count('class="ribbon') == 1, (
        "more than one ribbon is visible between the status strip and the board")


def test_the_status_strip_carries_only_what_is_needed_to_trust_the_board():
    strip = HTML[HTML.find('class="ribbon glass statusbar"'):]
    strip = strip[:strip.find('id="status-detail"')]
    for keep in ("rb-mcap", "rb-str", "rb-upd", "rb-lev", "status", "rewind"):
        assert f'id="{keep}"' in strip, f"{keep} left the live strip"
    # Everything else moved behind Status, not away.
    for moved in ("rb-turn", "rb-conv"):
        assert f'id="{moved}"' not in strip, f"{moved} is still on the live strip"
        assert f'id="{moved}"' in HTML, f"{moved} was DELETED rather than relocated"


def test_the_status_detail_is_collapsed_by_default_and_keeps_its_panels():
    assert 'id="status-detail" hidden' in HTML
    panel = HTML[HTML.find('id="status-detail"'):]
    panel = panel[:panel.find("<!-- LEFT")]
    for kept in ("health", "macro", "rb-turn", "rb-conv"):
        assert f'id="{kept}"' in panel, f"{kept} is not in the Status expand"
    assert 'href="methodology.html"' in panel, "the methodology link is unreachable"


def test_the_status_toggle_is_wired_and_announced():
    assert 'id="status-toggle"' in HTML
    assert 'aria-expanded="false"' in HTML and 'aria-controls="status-detail"' in HTML
    assert 'statusBtn.setAttribute("aria-expanded"' in SCRIPT


def test_the_status_panels_render_whether_or_not_the_expand_is_open():
    """Otherwise the first open shows a stale ribbon, and a reader who never opens it
    silently loses the pipeline-health checks the nightly writes."""
    ledger = SCRIPT[SCRIPT.find("const ledger = loadLedger()"):]
    ledger = ledger[:ledger.find("\n  try{")]
    assert "renderHealth()" in ledger and "renderMacro()" in ledger


def test_the_secondary_pane_is_one_panel_with_four_tabs():
    """Liquidity and Momentum re-presented columns the matrix already shows, in two more
    full-height scroll-panes, with Sectors and Funding as two further panels below."""
    tabs = re.findall(r'data-tab="([a-z]+)"', HTML)
    assert tabs == ["liq", "mom", "sec", "fund"], tabs
    panes = re.findall(r'data-pane="([a-z]+)"', HTML)
    assert panes == tabs
    for gone in ("MODULE A — LIQUIDITY", "MODULE C — MOMENTUM",
                 "SECTOR CAPITAL FLOW", "MODULE E — DERIVATIVES"):
        assert gone not in HTML, f"{gone} still has its own panel"


def test_the_tabbed_pane_kept_every_table_and_its_renderer():
    """Folding four panels into one must not have touched a single render path."""
    for tid in ("tbl-liq", "tbl-mom", "tbl-fund"):
        assert f'id="{tid}"' in HTML, f"{tid} was lost in the fold"
    assert 'id="sectors"' in HTML
    # every per-panel subtitle the renderers write to still exists
    for note in ("lia-note", "mom-note", "sec-note", "fh-note"):
        assert f'id="{note}"' in HTML, f"{note} was dropped with its old card header"


def test_only_the_default_tab_is_visible():
    assert '<button class="tab on" data-tab="liq"' in HTML
    for hidden in ("mom", "sec", "fund"):
        assert f'data-pane="{hidden}" hidden' in HTML


def test_switching_a_tab_moves_its_pane_and_its_subtitle_together():
    assert 'pane.hidden = pane.dataset.pane!==want' in SCRIPT
    assert 'n.hidden = n.dataset.note!==want' in SCRIPT


def test_the_long_prose_is_relocated_not_deleted():
    """The brief was explicit: move it, never remove it."""
    markup = HTML[:HTML.find("<script>")]
    assert markup.count('<details class="why">') >= 5
    # Compared on the TEXT, not the raw HTML: several of these sentences carry a <b>
    # mid-phrase, and a substring search across the markup would report a false loss.
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", markup))
    for phrase in ("did capital move here",
                   "compared within their overlap",
                   "top-25 market-cap rank",
                   "structural quality",
                   "effective breadth"):
        assert phrase.lower() in text.lower(), \
            f"relocated prose lost the phrase {phrase!r} — it was moved, not deleted"


def test_the_relocated_prose_is_closed_by_default():
    markup = HTML[:HTML.find("<script>")]
    assert "<details class=\"why\" open" not in markup
    assert "details.why > summary" in HTML, "the summary has no styling to click"


def test_the_banners_were_not_relocated():
    """Stub, rewound and degraded disclosures must stay impossible to miss — they are
    the one category of prose the brief said to leave exactly where it is."""
    assert "PREVIEW — PRICES ARE NOT LIVE" in SCRIPT
    assert "body.stubbed" in HTML and "body.rewound" in HTML
    banner = HTML[HTML.find(".stub-banner{"):]
    assert "details" not in banner[:banner.find("}")]


def test_the_rewind_caveat_moved_into_the_expand_rather_than_vanishing():
    assert 'id="rw-detail"' in HTML
    assert "are\n       <b>not</b> rewound" in SCRIPT or "not</b> rewound" in SCRIPT


def test_the_feed_chip_speaks_the_same_vocabulary_as_the_provenance_card():
    """'OK' beside a card reading 'STUBBED' is the quiet disagreement the provenance
    split was built to remove."""
    assert "const lf=liveFeedState();" in SCRIPT
    assert 'st.textContent=lf.label' in SCRIPT
    assert 'st.textContent="OK"' not in SCRIPT


def test_the_sizing_panel_shows_numbers_and_folds_its_prose():
    """The left rail's own density rule: the figures stay, the paragraph folds.

    Allocated, unallocated and the entry-cost estimate are the panel's output and must
    read without a click. Everything qualifying them — including the caveat that these
    are bounds rather than bet sizes — sits behind one closed summary.

    Asserted against the script half because this block is built in a template literal
    and so never appears in the static markup the other relocation test reads, which is
    exactly why it survived the first pass still sitting on the first screen.
    """
    for label in ("Allocated", "Unallocated", "Est. entry cost"):
        assert f"<span>{label}</span>" in SCRIPT, \
            f"the sizer stopped showing {label!r} — the numbers are not the prose"
    fold = SCRIPT.find("what these figures are and are not")
    assert fold != -1, "the sizing prose is no longer behind a summary"
    opening = SCRIPT.rfind("<details", 0, fold)
    assert opening != -1 and " open" not in SCRIPT[opening:fold], \
        "the sizing prose renders expanded"
    # Moved, not deleted — and all of it inside the one <details>, not half in and half
    # out, which is the state this panel was in before the refinement.
    body = SCRIPT[opening:SCRIPT.index("</details>", opening)]
    for phrase in ("Not an optimal allocation",
                   "caps refuse stays unallocated",
                   "whatever happened to be liquid"):
        assert phrase in body, f"sizing prose lost {phrase!r} — it was to move, not go"


def test_the_module_b_provenance_note_folds_too():
    """Standing provenance, not a degraded-state banner.

    The distinction matters: the stub and rewound banners must stay unmissable, but
    Module B is working — on a documented proxy — and the SYSTEM rows already say so in
    a single line. The paragraph explaining the substitution is prose, so it folds.
    """
    markup = HTML[:HTML.find("<script>")]
    fold = markup.find("what Module B is measuring")
    assert fold != -1, "the Module B note is back out in the open"
    opening = markup.rfind("<details", 0, fold)
    assert opening != -1 and " open" not in markup[opening:fold]
    body = markup[opening:markup.index("</details>", opening)]
    assert "FDV/MCap dilution proxy" in body and "DUNE_UNLOCK_QUERY_ID" in body, \
        "the Module B note was deleted rather than folded"
    assert 'id="sys-dune"' in markup, \
        "the one-line Module B status row went with it — the row was the point"


# ---------------------------------------------------------------------------
# The Status expand overlays; it does not push
# ---------------------------------------------------------------------------
def test_the_status_expand_is_positioned_rather_than_stacked():
    """Rendered in flow, opening Status re-injected the health and macro ribbons as real
    grid rows and drove the matrix from 57px down to 207px — handing back the whole
    density win in exchange for one click. It is now anchored to the strip and overlays
    the board, so opening it costs no layout at all.
    """
    css = HTML[:HTML.find("<script>")]
    assert ".statuswrap{" in css and "position:relative" in css
    panel = css[css.index(".statuspanel{"):css.index(".statuspanel[hidden]")]
    assert "position:absolute" in panel, "the expand is back in the document flow"
    assert "grid-column" not in panel, "the expand is claiming a grid row again"
    assert "max-height" in panel and "overflow:auto" in panel, \
        "a positioned panel taller than the viewport has no way to be read"


def test_the_status_expand_can_be_dismissed_without_the_button():
    """An overlay hides content while it is open, so dismissal is part of the control."""
    assert "function setStatusOpen(" in SCRIPT
    assert 'if(statusIsOpen()){ setStatusOpen(false);' in SCRIPT, \
        "Escape no longer closes the Status expand"
    # Located by content, not by being the first pointerdown listener on the page. It was
    # the only one until the tooltip primitive added its own for touch, at which point
    # "the first one" silently became a different handler and this test failed on a
    # change that had nothing to do with the Status expand.
    marker = 'document.addEventListener("pointerdown"'
    starts = [i for i in range(len(SCRIPT)) if SCRIPT.startswith(marker, i)]
    cands = []
    for i in starts:
        body = SCRIPT[i:]
        cands.append(body[:body.index("});") + 3])
    outside = next((c for c in cands if "status-detail" in c), "")
    assert outside, "no pointerdown listener references #status-detail at all"
    assert 'closest("#status-detail")' in outside and 'closest("#status-toggle")' in outside, \
        "the outside-click handler will close the panel on the press that opened it"


def test_the_collapsed_expand_is_display_none_not_merely_hidden():
    """`display:flex` outranks the user-agent `[hidden]` rule. Without the explicit rule
    the collapsed panel was 136px of invisible chrome in flow, and is an invisible
    click-blocker over the board now that it is positioned."""
    assert ".statuspanel[hidden]{display:none}" in HTML


# ---------------------------------------------------------------------------
# RWA flows: materiality, not percentage
# ---------------------------------------------------------------------------
def test_the_flows_tab_ranks_by_dollars_and_not_by_percentage():
    """The default ordering is the claim the panel makes. Sorted on the daily percentage
    it opened on TMO at +1,724.8% against a $244K tokenized cap, followed by four more
    sub-$100K names: arithmetically exact and useless, and the first thing a reader saw."""
    assert 'RWA_FLOW_ORDERS = {' in CODE
    assert re.search(r'let RWA_FLOW_ORDER\s*=', CODE), "the ordering is no longer a variable"
    default = re.search(r'return RWA_FLOW_ORDERS\[v\] \? v : "(\w+)"', CODE)
    assert default and default.group(1) == "usd", "the default ordering is not residual $"
    assert 'usd:' in CODE and 'residual_usd' in CODE


def test_the_flow_ordering_never_coerces_a_missing_residual_to_zero():
    """Nulls last, never as zero. `||0` parked an unmeasured row in the middle of the
    ranking among the genuine near-zeros, which renders an absence as a reading."""
    body = re.search(r"function renderRwaFlows\(\)\{(.*?)\n\}", CODE, re.S).group(1)
    assert "if(x==null) return 1;" in body and "if(y==null) return -1;" in body
    assert "residual_pct_daily)||0)" not in body, "the old zero-coercing comparator is back"


def test_the_flows_table_declares_its_span_and_keeps_the_error_row_spanning():
    """span_days is what says whether Per Day is the observed one-night residual or a
    geometric rate spread across a gap. A stale colspan leaves a ragged failure cell."""
    header = re.search(r'<table id="tbl-rwa-flow"><thead><tr>(.*?)</tr>', HTML, re.S).group(1)
    assert ">Span</th>" in header
    cols = header.count("<th")
    span = int(re.search(r'<td colspan="(\d+)" class="muted" style="padding:14px">Flow ledger', HTML).group(1))
    assert cols == span, f"{cols} columns but the flow failure row spans {span}"


# ---------------------------------------------------------------------------
# RWA off-hours: an agreement statistic needs more than one voter
# ---------------------------------------------------------------------------
def test_no_row_asserts_agreement_over_a_single_voter():
    """"1/1 wrappers agree" is one wrapper agreeing with itself, printed in the grammar
    of a consensus. 184 of the 303 live windows read that way and LULU printed -19.28%
    on exactly that basis."""
    body = re.search(r"function ohCorrobText\((.*?)\n\}", CODE, re.S).group(1)
    assert 'if(v>=2) return' in body, "the agreement string is no longer gated on the voter count"
    assert "single wrapper — no corroboration" in body
    # a null vote count is not a count of zero voters
    assert 'if(v==null)' in body and body.index('if(v==null)') < body.index('if(v>=2)')


def test_the_offhours_panel_leads_with_corroborated_windows():
    """Ranked on the size of the move alone, a single-wrapper -19.28% sat above every
    corroborated move on the board. Size is the wrong first sort when the evidence
    behind the moves differs this much."""
    body = re.search(r"function renderRwaOffhours\(r\)\{(.*?)\n\}", CODE, re.S).group(1)
    assert "const corr=live.filter(ohCorroborated)" in body
    assert body.index('grp("Corroborated"') < body.index('grp("Single wrapper"')
    # corroboration is >= 2 VOTERS, never >= 2 live wrappers: AMC has two live wrappers
    # and one voter, and counting the silent one printed a second opinion that nobody gave.
    rule = re.search(r"const ohCorroborated=(.*?\};)", CODE, re.S).group(1)
    assert "wrappers_voting" in CODE and "v!=null && v>=2" in rule


def test_the_single_wrapper_group_is_demoted_and_never_hidden():
    """Nothing is filtered out silently. Both groups state how many of their population
    they are showing, so neither cap is a silent slice."""
    body = re.search(r"function renderRwaOffhours\(r\)\{(.*?)\n\}", CODE, re.S).group(1)
    assert "solo.length" in body, "the single-wrapper group is not counted on screen"
    assert "widest '+shown+' of '+total" in body


def test_an_inferred_close_is_marked_as_derived_with_its_provenance_verbatim():
    """Every live window tonight derives its close from a sparkline whose hourly stamps
    were inferred backwards from last_updated, and the return was printed in the same
    typeface as an observed one. 51 of those strings say the cadence may not be hourly
    at all, which is why the detail is carried whole rather than summarised."""
    body = re.search(r"function ohLine\(x\)\{(.*?)\n\}", CODE, re.S).group(1)
    assert 'w.inferred_hours ?' in body
    assert 'ev ev-derived' in body and '>inferred close</span>' in body
    assert 'rwEsc(w.sparkline||"")' in body, "the provenance string is not carried verbatim"
    assert "title=" not in body, "a native title renders on no phone; data-tip is the contract"
