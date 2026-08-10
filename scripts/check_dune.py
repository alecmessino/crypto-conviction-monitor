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

    # A real page, not a sample. The first version of this script asked for five rows
    # and read the column names off rows[0], and reported "no column this feed
    # recognises" about a query that was in fact enriching 99 tokens — the sampled rows
    # simply were not representative of the result set. A diagnostic that is confidently
    # wrong is worse than none, because it gets believed.
    url = f"{nightly.DUNE_BASE}/query/{args.query_id}/results?limit=1000"
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

    # Union across every row. Dune omits keys whose value is null, so a column present
    # in the query can be missing from any given row.
    columns = sorted({k for r in rows for k in r})
    print(f"query {args.query_id}: {len(rows)} row(s)")
    print(f"columns across all rows: {', '.join(columns)}\n")

    # Recognition, not just presence: the fetcher matches through DUNE_ALIASES, so a
    # column can be there under a name nothing looks for. Counted over the whole page,
    # because "does row zero have it" is a different question from "does this feed
    # carry it" and only the second one matters.
    resolved = {f: sum(1 for r in rows if nightly._pick(r, f) is not None)
                for f in ("symbol",) + WANTED}
    for field, n in resolved.items():
        pct = 100.0 * n / len(rows)
        print(f"  {'OK  ' if n else 'MISS'} {field:<20} {n:>5}/{len(rows)} rows ({pct:.0f}%)")

    known = {a for aliases in nightly.DUNE_ALIASES.values() for a in aliases}
    unrecognised = [c for c in columns if c.lower() not in known]
    if unrecognised:
        print(f"\nnot recognised: {', '.join(unrecognised)}")
        print("If one of these is the data you want, add its name to the matching entry "
              "in DUNE_ALIASES in nightly.py rather than renaming the column in Dune.")

    if not resolved["symbol"]:
        print("\nWithout a symbol column every row is dropped and the feed records as "
              "null — same as not configuring it at all.")
        return 1

    # The number that actually decides whether this query is worth keeping: how much of
    # it lands on the board being scored. A query can be perfectly healthy and still be
    # about a universe this project never looks at.
    syms = {str(nightly._pick(r, "symbol")).upper().strip() for r in rows}
    syms.discard("NONE")
    try:
        board_rows = nightly._read_signals_rows()
        latest = max((r.get("date") or "" for r in board_rows), default="")
        board = {(r.get("symbol") or "").upper() for r in board_rows
                 if r.get("date") == latest and r.get("symbol")}
    except Exception:  # noqa: BLE001
        board = set()
    if board:
        hit = syms & board
        print(f"\noverlap with the {latest} board: {len(hit)} of {len(board)} scored "
              f"assets ({', '.join(sorted(hit)[:12]) or 'none'})")
        if len(hit) < len(board) * 0.2:
            print("Most of the board gets nothing from this query. It resolves and it "
                  "records, but it is largely about a different universe.")

    missing = [f for f in WANTED if not resolved[f]]
    if missing:
        print(f"\nUsable, partially — no values at all for {', '.join(missing)}. "
              "Recognised columns record; the rest stay null, which is the honest "
              "reading and breaks nothing, since nothing here is scored.")
        return 0
    print("\nAll four resolve. Set DUNE_UNLOCK_QUERY_ID to this id.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
