"""The terminal under hostile or broken inputs, in a real browser.

Three failures the source-reading gates could not see, each reproduced before it was
fixed (docs/AUDIT-2026-09-23.md):

  * A CoinGecko symbol reached innerHTML unescaped at six sites, so a listed coin whose
    ticker carried markup executed it on every visitor's page.
  * The ledger renderers ran as one chain inside the promise the live fetch awaited. One
    panel's throw skipped every panel after it, never built the board, and reported the
    fault as "Live fetch failed … CoinGecko rate-limits" — blaming the one thing that
    had not failed.
  * A throttled first load showed a single error row while the last recorded board sat
    in memory, and a failed refresh overwrote a board the visitor had rewound to.

Skips, rather than passes, when playwright or a browser is missing.
"""
import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_render import SKIP, NoBrowser, _ROOT, _find_browser, build_fixture, serve  # noqa: E402

if SKIP is None:
    from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.skipif(SKIP is not None, reason=SKIP or "")

# The board upper-cases symbols, so a payload's script text becomes WINDOW.__XSS and
# throws rather than counting — an execution test alone passes against the vulnerable
# page. The markup is what is asserted: an element the symbol should never have created,
# matched case-insensitively.
PAYLOAD = '"><b class=pwn>S</b><img src=x onerror="window.__xss=1">'


def _browser(pw):
    try:
        return _find_browser(pw)
    except NoBrowser as exc:
        pytest.skip(str(exc))


def _open(pw, url, markets, extra_routes=None, clock=False):
    """A page with CoinGecko stubbed. `markets` is a callable returning (status, body)."""
    browser = _browser(pw)
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if clock:
        page.clock.install()

    def cg(route):
        if "/coins/markets" in route.request.url:
            status, body = markets()
            return route.fulfill(status=status, content_type="application/json",
                                 body=json.dumps(body))
        # The enrichment feeds are not under test; answer them empty.
        return route.fulfill(status=429, content_type="application/json", body="{}")
    page.route("**/api.coingecko.com/**", cg)
    for pattern, handler in (extra_routes or {}).items():
        page.route(pattern, handler)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    return browser, page, errors


def test_a_hostile_symbol_is_rendered_as_text():
    fixture = build_fixture()
    evil = copy.deepcopy(fixture[0])
    evil.update({"id": "evil", "symbol": "zz" + PAYLOAD, "name": "Evil"})
    fixture.insert(1, evil)
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, errors = _open(pw, url, lambda: (200, fixture))
        try:
            page.wait_for_function("()=>STATE.length>0", timeout=30000)
            page.wait_for_timeout(1500)
            fired = page.evaluate("()=>window.__xss||0")
            injected = page.evaluate(
                "()=>document.querySelectorAll('[class=\"pwn\" i], img[src=\"x\" i]').length")
            onboard = page.evaluate("()=>STATE.some(t=>t.sym.startsWith('ZZ'))")
        finally:
            browser.close()
    assert onboard, "the hostile row was not scored, so nothing was tested"
    assert fired == 0, f"a symbol's markup executed {fired} time(s)"
    assert injected == 0, f"{injected} element(s) were injected by a symbol's markup"
    assert not [e for e in errors if "WINDOW" in e], errors


def test_a_throwing_ledger_panel_does_not_take_the_board_with_it():
    fixture = build_fixture()
    with open(os.path.join(_ROOT, "ledger", "market_breadth.json"), encoding="utf-8") as fh:
        breadth = json.load(fh)
    # The shape drift the audit used: a number where the tier-diff renderer iterates.
    breadth["tier_diff"] = {"marginal": 5}

    def drifted(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(breadth))
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, errors = _open(pw, url, lambda: (200, fixture),
                                      {"**/ledger/market_breadth.json": drifted})
        try:
            page.wait_for_function("()=>STATE.length>0", timeout=30000)
            page.wait_for_timeout(1000)
            rows = page.evaluate("()=>document.querySelectorAll('#tbl-conv tbody tr.row').length")
            text = page.evaluate("()=>document.querySelector('#tbl-conv tbody').innerText")
            status = page.evaluate("()=>document.querySelector('#status').textContent")
            # Panels after the throwing one in the chain still rendered.
            rewind = page.evaluate("()=>REWIND_DATES.length")
        finally:
            browser.close()
    assert rows > 0, "the board did not render"
    assert "Live fetch failed" not in text
    assert "FAIL" not in status.upper(), status
    assert rewind > 1


def test_a_throttled_first_load_shows_the_recorded_board_and_recovers():
    fixture = build_fixture()
    state = {"code": 429}

    def markets():
        return (200, fixture) if state["code"] == 200 else (429, {})
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, errors = _open(pw, url, markets, clock=True)
        try:
            page.wait_for_function("()=>REWIND_DATE!=null", timeout=30000)
            last = page.evaluate("()=>REWIND_DATES[REWIND_DATES.length-1]")
            shown = page.evaluate("()=>REWIND_DATE")
            rows = page.evaluate("()=>document.querySelectorAll('#tbl-conv tbody tr.row').length")
            rewound = page.evaluate("()=>document.body.classList.contains('rewound')")
            # A second failed refresh must leave the recorded board alone.
            page.clock.run_for(121000)
            page.wait_for_timeout(1500)
            still = page.evaluate("()=>document.querySelectorAll('#tbl-conv tbody tr.row').length")
            # Live again: the fallback steps aside by itself.
            state["code"] = 200
            page.clock.run_for(121000)
            page.wait_for_function("()=>STATE.length>0 && REWIND_DATE==null", timeout=30000)
            live_rows = page.evaluate(
                "()=>document.querySelectorAll('#tbl-conv tbody tr.row').length")
        finally:
            browser.close()
    assert shown == last, f"showed {shown}, the last recorded night is {last}"
    assert rows > 0 and rewound, "the recorded board was not put up, or not labelled as one"
    assert still == rows, "a failed refresh overwrote the recorded board"
    assert live_rows > 0
