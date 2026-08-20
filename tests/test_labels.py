"""The chart label engine, executed as the real terminal ships it.

Same approach the parity gate uses on the scoring block, and for the same reason: a
hand-written transcription of the placement logic is a second implementation that
happens to agree, and editing the JS alone would leave this green while the charts drift.
The block between the LABEL ENGINE markers in index.html is extracted and run under node.

The properties this file exists to hold:

  * EVERY POINT IS PLOTTED. Only the labels are budgeted. A test that let the engine
    drop points to relieve crowding would be enforcing the opposite of the requirement.
  * No two placed labels overlap. Ever, on any input.
  * No placed label leaves the plot rect. A label clipped by the canvas edge is the
    defect that got the first screenshots rejected.
  * Placement is deterministic. The same board renders the same labels in the same
    positions, so a screenshot is reproducible and a diff means something changed.
  * Nothing is solved by shrinking the type. The engine never sees a font size; the
    caller measures, and the engine's only lever is whether to place a label at all.

Runs two ways, matching the parity gate:
  * python -m pytest tests/test_labels.py   (skips without node)
  * python tests/test_labels.py             (standalone; a missing node is a FAILURE,
                                             not a pass)
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
MARKER_START = "LABEL ENGINE"
MARKER_END = "END LABEL ENGINE"


def extract_engine() -> str:
    """Pull the delimited label-placement block out of the terminal's inline script."""
    html = open(TERMINAL, encoding="utf-8").read()
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert script, "index.html has no inline <script> block"
    body = script.group(1)
    start, end = body.find(MARKER_START), body.find(MARKER_END)
    assert start != -1 and end != -1, (
        f"could not find the {MARKER_START}/{MARKER_END} markers in index.html — "
        "the label tests cannot verify an engine they cannot locate")
    open_close = body.find("*/", start)
    assert open_close != -1, "the opening LABEL ENGINE marker is not inside a /* */ comment"
    close_open = body.rfind("/*", start, end)
    assert close_open != -1, "the END LABEL ENGINE marker is not inside a /* */ comment"
    block = body[open_close + 2:close_open]
    assert "function placeLabels" in block, \
        "the extracted block does not contain placeLabels"
    return block


def run_js(driver_body: str):
    node = shutil.which("node")
    assert node, "node is required to execute the label engine"
    src = extract_engine() + "\n" + driver_body
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        res = subprocess.run([node, path], capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            raise AssertionError(f"node failed running the label engine: {res.stderr.strip()}")
        return json.loads(res.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


# A monospace-ish measurer: 8px type, ~4.8px per character. Close enough to the real
# metrics that the geometry is exercised, and fixed so the test is deterministic.
MEASURE = "const measure = t => ({w: t.length * 4.8, h: 8});"

# Three adversarial boards. The pathological one is the point: 240 names stacked in a
# tiny area is exactly what quad() faced with 234 rows, and is what produced the smear.
BOARDS = f"""
function board(n, spread, seedMul) {{
  // Deterministic pseudo-points. No Math.random — a placement test that cannot be
  // reproduced is not a regression test.
  const out = [];
  for (let i = 0; i < n; i++) {{
    const a = (i * seedMul) % 97 / 97, b = (i * 31 % 89) / 89;
    out.push({{
      text: "SYM" + i,
      x: 40 + a * spread,
      y: 20 + b * spread,
      r: 3 + (i % 5),
      priority: i % 7 === 0 ? 80 : i % 3 === 0 ? 60 : 40,
    }});
  }}
  return out;
}}
const RECT = {{l: 20, t: 10, r: 380, b: 220}};
{MEASURE}
"""


def _overlaps(a, b, pad=2):
    return not (a["x1"] + pad <= b["x0"] or b["x1"] + pad <= a["x0"] or
                a["y1"] + pad <= b["y0"] or b["y1"] + pad <= a["y0"])


def _check_placement(res, rect, budget):
    placed = res["placed"]
    assert len(placed) <= budget, f"budget of {budget} exceeded: {len(placed)} labels"
    for L in placed:
        assert L["x0"] >= rect["l"] - 1e-9, f"{L['text']} escapes left"
        assert L["x1"] <= rect["r"] + 1e-9, f"{L['text']} escapes right"
        assert L["y0"] >= rect["t"] - 1e-9, f"{L['text']} escapes top"
        assert L["y1"] <= rect["b"] + 1e-9, f"{L['text']} escapes bottom"
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            assert not _overlaps(a, b), f"{a['text']} overlaps {b['text']}"


def check_a_crowded_board_never_overlaps():
    """240 points in a small area — the case that produced the rejected screenshots."""
    rect = {"l": 20, "t": 10, "r": 380, "b": 220}
    res = run_js(BOARDS + """
const out = placeLabels(board(240, 120, 7), RECT, measure, 12, []);
console.log(JSON.stringify(out));
""")
    _check_placement(res, rect, 12)
    assert res["placed"], "a crowded board placed no labels at all"


def check_a_sparse_board_labels_up_to_its_budget():
    rect = {"l": 20, "t": 10, "r": 380, "b": 220}
    res = run_js(BOARDS + """
const out = placeLabels(board(14, 300, 11), RECT, measure, 12, []);
console.log(JSON.stringify(out));
""")
    _check_placement(res, rect, 12)
    assert len(res["placed"]) >= 8, \
        f"a sparse board placed only {len(res['placed'])} of a 12 budget"


def check_points_on_the_boundary_are_labelled_inward_or_not_at_all():
    """The clipping defect, isolated: points pressed against every edge.

    A label must flip to an anchor that keeps it inside, or be skipped. It must never be
    drawn half off the canvas, which is what a fixed +4/-4 offset does in a corner.
    """
    rect = {"l": 20, "t": 10, "r": 380, "b": 220}
    res = run_js(BOARDS + """
const edge = [
  {text:"TOPLEFT",  x:20,  y:10,  r:5, priority:80},
  {text:"TOPRIGHT", x:380, y:10,  r:5, priority:80},
  {text:"BOTLEFT",  x:20,  y:220, r:5, priority:80},
  {text:"BOTRIGHT", x:380, y:220, r:5, priority:80},
  {text:"MIDLEFT",  x:20,  y:115, r:5, priority:80},
  {text:"MIDRIGHT", x:380, y:115, r:5, priority:80},
];
const out = placeLabels(edge, RECT, measure, 12, []);
console.log(JSON.stringify(out));
""")
    _check_placement(res, rect, 12)


def check_reserved_regions_are_never_written_over():
    """Quadrant names and axis titles are placed first and handed in as reserved.

    The old charts drew them LAST at fixed coordinates, straight through whatever
    tickers were already there.
    """
    rect = {"l": 20, "t": 10, "r": 380, "b": 220}
    res = run_js(BOARDS + """
const reserved = [{x0:20,y0:10,x1:180,y1:24},{x0:250,y0:200,x1:380,y1:216}];
const out = placeLabels(board(200, 160, 7), RECT, measure, 12, reserved);
console.log(JSON.stringify({placed: out.placed, reserved}));
""")
    _check_placement({"placed": res["placed"]}, rect, 12)
    for L in res["placed"]:
        for rsv in res["reserved"]:
            assert not _overlaps(L, rsv), f"{L['text']} was drawn over a reserved region"


def check_placement_is_deterministic():
    """Same input, same output — twice in one process and twice across processes."""
    driver = BOARDS + """
const a = placeLabels(board(120, 140, 7), RECT, measure, 12, []);
const b = placeLabels(board(120, 140, 7), RECT, measure, 12, []);
console.log(JSON.stringify({a: a.placed, b: b.placed}));
"""
    first = run_js(driver)
    assert first["a"] == first["b"], "two calls in one process disagreed"
    second = run_js(driver)
    assert first["a"] == second["a"], "two separate node processes disagreed"


def check_priority_decides_who_gets_the_label():
    """When the budget binds, the selected and qualified names must win it."""
    res = run_js(BOARDS + """
const pts = board(200, 100, 7);
pts.push({text:"SELECTED", x:200, y:120, r:4, priority:100});
const out = placeLabels(pts, RECT, measure, 6, []);
console.log(JSON.stringify(out));
""")
    texts = [L["text"] for L in res["placed"]]
    assert "SELECTED" in texts, "the selected name lost its label to a lower priority"
    assert len(res["placed"]) <= 6


def check_the_engine_never_changes_the_font():
    """The requirement was explicit: do not solve overlap by shrinking the type.

    The engine is handed a measure function and never sees a font, so it has no lever to
    shrink one. Asserted against the source so a future edit cannot quietly add one.
    """
    block = extract_engine()
    for banned in ("font", "px monospace", "measureText"):
        assert banned not in block, \
            f"the label engine references {banned!r} — it must stay measurement-agnostic"


def check_the_engine_is_free_of_page_state():
    block = extract_engine()
    for banned in ("document.", "window.", "STATE", "$(\"#"):
        assert banned not in block, \
            f"the label engine touches {banned!r} and can no longer be executed standalone"


# ---- dual-mode entrypoint ----
_CHECKS = [
    ("a crowded board never overlaps", check_a_crowded_board_never_overlaps),
    ("a sparse board fills its budget", check_a_sparse_board_labels_up_to_its_budget),
    ("boundary points stay inside", check_points_on_the_boundary_are_labelled_inward_or_not_at_all),
    ("reserved regions are respected", check_reserved_regions_are_never_written_over),
    ("placement is deterministic", check_placement_is_deterministic),
    ("priority wins a binding budget", check_priority_decides_who_gets_the_label),
    ("the engine never changes the font", check_the_engine_never_changes_the_font),
    ("the engine is free of page state", check_the_engine_is_free_of_page_state),
]


def _pytest_wrappers():
    import pytest

    @pytest.mark.parametrize("name,fn", _CHECKS, ids=[c[0] for c in _CHECKS])
    def test_label_engine(name, fn):
        if shutil.which("node") is None:
            pytest.skip("node is not available")
        fn()

    return test_label_engine


test_label_engine = _pytest_wrappers()


if __name__ == "__main__":
    print("Label engine checks (standalone mode, no pytest required):")
    if shutil.which("node") is None:
        print("  ERROR node is not available — the label engine cannot be executed")
        sys.exit(1)
    failures = []
    for name, fn in _CHECKS:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            print(f"  FAIL  {name}: {e}")
            failures.append(name)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {name}: {e}")
            failures.append(name)
    if failures:
        print(f"\nFAILED: {len(failures)} check(s): {failures}")
        sys.exit(1)
    print("\nALL LABEL ENGINE CHECKS PASSED")
