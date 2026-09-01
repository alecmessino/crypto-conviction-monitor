"""The workspace layer: four screens over one grid, and the crypto board untouched.

The properties this file exists to hold:

  * The crypto workspace is what opens. Adding a screen selector must not put anything
    between a reader and the ranked board, and must not change what that board is.
  * `.ws` is `display:contents`. Any other wrapper collapses the 240px/1fr/400px tracks
    to a single grid item and de-activates every `grid-column` rule in the file — a break
    that passes every source-reading gate because those read the CSS text rather than the
    render, and shows up only as a layout nobody looks at twice.
  * The attribute namespaces do not overlap. The secondary-pane handler selects
    `[data-pane]` and `[data-note]` document-globally and unscoped, so a workspace
    reusing either name means clicking Liquidity silently hides part of another screen.
  * No entry in the nav is dead, and the one that leaves the page is a real link.
  * The RWA workspace ships no <canvas>. A canvas measured while hidden draws at width
    zero and publishes an empty accessible table.
"""
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
MARKUP = HTML[:HTML.find("<script>")]

WORKSPACES = ("crypto", "rwa")          # own a subtree
ROUTES = ("index", "portfolio")         # scroll to a panel that stays in crypto


def _strip_js_comments(src: str) -> str:
    """Block and line comments removed, so a content assertion reads code.

    Crude on purpose: it does not understand strings or regex literals, which is fine
    for the identifier scans here and would not be for anything that parsed."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", " ", src)


def test_every_nav_entry_reaches_something():
    """A nav with a dead entry is worse than a shorter nav — but reaching something does
    not require owning a subtree. CRYPTO and RWA are workspaces; INDEX and PORTFOLIO are
    routes to panels that stay exactly where the canonical layout puts them."""
    buttons = set(re.findall(r'data-ws-btn="([\w-]+)"', MARKUP))
    roots = set(re.findall(r'<div class="ws" data-ws="([\w-]+)"', MARKUP))
    assert buttons == set(WORKSPACES) | set(ROUTES), f"nav buttons are {sorted(buttons)}"
    assert roots == set(WORKSPACES), f"the document defines {sorted(roots)}"
    script = _strip_js_comments(HTML)
    routes = set(re.findall(r"^\s*(\w+):\s*\{ws:", script, re.M))
    assert routes == set(ROUTES), f"WS_ROUTES defines {sorted(routes)}"
    # Every route must name an anchor that exists in the markup.
    for anchor in re.findall(r'anchor:\s*"([\w-]+)"', script):
        assert f'id="{anchor}"' in MARKUP, f"route anchor #{anchor} is not in the document"


def test_method_is_a_real_destination_and_not_a_workspace():
    """methodology.html is a document, not a panel. It must not enter the hide/show
    sweep, and it must not be a button that goes nowhere."""
    nav = MARKUP[MARKUP.index('<nav class="ws-nav"'):MARKUP.index("</nav>")]
    assert 'href="methodology.html"' in nav
    assert (ROOT / "methodology.html").exists()
    method = re.search(r'<a class="ws-tab"[^>]*>', nav).group(0)
    assert "data-ws-btn" not in method


def test_crypto_is_the_workspace_that_opens():
    """Every other root carries `hidden`; the crypto one must not."""
    for ws in WORKSPACES:
        tag = re.search(r'<div class="ws" data-ws="%s"([^>]*)>' % ws, MARKUP).group(1)
        if ws == "crypto":
            assert "hidden" not in tag, "the board is not what opens"
        else:
            assert "hidden" in tag, f"the {ws} workspace is visible on load"


def test_the_workspace_wrapper_is_display_contents():
    """The three column roots must stay DIRECT grid items of .wrap. Any other wrapping
    form silently collapses the grid, and every static gate still passes."""
    assert re.search(r"\.ws\{display:contents\}", HTML), \
        ".ws is not display:contents — the three-column grid is collapsed"
    assert re.search(r"\.ws\[hidden\]\{display:none!important\}", HTML), \
        "[hidden] cannot out-specify .ws without the attribute selector"


def test_the_attribute_namespaces_do_not_overlap():
    """The secondary-pane handler is document-global and unscoped. A workspace reusing
    data-pane or data-note would mean a click on Liquidity hides another screen."""
    for root in re.finditer(r'<div class="ws" data-ws="([\w-]+)"', MARKUP):
        pass
    assert 'data-ws="crypto"' in MARKUP
    # The RWA plate has its own names.
    assert "data-rwapane" in MARKUP and "data-rwatab" in MARKUP
    rwa_start = MARKUP.index('<div class="ws" data-ws="rwa"')
    rwa_end = MARKUP.index("<!-- /ws rwa -->")
    rwa_block = MARKUP[rwa_start:rwa_end]
    assert "data-pane=" not in rwa_block and "data-note=" not in rwa_block, (
        "the RWA workspace reuses the crypto pane attributes, so switching a crypto tab "
        "will hide part of it")


def test_the_rwa_workspace_ships_no_canvas():
    """A canvas sized while its workspace is hidden measures clientWidth zero,
    attachChartInteraction registers no points, and syncChartTable then publishes a
    chart table with no rows — which fails the render gate naming no cause."""
    rwa_block = MARKUP[MARKUP.index('<div class="ws" data-ws="rwa"'):
                       MARKUP.index("<!-- /ws rwa -->")]
    assert "<canvas" not in rwa_block


def test_the_crypto_board_and_its_inspector_did_not_move():
    """The locked layout: a dominant ranked board, a persistent inspector, one tabbed
    plate. All three must still be inside the crypto workspace."""
    block = MARKUP[MARKUP.index('<div class="ws" data-ws="crypto"'):
                   MARKUP.index("<!-- /ws crypto -->")]
    for needle in ('id="tbl-conv"', 'id="sec-tabs"', 'id="fb"', "CONVICTION MATRIX"):
        assert needle in block, f"{needle} left the crypto workspace"


def test_the_crypto_columns_are_byte_identical_to_canonical():
    """THE load-bearing test of this whole change. RWA is additive; crypto is not
    redesigned as collateral work. An earlier version physically re-parented four panels
    out of the sidebar and the rail to make the INDEX and PORTFOLIO nav entries own a
    subtree, which is exactly the kind of change this asserts against. The nav and the
    display:contents wrapper sit OUTSIDE this span."""
    import subprocess

    def canonical():
        return subprocess.run(["git", "show", "origin/main:index.html"],
                              capture_output=True, text=True, cwd=ROOT).stdout
    canon = canonical()
    if not canon:
        # A CI checkout is shallow and holds only the branch under test. Fetch the
        # canonical file rather than skip: this gate skipping on exactly the machine
        # that decides whether a pull request merges was a pass it had not earned, and
        # in standalone mode the skip escaped as a traceback. Measured on the first
        # dispatch of the release path.
        # An explicit refspec: a single-branch clone's remote only maps its own branch,
        # so a bare "fetch origin main" lands in FETCH_HEAD and origin/main stays
        # unset. Measured in a --depth 1 -b clone.
        subprocess.run(["git", "fetch", "--quiet", "--depth=1", "origin",
                        "+refs/heads/main:refs/remotes/origin/main"],
                       capture_output=True, text=True, cwd=ROOT)
        canon = canonical()
    assert canon, ("origin/main could not be read or fetched, so the byte-identity gate "
                   "cannot run — and a gate that cannot run must not pass")

    def columns(t):
        a = t.index("  <!-- LEFT -->")
        b = t.index("</aside>", t.index("  <!-- RIGHT -->")) + len("</aside>")
        return t[a:b]

    assert columns(HTML) == columns(canon), (
        "the crypto sidebar, board column or rail differs from origin/main")


def test_the_rwa_workspace_never_calls_the_crypto_inspector():
    """factorBreakdown() falls back to STATE[0] on a miss, so an RWA id handed to
    select() would silently paint a different crypto asset's factor ledger — a wrong
    number that looks entirely normal."""
    script = _strip_js_comments(HTML[HTML.index("/* ===================== RWA WORKSPACE"):])
    # A bare select(...) call, not querySelector / selectedSpec / a property access, and
    # over CODE rather than prose — the comment directly above this block says "RWA rows
    # do NOT route through select()", and a scan that reads comments finds the sentence
    # promising the thing it is looking for.
    calls = re.findall(r"(?<![A-Za-z0-9_$.])select\s*\(", script)
    assert not calls, "RWA rendering calls the crypto selection path"
    assert "RWA_SEL" in script and "renderRwaInspector" in script


def test_nothing_in_the_rwa_workspace_claims_an_executable_dislocation():
    """Executable means after spread, depth and cost-to-move, and all three live behind
    an endpoint this plan cannot call."""
    rwa_block = MARKUP[MARKUP.index('<div class="ws" data-ws="rwa"'):
                       MARKUP.index("<!-- /ws rwa -->")]
    low = rwa_block.lower()
    assert "not executable" in low or "nothing here is executable" in low
    assert "arbitrage" not in low, "the tape must not be described as an arbitrage"


def test_the_rwa_labels_never_appear_beside_the_crypto_tiers():
    """Two vocabularies that share a surface invite the reading that they share a
    scale."""
    rwa_block = MARKUP[MARKUP.index('<div class="ws" data-ws="rwa"'):
                       MARKUP.index("<!-- /ws rwa -->")]
    for tier in ("STRONG", "AVOID", "WATCH"):
        assert tier not in rwa_block, f"the crypto tier {tier} appears in the RWA workspace"


def test_the_nav_sits_above_the_status_strip():
    """Everything between a reader and the board is a cost, and the brief's stacking is
    a 36px nav over the 48px command strip."""
    assert MARKUP.index('<nav class="ws-nav"') < MARKUP.index('class="ribbon glass statusbar"')


def test_the_reveal_hooks_name_functions_that_exist():
    """A typeof guard around a misspelled name is a silent no-op: the branch never fires,
    nothing throws, and the charts it was written to redraw stay blank. The first version
    of this called renderQuad and renderAlpha, which are named quad and alphaMap."""
    script = _strip_js_comments(HTML)
    switch = script[script.index("function switchWorkspace"):]
    switch = switch[:switch.index("\n}")]
    called = set(re.findall(r"typeof\s+(\w+)\s*===\s*[\"']function", switch))
    assert called, "switchWorkspace redraws nothing on reveal"
    for name in called:
        assert re.search(r"function\s+" + name + r"\s*\(", script), (
            f"switchWorkspace guards on {name}(), which is not defined anywhere — the "
            f"branch can never fire")


def test_every_workspace_gets_a_reveal_hook():
    """A canvas measured while its workspace was hidden draws at width zero, and there is
    no resize observer in this file. Every workspace holding one must be redrawn on
    reveal — crypto included, which the first version forgot. Routes are covered by the
    workspace they reveal, which is what revealFor() resolves."""
    script = _strip_js_comments(HTML)
    switch = script[script.index("function switchWorkspace"):]
    switch = switch[:switch.index("\n}")]
    for ws in WORKSPACES:
        assert f'reveal==="{ws}"' in switch, f"{ws} has no reveal hook"
    assert "revealFor(want)" in switch, "routes never resolve to a workspace"


def test_a_route_redraws_the_panel_it_scrolls_to():
    """INDEX and PORTFOLIO reveal the crypto workspace, so the crypto branch is what has
    to redraw the index chart and the position tools — otherwise the nav entry scrolls to
    a panel that was last drawn at width zero."""
    script = _strip_js_comments(HTML)
    switch = script[script.index("function switchWorkspace"):]
    switch = switch[:switch.index("\n}")]
    crypto = switch[switch.index('reveal==="crypto"'):]
    for fn in ("renderIndex", "renderSizing", "renderParser", "quad", "alphaMap"):
        assert fn in crypto, f"the crypto reveal does not redraw {fn}"


if __name__ == "__main__":
    import traceback
    fns = [(n, o) for n, o in sorted(globals().items())
           if n.startswith("test_") and callable(o)]
    bad = []
    for name, fn in fns:
        try:
            fn()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001
            # BaseException, not Exception: pytest's Skipped is an OutcomeException
            # under BaseException, and a skip that escaped this loop took the whole
            # gate down with a traceback instead of a named failure. In standalone mode
            # a skip IS a failure — the gate exists so that nothing here can pass
            # without running.
            bad.append((name, traceback.format_exc()))
    for name, tb in bad:
        print(f"FAIL {name}\n{tb}")
    print(f"[workspaces] {len(fns) - len(bad)}/{len(fns)} check(s) passed")
    raise SystemExit(1 if bad else 0)
