"""What the board actually renders, in a real browser, on a desktop and on a phone.

Every other gate in this directory reads the source. That is cheap, it runs everywhere,
and it cannot answer the two questions that matter most about the changes it guards:

  * does the factor decomposition reconstruct the published conviction for every row on
    the board, or only for the rows someone happened to check
  * does a tap on a phone actually open the explanation, which is the whole point of
    replacing `title`

So this one serves the directory over http, drives Chromium, stubs the price feed with a
fixture built from the recorded ledger, and asserts on the DOM that comes out.

It SKIPS, loudly, when playwright or a browser is unavailable, in the same spirit as the
parity gate skipping without node under pytest: an environment that cannot run a browser
should not fail a pull request, but it should also never report a pass it did not earn.
Run standalone (`python tests/test_render.py`) and a skip is reported as a skip, not
silently swallowed.
"""
import contextlib
import functools
import http.server
import json
import os
import socketserver
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

SKIP = None
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    SKIP = "playwright is not installed (pip install playwright && playwright install chromium)"


@contextlib.contextmanager
def serve(directory):
    """The terminal fetches ledger/*.json, which file:// forbids."""
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):    # the request log buries the only line that matters
            pass

    handler = functools.partial(Quiet, directory=directory)
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            yield f"http://127.0.0.1:{port}/index.html"
        finally:
            httpd.shutdown()


def build_fixture():
    """A markets payload shaped like CoinGecko's, valued from the recorded ledger.

    Real symbols, real market caps, real relative strengths. The 24h changes are
    synthetic and deliberately span both signs, because the delta column is one of the
    things under test and a fixture where everything is green would prove nothing about
    the half of the ramp that was failing.
    """
    with open(os.path.join(_ROOT, "ledger", "signals.json"), encoding="utf-8") as fh:
        rows = json.load(fh)["rows"]
    latest = max(r["date"] for r in rows)
    rows = [r for r in rows if r["date"] == latest]

    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    out = []
    for i, r in enumerate(rows):
        price = f(r["price"]) or 1.0
        mc = f(r["market_cap"]) or 0.0
        turn = f(r["turnover_pct"]) or 5.0
        out.append({
            "id": r["symbol"].lower(), "symbol": r["symbol"].lower(), "name": r["name"],
            "current_price": price, "market_cap": mc, "total_volume": mc * turn / 100.0,
            "fully_diluted_valuation": f(r["fdv_usd"]),
            "price_change_percentage_24h": round((i % 21) - 10 + 0.3, 2),
            "market_cap_change_percentage_24h": round((i % 9) - 4, 2),
            "high_24h": f(r["high_24h"]), "low_24h": f(r["low_24h"]),
            "ath": price * 2.4, "atl": price * 0.3,
            "price_change_percentage_1h_in_currency": 0.4,
            "price_change_percentage_7d_in_currency": f(r["rs7"]) or 3.0,
            "price_change_percentage_14d_in_currency": f(r["rs14"]) or 4.0,
            "price_change_percentage_30d_in_currency": f(r["rs30"]) or 5.0,
            "price_change_percentage_200d_in_currency": f(r["rs200"]) or 20.0,
            "sparkline_in_7d": {"price": [price * (1 + 0.01 * ((j % 7) - 3)) for j in range(20)]},
        })
    if not any(t["symbol"] == "btc" for t in out):
        # rsBlendOf reads BTC as the benchmark; without it every RS is measured against
        # nothing and the confirmation term is meaningless.
        out.append({**out[0], "id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
                    "current_price": 95000.0, "market_cap": 1.9e12, "total_volume": 4e10})
    return out


class NoBrowser(Exception):
    """Playwright is installed but no browser is. A skip, not a failure."""


def _find_browser(pw):
    """Playwright's own resolution first, then whatever chromium is on the box.

    Some environments ship a browser at a path playwright's version pin does not expect,
    which is a reason to look rather than a reason to fail.
    """
    try:
        return pw.chromium.launch()
    except Exception:
        pass
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "", "/opt/pw-browsers"]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root), reverse=True):
            for rel in ("chrome-linux/chrome", "chrome-linux/headless_shell", "chrome"):
                cand = os.path.join(root, entry, rel)
                if os.path.exists(cand):
                    try:
                        return pw.chromium.launch(executable_path=cand)
                    except Exception:
                        continue
    raise NoBrowser("playwright is installed but no chromium was found "
                    "(run `playwright install chromium`)")


def run_checks() -> tuple:
    """Returns (failures, notes). Every assertion in one browser session."""
    failures, notes = [], []
    fixture = build_fixture()

    def check(name, fn):
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:                      # a stale locator, not a passing test
            failures.append(f"{name}: check could not run ({type(exc).__name__}: {exc})")

    with serve(_ROOT) as url, sync_playwright() as pw:
        try:
            browser = _find_browser(pw)
        except NoBrowser as exc:
            return [], [f"SKIP: {exc}"]
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/api.coingecko.com/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(fixture)))
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2500)

        check("no uncaught errors on load",
              lambda: (_ for _ in ()).throw(AssertionError("; ".join(errors)))
              if errors else None)

        # --- the acceptance case for the factor decomposition -------------------
        mismatches = page.evaluate("""() => {
          const bad = [];
          STATE.forEach(t => {
            const F = convictionFactors(t);
            const prod = 100*F.depth*F.cm*F.a_frac*F.em*F.perp;
            const shown = Math.max(0, Math.min(100, Math.round(prod)));
            if (shown !== t.conv) bad.push({sym:t.sym, shown, conv:t.conv});
          });
          return {n: STATE.length, bad};
        }""")
        check("decomposition reconstructs every published score", lambda: (
            _assert(mismatches["n"] > 0, "the board rendered no assets, so nothing was checked"),
            _assert(not mismatches["bad"],
                    f"{len(mismatches['bad'])} of {mismatches['n']} rows do not reconstruct: "
                    f"{mismatches['bad'][:5]}")))

        # --- every title became a real popover ---------------------------------
        tips = page.evaluate("""() => ({
          leftover: document.querySelectorAll('[title]').length,
          upgraded: document.querySelectorAll('[data-tip]').length,
        })""")
        check("no native title survives", lambda: _assert(
            tips["leftover"] == 0,
            f"{tips['leftover']} elements still carry a native title, which renders on no phone"))
        check("the explanation layer is substantial", lambda: _assert(
            tips["upgraded"] > 500,
            f"only {tips['upgraded']} elements carry an explanation; the board had over a thousand"))

        # --- the charts describe themselves ------------------------------------
        charts = page.evaluate("""() => [...document.querySelectorAll('.vh')]
            .map(t => ({id: t.id, rows: t.querySelectorAll('tbody tr').length}))""")
        check("each canvas publishes its points as a table", lambda: _assert(
            len(charts) >= 3 and all(c["rows"] > 0 for c in charts),
            f"chart data tables are missing or empty: {charts}"))

        # --- the sizer refuses a stop it cannot derive, and computes one it can --
        no_atr = page.evaluate("""() => { renderSizing();
            return document.querySelector('#sz-out').innerText; }""")
        check("a missing ATR14 is refused, not approximated", lambda: _assert(
            "no ATR14 recorded" in no_atr and "24h range is deliberately not substituted" in no_atr,
            "the sizer did not say why it has no stop, or substituted the 24h range"))
        with_atr = page.evaluate("""() => {
            STATE.forEach(t => { ATR14[t.sym] = {v: t.price * 0.045, date: '2026-08-25', bars: 15}; });
            document.querySelector('#sz-margin').value = '10';
            renderSizing();
            return document.querySelector('#sz-out').innerText; }""")
        low = with_atr.lower()
        check("a derivable stop is shown with its rule", lambda: _assert(
            "atr stop" in low and "atr14" in low and "of book" in low,
            "no ATR stop, rule or per-line exposure rendered"))
        check("the reading keeps its observation date and bar count", lambda: _assert(
            "atr recorded 2026-08-25" in low and "15 bars" in low,
            "the ATR value is shown detached from the night it was recorded"))
        check("the book-level exposure renders above the rows", lambda: _assert(
            "stop-loss exposure" in low and "atr stop" in low
            and low.index("stop-loss exposure") < low.index("atr stop"),
            "the book exposure total is missing or below the line items it summarises"))
        check("implied leverage appears without a liquidation-ordering claim", lambda: _assert(
            "implied leverage" in low
            and "liquidation ordering unknown" in low
            and "gets there first" not in low,
            "leverage is absent, or the panel still claims to know which of the stop and "
            "the exchange arrives first, which initial margin cannot determine"))
        check("nothing calls the ATR threshold an invalidation level", lambda: _assert(
            "invalid below" not in low and "invalidation" not in low,
            "the sizer calls a range multiple an invalidation level"))
        check("nothing calls the exposure a risk budget", lambda: _assert(
            "risk budget" not in low or "not a risk budget" in low,
            "the panel describes a risk budget, which is a control the sizer does not enforce"))

        # --- the parser takes perpetuals only -----------------------------------
        parser_opts = page.evaluate("""() => [...document.querySelectorAll('#pp-contract option')]
            .map(o => ({value: o.value, text: o.textContent}))""")
        codes = [o["value"] for o in parser_opts if o["value"] != "none"]
        check("a dated future cannot be selected in the funding parser", lambda: _assert(
            "ET" not in codes and "BIT" not in codes and "SOL" not in codes and "HED" not in codes,
            f"a dated contract is offered in the funding parser: {codes}"))
        check("the funding-bearing perpetuals are offered", lambda: _assert(
            {"BIP", "ETP", "SLP", "XPP", "HEP"} <= set(codes),
            f"a verified perpetual is missing from the parser: {codes}"))
        default_code = page.evaluate("() => document.querySelector('#pp-contract').value")
        check("the parser defaults to a verified perpetual, not a dated future",
              lambda: _assert(
                  default_code in codes and default_code not in ("ET", "BIT", "SOL", "HED"),
                  f"the parser defaults to {default_code!r}"))
        check("the default is funding-bearing", lambda: _assert(
            page.evaluate("() => { const p = selectedSpec(); return !!(p && p.funding_bearing "
                          "&& p.instrument_type === 'perpetual'); }"),
            "the preselected contract pays no funding, in a panel that recovers funding"))

        # --- the parser is the number the desk would recognise ------------------
        parsed = page.evaluate("""() => {
          const sel = document.querySelector('#pp-contract');
          sel.value = 'HEP'; sel.onchange();
          document.querySelector('#pp-contracts').value = '12';
          document.querySelector('#pp-price').value = '0.0802';
          renderParser();
          return {out: document.querySelector('#pp-out').innerText,
                  mult: document.querySelector('#pp-mult').value};
        }""")
        check("12 HEP contracts at $0.0802 is $4,812 of notional", lambda: _assert(
            parsed["mult"] == "5000" and "$4,812" in parsed["out"],
            f"multiplier {parsed['mult']}, output did not show $4,812: {parsed['out'][:200]}"))
        check("a verified contract is not marked unverified", lambda: _assert(
            "USER-ENTERED MULTIPLIER" not in parsed["out"],
            "a primary-verified contract is being reported as unverified"))

        tainted = page.evaluate("""() => {
          document.querySelector('#pp-mult').value = '1234';
          renderParser();
          return document.querySelector('#pp-out').innerText;
        }""")
        check("a hand-entered multiplier taints every derived figure", lambda: _assert(
            "USER-ENTERED MULTIPLIER" in tainted and "UNVERIFIED" in tainted,
            "typing over a verified contract size produced figures with no unverified mark"))

        unknown = page.evaluate("""() => {
          const sel = document.querySelector('#pp-contract');
          sel.value = 'none'; sel.onchange();
          return {out: document.querySelector('#pp-out').innerText,
                  mult: document.querySelector('#pp-mult').value};
        }""")
        check("an unmapped contract refuses, never defaults to 1", lambda: _assert(
            unknown["mult"] == "" and "not mapped" in unknown["out"].lower(),
            "an unmapped contract did not refuse. This is the original bug, a silent "
            "default of 1"))
        check("refusing does not claim the exchange lists no contract", lambda: _assert(
            "spot only" not in unknown["out"].lower()
            and "no listed contract" not in unknown["out"].lower()
            and "gap in this table" in unknown["out"].lower(),
            "the refusal asserts something about Coinbase's product list rather than "
            "about this board's table"))
        sizer_unmapped = page.evaluate("""() => {
            renderSizing(); return document.querySelector('#sz-out').innerText; }""")
        check("the sizer's unmapped wording is about this table, not the venue",
              lambda: _assert(
                  "spot only" not in sizer_unmapped.lower()
                  and "no listed contract" not in sizer_unmapped.lower(),
                  "the sizer claims Coinbase lists no contract for an unmapped symbol"))

        # --- zero orphaned explanations -----------------------------------------
        # Counting upgraded [data-tip] nodes proves nothing about reach. Tips inside
        # tr.row are deliberately neither focusable nor individually tappable, so the
        # question is not how many were upgraded but whether every one of them has a
        # route: directly focusable, inside a roving grid, or reproduced verbatim in a
        # drawer the row can open by touch AND by keyboard.
        audit = page.evaluate("""() => {
          const all = [...document.querySelectorAll('[data-tip]')];
          const orphans = [], byKind = {};
          all.forEach(n => {
            const ti = n.getAttribute('tabindex');
            const direct = (ti !== null && ti !== '-1')
                        || n.matches('a[href],button,input,select,textarea');
            const roving = !!n.closest('[data-tip-group]');
            const inRow  = !!n.closest('tr.row');
            let kind;
            if (direct) kind = 'direct';
            else if (roving) kind = 'roving';
            else if (inRow) kind = 'row-drawer';
            else { kind = 'ORPHAN';
                   orphans.push({tag: n.tagName, cls: String(n.className).slice(0, 40),
                                 tip: (n.getAttribute('data-tip') || '').slice(0, 70)}); }
            byKind[kind] = (byKind[kind] || 0) + 1;
          });
          return {total: all.length, byKind, orphans: orphans.slice(0, 10)};
        }""")
        check("no explanation is unreachable", lambda: _assert(
            audit["total"] > 500 and not audit["orphans"],
            f"{len(audit['orphans'])} of {audit['total']} explanations have no touch or "
            f"keyboard route: {audit['orphans']}"))

        # --- keyboard: focus, description, arrows in a dense group, Escape -------
        kb = page.evaluate("""() => {
          const t = document.querySelector('th[data-tip]');
          t.focus();
          const shown = !document.getElementById('tip-pop').hidden;
          const described = t.getAttribute('aria-describedby');
          const role = document.getElementById('tip-pop').getAttribute('role');
          return {shown, described, role, tab: t.getAttribute('tabindex')};
        }""")
        check("keyboard focus opens the explanation", lambda: _assert(
            kb["shown"] and kb["described"] == "tip-pop" and kb["role"] == "tooltip",
            f"focus did not announce the tip: {kb}"))
        page.keyboard.press("Escape")
        check("Escape dismisses it", lambda: _assert(
            page.evaluate("() => document.getElementById('tip-pop').hidden") is True,
            "the popover survived Escape"))

        rove = page.evaluate("""() => {
          // A visible group: an element inside a hidden tab pane cannot take focus, so
          // testing one would report "arrows do not work" about a display rule.
          const g = [...document.querySelectorAll('[data-tip-group]')]
                    .find(n => n.offsetParent !== null);
          if (!g) return {skip: true};
          // The group's managed set is its DIRECT explained children; nested elements
          // in sub-containers are other groups' business.
          const items = [...g.children].filter(n => n.matches('[data-tip][tabindex]'));
          const stops = items.filter(n => n.getAttribute('tabindex') === '0').length;
          items[0].focus();
          const before = document.activeElement;
          g.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
          return {skip: false, size: items.length, stops,
                  moved: document.activeElement !== before};
        }""")
        check("a dense group is one tab stop with arrow movement inside it",
              lambda: _assert(
                  rove.get("skip") or (rove["stops"] == 1 and rove["moved"]),
                  f"roving group is not navigable: {rove}"))

        # --- keyboard: the row drawer, and the notes it has to carry -------------
        drawer = page.evaluate("""() => {
          const row = document.querySelector('tr.row');
          const sym = row.dataset.sym;
          const tips = [...row.querySelectorAll('[data-tip]')]
                        .map(n => n.getAttribute('data-tip').trim()).filter(Boolean);
          row.focus();
          const focused = document.activeElement === row;
          const tb = row.closest('tbody');
          tb.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          const body = document.querySelector('#dw-body').innerText;
          return {sym, focused, tabindex: row.getAttribute('tabindex'),
                  open: document.querySelector('#drawer').classList.contains('open'),
                  nTips: tips.length,
                  missing: tips.filter(x => !body.includes(x.slice(0, 60))).length};
        }""")
        check("a matrix row is keyboard focusable", lambda: _assert(
            drawer["focused"] and drawer["tabindex"] is not None,
            f"matrix rows take no keyboard focus: {drawer}"))
        check("Enter on a focused row opens its drawer", lambda: _assert(
            drawer["open"], "the drawer cannot be opened from the keyboard"))
        check("the drawer reproduces every row explanation verbatim", lambda: _assert(
            drawer["nTips"] > 0 and drawer["missing"] == 0,
            f"{drawer['missing']} of {drawer['nTips']} row explanations are missing from "
            f"the drawer that is supposed to carry them"))

        # --- the phone -----------------------------------------------------------
        iphone = pw.devices["iPhone 13"]
        ctx = browser.new_context(**iphone)
        mob = ctx.new_page()
        mob_errors = []
        mob.on("pageerror", lambda e: mob_errors.append(str(e)))
        mob.route("**/api.coingecko.com/**", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(fixture)))
        mob.goto(url, wait_until="networkidle", timeout=60000)
        mob.wait_for_timeout(2500)
        check("no uncaught errors on a phone",
              lambda: _assert(not mob_errors, "; ".join(mob_errors)))

        # A DYNAMIC row-cell caveat, not a static column header: the header is in the
        # source and would pass even if nothing the board renders were reachable.
        dyn = mob.query_selector("#sz-out [data-tip]") or mob.query_selector("tbody [data-tip]")
        check("there is a rendered, non-static explanation to reach",
              lambda: _assert(dyn is not None, "no dynamically rendered explanation found"))
        if dyn:
            in_row = mob.evaluate("(el) => !!el.closest('tr.row')", dyn)
            if in_row:
                # Its route is the drawer, so that is what must open on tap.
                mob.evaluate("(el) => el.closest('tr.row').scrollIntoView()", dyn)
                dyn.tap()
                mob.wait_for_timeout(400)
                reached = mob.evaluate("""(el) => {
                    const txt = (el.getAttribute('data-tip') || '').slice(0, 60);
                    const body = document.querySelector('#dw-body').innerText;
                    return document.querySelector('#drawer').classList.contains('open')
                        && body.includes(txt); }""", dyn)
                check("tapping a row cell reaches its explanation through the drawer",
                      lambda: _assert(reached,
                                      "a row cell's caveat is unreachable by tap on a phone"))
            else:
                mob.evaluate("(el) => el.scrollIntoView()", dyn)
                dyn.tap()
                mob.wait_for_timeout(400)
                shown = mob.evaluate("""() => { const e = document.getElementById('tip-pop');
                    return {hidden: e ? e.hidden : true, len: e ? e.textContent.length : 0}; }""")
                check("tapping a rendered explanation opens it", lambda: _assert(
                    shown["hidden"] is False and shown["len"] > 20,
                    "tapping a dynamically rendered explanation on a phone showed nothing"))

        # And a static header too, since that is the densest surface on the board.
        target = mob.query_selector("th[data-tip]")
        if target:
            target.tap()
            mob.wait_for_timeout(300)
            shown = mob.evaluate("""() => { const e = document.getElementById('tip-pop');
                return {hidden: e ? e.hidden : true, len: e ? e.textContent.length : 0}; }""")
            check("tapping a column header opens its explanation", lambda: _assert(
                shown["hidden"] is False and shown["len"] > 20,
                "tapping an explained column header on a phone showed nothing. This is "
                "the bug the popover exists to fix"))
            mob.keyboard.press("Escape")
            mob.wait_for_timeout(150)
            check("Escape dismisses it on the phone too", lambda: _assert(
                mob.evaluate("() => document.getElementById('tip-pop').hidden") is True,
                "the popover cannot be dismissed"))
        check("the phone does not scroll sideways", lambda: _assert(
            mob.evaluate("() => document.documentElement.scrollWidth <= "
                         "document.documentElement.clientWidth + 1"),
            "the page scrolls horizontally on a phone"))

        browser.close()
    return failures, notes


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    return True


if __name__ == "__main__":
    if SKIP:
        print(f"SKIP: {SKIP}")
        print("render: 0 checks run")
        sys.exit(0)
    fails, notes = run_checks()
    for n in notes:
        print(n)
    for f in fails:
        print("FAIL " + f)
    if notes and not fails:
        print("render: skipped, nothing was verified")
    else:
        print("render: all checks passed" if not fails else f"render: {len(fails)} failed")
    sys.exit(1 if fails else 0)
else:
    import pytest

    @pytest.mark.skipif(SKIP is not None, reason=SKIP or "")
    def test_the_board_renders_correctly():
        fails, notes = run_checks()
        if notes and not fails:
            pytest.skip(notes[0])
        assert not fails, "\n".join(fails)
