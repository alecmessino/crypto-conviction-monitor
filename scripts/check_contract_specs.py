#!/usr/bin/env python3
"""Promote the pinned contract multipliers from "reported" to "confirmed".

`contract_specs.json` says 5,000 HBAR per contract because two news outlets said so, and
it says on its face that this is what it is. That is honest and it is not the same thing
as having read the number off the venue, which matters here more than usual: the
multiplier is the denominator under every figure the funding parser prints, so an error
in it does not degrade the output, it scales the output by a constant and leaves it
looking exactly as confident as a correct answer.

This is the script that closes that gap. It fetches Coinbase's own product pages and
compares each pinned multiplier against what the page says.

    python scripts/check_contract_specs.py
    python scripts/check_contract_specs.py --write     # stamp verification/verified_on

WHY IT IS NOT WIRED INTO CI: coinbase.com returns 403 to the automated agents this
repository runs under, and the CFTC product-certification filings that would be the
primary source are scanned images with no extractable text. Neither the nightly nor the
test workflow can reach a source that would settle it. Same shape as check_dune.py:
manual, run from somewhere that can actually see the thing, and read for its output
rather than its exit code.

Exit codes: 0 every pinned multiplier confirmed. 1 a disagreement, which is the case
worth waking up for. 2 the sources could not be reached, which is not a disagreement and
deliberately does not report as one.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = ROOT / "contract_specs.json"

# Where a human would look. Listed per product rather than as one page, because the
# exchange has never had a single page carrying every contract unit.
PRODUCT_PAGES = (
    "https://www.coinbase.com/derivatives",
    "https://www.coinbase.com/derivatives/products",
    "https://help.coinbase.com/coinbase/trading-and-funding/derivatives/futures-intro",
)

_UA = "Mozilla/5.0 (compatible; conviction-monitor contract-spec check)"


def fetch(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        print(f"  could not read {url}: {exc}", file=sys.stderr)
        return None


def find_multiplier(text: str, product: str, unit: str) -> float | None:
    """Look for "<n> <UNIT>" near the product name.

    Deliberately narrow. A loose pattern over a marketing page will find a number, and a
    number found by a loose pattern is worse than no number at all: it would stamp
    `verification: exchange` onto a value nobody checked.
    """
    idx = text.lower().find(product.lower())
    if idx == -1:
        return None
    window = text[max(0, idx - 400): idx + 1200]
    m = re.search(rf"([\d,]+(?:\.\d+)?)\s*{re.escape(unit)}\b", window, re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="stamp verification=exchange and today's date on confirmed rows")
    args = ap.parse_args()

    raw = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    products = raw["products"]

    print(f"Pinned {raw['as_of']} · {len(products)} contracts · checking {len(PRODUCT_PAGES)} pages")
    pages = [p for p in (fetch(u) for u in PRODUCT_PAGES) if p]
    if not pages:
        print("\nNo Coinbase page could be read from here. Nothing is confirmed and "
              "nothing is contradicted. The table stays exactly as pinned.")
        return 2

    today = _dt.date.today().isoformat()
    confirmed, disagreed, unseen = [], [], []
    for p in products:
        found = None
        for page in pages:
            found = find_multiplier(page, p["product"], p["unit"])
            if found is not None:
                break
        label = f"{p['symbol']} {p['product']}"
        if found is None:
            unseen.append(label)
        elif abs(found - p["multiplier"]) < 1e-9:
            confirmed.append(label)
            if args.write:
                p["verification"] = "exchange"
                p["verified_on"] = today
        else:
            disagreed.append(f"{label}: pinned {p['multiplier']}, page says {found}")

    print(f"\nconfirmed  {len(confirmed)}")
    for c in confirmed:
        print(f"  {c}")
    print(f"not found on any page  {len(unseen)}")
    for u in unseen:
        print(f"  {u}")
    if disagreed:
        print(f"\nDISAGREEMENT  {len(disagreed)}")
        for d in disagreed:
            print(f"  {d}")
        print("\nA pinned multiplier is wrong. Every funding figure derived from that "
              "contract is wrong by the ratio. Fix contract_specs.json, regenerate the "
              "inline literal in index.html, and re-run tests/test_contract_specs.py.")

    if args.write and confirmed:
        SPEC_FILE.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        print(f"\nStamped {len(confirmed)} row(s) as exchange-verified on {today}. "
              "Regenerate the inline literal in index.html so the gate stays green.")

    return 1 if disagreed else 0


if __name__ == "__main__":
    raise SystemExit(main())
