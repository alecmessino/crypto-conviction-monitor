"""Artifact freshness, and the terminal behaviours that depend on what was loaded when.

Every ledger artifact the terminal reads is dated by the field it names for itself and
classed CURRENT / STALE / ABSENT against the newest night in the signals ledger. A STALE
or ABSENT artifact is still rendered — it is what was recorded — but every panel built
from it carries a chip saying so. walkforward.json sat six nights stale under a panel
that read as current before this existed.

Also here, because each is a question of what the page fetched and when: rwa.json loads
on the first RWA reveal, the seeded inspector does not prefetch neighbours, the markets
call honours the shared 429 backoff, keyboard focus survives a refresh, the drawer's
derivatives context is the newest night's only, and the drawer's "Score modifier" is the
multiplier the score applied.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the rule, under node
# ---------------------------------------------------------------------------
def _block():
    start = HTML.index("/* FRESHNESS")
    end = HTML.index("/* END FRESHNESS */")
    return HTML[start:end]


CASES = r"""
const ref = "2026-09-23";
const out = {
  current:  artifactFreshness("funding.json", {date: "2026-09-23"}, ref),
  ahead:    artifactFreshness("market_intel.json", {date: "2026-09-24"}, ref),
  stale:    artifactFreshness("walkforward.json", {to: "2026-09-17"}, ref),
  absent:   artifactFreshness("monitor.json", null, ref),
  undated:  artifactFreshness("funding.json", {generated_at: "x"}, ref),
  perp:     artifactFreshness("perp.json", {as_of: "2026-09-17"}, ref),
  breadth:  artifactFreshness("market_breadth.json", {generated_at: "2026-09-23T23:36:25Z"}, ref),
  index:    artifactFreshness("index.json", {latest: {date: "2026-09-22"}, generated_at: "2026-09-23T11:00:00Z"}, ref),
  parityOk: artifactFreshness("parity.json", {spec_hash: "91bbc2a7e466"}, ref, "91bbc2a7e466"),
  parityOld:artifactFreshness("parity.json", {spec_hash: "ab16684ad5c1"}, ref, "91bbc2a7e466"),
  sigNow:   signalsFreshness({rows: [{date: "2026-09-22"}, {date: "2026-09-23"}]}, "2026-09-24"),
  sigOld:   signalsFreshness({rows: [{date: "2026-09-20"}]}, "2026-09-24"),
  sigNone:  signalsFreshness(null, "2026-09-24"),
};
console.log(JSON.stringify(out));
"""


def test_the_freshness_rule():
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    res = subprocess.run(["node", "-e", _block() + CASES], capture_output=True, text=True,
                         timeout=30)
    assert res.returncode == 0, res.stderr
    o = json.loads(res.stdout)
    st = {k: v["state"] for k, v in o.items()}
    assert st == {"current": "CURRENT", "ahead": "CURRENT", "stale": "STALE",
                  "absent": "ABSENT", "undated": "STALE", "perp": "STALE",
                  "breadth": "CURRENT", "index": "STALE", "parityOk": "CURRENT",
                  "parityOld": "STALE", "sigNow": "CURRENT", "sigOld": "STALE",
                  "sigNone": "ABSENT"}, st
    assert o["stale"]["date"] == "2026-09-17" and o["stale"]["field"] == "to"
    assert o["index"]["field"] == "latest.date"


def test_every_artifact_the_terminal_fetches_is_dated():
    fetched = {f.split("/")[-1] for f in
               __import__("re").findall(r'fetch\("(ledger/[a-z_]+\.json)"', HTML)}
    block = _block()
    for f in fetched:
        assert f in block or f == "parity.json", f"{f} is fetched but never dated"
    assert "parity.json" in block


# ---------------------------------------------------------------------------
# in the browser
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_render import SKIP, NoBrowser, _ROOT, _find_browser, build_fixture, serve  # noqa: E402

if SKIP is None:
    from playwright.sync_api import sync_playwright


def _page(pw, url, fixture, routes=None, seen=None, markets_status=200):
    try:
        browser = _find_browser(pw)
    except NoBrowser as exc:
        pytest.skip(str(exc))
    page = browser.new_page()
    seen = seen if seen is not None else []

    def cg(route):
        u = route.request.url
        seen.append(u)
        if "/coins/markets" in u and "ids=" not in u:
            return route.fulfill(status=markets_status, content_type="application/json",
                                 body=json.dumps(fixture) if markets_status == 200 else "{}")
        return route.fulfill(status=200, content_type="application/json", body="{}")
    page.route("**/api.coingecko.com/**", cg)
    page.on("request", lambda r: seen.append(r.url) if "/ledger/" in r.url else None)
    for pat, h in (routes or {}).items():
        page.route(pat, h)
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    return browser, page, seen


def _json_route(doc, status=200):
    return lambda r: r.fulfill(status=status, content_type="application/json",
                               body=json.dumps(doc) if doc is not None else "{}")


def test_stale_and_absent_artifacts_are_labelled_where_they_are_used():
    if SKIP is not None:
        pytest.skip(SKIP)
    wf = json.loads((ROOT / "ledger" / "walkforward.json").read_text())
    wf["to"] = "2026-09-17"
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, seen = _page(pw, url, build_fixture(), {
            "**/ledger/walkforward.json": _json_route(wf),
            "**/ledger/funding.json": _json_route(None, 404)})
        try:
            page.wait_for_function("()=>Object.keys(ARTIFACT_STATE).length>0", timeout=30000)
            states = page.evaluate("()=>Object.fromEntries(Object.entries(ARTIFACT_STATE)"
                                   ".map(([k,v])=>[k,v.state]))")
            chips = page.evaluate("""()=>[...document.querySelectorAll('.stale-chip')]
                .map(c=>({for:c.dataset.for, next:c.nextElementSibling&&c.nextElementSibling.id,
                          text:c.textContent}))""")
            listed = page.evaluate("()=>document.querySelector('#artifact-fresh').textContent")
        finally:
            browser.close()
    assert states["walkforward.json"] == "STALE" and states["funding.json"] == "ABSENT"
    assert states["monitor.json"] == "CURRENT", states   # the nightly's own companion
    by = {(c["for"], c["next"]) for c in chips}
    assert ("walkforward.json", "ic-matrix") in by
    assert ("funding.json", "fh-sub") in by
    wf_chip = next(c for c in chips if c["for"] == "walkforward.json")
    assert "2026-09-17" in wf_chip["text"] and "not as current" in wf_chip["text"]
    assert not [c for c in chips if c["for"] == "monitor.json"]
    assert "walkforward.json" in listed and "STALE" in listed
    # rwa.json is not requested until its workspace is, so it is not listed as ABSENT.
    assert "rwa.json" not in states


def test_rwa_json_loads_on_first_reveal_and_not_before():
    if SKIP is not None:
        pytest.skip(SKIP)
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, seen = _page(pw, url, build_fixture())
        try:
            page.wait_for_function("()=>STATE.length>0", timeout=30000)
            page.wait_for_timeout(500)
            before = [u for u in seen if u.endswith("/ledger/rwa.json")]
            page.evaluate("()=>switchWorkspace('rwa', false)")
            page.wait_for_function("()=>RWA!==null", timeout=30000)
            after = [u for u in seen if u.endswith("/ledger/rwa.json")]
            rwa_state = page.evaluate("()=>ARTIFACT_STATE['rwa.json']&&ARTIFACT_STATE['rwa.json'].state")
        finally:
            browser.close()
    assert before == [], "rwa.json was fetched on first paint"
    assert len(after) == 1
    assert rwa_state in ("CURRENT", "STALE")


def test_first_paint_fetches_the_seeded_row_and_no_neighbours():
    if SKIP is not None:
        pytest.skip(SKIP)
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, seen = _page(pw, url, build_fixture())
        try:
            page.wait_for_function("()=>STATE.length>0 && SEL", timeout=30000)
            page.wait_for_timeout(1500)
            coin_calls = [u for u in seen if "/coins/" in u and "/coins/markets" not in u
                          and "/coins/categories" not in u]
            # A reader choosing a row does get the neighbours prefetched.
            second = page.evaluate("()=>BOARD_VIEW[3].sym")
            page.evaluate("s=>select(s)", second)
            page.wait_for_timeout(1500)
            after = [u for u in seen if "/coins/" in u and "/coins/markets" not in u
                     and "/coins/categories" not in u]
        finally:
            browser.close()
    assert len(coin_calls) == 1, coin_calls
    assert len(after) >= 3, after


def test_the_markets_call_honours_the_shared_backoff():
    if SKIP is not None:
        pytest.skip(SKIP)
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, seen = _page(pw, url, build_fixture())
        try:
            page.wait_for_function("()=>STATE.length>0", timeout=30000)
            n0 = len([u for u in seen if "/coins/markets" in u and "ids=" not in u])
            msg = page.evaluate("""async()=>{ CG_BLOCKED_UNTIL = Date.now() + 60000;
                                  await load(); return FEED.state + '|' + FEED.detail; }""")
            n1 = len([u for u in seen if "/coins/markets" in u and "ids=" not in u])
        finally:
            browser.close()
    assert n1 == n0, "the markets call ignored the shared 429 backoff"
    assert msg.startswith("throttled|") and "shared backoff" in msg


def test_keyboard_focus_survives_a_refresh():
    if SKIP is not None:
        pytest.skip(SKIP)
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, seen = _page(pw, url, build_fixture())
        try:
            page.wait_for_function("()=>STATE.length>0", timeout=30000)
            sym = page.evaluate("""()=>{ const tr=document.querySelectorAll('#tbl-conv tbody tr.row')[2];
                                   tr.setAttribute('tabindex', tr.getAttribute('tabindex')||'0');
                                   tr.focus(); return tr.dataset.sym; }""")
            page.evaluate("async()=>{ await load(); }")
            now = page.evaluate("""()=>{ const a=document.activeElement;
                                   return a && a.closest && a.closest('#tbl-conv') ? a.dataset.sym : a.tagName; }""")
        finally:
            browser.close()
    assert now == sym


def test_drawer_context_is_the_newest_night_and_its_modifier_is_the_applied_one():
    if SKIP is not None:
        pytest.skip(SKIP)
    with serve(_ROOT) as url, sync_playwright() as pw:
        browser, page, seen = _page(pw, url, build_fixture())
        try:
            page.wait_for_function("()=>STATE.length>0", timeout=30000)
            o = page.evaluate("""()=>{
              const latest = REWIND_DATES[REWIND_DATES.length-1];
              const tonight = new Set((LEDGER_BY_DATE[latest]||[]).map(r=>(r.symbol||'').toUpperCase()));
              const stray = Object.keys(PERPS).filter(s=>!tonight.has(s));
              // A funding.json modifier the score did not apply must be named as such.
              const sym = STATE[0].sym;
              const cell = scoreModifierCell(sym, {score_modifier: perpReading(sym).applied === 1 ? 0.9 : 1.0});
              // A rewound row states the multiplier ITS recorded score applied.
              const past = scoreModifierCell(sym, {score_modifier: 1.0},
                                             {_rewound: true, _perpMult: 0.93, _date: "2026-09-20"});
              const pastWords = scoreFundingSentence(sym, {_rewound: true, _perpMult: 0.93, _date: "2026-09-20"});
              // A current transport with no rows is not "did not load".
              const saved = [PERP_LOADED, PERP_DOC, PERP_WITHHELD];
              PERP_LOADED = false; PERP_DOC = {as_of: latest, rows: {}}; PERP_WITHHELD = null;
              const empty = perpReading(sym).state;
              [PERP_LOADED, PERP_DOC, PERP_WITHHELD] = saved;
              return {asOf: PERPS_AS_OF, latest, stray, cell, past, pastWords, empty};
            }""")
        finally:
            browser.close()
    assert o["asOf"] == o["latest"] and o["stray"] == [], o
    assert "not applied here" in o["cell"], o["cell"]
    assert "×0.930" in o["past"] and "2026-09-20" in o["past"], o["past"]
    assert "×0.930" in o["pastWords"] and "2026-09-20" in o["pastWords"]
    assert o["empty"] == "withheld-empty"
