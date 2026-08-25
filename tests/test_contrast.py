"""Legibility gate for the colours that carry meaning.

The 24H delta column wrote inline `color:rgb(...)` from a continuous ramp, and the ramp
was biased. Measured against the page background #0B0F19, every negative value on the
board failed WCAG AA for 12px text - rgb(124,48,58) at 2.13:1, rgb(156,37,44) at 2.48:1,
against a floor of 4.5:1 - while large positives cleared it threefold, rgb(0,255,0) at
13.96:1. The interface faded losses and brightened gains. On a tool used to size risk that
is a thumb on the scale, and it is the kind of bias that is invisible to the person it
acts on, because nothing on screen says "this number was harder to read than that one".

A ramp is not a fixed set of colours, so eyeballing a few swatches proves nothing about
the ones in between. This walks the real function, extracted from index.html and executed
under node, across its whole domain and fails on the worst step.

It also pins the palette tokens, for the same reason: --tert was #475569 at 2.53:1 and was
colouring .muted, .note and every provenance caption on the board - which is to say the
reasoning that makes the thing worth reading was the least legible part of it.

Runs under pytest, and standalone for a nightly that must not depend on pytest.
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

# WCAG 2.1 AA, normal-size text. The board sets the delta column at 12px.
AA_NORMAL = 4.5
BG = "#0B0F19"

# Tokens that colour text a reader is expected to act on. --grid and --border are
# deliberately absent: they are hairlines, and a hairline is not text.
MEANING_TOKENS = ("--txt", "--sec", "--tert", "--green", "--red", "--amber")


def _srgb_to_lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb) -> float:
    r, g, b = (_srgb_to_lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _hex(s: str):
    s = s.strip().lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def read_tokens() -> dict:
    """The :root custom properties, as declared."""
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    return {m.group(1): m.group(2)
            for m in re.finditer(r"(--[a-z]+)\s*:\s*(#[0-9A-Fa-f]{6})", html)}


def check_palette_tokens_are_legible():
    tokens = read_tokens()
    bg = _hex(tokens.get("--bg", BG))
    missing = [t for t in MEANING_TOKENS if t not in tokens]
    assert not missing, f"palette tokens not found in index.html: {missing}"
    failures = []
    for name in MEANING_TOKENS:
        ratio = contrast(_hex(tokens[name]), bg)
        if ratio < AA_NORMAL:
            failures.append(f"{name} {tokens[name]} is {ratio:.2f}:1 against {tokens.get('--bg', BG)}")
    assert not failures, (
        "palette tokens below WCAG AA for normal text (4.5:1): " + "; ".join(failures))


def check_the_delta_ramp_never_drops_below_aa():
    """Walk the real deltaColor across its domain.

    Extracted from the terminal and run under node rather than transcribed, for the same
    reason the parity gate does it: a transcription is a second implementation that
    agrees right up until somebody edits one of them.
    """
    node = shutil.which("node")
    if node is None:
        return "SKIP: node not available, the ramp itself was not walked"
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    m = re.search(r"(const _RAMP_NEU=.*?function deltaColor\(pct\)\{.*?\n\})", html, re.S)
    assert m, "deltaColor and its ramp constants are no longer in the expected shape"
    driver = m.group(1) + """
const out = [];
for (let i = -400; i <= 400; i++) out.push(deltaColor(i / 10));
console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "ramp.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(driver)
        res = subprocess.run([node, path], capture_output=True, text=True)
    assert res.returncode == 0, f"node failed: {res.stderr}"
    colors = json.loads(res.stdout)

    tokens = read_tokens()
    bg = _hex(tokens.get("--bg", BG))
    worst, worst_col = 99.0, None
    for c in colors:
        rgb = tuple(int(x) for x in re.findall(r"\d+", c))
        ratio = contrast(rgb, bg)
        if ratio < worst:
            worst, worst_col = ratio, c
    assert worst >= AA_NORMAL, (
        f"the 24H delta ramp drops to {worst:.2f}:1 at {worst_col}, below the {AA_NORMAL}:1 "
        f"AA floor for 12px text. This is the bias the ramp was rebuilt to remove.")

    # Lightness roughly constant, chroma doing the work. Without this the gate passes a
    # ramp that clears the floor by getting steadily brighter with magnitude, which is
    # the original failure in a form that happens to be legible.
    ratios = []
    for c in colors:
        rgb = tuple(int(x) for x in re.findall(r"\d+", c))
        ratios.append(contrast(rgb, bg))
    spread = max(ratios) / min(ratios)
    assert spread < 2.0, (
        f"contrast across the ramp spans {spread:.2f}x (worst {min(ratios):.2f}:1, best "
        f"{max(ratios):.2f}:1). Magnitude is being encoded as brightness again. Vary "
        f"chroma and let the .dbar carry magnitude.")
    return None


def check_magnitude_is_not_carried_by_colour_alone():
    """Red and green alone fails for a red-green colour vision deficiency.

    The bar was already in the markup; the sign glyph is what makes the column readable
    without separating the two hues at all.
    """
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    assert "function deltaGlyph" in html, "no sign glyph: the delta column is hue-only"
    assert 'class="dsign"' in html, "the sign glyph is not rendered in the delta cell"
    assert 'class="dbar"' in html, "the magnitude bar is gone from the delta cell"


def check_reduced_motion_is_honoured():
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    assert "prefers-reduced-motion" in html, (
        "no prefers-reduced-motion block: the live dot pulses indefinitely and the drawer "
        "and every bar animate")


def check_the_off_palette_green_is_gone():
    """#00FF66 was hardcoded in a dozen places and is in no token."""
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    assert "00FF66" not in html.upper(), "#00FF66 is back; --green is the token"
    assert "0,255,102" not in html.replace(" ", ""), "rgba(0,255,102,...) is back"


_CHECKS = [
    check_palette_tokens_are_legible,
    check_the_delta_ramp_never_drops_below_aa,
    check_magnitude_is_not_carried_by_colour_alone,
    check_reduced_motion_is_honoured,
    check_the_off_palette_green_is_gone,
]


def _run_all():
    failures, notes = [], []
    for fn in _CHECKS:
        try:
            note = fn()
            if note:
                notes.append(note)
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
    return failures, notes


if __name__ == "__main__":
    failures, notes = _run_all()
    for n in notes:
        print(n)
    for f in failures:
        print("FAIL " + f)
    print(f"contrast: {len(_CHECKS) - len(failures)}/{len(_CHECKS)} passed")
    sys.exit(1 if failures else 0)
else:
    def test_palette_tokens_are_legible():
        check_palette_tokens_are_legible()

    def test_delta_ramp_meets_aa():
        check_the_delta_ramp_never_drops_below_aa()

    def test_magnitude_not_colour_alone():
        check_magnitude_is_not_carried_by_colour_alone()

    def test_reduced_motion():
        check_reduced_motion_is_honoured()

    def test_off_palette_green_gone():
        check_the_off_palette_green_is_gone()
