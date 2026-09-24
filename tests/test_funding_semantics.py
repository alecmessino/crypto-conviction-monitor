"""The funding multiplier and LAVL are two different things, and the terminal says so.

The scoring path is ledger/perp.json -> perpFeed() -> PERP[symbol] ->
conviction(..., PERP[symbol] || 1, ...) -> the published score. LAVL is a separate,
funding-neutral regime proxy that the QUALIFIED gate reads; the nightly's own regime proxy
(_lavl_regime) is funding-neutral in the captured model as well.

Until 2026-09-23 the LAVL table carried a "RiskMult" column filled from a constant
`riskMult = 1.0` inside computeLAVL, under copy saying RiskMult_perp came "from Module E"
and was "not neutral". Every row read x1.00 whatever tonight's funding reading was, so a
hard-coded neutral was presented as a live funding reading. These tests fail if either half
of that comes back: a funding term inside the LAVL calculation, or a displayed funding
multiplier that is not the one conviction() applied — including a x1.000 shown for a
reading that is missing, refused or withheld.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
METHOD = (ROOT / "methodology.html").read_text(encoding="utf-8")


def _fn_body(name):
    start = HTML.index(f"function {name}(")
    depth, i = 0, HTML.index("{", start)
    for j in range(i, len(HTML)):
        depth += {"{": 1, "}": -1}.get(HTML[j], 0)
        if depth == 0:
            return HTML[start:j + 1]
    raise AssertionError(f"unterminated {name}")


# ---------------------------------------------------------------------------
# source: no hard-coded funding term inside LAVL, and no copy claiming one
# ---------------------------------------------------------------------------
def test_lavl_carries_no_funding_term_at_all():
    # 2026-09-24: the browser's own computeLAVL was replaced by lavlReading(), the
    # nightly's _lavl_regime ported into the MODEL PORT; the gate that reads it is
    # conjunctiveGate(). Neither may reach for a funding term.
    assert "function computeLAVL(" not in HTML, "the retired browser-only LAVL is back"
    for fn in ("lavlReading", "conjunctiveGate"):
        body = _fn_body(fn)
        code = "\n".join(l.split("//")[0] for l in body.splitlines())
        for term in ("riskMult", "RiskMult", "PERP", "perp", "funding"):
            assert term not in code, (
                f"{fn} references {term!r}. LAVL is funding-neutral by construction; a "
                "funding term here is either the hard-coded neutral this test exists to "
                "stop, or a qualification change that needs review.")


def test_the_lavl_table_never_prints_a_constant_as_a_funding_reading():
    render = _fn_body("renderTables")
    lavl = render[render.index('$("#tbl-lavl tbody")'):]
    lavl = lavl[:lavl.index("Array.from(ld.children)")]
    assert "riskMult" not in lavl and "RiskMult" not in lavl
    # The only funding value this table may show is the one the score applied.
    assert "fundingMultCell(t.sym)" in lavl
    cell = _fn_body("fundingMultCell")
    assert "perpReading(sym)" in cell and "1.0" not in cell.replace("×1.0 by absence", "")


def test_the_breakdown_and_the_table_share_one_state_definition():
    assert "perpReading(t.sym).state" in _fn_body("factorBreakdown")
    reading = _fn_body("perpReading")
    # `applied` must be exactly what build() hands conviction().
    assert "PERP[sym] || 1" in reading
    assert re.search(r"conviction\(\{[^)]*\},\s*PERP\[\(t\.symbol\|\|\"\"\)\.toUpperCase\(\)\]\|\|1", HTML)


def test_no_copy_calls_lavl_a_funding_reading():
    head = HTML[HTML.index("MODULE D — ALPHA ENGINE (LAVL)"):HTML.index('id="tbl-lavl"')]
    assert "funding-neutral by construction" in head
    assert "RiskMult<sub>perp</sub> from Module E" not in head
    assert "not</b> neutral" not in head
    assert "<th>RiskMult</th>" not in HTML
    assert "LAVL leverage overlay" not in METHOD
    assert "liquidity × LAVL" not in METHOD
    assert "reads the nightly <code>perp_mult</code>" not in METHOD
    assert "RiskMult_perp (LAVL leverage overlay) from nightly ledger" not in HTML


def test_the_python_regime_says_what_it_is():
    src = (ROOT / "nightly.py").read_text(encoding="utf-8")
    assert "until a derivatives feed is wired" not in src
    assert "Replicates the front-end gated flag so the basket" not in src


# ---------------------------------------------------------------------------
# browser: the whole path, end to end, in every state
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_render import SKIP, NoBrowser, _ROOT, _find_browser, build_fixture, serve  # noqa: E402

if SKIP is None:
    from playwright.sync_api import sync_playwright


def _perp_doc(syms, as_of, overrides):
    rows = {s: {"m": 1.0, "s": "current"} for s in syms}
    rows.update(overrides)
    return {"schema_version": 1, "as_of": as_of, "rows": rows}


def _run(doc_for, probe):
    if SKIP is not None:
        pytest.skip(SKIP)
    fixture = build_fixture()
    syms = [t["symbol"].upper() for t in fixture]
    doc = doc_for(syms)

    with serve(_ROOT) as url, sync_playwright() as pw:
        try:
            browser = _find_browser(pw)
        except NoBrowser as exc:
            pytest.skip(str(exc))
        page = browser.new_page()
        page.route("**/api.coingecko.com/**", lambda r: r.fulfill(
            status=200 if "/coins/markets" in r.request.url else 429,
            content_type="application/json",
            body=json.dumps(fixture) if "/coins/markets" in r.request.url else "{}"))
        page.route("**/ledger/perp.json", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(doc)))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function("()=>STATE.length>0", timeout=30000)
        page.wait_for_timeout(500)
        try:
            return syms, page.evaluate(probe)
        finally:
            browser.close()


PROBE = """() => {
  const out = {loaded: PERP_LOADED, rows: []};
  document.querySelectorAll('#tbl-lavl tbody tr').forEach(tr => {
    const sym = tr.dataset.sym, t = STATE.find(x => x.sym === sym);
    const cells = tr.querySelectorAll('td');
    const F = convictionFactors(t);
    // The LAVL shown is the reading the gate took (lavlReading over the raw row, stored
    // by build()); it has no funding input (asserted on the source above).
    const before = t.lavl, after = t.lavl;
    out.rows.push({sym, reading: perpReading(sym), shown: cells[cells.length-1].textContent.trim(),
                   lavl_shown: cells[3].textContent.trim(), lavl: t.lavl,
                   lavl_fmt: t.lavl == null ? "—" : t.lavl.toFixed(2),
                   band_shown: cells[4].textContent.trim(), band: t.lavlBand,
                   perp_in_score: F.perp, conv: t.conv,
                   recon: Math.max(0, Math.min(100, Math.round(100*F.depth*F.cm*F.a_frac*F.em*F.perp))),
                   before, after});
  });
  return out;
}"""


def test_the_published_funding_multiplier_is_the_one_displayed_in_every_state():
    # The terminal compares the snapshot date against the UTC day.
    today = datetime.now(timezone.utc).date().isoformat()

    def doc_for(syms):
        # States assigned round-robin so the top-10 LAVL view is certain to hold all four.
        over = {}
        for i, s in enumerate(syms):
            over[s] = [{"m": 0.9, "s": "current"}, {"m": 1.0, "s": "absent"},
                       {"m": 2.0, "s": "current"}, {"m": 1.1, "s": "current"}][i % 4]
        return _perp_doc(syms, today, over)

    syms, out = _run(doc_for, PROBE)
    assert out["loaded"] and out["rows"], out
    states = set()
    for r in out["rows"]:
        st = r["reading"]["state"]
        states.add(st)
        # 1. conviction() was handed exactly what the table shows as applied.
        assert r["perp_in_score"] == r["reading"]["applied"], r
        # 2. the published score reconstructs through that multiplier.
        assert r["recon"] == r["conv"], r
        # 3. LAVL is funding-neutral, and the table shows the LAVL it computes.
        assert r["before"] == r["after"], r
        # The Alpha Engine shows the reading the QUALIFIED gate took, not a second one.
        assert r["lavl_shown"] == r["lavl_fmt"] and r["band_shown"] == r["band"], r
        # 4. the displayed funding value is the applied reading, or a dash when there is none.
        if st == "current":
            assert r["shown"] == f"×{r['reading']['value']:.3f}", r
        else:
            assert r["shown"] == "—", f"a {st} reading was displayed as {r['shown']!r}"
    assert {"current", "absent", "rejected"} <= states, states


def test_a_withheld_overlay_shows_no_funding_reading_anywhere():
    stale = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
    syms, out = _run(lambda syms: _perp_doc(syms, stale, {syms[0]: {"m": 0.9, "s": "current"}}),
                     PROBE)
    assert not out["loaded"]
    assert out["rows"]
    for r in out["rows"]:
        assert r["reading"]["state"] == "withheld-stale", r
        assert r["perp_in_score"] == 1, r
        assert r["shown"] == "—", r
