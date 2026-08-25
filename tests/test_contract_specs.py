"""Contract-specification gate.

The funding parser used to compute notional as `contracts * price`. That identity holds
only for a contract whose unit is one unit of the underlying, and no Coinbase Derivatives
contract is one: nano Bitcoin is 0.01 BTC, nano Ether 0.1 ETH, Hedera 5,000 HBAR. Twelve
HBAR contracts at $0.0802 is $4,812 of notional; the parser returned $0.96, so the APR was
wrong by 5,000x and the regime badge misclassified with total confidence.

Two things have to stay true for the fix to keep meaning anything, and this file gates
both:

  1. The table inlined in index.html is the table in contract_specs.json. The terminal is
     a single self-contained file and cannot fetch the JSON without inventing a failure
     mode for the parser, so the literal is generated from it, which is only safe if a
     drift between the two is a test failure rather than a discovery six months later.
     Same reasoning as the MODEL PORT parity gate next door.

  2. An unknown symbol yields "multiplier unknown", never 1. Defaulting to 1 is the
     original bug, and it is the kind of bug that reappears as a convenience.

Runs under pytest, and standalone (`python tests/test_contract_specs.py`) for the nightly,
which must never depend on pytest being installed.
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
SPEC_FILE = os.path.join(_ROOT, "contract_specs.json")
MARKER_START = "CONTRACT SPECS"
MARKER_END = "END CONTRACT SPECS"


def load_file_specs() -> dict:
    """The source of truth, minus the commentary keys."""
    with open(SPEC_FILE, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def extract_inline_specs() -> dict:
    """Parse the literal back out of the terminal.

    Deliberately a parse rather than a substring match: what has to agree is the data,
    not the whitespace, and a formatting-sensitive gate is one that gets suppressed the
    first time someone runs a formatter over the file.
    """
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    start, end = html.find(MARKER_START), html.find(MARKER_END)
    assert start != -1 and end != -1, (
        f"could not find the {MARKER_START}/{MARKER_END} markers in index.html. The gate "
        "cannot verify a table it cannot locate")
    block = html[start:end]
    m = re.search(r"const\s+CONTRACT_SPECS\s*=\s*(\{.*?\});", block, re.S)
    assert m, "the CONTRACT SPECS block does not contain a `const CONTRACT_SPECS = {...};`"
    return json.loads(m.group(1))


def check_inline_table_matches_the_file():
    """The terminal's table IS contract_specs.json."""
    disk, inline = load_file_specs(), extract_inline_specs()
    assert inline == disk, (
        "index.html's CONTRACT_SPECS has drifted from contract_specs.json. Regenerate the "
        "inline literal from the file rather than editing it in place.")


def check_every_row_carries_its_provenance():
    """A multiplier with no source is a number someone remembered.

    The whole reason this file exists is that the original was hardcoded from nothing.
    Every row states where it came from and how far it is from having been read off the
    exchange, and `verification` is not allowed to claim 'exchange' without a date.
    """
    disk = load_file_specs()
    assert disk.get("as_of"), "contract_specs.json has no as_of date"
    assert disk.get("products"), "contract_specs.json lists no products"
    for p in disk["products"]:
        who = f"{p.get('symbol')}/{p.get('product')}"
        assert isinstance(p.get("multiplier"), (int, float)) and p["multiplier"] > 0, \
            f"{who}: multiplier must be a positive number"
        assert p.get("unit"), f"{who}: no unit recorded"
        assert p.get("sources"), f"{who}: no source recorded. An unsourced multiplier is a guess"
        assert p.get("verification") in ("secondary", "exchange"), \
            f"{who}: verification must be 'secondary' or 'exchange'"
        if p["verification"] == "exchange":
            assert p.get("verified_on"), \
                f"{who}: claims to be exchange-verified but records no date"


def check_hbar_notional_is_the_number_the_desk_would_recognise():
    """The acceptance case from the review, computed the way the terminal computes it.

    12 nano-sized HBAR contracts at $0.0802 is $4,812. The old parser said $0.96.
    """
    disk = load_file_specs()
    hbar = [p for p in disk["products"] if p["symbol"] == "HBAR"]
    assert hbar, "no HBAR contract in the table"
    mult = min(p["multiplier"] for p in hbar)
    assert mult == 5000, f"HBAR multiplier is {mult}, expected 5000"
    notional = 12 * mult * 0.0802
    assert abs(notional - 4812.0) < 1e-9, f"12 HBAR contracts at $0.0802 gave {notional}"
    # And the shape of the bug it replaces, stated as a value so a regression reads clearly.
    assert abs(12 * 0.0802 - 0.9624) < 1e-9
    assert notional / (12 * 0.0802) == 5000


def check_unknown_symbol_is_unknown_rather_than_one():
    """`selectedSpec()` returns null for an unlisted contract, and the parser refuses.

    Executed under node against the real terminal rather than a transcription, for the
    same reason the parity gate does: a transcription is a second implementation that
    agrees until someone edits one of them.
    """
    node = shutil.which("node")
    if node is None:
        return "SKIP: node not available"
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    start, end = html.find(MARKER_START), html.find(MARKER_END)
    block = html[start:end]
    m = re.search(r"(const\s+CONTRACT_SPECS\s*=\s*\{.*?\};)", block, re.S)
    # specsFor / smallestSpec sit just after the literal; pull them by name so the gate
    # exercises the terminal's own lookup rather than a reimplementation of it.
    tail = html[end:end + 4000]
    fns = re.search(r"(function specsFor\(sym\)\{.*?\}\s*function smallestSpec\(sym\)\{.*?\})",
                    tail, re.S)
    assert fns, "specsFor/smallestSpec are no longer directly after the CONTRACT SPECS block"
    driver = m.group(1) + "\n" + fns.group(1) + """
const out = {
  listed: smallestSpec("HBAR") ? smallestSpec("HBAR").multiplier : null,
  unlisted: smallestSpec("DOGE") ? smallestSpec("DOGE").multiplier : null,
  empty: smallestSpec("") ? 1 : null,
};
console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "specs.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(driver)
        res = subprocess.run([node, path], capture_output=True, text=True)
    assert res.returncode == 0, f"node failed: {res.stderr}"
    out = json.loads(res.stdout)
    assert out["listed"] == 5000, f"HBAR lookup returned {out['listed']}"
    assert out["unlisted"] is None, \
        "an unlisted symbol resolved to a multiplier. This is the original bug, which " \
        "was a silent default of 1"
    assert out["empty"] is None, "the empty symbol resolved to a contract"
    return None


_CHECKS = [
    check_inline_table_matches_the_file,
    check_every_row_carries_its_provenance,
    check_hbar_notional_is_the_number_the_desk_would_recognise,
    check_unknown_symbol_is_unknown_rather_than_one,
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
    print(f"contract specs: {len(_CHECKS) - len(failures)}/{len(_CHECKS)} passed")
    sys.exit(1 if failures else 0)
else:
    def test_inline_table_matches_the_file():
        check_inline_table_matches_the_file()

    def test_every_row_carries_its_provenance():
        check_every_row_carries_its_provenance()

    def test_hbar_notional():
        check_hbar_notional_is_the_number_the_desk_would_recognise()

    def test_unknown_symbol_is_unknown():
        check_unknown_symbol_is_unknown_rather_than_one()
