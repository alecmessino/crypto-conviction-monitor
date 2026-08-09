#!/usr/bin/env python3
"""Is this query id the right one? Answer in one command instead of one night.

Without this, the only way to find out whether a Dune query id is correct is to set the
secret, wait for the nightly, and read "0 of 4 contextual fields carry values" — which
is the same message you get for a wrong id, a query that was never executed, a query
returning different column names, and an expired key. Four causes, one symptom, a day
apart. This separates them.

    export DUNE_API_KEY=...          # never pass the key as an argument
    python scripts/check_dune.py 6987652

Reads only. It does not execute the query, spend credits, or write anything.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("nightly_dune", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

WANTED = ("unlocks_usd", "supply_increase_pct", "addr_growth_pct")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query_id", help="the number in a dune.com/queries/<id> URL")
    args = ap.parse_args()

    key = os.environ.get("DUNE_API_KEY")
    if not key:
        print("DUNE_API_KEY is not set in this shell. Export it first — do not pass it "
              "as an argument, where it lands in your shell history.", file=sys.stderr)
        return 2

    url = f"{nightly.DUNE_BASE}/query/{args.query_id}/results?limit=5"
    try:
        data = nightly._get_json(url, headers={"X-Dune-Api-Key": key})
    except Exception as exc:  # noqa: BLE001
        # The three failures worth telling apart, because the fix differs for each.
        msg = str(exc)
        hint = ("the key is wrong or expired" if "401" in msg or "403" in msg else
                "no query with that id, or it is private to another account"
                if "404" in msg else
                "the query exists but has never been executed — /results returns the "
                "last execution, so run it once in the Dune editor" if "409" in msg else
                "unexpected")
        print(f"FAIL  {msg}\n      likely: {hint}", file=sys.stderr)
        return 1

    rows = (data.get("result") or {}).get("rows") or []
    if not rows:
        print("FAIL  the query resolved but returned no rows. It has probably never "
              "been executed — /results serves the last execution rather than running "
              "a fresh one.", file=sys.stderr)
        return 1

    columns = list(rows[0].keys())
    print(f"query {args.query_id}: {len(rows)} sample row(s)")
    print(f"columns returned: {', '.join(columns)}\n")

    # Recognition, not just presence: the fetcher matches through DUNE_ALIASES, so a
    # column can be there under a name nothing looks for. That is the failure this
    # script exists to make visible, because it is invisible everywhere else.
    sym = nightly._pick(rows[0], "symbol")
    ok = sym is not None
    print(f"  {'OK  ' if ok else 'MISS'} symbol -> {sym!r}")
    for field in WANTED:
        v = nightly._pick(rows[0], field)
        print(f"  {'OK  ' if v is not None else 'MISS'} {field} -> {v!r}")
        ok = ok and v is not None

    unrecognised = [c for c in columns
                    if not any(c.lower() in a for a in nightly.DUNE_ALIASES.values())]
    if unrecognised:
        print(f"\nnot recognised: {', '.join(unrecognised)}")
        print("If one of these is the data you want, add its name to the matching entry "
              "in DUNE_ALIASES in nightly.py rather than renaming the column in Dune.")

    if not sym:
        print("\nWithout a symbol column every row is dropped and the feed records as "
              "null — same as not configuring it at all.")
        return 1
    if not ok:
        print("\nUsable, partially. Recognised columns record; the rest stay null, "
              "which is the honest reading and breaks nothing — nothing here is scored.")
        return 0
    print("\nAll four resolve. Set DUNE_UNLOCK_QUERY_ID to this id.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
