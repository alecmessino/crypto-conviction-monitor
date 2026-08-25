"""The explanation layer has to exist on the device the board is actually opened on.

The reasoning was carried in `title` attributes: 1,107 of them in the rendered DOM
against 10 aria attributes. Native `title` does not render on iOS or Android, so on a
phone this board was an unexplained grid of numbers and the provenance, the windows and
the caveats - the part that makes it worth trusting - were not hidden behind a gesture,
they were absent.

These are structural checks on the primitive that replaced it. They are cheap and they
run everywhere. The behavioural half - that a tap actually opens a popover on a phone -
lives in tests/test_render.py, which drives a real browser and skips when there is none.
Both exist because neither is sufficient: this file cannot prove the popover opens, and
that file does not run in every environment.
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
TERMINAL = os.path.join(_ROOT, "index.html")

with open(TERMINAL, encoding="utf-8") as _fh:
    HTML = _fh.read()
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)


def check_the_popover_primitive_exists_and_is_announced():
    for needle, why in (
        ('setAttribute("role","tooltip")', "the popover is not exposed as a tooltip"),
        ('setAttribute("aria-describedby"', "the trigger does not point at its own description"),
        ("function tipShow(", "no show path"),
        ("function tipHide(", "no hide path"),
        ("function initTips(", "the primitive is never initialised"),
    ):
        assert needle in SCRIPT, why
    assert re.search(r"^initTips\(\);", SCRIPT, re.M), \
        "initTips() is defined but never called, so nothing is upgraded"


def check_it_serves_hover_focus_and_touch():
    """All three, or it is the same failure in a new shape.

    Hover alone is the bug being fixed. Hover plus focus still leaves the phone with
    nothing. Hover plus tap leaves the keyboard reader where the phone reader was.
    """
    for evt in ("mouseover", "focusin", "pointerdown"):
        assert f'addEventListener("{evt}"' in SCRIPT, f"the primitive does not handle {evt}"
    assert 'e.pointerType==="mouse"' in SCRIPT, \
        "the touch path does not distinguish a pointer type, so mouse users get a second tooltip on click"


def check_it_can_be_dismissed():
    tip = SCRIPT[SCRIPT.index("function initTips("):]
    tip = tip[:tip.index("\n}")]
    assert 'e.key==="Escape"' in tip, "Escape does not dismiss the popover"
    assert "tipHide()" in tip and "click" in tip, "an outside tap cannot dismiss the popover"


def check_title_is_moved_rather_than_copied():
    """Copying leaves the native tooltip in place, so desktop shows two."""
    up = SCRIPT[SCRIPT.index("function tipUpgrade("):]
    up = up[:up.index("\nfunction ")]
    assert 'setAttribute("data-tip"' in up and 'removeAttribute("title")' in up, \
        "title is not moved to data-tip"


def check_authored_data_tips_are_upgraded_too():
    """The orphan class the first pass created and could not see.

    Panels written since the popover landed author `data-tip` in their markup rather than
    `title`, which is the better way round. The upgrade only swept `[title]`, so those
    were never made focusable: nineteen explanations had no keyboard and no touch route,
    including every row of the sizing panel. Counting upgraded nodes cannot detect this;
    only asking whether each node has a route can.
    """
    up = SCRIPT[SCRIPT.index("function tipUpgrade("):]
    up = up[:up.index("\nfunction ")]
    assert 'scope.querySelectorAll("[data-tip]")' in up, \
        "the upgrade only sweeps [title], so elements authored with data-tip stay orphaned"
    assert "makeReachable" in up, "no second pass makes explained elements reachable"


def check_matrix_rows_are_keyboard_reachable():
    """A row's cells are not individual tab stops, so the row itself has to open the
    drawer that carries them, from the keyboard as well as from a tap."""
    assert "function wireMatrixRows(" in SCRIPT, "matrix rows take no keyboard focus"
    fn = SCRIPT[SCRIPT.index("function wireMatrixRows("):]
    fn = fn[:fn.index("\n}\n")] if "\n}\n" in fn else fn[:4000]
    assert 'e.key==="Enter"' in fn and "select(sym)" in fn, \
        "Enter on a focused row does not open its drawer"
    assert "ArrowDown" in fn and "ArrowUp" in fn, "rows cannot be walked by arrow key"


def check_the_drawer_reproduces_row_explanations_verbatim():
    """Excusing a tip from focus is only legitimate if its text is reachable elsewhere."""
    assert "function rowTipNotes(" in SCRIPT, \
        "row explanations are excused from focus but reproduced nowhere"
    fn = SCRIPT[SCRIPT.index("function rowTipNotes("):]
    fn = fn[:fn.index("\n}\n")] if "\n}\n" in fn else fn[:3000]
    assert 'getAttribute("data-tip")' in fn, \
        "the drawer re-authors the notes instead of reading the row's own text, which " \
        "is how the two drift apart"
    assert "rowTipNotes(t.sym)" in SCRIPT, "the drawer never renders the row's notes"


def check_late_rendered_content_is_upgraded_too():
    """Most of the tips are rendered after load: the matrix, the sizing rows, the
    funding cells are rebuilt on every load and every rewind. A one-time sweep would
    cover the first paint and nothing after it."""
    assert "MutationObserver" in SCRIPT, \
        "no observer: tips rendered after the first paint keep their dead `title`"


def check_dense_grids_do_not_become_two_hundred_tab_stops():
    """Reachable and usable are different claims.

    The first pass made every explained element focusable and produced 318 tab stops,
    196 of them cells of one correlation matrix.
    """
    assert "function tipRoving(" in SCRIPT, "no roving tabindex for dense grids"
    rov = SCRIPT[SCRIPT.index("function tipRoving("):]
    rov = rov[:rov.index("\n/* The matrix rows")] if "\n/* The matrix rows" in rov else rov[:5000]
    assert 'i===keep?"0":"-1"' in rov, \
        "the group does not collapse to a single tab stop"
    assert "function groupItems(" in SCRIPT, \
        "assignment and arrow navigation do not share one definition of the group's " \
        "members, which is how a group ends up with several tab stops while claiming one"
    assert "ArrowRight" in rov and "ArrowLeft" in rov, \
        "the group is one tab stop with no way to move inside it, which is worse than many"


def check_the_canvases_have_a_representation_that_is_not_pixels():
    """Each chart already registers the points it drew. That is a complete description
    sitting in memory; it is now published rather than left there."""
    assert "function syncChartTable(" in SCRIPT, "the charts publish no data table"
    attach = SCRIPT[SCRIPT.index("function attachChartInteraction("):]
    attach = attach[:attach.index("\n}")]
    assert "syncChartTable(id)" in attach, "the table is never generated"
    assert attach.index("syncChartTable(id)") < attach.index("dataset.wired"), (
        "the table is behind the once-only wiring guard, so it freezes at the first "
        "paint while the chart keeps redrawing")
    for cid in ("quad", "alpha", "fquad"):
        m = re.search(rf'<canvas id="{cid}"[^>]*>', HTML)
        assert m and "aria-label=" in m.group(0), f"canvas #{cid} has no accessible name"
    assert ".vh{" in HTML and "clip-path:inset(50%)" in HTML, \
        "no visually-hidden class, so the data tables either do not exist or are drawn twice"


_CHECKS = [
    check_the_popover_primitive_exists_and_is_announced,
    check_it_serves_hover_focus_and_touch,
    check_it_can_be_dismissed,
    check_title_is_moved_rather_than_copied,
    check_authored_data_tips_are_upgraded_too,
    check_matrix_rows_are_keyboard_reachable,
    check_the_drawer_reproduces_row_explanations_verbatim,
    check_late_rendered_content_is_upgraded_too,
    check_dense_grids_do_not_become_two_hundred_tab_stops,
    check_the_canvases_have_a_representation_that_is_not_pixels,
]


def _run_all():
    failures = []
    for fn in _CHECKS:
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
    return failures


if __name__ == "__main__":
    fails = _run_all()
    for f in fails:
        print("FAIL " + f)
    print(f"a11y: {len(_CHECKS) - len(fails)}/{len(_CHECKS)} passed")
    sys.exit(1 if fails else 0)
else:
    def test_popover_primitive_exists():
        check_the_popover_primitive_exists_and_is_announced()

    def test_hover_focus_and_touch():
        check_it_serves_hover_focus_and_touch()

    def test_dismissable():
        check_it_can_be_dismissed()

    def test_title_moved_not_copied():
        check_title_is_moved_rather_than_copied()

    def test_authored_data_tips_upgraded():
        check_authored_data_tips_are_upgraded_too()

    def test_matrix_rows_keyboard_reachable():
        check_matrix_rows_are_keyboard_reachable()

    def test_drawer_reproduces_row_notes():
        check_the_drawer_reproduces_row_explanations_verbatim()

    def test_late_content_upgraded():
        check_late_rendered_content_is_upgraded_too()

    def test_dense_grids_rove():
        check_dense_grids_do_not_become_two_hundred_tab_stops()

    def test_canvases_have_data_tables():
        check_the_canvases_have_a_representation_that_is_not_pixels()
