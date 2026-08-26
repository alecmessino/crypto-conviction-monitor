"""Every drawer the board can open, opened — by mouse and by keyboard.

Issue #28: clicking HBAR threw `n.toFixed is not a function` inside the click handler.
The header rendered, the body did not, and nothing surfaced, because a throw inside an
onclick goes nowhere. `openDrawer`'s own comment describes that exact symptom being fixed
once before, from a different cause. It came back because the gate that was supposed to
hold it checked ONE row.

That is the same cohort-versus-row-level gap corrected for the ATR invariant in #27: a
test that opens the first drawer proves the first drawer opens. So this file iterates
every symbol the board can render and asserts on all of them.

Two causes, two properties:

  * `fmtUsd`/`fmtPrice` guarded with `isNaN`, which is coercive — `isNaN("2.8")` is false,
    so a CSV string walked past the guard, and the magnitude branches coerced it back to
    a number through arithmetic on every branch except the last, which calls `.toFixed`
    on the value itself. `fmtUsd("5000")` returned "$5.0K"; `fmtUsd("2.8")` threw. Every
    unlocks_usd in the ledger is under a thousand.

  * `openDrawer` looked its ledger row up by SYMBOL alone, over a list pre-filtered to
    the rows that have an ERA — four rows, all recorded 2026-08-03. Clicking HBAR on any
    later board attached a three-week-old unlock reading to tonight's row.

Both are pinned here. Both were verified by mutation before this gate was trusted:
reintroducing the raw-string `.toFixed` fails the numeric-boundary checks and every ERA
row's body check, and restoring the symbol-only lookup fails the stale-ERA checks. The
mutations are not shipped as a second browser pass — this file already costs two browser
contexts and CI has a ten-minute budget — but neither cause can be reintroduced without
turning this file red.

Skips loudly without playwright or a browser, matching test_render.py: an environment
that cannot run a browser must not fail a pull request, and must not report a pass it
did not earn either.
"""
import contextlib
import functools
import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

SKIP = None
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    SKIP = "playwright is not installed (pip install playwright && playwright install chromium)"

sys.path.insert(0, _HERE)
from test_render import NoBrowser, _find_browser, serve   # noqa: E402  one harness, not two

# The four symbols that carry an ERA reading in the recorded ledger, all on one night.
# Named because they are the payload of #28: the gate has to prove these specifically,
# not merely that "some row" works.
ERA_SYMBOLS = ("BEAT", "HBAR", "MON", "UAI")
REFUSAL = "No ERA observation recorded for this snapshot"


def ledger_rows(path=None):
    with open(path or os.path.join(_ROOT, "ledger", "signals.json"), encoding="utf-8") as fh:
        return json.load(fh)["rows"]


def era_rows(rows):
    return [r for r in rows if str(r.get("era", "")).strip() not in ("", "None")]


def build_fixture(rows, extra_symbols=()):
    """A CoinGecko-shaped payload from a night's ledger rows, plus named extras.

    `extra_symbols` is how BEAT, MON and UAI reach the board at all: they were recorded
    on 2026-08-03 and are not in tonight's universe, so a fixture built only from the
    latest night could not click them and the pin would silently test nothing.
    """
    latest = max(r["date"] for r in rows)
    night = [r for r in rows if r["date"] == latest]
    have = {r["symbol"] for r in night}
    for sym in extra_symbols:
        if sym in have:
            continue
        found = next((r for r in rows if r["symbol"] == sym), None)
        if found:
            night.append(found)

    def f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    out = []
    for i, r in enumerate(night):
        price = f(r.get("price")) or 1.0
        mc = f(r.get("market_cap")) or 1e8
        turn = f(r.get("turnover_pct")) or 5.0
        out.append({
            "id": r["symbol"].lower(), "symbol": r["symbol"].lower(), "name": r.get("name") or r["symbol"],
            "current_price": price, "market_cap": mc, "total_volume": mc * turn / 100.0,
            "fully_diluted_valuation": f(r.get("fdv_usd")),
            "price_change_percentage_24h": round((i % 21) - 10 + 0.3, 2),
            "market_cap_change_percentage_24h": round((i % 9) - 4, 2),
            "high_24h": f(r.get("high_24h")), "low_24h": f(r.get("low_24h")),
            "ath": price * 2.4, "atl": price * 0.3,
            "price_change_percentage_1h_in_currency": 0.4,
            "price_change_percentage_7d_in_currency": f(r.get("rs7")) or 3.0,
            "price_change_percentage_14d_in_currency": f(r.get("rs14")) or 4.0,
            "price_change_percentage_30d_in_currency": f(r.get("rs30")) or 5.0,
            "price_change_percentage_200d_in_currency": f(r.get("rs200")) or 20.0,
            "sparkline_in_7d": {"price": [price * (1 + 0.01 * ((j % 7) - 3)) for j in range(20)]},
        })
    if not any(t["symbol"] == "btc" for t in out):
        out.append({**out[0], "id": "bitcoin", "symbol": "btc", "name": "Bitcoin",
                    "current_price": 95000.0, "market_cap": 1.9e12, "total_volume": 4e10})
    return out


@contextlib.contextmanager
def same_date_tree():
    """A throwaway copy of the site whose ERA rows are stamped with the LATEST night.

    The recorded ledger is never modified: proving that a same-date ERA renders needs a
    same-date ERA to exist, and manufacturing one on disk would be backfilling history to
    make a test pass. It is built in a temp tree, used, and deleted.
    """
    tmp = tempfile.mkdtemp(prefix="drawer-samedate-")
    try:
        os.makedirs(os.path.join(tmp, "ledger"), exist_ok=True)
        for name in ("index.html", "methodology.html"):
            shutil.copy(os.path.join(_ROOT, name), os.path.join(tmp, name))
        for name in os.listdir(os.path.join(_ROOT, "ledger")):
            shutil.copy(os.path.join(_ROOT, "ledger", name), os.path.join(tmp, "ledger", name))
        path = os.path.join(tmp, "ledger", "signals.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        rows = doc["rows"]
        latest = max(r["date"] for r in rows)
        # Written ONTO the latest night's rows, not appended beside them. An appended
        # clone is shadowed by the original: the lookup takes the first (symbol, date)
        # match, which is the real row that has no ERA, and the fixture would then be
        # testing the refusal it was built to disprove.
        by_sym = {r["symbol"]: r for r in rows if r["date"] == latest}
        moved = []
        for src in era_rows(rows):
            target = by_sym.get(src["symbol"])
            if target is None:
                target = {**src, "date": latest}
                rows.append(target)
                by_sym[src["symbol"]] = target
            target["era"] = src["era"]
            target["unlocks_usd"] = src.get("unlocks_usd")
            moved.append(src["symbol"])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        yield tmp, latest, moved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- the DOM-side probe, shared by both scenarios ---------------------------------
# Every symbol the board can render, not the ten it renders by default.
#
# The matrix shows `base.slice(0,10)` unfiltered, so a loop over what happens to be on
# screen tests the first screen and calls it the board. The filter is the user's own
# route to the rest of the universe, so the loop drives that: filter to the symbol, click
# the row it brings up, assert, move on.
#
# `.drawer` is `position:fixed`, which makes `offsetParent` null even when it is fully
# visible — openness is the `open` class that drives its transform, and nothing else.
OPEN_EVERY_ROW = """
async ([mode, symbols]) => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const drawer = () => document.getElementById('drawer');
  const isOpen = () => !!(drawer() && drawer().classList.contains('open'));
  const rowFor = sym => [...document.querySelectorAll('#tbl-conv tbody tr.row')]
                        .find(r => r.dataset.sym === sym);
  const seen = [], bad = [];
  for (const sym of symbols) {
    setFilter(sym);
    await sleep(20);
    const row = rowFor(sym);
    if (!row) { bad.push({sym, why: 'the filter could not bring this symbol onto the board'}); continue; }
    const tabindex = row.getAttribute('tabindex');
    const role = row.getAttribute('role');
    if (mode === 'keyboard') {
      row.focus();
      row.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
    } else {
      const b = row.getBoundingClientRect();
      const o = {bubbles: true, clientX: b.x + 5, clientY: b.y + 5};
      row.dispatchEvent(new PointerEvent('pointerdown', {...o, pointerType: mode}));
      row.dispatchEvent(new PointerEvent('pointerup', {...o, pointerType: mode}));
      row.click();
    }
    await sleep(40);
    const body = document.querySelector('.drawer-b');
    const text = body ? body.innerText.trim() : '';
    const rec = {sym, len: text.length, open: isOpen(), tabindex, role,
                 era: (document.querySelector('#dw-era') || {}).textContent || ''};
    seen.push(rec);
    // A drawer is acceptable when it has a body. An explicit refusal IS a body.
    if (!rec.open) bad.push({sym, why: 'drawer did not open'});
    else if (!text.length) bad.push({sym, why: 'drawer opened with an empty body'});
    if (tabindex === null) bad.push({sym, why: 'row carries no tabindex, so it is unreachable by keyboard'});
    closeDrawer();
    await sleep(10);
  }
  setFilter('');
  await sleep(30);
  return {seen, bad, count: symbols.length};
}
"""

# Every symbol in the scored universe, which is what the filter can reach.
UNIVERSE = "() => STATE.map(t => t.sym)"

HELPER_CASES = """
() => {
  const cases = [
    ['2.8', 2.8], ['12.6', 12.6], ['5000', 5000],
    [2.8, 2.8], [12.6, 12.6], [5000, 5000],
  ];
  const out = {accepted: [], rejected: [], identical: true};
  for (const [input, want] of cases) {
    const got = numOrNull(input);
    out.accepted.push({input: String(input), type: typeof input, got, want, ok: got === want});
  }
  for (const [label, v] of [['blank', ''], ['whitespace', '   '], ['tab', '\\t'],
                            ['null', null], ['undefined', undefined], ['None', 'None'],
                            ['text', 'abc'], ['NaN', NaN], ['inf', Infinity],
                            ['-inf', -Infinity], ['bool', true], ['numeric-ish', '1.2.3']]) {
    out.rejected.push({label, got: numOrNull(v), usd: fmtUsd(v), price: fmtPrice(v)});
  }
  for (const s of ['2.8', '12.6', '5000', '0.004']) {
    if (fmtUsd(s) !== fmtUsd(Number(s)) || fmtPrice(s) !== fmtPrice(Number(s))) out.identical = false;
  }
  out.format = {usd28: fmtUsd('2.8'), usd126: fmtUsd('12.6'), usd5000: fmtUsd('5000'),
                price28: fmtPrice('2.8'), price5000: fmtPrice('5000')};
  return out;
}
"""


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    return True


INFO = []          # coverage lines for the reader; NEVER a skip reason


def run_checks() -> tuple:
    """Returns (failures, skips). `skips` is only ever a missing browser.

    Informational output goes to INFO. It used to go to `notes`, and both entrypoints
    read a non-empty `notes` with no failures as "this gate did not run" — so adding a
    coverage line turned a passing gate into a silent skip, which is the precise failure
    this file's own docstring warns against.
    """
    failures, notes = [], []
    INFO.clear()

    def check(name, fn):
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:
            failures.append(f"{name}: check could not run ({type(exc).__name__}: {exc})")

    def probe(name, fn, default):
        """Run a page.evaluate; turn a throw into a FAILURE rather than a crash.

        The page under test is the thing that may throw — that is the whole point — and
        an exception escaping run_checks() aborts the run with a traceback instead of a
        verdict. Restoring the raw-string `.toFixed` made exactly that happen: the gate
        detected the bug and then died of it.
        """
        try:
            return fn()
        except Exception as exc:
            failures.append(f"{name}: the page threw while being probed "
                            f"({type(exc).__name__}: {str(exc).splitlines()[0]})")
            return default

    rows = ledger_rows()
    eras = era_rows(rows)
    latest = max(r["date"] for r in rows)
    fixture = build_fixture(rows, extra_symbols=ERA_SYMBOLS)

    with sync_playwright() as pw:
        try:
            browser = _find_browser(pw)
        except NoBrowser as exc:
            return [], [f"SKIP: {exc}"]

        # ---------- scenario A: the recorded ledger, unmodified ----------
        with serve(_ROOT) as url:
            for label, device in (("desktop", None), ("iPhone 13", pw.devices["iPhone 13"])):
                ctx = browser.new_context(**(device or {}))
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.route("**/api.coingecko.com/**", lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(fixture)))
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000)

                mode = "touch" if device else "mouse"
                universe = probe(f"[{label}] reading the universe",
                                 lambda: page.evaluate(UNIVERSE), [])
                # Every symbol the board can reach, plus the four pinned ones in case a
                # night's universe does not contain them.
                targets = list(dict.fromkeys(list(universe) + list(ERA_SYMBOLS)))
                empty = {"seen": [], "bad": [], "count": 0}
                pointer = probe(f"[{label}] opening every drawer by {mode}",
                                lambda: page.evaluate(OPEN_EVERY_ROW, [mode, targets]), empty)
                keyboard = probe(f"[{label}] opening every drawer by keyboard",
                                 lambda: page.evaluate(OPEN_EVERY_ROW, ["keyboard", targets]), empty)

                INFO.append(f"[{label}] opened {len(pointer['seen'])} drawers by {mode} "
                             f"and {len(keyboard['seen'])} by keyboard, over {pointer['count']} "
                             f"symbols")
                check(f"[{label}] the board rendered rows to open", lambda: _assert(
                    pointer["count"] > 20 and len(pointer["seen"]) > 20,
                    f"only {len(pointer['seen'])} of {pointer['count']} symbols were opened — "
                    f"a loop over the first screen is not a loop over the board"))
                check(f"[{label}] every drawer opens by {mode}", lambda: _assert(
                    not pointer["bad"],
                    f"{len(pointer['bad'])} of {pointer['count']} rows failed: "
                    f"{pointer['bad'][:6]}"))
                check(f"[{label}] every drawer opens by keyboard", lambda: _assert(
                    not keyboard["bad"],
                    f"{len(keyboard['bad'])} of {keyboard['count']} rows failed: "
                    f"{keyboard['bad'][:6]}"))
                # Roving tabindex: one row is 0 and the rest are -1, reachable by
                # arrow key within the group. Asserting every row is 0 would be asserting
                # against the pattern the board actually implements; what must hold is
                # that every row carries a tabindex and a role, and that Enter on a
                # focused row opens it — which the keyboard pass above proves directly.
                check(f"[{label}] every matrix row is keyboard-addressable", lambda: _assert(
                    all(r["tabindex"] is not None and r["role"] for r in keyboard["seen"]),
                    "some rows carry no tabindex or role, so they are unreachable"))

                # The four that carry an ERA, by name.
                opened = {r["sym"]: r for r in pointer["seen"]}
                present = [s for s in ERA_SYMBOLS if s in opened]
                check(f"[{label}] the ERA symbols are on the board to be tested",
                      lambda: _assert(len(present) == len(ERA_SYMBOLS),
                                      f"only {present} of {list(ERA_SYMBOLS)} rendered; the "
                                      f"pin would be testing nothing"))
                for sym in present:
                    rec = opened[sym]
                    check(f"[{label}] {sym} opens with a body", lambda rec=rec, sym=sym: _assert(
                        rec["open"] and rec["len"] > 0,
                        f"{sym}: open={rec['open']} bodyLen={rec['len']} — this is the #28 symptom"))
                    check(f"[{label}] {sym} shows no stale ERA", lambda rec=rec, sym=sym: _assert(
                        REFUSAL in rec["era"],
                        f"{sym}: ERA reads {rec['era']!r}. Its only recorded ERA is on "
                        f"{sorted({r['date'] for r in eras})}, not {latest}, so anything "
                        f"other than the refusal is a historical row leaking forward."))

                check(f"[{label}] no uncaught errors across every drawer", lambda: _assert(
                    not errors, "; ".join(errors[:4])))

                if not device:
                    # Factor decomposition still reconstructs the published score with a
                    # drawer open — the panel and the drawer render from the same click.
                    recon = probe("[desktop] factor reconstruction", lambda: page.evaluate("""() => {
                      const bad = [];
                      STATE.forEach(t => {
                        const F = convictionFactors(t);
                        const prod = 100*F.depth*F.cm*F.a_frac*F.em*F.perp;
                        const shown = Math.max(0, Math.min(100, Math.round(prod)));
                        if (shown !== t.conv) bad.push({sym: t.sym, shown, conv: t.conv});
                      });
                      return {bad, n: STATE.length};
                    }"""), {"bad": [{"sym": "?"}], "n": 0})
                    check("[desktop] factor decomposition reconstructs every score",
                          lambda: _assert(not recon["bad"],
                                          f"{len(recon['bad'])} of {recon['n']} rows do not "
                                          f"reconstruct: {recon['bad'][:5]}"))

                    helper = probe("[desktop] the numeric boundary",
                                   lambda: page.evaluate(HELPER_CASES),
                                   {"accepted": [{"ok": False}], "rejected": [{"got": 1}],
                                    "identical": False, "format": {}})
                    check("[desktop] the numeric boundary accepts numbers and numeric strings",
                          lambda: _assert(all(c["ok"] for c in helper["accepted"]),
                                          f"rejected a valid value: "
                                          f"{[c for c in helper['accepted'] if not c['ok']]}"))
                    check("[desktop] a string and its number format identically",
                          lambda: _assert(helper["identical"],
                                          "a numeric string formats differently from its number"))
                    check("[desktop] blanks never become a fabricated zero", lambda: _assert(
                        all(c["got"] is None and c["usd"] == "—" and c["price"] == "—"
                            for c in helper["rejected"]),
                        f"a rejected input produced a value: "
                        f"{[c for c in helper['rejected'] if c['got'] is not None]}"))
                    check("[desktop] the existing output format is preserved", lambda: _assert(
                        helper["format"] == {"usd28": "$2.80", "usd126": "$12.60",
                                             "usd5000": "$5.0K", "price28": "$2.80",
                                             "price5000": "$5,000"},
                        f"formatting changed: {helper['format']}"))
                ctx.close()

        # ---------- scenario B: a same-date ERA, in a temp tree ----------
        with same_date_tree() as (tree, night, moved):
            with serve(tree) as url2:
                ctx = browser.new_context()
                page = ctx.new_page()
                errors2 = []
                page.on("pageerror", lambda e: errors2.append(str(e)))
                page.route("**/api.coingecko.com/**", lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(fixture)))
                page.goto(url2, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(3000)
                shown = probe("[same-date] opening the ERA drawers", lambda: page.evaluate("""async (syms) => {
                  const sleep = ms => new Promise(r => setTimeout(r, ms));
                  const out = {};
                  for (const sym of syms) {
                    setFilter(sym); await sleep(30);
                    const row = [...document.querySelectorAll('#tbl-conv tbody tr.row')]
                                .find(r => r.dataset.sym === sym);
                    if (!row) continue;
                    row.click(); await sleep(200);
                    out[sym] = {era: (document.querySelector('#dw-era')||{}).textContent || '',
                                len: (document.querySelector('.drawer-b')||{}).innerText.trim().length};
                    document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
                    await sleep(20);
                  }
                  return out;
                }""", list(ERA_SYMBOLS)), {})

                by_sym = {r["symbol"]: r for r in eras}
                for sym in ERA_SYMBOLS:
                    if sym not in shown or sym not in by_sym:
                        continue
                    txt, src = shown[sym]["era"], by_sym[sym]
                    check(f"[same-date] {sym} renders its ERA value", lambda txt=txt, src=src, sym=sym: _assert(
                        f"{float(src['era']):.2f}" in txt,
                        f"{sym}: ERA {src['era']} is not in {txt!r}"))
                    check(f"[same-date] {sym} renders its unlock value", lambda txt=txt, src=src, sym=sym: _assert(
                        "unlocks $" in txt,
                        f"{sym}: no unlock figure in {txt!r} (source has {src.get('unlocks_usd')!r})"))
                    check(f"[same-date] {sym} renders the observation date", lambda txt=txt, sym=sym: _assert(
                        night in txt,
                        f"{sym}: the ERA is shown without the night it was observed: {txt!r}"))
                check("[same-date] no uncaught errors", lambda: _assert(
                    not errors2, "; ".join(errors2[:4])))
                ctx.close()
        browser.close()
    return failures, notes


if __name__ == "__main__":
    if SKIP:
        print(f"SKIP: {SKIP}")
        print("drawer: 0 checks run")
        sys.exit(0)
    fails, notes = run_checks()
    for n in INFO:
        print("  " + n)
    for n in notes:
        print(n)
    for f in fails:
        print("FAIL " + f)
    if notes and not fails:
        print("drawer: skipped, nothing was verified")
    else:
        print("drawer: all checks passed" if not fails else f"drawer: {len(fails)} failed")
    sys.exit(1 if fails else 0)
else:
    import pytest

    @pytest.mark.skipif(SKIP is not None, reason=SKIP or "")
    def test_every_drawer_opens():
        fails, notes = run_checks()
        if notes and not fails:
            pytest.skip(notes[0])
        assert not fails, "\n".join(fails)
