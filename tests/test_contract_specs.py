"""Contract-specification gate.

The funding parser used to compute notional as `contracts * price`. That identity holds
only for a contract whose unit is one unit of the underlying, and no Coinbase Derivatives
contract is one. Twelve HEP contracts at $0.0802 is $4,812 of notional; the parser
returned $0.96, so the APR was wrong by 5,000x and the regime badge misclassified with
total confidence.

Four things have to stay true for the fix to keep meaning anything, and this file gates
all four:

  1. The table inlined in index.html is the table in contract_specs.json. The terminal is
     a single self-contained file and cannot fetch the JSON without inventing a failure
     mode for the parser, so the literal is generated from it, which is only safe if a
     drift between the two is a test failure rather than a discovery six months later.

  2. A perpetual is not a dated future. Only a perpetual-style contract has funding, so
     only a perpetual may appear in a panel that recovers a funding rate. ET and ETP are
     both 0.1 ETH and only one of them has a rate to recover, which is exactly why the
     multiplier alone cannot be the thing being checked.

  3. An unmapped symbol yields "no verified contract mapping", never 1 and never a claim
     about what Coinbase lists. Defaulting to 1 is the original bug, and 1 is a real
     contract size here (BTI is 1 BTC), which is what made it dangerous rather than
     merely wrong.

  4. A multiplier the reader typed marks everything derived from it as unverified.

Runs under pytest, and standalone (`python tests/test_contract_specs.py`) for the
nightly, which must never depend on pytest being installed.
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

# Every field a row must carry. Nulls are not allowed: `instrument_type` and
# `funding_bearing` are what keep a dated future out of the funding parser, and a null
# contract code is a row that cannot be re-verified against the exchange at all.
INTENDED_FIELDS = ("code", "symbol", "product", "instrument_type", "funding_bearing",
                   "multiplier", "unit", "active_as_of", "verification", "verified_on")


def load_file_specs() -> dict:
    with open(SPEC_FILE, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def terminal_html() -> str:
    with open(TERMINAL, encoding="utf-8") as fh:
        return fh.read()


def extract_inline_specs() -> dict:
    """Parse the literal back out of the terminal.

    Deliberately a parse rather than a substring match: what has to agree is the data,
    not the whitespace, and a formatting-sensitive gate is one that gets suppressed the
    first time somebody runs a formatter over the file.
    """
    html = terminal_html()
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
        "index.html's CONTRACT_SPECS has drifted from contract_specs.json. Run "
        "scripts/sync_contract_specs.py rather than editing the inline literal.")


def check_every_row_carries_its_provenance():
    """A specification with no source is a number someone remembered."""
    disk = load_file_specs()
    assert disk.get("as_of"), "contract_specs.json has no as_of date"
    assert disk.get("products"), "contract_specs.json lists no products"
    assert disk.get("source_documents"), "no source documents recorded"
    codes = set()
    for p in disk["products"]:
        who = f"{p.get('code')}/{p.get('product')}"
        for field in INTENDED_FIELDS:
            assert p.get(field) not in (None, ""), f"{who}: {field} is null or missing"
        assert p["code"] not in codes, f"duplicate contract code {p['code']}"
        codes.add(p["code"])
        assert isinstance(p["multiplier"], (int, float)) and p["multiplier"] > 0, \
            f"{who}: multiplier must be a positive number"
        assert p["instrument_type"] in ("perpetual", "dated"), \
            f"{who}: instrument_type must be 'perpetual' or 'dated', not {p['instrument_type']!r}"
        assert isinstance(p["funding_bearing"], bool), \
            f"{who}: funding_bearing must be an explicit boolean"
        assert p.get("sources"), f"{who}: no source recorded. An unsourced multiplier is a guess"
        assert p["verification"] in ("primary", "secondary", "unverified"), \
            f"{who}: unknown verification tier {p['verification']!r}"


def check_perpetuals_and_dated_futures_are_distinguished():
    """The distinction the funding parser depends on.

    Only a perpetual-style contract has funding. A dated future expires and pays none, so
    recovering an APR from one produces a confident number for a cash flow the instrument
    cannot generate.
    """
    by_code = {p["code"]: p for p in load_file_specs()["products"]}
    for code in ("BIP", "ETP", "SLP", "XPP", "HEP"):
        assert code in by_code, f"{code} is not in the table"
        assert by_code[code]["instrument_type"] == "perpetual", f"{code} is not marked perpetual"
        assert by_code[code]["funding_bearing"] is True, f"{code} is not marked funding-bearing"
    for code in ("BIT", "ET", "SOL", "HED"):
        assert code in by_code, f"{code} is not in the table"
        assert by_code[code]["instrument_type"] == "dated", f"{code} is not marked dated"
        assert by_code[code]["funding_bearing"] is False, \
            f"{code} is a dated future and must not be marked funding-bearing"
    # The trap itself, pinned so it cannot be quietly collapsed: identical size, and only
    # one of the two has a funding rate.
    assert by_code["ET"]["multiplier"] == by_code["ETP"]["multiplier"] == 0.1
    assert by_code["ET"]["funding_bearing"] != by_code["ETP"]["funding_bearing"]
    assert by_code["HED"]["multiplier"] == by_code["HEP"]["multiplier"] == 5000
    assert by_code["HED"]["funding_bearing"] != by_code["HEP"]["funding_bearing"]


def check_the_terminal_gates_the_parser_on_funding():
    """Enforced in code, not by convention."""
    html = terminal_html()
    fn = html[html.index("function parserSpecs()"):]
    fn = fn[:fn.index("\n}")]
    assert 'instrument_type === "perpetual"' in fn, "the parser does not filter on instrument type"
    assert "funding_bearing === true" in fn, "the parser does not filter on funding"
    assert 'verification === "primary"' in fn, "the parser accepts unverified rows"


def check_the_wording_never_claims_the_exchange_has_no_contract():
    """This table is a dated snapshot, not a census of the venue.

    Coinbase lists contracts on assets absent from it, so "spot only" and "no listed
    contract" assert something about the exchange's product list that this repository has
    not established, out of a gap in a local file.
    """
    html = terminal_html()
    for banned in ('"spot only', ">spot only", "spot only ·", "no listed contract"):
        assert banned not in html, (
            f"{banned!r} appears in the terminal: that states a fact about Coinbase's "
            "product list, when the only fact available is that this table lacks a row")
    assert "no verified contract mapping" in html, \
        "the sizer no longer states the honest version of an unmapped symbol"
    assert "Contract specification not mapped" in html, \
        "the parser no longer states the honest version of an unmapped contract"


def check_a_user_entered_multiplier_taints_every_derived_figure():
    html = terminal_html()
    assert "USER-ENTERED MULTIPLIER" in html, \
        "a hand-entered multiplier produces figures that are not marked unverified"
    assert "pp-unverified" in html, "no styling distinguishes an unverified derivation"


def check_hep_notional_is_the_number_the_desk_would_recognise():
    """The acceptance case, on HEP: the funding-bearing Hedera perpetual."""
    disk = load_file_specs()
    hep = next((p for p in disk["products"] if p["code"] == "HEP"), None)
    assert hep, "HEP is not in the table"
    assert hep["funding_bearing"] is True and hep["instrument_type"] == "perpetual"
    assert hep["multiplier"] == 5000, f"HEP multiplier is {hep['multiplier']}, expected 5000"
    notional = 12 * hep["multiplier"] * 0.0802
    assert abs(notional - 4812.0) < 1e-9, f"12 HEP contracts at $0.0802 gave {notional}"
    # And the shape of the bug it replaces, stated as a value so a regression reads clearly.
    assert abs(12 * 0.0802 - 0.9624) < 1e-9
    assert notional / (12 * 0.0802) == 5000


def check_the_lookups_behave(_cache={}):
    """Run the terminal's own selection logic under node.

    Executed against the real file rather than a transcription, for the same reason the
    parity gate does it: a transcription is a second implementation that agrees right up
    until somebody edits one of them.
    """
    node = shutil.which("node")
    if node is None:
        return "SKIP: node not available, the terminal's lookups were not executed"
    html = terminal_html()
    start, end = html.find(MARKER_START), html.find(MARKER_END)
    lit = re.search(r"(const\s+CONTRACT_SPECS\s*=\s*\{.*?\};)", html[start:end], re.S)
    assert lit, "the CONTRACT SPECS literal is gone"
    tail = html[end:end + 8000]
    fns = re.search(r"(function parserSpecs\(\)\{.*?\n\})", tail, re.S)
    szf = re.search(r"(function sizingSpecs\(sym\)\{.*?\n\})", tail, re.S)
    sml = re.search(r"(function smallestSpec\(sym\)\{.*?\})", tail, re.S)
    assert fns and szf and sml, \
        "parserSpecs/sizingSpecs/smallestSpec are no longer after the CONTRACT SPECS block"
    driver = "\n".join([lit.group(1), fns.group(1), szf.group(1), sml.group(1), """
const ps = parserSpecs();
console.log(JSON.stringify({
  parserCodes: ps.map(p => p.code).sort(),
  parserHasDated: ps.some(p => p.instrument_type !== "perpetual"),
  parserHasUnfunded: ps.some(p => p.funding_bearing !== true),
  parserHasUnverified: ps.some(p => p.verification !== "primary"),
  hbar: smallestSpec("HBAR") ? smallestSpec("HBAR").multiplier : null,
  unmapped: smallestSpec("AAVE") ? smallestSpec("AAVE").multiplier : null,
  empty: smallestSpec("") ? 1 : null,
}));
"""])
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "specs.js")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(driver)
        res = subprocess.run([node, path], capture_output=True, text=True)
    assert res.returncode == 0, f"node failed: {res.stderr}"
    out = json.loads(res.stdout)

    assert out["hbar"] == 5000, f"HBAR lookup returned {out['hbar']}"
    assert out["unmapped"] is None, \
        "a symbol with no row resolved to a multiplier. This is the original bug, which " \
        "was a silent default of 1"
    assert out["empty"] is None, "the empty symbol resolved to a contract"
    assert not out["parserHasDated"], \
        f"a dated future is selectable in the funding parser: {out['parserCodes']}"
    assert not out["parserHasUnfunded"], \
        f"a contract with no funding is selectable in the funding parser: {out['parserCodes']}"
    assert not out["parserHasUnverified"], \
        f"an unverified row is selectable in the funding parser: {out['parserCodes']}"
    assert "ET" not in out["parserCodes"], \
        "the dated nano Ether future ET is selectable in the funding parser"
    assert "ETP" in out["parserCodes"], "the Ether perpetual ETP is not offered"
    return None


_CHECKS = [
    check_inline_table_matches_the_file,
    check_every_row_carries_its_provenance,
    check_perpetuals_and_dated_futures_are_distinguished,
    check_the_terminal_gates_the_parser_on_funding,
    check_the_wording_never_claims_the_exchange_has_no_contract,
    check_a_user_entered_multiplier_taints_every_derived_figure,
    check_hep_notional_is_the_number_the_desk_would_recognise,
    check_the_lookups_behave,
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

    def test_perpetuals_distinguished_from_dated():
        check_perpetuals_and_dated_futures_are_distinguished()

    def test_parser_gated_on_funding():
        check_the_terminal_gates_the_parser_on_funding()

    def test_wording_does_not_overclaim():
        check_the_wording_never_claims_the_exchange_has_no_contract()

    def test_user_entered_multiplier_is_marked():
        check_a_user_entered_multiplier_taints_every_derived_figure()

    def test_hep_notional():
        check_hep_notional_is_the_number_the_desk_would_recognise()

    def test_terminal_lookups():
        check_the_lookups_behave()
