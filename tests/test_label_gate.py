"""The label gate, executed as the terminal ships it.

AUDIT-2026-09 1.2. The board printed STRONG / BUY / AVOID unconditionally while the
Selection Edge panel published the measurement those words imply. For most of this
ledger's life that measurement could not be distinguished from zero; on 2026-09-15 it
stopped spanning zero on the NEGATIVE side. Imperative labels over evidence pointing the
other way is the single loudest thing this terminal could get wrong, so the rule is:

    labelsAreActionable = edge.measurable && edge.ci[0] > 0

Same approach as the parity gate and the label engine, for the same reason: the block
between the LABEL GATE markers is extracted from index.html and run under node, so a
hand-written transcription cannot quietly agree with a page that has drifted.

The four states are driven from fixed edge blocks, not from the live ledger. A gate
tested against today's numbers is the snapshot tripwire this repository has already had
to remove twice from tests/test_edge.py.

Runs two ways, matching the other JS gates:
  * python -m pytest tests/test_label_gate.py   (skips without node)
  * python tests/test_label_gate.py             (standalone; a missing node FAILS)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
TERMINAL = os.path.join(_ROOT, "index.html")
MARKER_START = "LABEL GATE"
MARKER_END = "END LABEL GATE"

# The four states, and nothing about tonight. Each is a complete edge block of the shape
# _compute_edge() publishes, chosen to sit unambiguously inside one branch.
CASES = {
    "measurable_positive": {"measurable": True,  "ci": [0.02, 0.08],
                            "mean_ic": 0.05, "legs": 60, "min_legs": 40},
    "measurable_inverted": {"measurable": True,  "ci": [-0.1048, -0.0012],
                            "mean_ic": -0.053, "legs": 41, "min_legs": 40},
    "spans_zero":          {"measurable": False, "ci": [-0.1034, 0.0024],
                            "mean_ic": -0.0505, "legs": 40, "min_legs": 40},
    "insufficient":        {"measurable": False, "ci": None,
                            "mean_ic": None, "legs": 12, "min_legs": 40},
    # Degenerate, and it must fail CLOSED rather than throw: the artifact can be absent.
    "no_edge":             None,
}
TIERS = ["STRONG", "BUY", "HOLD", "WATCH", "AVOID"]


def extract_gate() -> str:
    html = open(TERMINAL, encoding="utf-8").read()
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert script, "index.html has no inline <script> block"
    body = script.group(1)
    start, end = body.find(MARKER_START), body.find(MARKER_END)
    assert start != -1 and end != -1, (
        f"could not find the {MARKER_START}/{MARKER_END} markers in index.html — "
        f"the gate moved and this test is no longer reading it")
    # Back up to the comment opener so the extracted text is valid JavaScript.
    start = body.rfind("/*", 0, start)
    return body[start:end + len(MARKER_END) + 3]


def run_gate() -> dict:
    """Evaluate every case under node and return the results as plain data."""
    node = shutil.which("node")
    if not node:
        return {}
    harness = extract_gate() + """
const CASES = %s, TIERS = %s;
const out = {};
for (const [name, edge] of Object.entries(CASES)){
  const g = labelGateState(edge);
  out[name] = {actionable: g.actionable, state: g.state, reason: g.reason,
               stats: g.stats, detail: g.detail,
               labels: TIERS.map(t => tierLabel([t, "g"], edge)),
               titles: TIERS.map(t => tierTitle([t, "g"], edge))};
}
console.log(JSON.stringify(out));
""" % (json.dumps(CASES), json.dumps(TIERS))
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "gate.js")
        open(path, "w", encoding="utf-8").write(harness)
        res = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, f"node failed:\n{res.stderr[-2000:]}"
    return json.loads(res.stdout)


def _cases():
    got = run_gate()
    if not got:
        import pytest
        pytest.skip("node is not available")
    return got


# --------------------------------------------------------------- the four states
def test_a_measurable_positive_interval_opens_the_gate():
    """The only state in which the board may tell anyone to do anything."""
    g = _cases()["measurable_positive"]
    assert g["actionable"] is True
    assert g["state"] == "measurable-positive"
    assert g["labels"] == TIERS, "the imperative vocabulary must survive unchanged"
    assert "actionable" in g["detail"]


def test_a_measurable_inverted_interval_withdraws_rather_than_reverses():
    """Today's state, and the one worth stating precisely.

    An interval entirely below zero is a finding about the ranking over forty-one nights
    of one-day returns. It is not a licence to print the labels backwards — that would be
    acting on the same thin evidence in the other direction — so the gate withdraws the
    vocabulary and says why.
    """
    g = _cases()["measurable_inverted"]
    assert g["actionable"] is False
    assert g["state"] == "measurable-inverted"
    assert g["labels"] == ["TIER 1", "TIER 2", "TIER 3", "TIER 4", "TIER 5"]
    # Not inverted: the top of the scale still reads as the top of the scale.
    assert g["labels"][0] != g["labels"][-1]
    assert "INVERTED" in g["reason"]
    assert "withdrawn rather than reversed" in g["detail"]


def test_an_interval_spanning_zero_closes_the_gate():
    g = _cases()["spans_zero"]
    assert g["actionable"] is False
    assert g["state"] == "spans-zero"
    assert g["labels"] == ["TIER 1", "TIER 2", "TIER 3", "TIER 4", "TIER 5"]
    assert "not distinguishable from zero" in g["reason"]


def test_insufficient_history_closes_the_gate():
    g = _cases()["insufficient"]
    assert g["actionable"] is False
    assert g["state"] == "insufficient"
    assert g["labels"] == ["TIER 1", "TIER 2", "TIER 3", "TIER 4", "TIER 5"]
    assert "not yet measurable" in g["reason"]
    assert "12 of 40 legs" in g["stats"], "the shortfall must be stated, not just the verdict"


def test_a_missing_edge_fails_closed():
    """An absent artifact must not print an imperative, and must not throw."""
    g = _cases()["no_edge"]
    assert g["actionable"] is False
    assert g["labels"] == ["TIER 1", "TIER 2", "TIER 3", "TIER 4", "TIER 5"]


# --------------------------------------------------------------- properties
def test_only_a_positive_lower_bound_opens_the_gate():
    """One-sided by construction: exactly one of the five states is actionable."""
    got = _cases()
    assert [k for k, v in got.items() if v["actionable"]] == ["measurable_positive"]


def test_the_ordering_of_the_scale_is_preserved_in_every_state():
    """Scores, cuts and ordering do not move — only what the tiers are called."""
    for name, g in _cases().items():
        assert len(set(g["labels"])) == 5, f"{name}: tiers collapsed onto each other"
        if not g["actionable"]:
            ranks = [int(l.split()[1]) for l in g["labels"]]
            assert ranks == [1, 2, 3, 4, 5], f"{name}: rank bands are out of order"


def test_no_closed_state_emits_action_vocabulary():
    """The point of the whole gate."""
    for name, g in _cases().items():
        if g["actionable"]:
            continue
        for word in ("STRONG", "BUY", "AVOID", "WATCH", "HOLD"):
            assert word not in g["labels"], f"{name}: {word} survived the gate"


def test_every_state_states_its_measurement():
    """Horizon, legs, IC and interval — a caveat with no numbers is a mood."""
    for name, g in _cases().items():
        assert "1-day" in g["reason"], f"{name}: the horizon is not named"
        assert "legs" in g["stats"], f"{name}: the sample size is not stated"
        if name not in ("insufficient", "no_edge"):
            assert "IC " in g["stats"] and "95% CI" in g["stats"], f"{name}: no interval"


def test_a_closed_band_says_which_slice_of_the_scale_it_is():
    """With the verb withdrawn, the band name alone does not carry the range."""
    g = _cases()["measurable_inverted"]
    assert "80-100" in g["titles"][0] and "STRONG" in g["titles"][0]
    assert "0-39" in g["titles"][-1]


if __name__ == "__main__":
    if not shutil.which("node"):
        print("FAIL  node is required for the standalone label-gate run")
        sys.exit(1)
    got = run_gate()
    import types
    mod = sys.modules[__name__]
    failures = 0
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        try:
            getattr(mod, name)()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(("FAIL" if failures else "PASS") + f"  label gate ({failures} failure(s))")
    sys.exit(1 if failures else 0)
