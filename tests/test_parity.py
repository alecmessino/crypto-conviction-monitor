"""Frontend<->backend parity (mandatory before every deploy) + frozen regression.

The terminal's `conviction()` must produce the SAME conviction and component
attribution as the nightly `score()`. If they diverge, the live board silently
disagrees with the persisted ledger and every historical result becomes untrustworthy.

**This gate used to be unable to detect that.** It compared `nightly.score()` against a
hand-written *Python transcription* of the frontend maths and never opened
`index.html`. Editing the JS without editing the transcription left parity green while
the real terminal drifted — the transcription was not the frontend, it was a second
implementation that happened to agree with the backend.

It now extracts the real JS between the `MODEL PORT` markers in `index.html` and
executes it under node, which is the only version of this check that means anything.

Runs two ways:
  * `python -m pytest tests/test_parity.py`   (local / dev CI; skips without node)
  * `python tests/test_parity.py`             (CI nightly gate — NO pytest needed;
                                              exits non-zero on any failure, and
                                              treats a missing node as a failure
                                              rather than a pass)
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_spec = importlib.util.spec_from_file_location("nightly", os.path.join(_ROOT, "nightly.py"))
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

TERMINAL = os.path.join(_ROOT, "index.html")
MARKER_START = "MODEL PORT"
MARKER_END = "END MODEL PORT"


def extract_port() -> str:
    """Pull the delimited scoring block out of the terminal's inline script.

    The markers sit inside comment blocks, so the slice runs from the *end* of the
    opening comment to the *start* of the closing one — anything else hands node a
    fragment of prose and fails with a syntax error rather than a parity result.
    """
    html = open(TERMINAL, encoding="utf-8").read()
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert script, "index.html has no inline <script> block"
    body = script.group(1)
    start, end = body.find(MARKER_START), body.find(MARKER_END)
    assert start != -1 and end != -1, (
        f"could not find the {MARKER_START}/{MARKER_END} markers in index.html — "
        "the parity gate cannot verify a port it cannot locate")
    open_close = body.find("*/", start)
    assert open_close != -1, "the opening MODEL PORT marker is not inside a /* */ comment"
    close_open = body.rfind("/*", start, end)
    assert close_open != -1, "the END MODEL PORT marker is not inside a /* */ comment"
    port = body[open_close + 2:close_open]
    assert "conviction" in port and "liquidityFit" in port, \
        "the extracted block does not contain the scoring functions"
    return port


def run_js(cases: list) -> list:
    """Execute the real frontend scoring over `cases`, returning its own output.

    Each case is {"t": {vol, mc, chg}, "perp": float, "asset": {...}, "btc": {...}}.
    """
    node = shutil.which("node")
    assert node, "node is required to execute the frontend port"
    driver = extract_port() + """
const CASES = %s;
const out = CASES.map(c => {
  const rs = rsBlendOf(c.asset, c.btc);
  const r = conviction(c.t, c.perp, rs);
  return {conv: r.conv, comp: r.comp, rsBlend: rs, signal: signal(r.conv)[0]};
});
console.log(JSON.stringify(out));
""" % json.dumps(cases)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(driver)
        path = fh.name
    try:
        res = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            raise AssertionError(f"node failed running the extracted port: {res.stderr.strip()}")
        return json.loads(res.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


# ---- shared fixture: BTC reference + fixed assets (deterministic inputs) ----
BTC = {
    "symbol": "BTC", "market_cap": 1.3e12, "total_volume": 3e10,
    "price_change_percentage_24h": 2.0,
    "price_change_percentage_7d_in_currency": 2.0,
    "price_change_percentage_14d_in_currency": 1.0,
    "price_change_percentage_30d_in_currency": 3.0,
    "price_change_percentage_200d_in_currency": -30.0,
}
FIXTURE = {
    "ETH": {"market_cap": 4e11, "total_volume": 1.5e10, "price_change_percentage_24h": -1.0,
            "price_change_percentage_7d_in_currency": 10.0, "price_change_percentage_14d_in_currency": 5.0,
            "price_change_percentage_30d_in_currency": 20.0, "price_change_percentage_200d_in_currency": 15.0},
    "SOL": {"market_cap": 8e10, "total_volume": 4e9, "price_change_percentage_24h": 3.0,
            "price_change_percentage_7d_in_currency": 18.0, "price_change_percentage_14d_in_currency": 12.0,
            "price_change_percentage_30d_in_currency": 30.0, "price_change_percentage_200d_in_currency": 40.0},
    "ADA": {"market_cap": 1.2e10, "total_volume": 6e8, "price_change_percentage_24h": 0.5,
            "price_change_percentage_7d_in_currency": 17.0, "price_change_percentage_14d_in_currency": 8.0,
            "price_change_percentage_30d_in_currency": 3.0, "price_change_percentage_200d_in_currency": -20.0},
    "LINK": {"market_cap": 9e9, "total_volume": 5e8, "price_change_percentage_24h": -2.0,
             "price_change_percentage_7d_in_currency": -5.0, "price_change_percentage_14d_in_currency": -3.0,
             "price_change_percentage_30d_in_currency": 8.0, "price_change_percentage_200d_in_currency": 25.0},
}


def _asset(sym, perp_mult=1.0):
    d = dict(FIXTURE[sym])
    d["symbol"] = sym
    return d, perp_mult


def check_frontend_backend_parity():
    """The real frontend conviction + attribution must equal nightly score()."""
    syms = list(FIXTURE)
    cases = []
    for sym in syms:
        t, _ = _asset(sym)
        cases.append({"t": {"vol": t["total_volume"], "mc": t["market_cap"],
                            "chg": t["price_change_percentage_24h"],
                            "fdv": t.get("fully_diluted_valuation")},
                      "perp": 1.0, "asset": t, "btc": BTC})
    fe_all = run_js(cases)
    for sym, fe in zip(syms, fe_all):
        t, _ = _asset(sym)
        era, be_conv, sig, be_comp = nightly.score(t, {}, BTC)
        assert fe["conv"] == be_conv, f"{sym}: frontend {fe['conv']} != backend {be_conv}"
        assert fe["signal"] == sig, f"{sym}: signal fe={fe['signal']} be={sig}"
        for k in ("liquidity", "era", "depth", "momentum"):
            assert fe["comp"][k] == round(be_comp[k], 1), \
                f"{sym}: {k} fe={fe['comp'][k]} be={be_comp[k]}"
        assert abs(fe["comp"]["rsBlend"] - be_comp["rs_blend"]) < 1e-9, \
            f"{sym}: rsBlend fe={fe['comp']['rsBlend']} be={be_comp['rs_blend']}"


def check_parity_under_perp_overlay():
    """The LAVL overlay must agree. The frontend reads PERP[sym], which is the backend's
    lavl_perp_mult() output, so the multiplier is derived the same way on both sides."""
    pairs = [("SOL", -0.002), ("ETH", 0.002)]
    cases, mults = [], []
    for sym, fr in pairs:
        t, _ = _asset(sym)
        pm = nightly.lavl_perp_mult(sym, {sym: {"funding_rate": fr, "open_interest": 0.0}})
        mults.append(pm)
        cases.append({"t": {"vol": t["total_volume"], "mc": t["market_cap"],
                            "chg": t["price_change_percentage_24h"],
                            "fdv": t.get("fully_diluted_valuation")},
                      "perp": pm, "asset": t, "btc": BTC})
    fe_all = run_js(cases)
    for (sym, fr), pm, fe in zip(pairs, mults, fe_all):
        t, _ = _asset(sym)
        era, be_conv, sig, be_comp = nightly.score(
            t, {sym: {"funding_rate": fr, "open_interest": 0.0}}, BTC)
        assert fe["conv"] == be_conv, \
            f"{sym}@perp{pm}: frontend {fe['conv']} != backend {be_conv}"


def check_emission_drag_parity():
    """Module F must agree across the boundary, including where it declines to read.

    Three of these four cases exist because of a specific way a port can drift and still
    look right. A token with no FDV must score EXACTLY as it did before Module F existed
    — not "almost", not "within a point" — because the whole envelope is a 10% haircut
    and a JS `||0` in the wrong place would turn "unknown" into "fully circulating" and
    move the score by less than the eye catches on a board of fifty rows.
    """
    ladder = [
        ("no FDV published", None),        # unknown -> neutral 1.0 on both sides
        ("fully circulating", 1.00),       # inside the free band -> no drag
        ("2x float expansion", 2.00),
        ("11x float expansion", 11.00),    # deep in the tanh tail, near the cap
    ]
    base = dict(FIXTURE["SOL"])
    base["symbol"] = "SOL"
    cases, assets = [], []
    for _, mult in ladder:
        t = dict(base)
        if mult is not None:
            t["fully_diluted_valuation"] = t["market_cap"] * mult
        assets.append(t)
        cases.append({"t": {"vol": t["total_volume"], "mc": t["market_cap"],
                            "chg": t["price_change_percentage_24h"],
                            "fdv": t.get("fully_diluted_valuation")},
                      "perp": 1.0, "asset": t, "btc": BTC})
    fe_all = run_js(cases)
    for (label, _), t, fe in zip(ladder, assets, fe_all):
        era, be_conv, sig, be_comp = nightly.score(t, {}, BTC)
        assert fe["conv"] == be_conv, \
            f"{label}: frontend {fe['conv']} != backend {be_conv}"
        assert fe["comp"]["emission_mult"] == be_comp["emission_mult"], \
            (f"{label}: emission_mult fe={fe['comp']['emission_mult']} "
             f"be={be_comp['emission_mult']}")
        fe_drag = fe["comp"]["emission_drag"]
        be_drag = be_comp["emission_drag"]
        assert (fe_drag is None) == (be_drag is None), \
            (f"{label}: one side read the overhang and the other did not "
             f"(fe={fe_drag}, be={be_drag}) — unknown and zero are different readings")
        if be_drag is not None:
            assert abs(fe_drag - be_drag) < 1e-6, \
                f"{label}: drag fe={fe_drag} be={be_drag}"
    # And the reason the ladder starts where it does: absent FDV must be inert.
    plain = dict(base)
    _, conv_absent, _, _ = nightly.score(plain, {}, BTC)
    assert conv_absent == FROZEN_CONVICTION["SOL"], (
        "a token with no published FDV no longer scores what it scored before Module F "
        f"existed ({conv_absent} vs {FROZEN_CONVICTION['SOL']}) — the neutral path is "
        "not neutral")


def check_the_gate_reads_the_real_terminal():
    """A guard on the guard.

    If the markers vanish or the block stops containing the scoring functions, every
    parity assertion above would still pass — against nothing. That is the failure this
    whole rewrite exists to remove, so it is asserted rather than assumed.
    """
    port = extract_port()
    for fn in ("function conviction", "function liquidityFit", "function depthScore",
               "function signal", "function rsBlendOf",
               "function emissionDrag", "function emissionMult"):
        assert fn in port, f"{fn} is no longer inside the MODEL PORT markers"
    assert "document." not in port and "PERP[" not in port, \
        "the port block touches page state and can no longer be executed standalone"


# ---- frozen regression: the v2 multiplicative scoring engine must not drift ----
# Pinned to the v2 composition (Quality x Confirmation x RiskAdjustment).
# If a future edit changes these, the check fails and forces a conscious sign-off.
FROZEN_CONVICTION = {
    "ETH": 81,
    "SOL": 92,
    "ADA": 69,
    "LINK": 70,
}


def check_frozen_conviction_regression():
    """Replacing the scoring engine (RS refactor) — pin exact outputs.

    If a future edit changes these, the check fails and forces a conscious
    sign-off rather than an accidental model shift.
    """
    for sym, expected in FROZEN_CONVICTION.items():
        t, _ = _asset(sym)
        era, conv, sig, comp = nightly.score(t, {}, BTC)
        assert conv == expected, f"{sym}: expected {expected}, got {conv} (comp={comp})"


# ---- dual-mode entrypoint ----
def _run_all():
    failures = []
    for name, fn in [
        ("frontend/backend parity", check_frontend_backend_parity),
        ("parity under perp overlay", check_parity_under_perp_overlay),
        ("emission drag parity", check_emission_drag_parity),
        ("frozen conviction regression", check_frozen_conviction_regression),
        ("gate reads the real terminal", check_the_gate_reads_the_real_terminal),
    ]:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:  # noqa: BLE001 - report any unexpected error as a failure
            print(f"  ERROR {name}: {e}")
            failures.append(name)
    return failures


def _write_result(failures: list, total: int) -> None:
    """Record the outcome as an artifact the terminal can read.

    The diagnostic bar claims a parity status, and a hardcoded "PASS" there would be
    decorative — this project has already removed one such string. This is the real
    result of the run that gated the commit, written next to the ledger it gated.
    """
    ledger = os.path.join(_ROOT, "ledger")
    if not os.path.isdir(ledger):
        return
    payload = {"passed": total - len(failures), "total": total,
               "ok": not failures, "failed": failures,
               "spec_hash": nightly.SPEC_HASH,
               "node": bool(shutil.which("node"))}
    with open(os.path.join(ledger, "parity.json"), "w") as fh:
        json.dump(payload, fh, indent=1)


if __name__ == "__main__":
    print("Parity + regression check (standalone mode, no pytest required):")
    if shutil.which("node") is None:
        # Not a skip. The gate cannot verify the frontend without node, and reporting
        # success it did not establish is the failure mode this file exists to remove.
        print("  ERROR node is not available — the frontend port cannot be executed")
        _write_result(["node unavailable"], 5)
        sys.exit(1)
    failures = _run_all()
    _write_result(failures, 5)
    if failures:
        print(f"\nFAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("\nALL PARITY + REGRESSION CHECKS PASSED")
    sys.exit(0)
else:
    # pytest mode: expose the same logic as decorated test functions.
    import pytest  # only needed when run under pytest

    # A developer without node gets a skip; CI does not, because the standalone
    # entrypoint above treats a missing node as a failure. A parity gate reporting
    # success when it could not run is the same category of lie this file was
    # rewritten to remove.
    needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                    reason="node is required to execute the frontend port")

    @needs_node
    def test_frontend_backend_parity():
        check_frontend_backend_parity()

    @needs_node
    def test_parity_under_perp_overlay():
        check_parity_under_perp_overlay()

    def test_frozen_conviction_regression():
        check_frozen_conviction_regression()

    def test_the_gate_reads_the_real_terminal():
        check_the_gate_reads_the_real_terminal()
