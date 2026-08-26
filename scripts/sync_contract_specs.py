#!/usr/bin/env python3
"""Regenerate index.html's inline CONTRACT_SPECS literal from contract_specs.json.

The terminal is one self-contained file and fetches its reference data from nowhere, so
the contract table has to be inlined. Inlining a table that also lives on disk is only
safe if the copy is generated and the drift is a test failure, which is what
tests/test_contract_specs.py enforces. This is the other half: the one command that makes
the copy correct again.

    python scripts/sync_contract_specs.py

Edit contract_specs.json, run this, run the gate. Never edit the literal by hand.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = ROOT / "contract_specs.json"
TERMINAL = ROOT / "index.html"


def main() -> int:
    raw = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    inline = {k: v for k, v in raw.items() if not k.startswith("_")}
    literal = json.dumps(inline, separators=(",", ":"), sort_keys=True)

    html = TERMINAL.read_text(encoding="utf-8")
    pattern = re.compile(r"(const\s+CONTRACT_SPECS\s*=\s*)\{.*?\}(;)", re.S)
    new, n = pattern.subn(lambda m: m.group(1) + literal + m.group(2), html, count=1)
    if n != 1:
        print("could not find `const CONTRACT_SPECS = {...};` in index.html", file=sys.stderr)
        return 1
    if new == html:
        print("index.html is already in sync with contract_specs.json")
        return 0
    TERMINAL.write_text(new, encoding="utf-8")
    print(f"index.html updated: {len(inline['products'])} contracts, as_of {inline['as_of']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
