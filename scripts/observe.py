#!/usr/bin/env python3
"""The two figures under observation, computed by the pipeline rather than by a person.

Why this exists
---------------
Both open questions on this repo are of the form "has X held for N nights". Answering
them was being done by scheduling a session to go and look, which fails in the obvious
way: the schedule lives in a session, the session ends, and the observation is lost. A
question that needs three consecutive nights cannot depend on something that does not
survive one.

So the nightly answers them itself. Two readings, appended to a small file the job
already commits:

  policy_blocked   Binance answers HTTP 451 and Bybit 403 from a US-hosted runner. Those
                   are policy — a jurisdiction block and a CDN country rule — not
                   outages, and the decision to drop a venue should rest on a count of
                   nights rather than on whoever remembers. funding.json holds only
                   tonight's status; this file holds the series.

  ls_fill          How much of the board carries a long/short ratio. The column was null
                   on every row for weeks after Binance became unreachable, then 3/50
                   when the Cryptometer sweep was selecting alphabetically from the full
                   250-market universe and never reached BTC. It is the fill rate, not
                   the presence of the feed, that says whether the fix worked.

Reads what the nightly already wrote and appends one row per date. Idempotent: re-running
on the same date replaces that date's rows rather than duplicating them, matching how
signals.csv handles a second run on one day.

Nothing here is scored, and nothing here touches nightly.py — the specification hash is
untouched by design.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "ledger"
HEALTH_CSV = LEDGER / "venue_health.csv"
FIELDS = ("date", "venue", "status", "http_status", "policy_blocked", "markets")

# The venues whose refusal is a standing policy rather than tonight's weather. Counted
# because the rule for dropping them is "N consecutive nights", and a rule stated in
# nights needs nights recorded.
WATCHED = ("binance", "bybit")


def _rows(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def append_venue_health(funding: dict, date: str) -> list:
    """One row per venue for `date`, replacing any existing rows for that date."""
    kept = [r for r in _rows(HEALTH_CSV) if r.get("date") != date]
    fresh = []
    for venue, rec in (funding.get("venues") or {}).items():
        fresh.append({
            "date": date, "venue": venue, "status": rec.get("status"),
            "http_status": rec.get("http_status") or "",
            # Written as the literal True/False rather than 1/0: this column is read by
            # people as often as by code, and "False" cannot be mistaken for a count.
            "policy_blocked": rec.get("policy_blocked", False),
            "markets": rec.get("markets", 0),
        })
    LEDGER.mkdir(parents=True, exist_ok=True)
    with HEALTH_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(kept + sorted(fresh, key=lambda r: r["venue"]))
    return kept + fresh


def blocked_streak(rows: list, venue: str) -> tuple:
    """Consecutive most-recent nights `venue` was policy-blocked, and nights recorded.

    A streak rather than a total: a venue blocked on three of the last ten nights is a
    flaky venue, and one blocked on the last three consecutively is a policy. Only the
    second justifies removing it.
    """
    dates = sorted({r["date"] for r in rows if r.get("date")}, reverse=True)
    streak = 0
    for d in dates:
        hit = [r for r in rows if r["date"] == d and r["venue"] == venue]
        if hit and str(hit[0].get("policy_blocked")).lower() == "true":
            streak += 1
        else:
            break
    return streak, len(dates)


def ls_fill(date: str) -> tuple:
    """Board rows carrying a long/short ratio, and whether the majors are among them."""
    rows = [r for r in _rows(LEDGER / "signals.csv") if r.get("date") == date]
    got = [r["symbol"] for r in rows if (r.get("long_short_ratio") or "").strip()]
    return len(got), len(rows), sorted(got)


def main() -> int:
    funding_path = LEDGER / "funding.json"
    if not funding_path.exists():
        print("[observe] no funding.json — nothing to record", file=sys.stderr)
        return 0
    funding = json.loads(funding_path.read_text(encoding="utf-8"))
    date = funding.get("date")
    if not date:
        print("[observe] funding.json carries no date", file=sys.stderr)
        return 0

    rows = append_venue_health(funding, date)

    n, total, got = ls_fill(date)
    majors = [s for s in ("BTC", "ETH") if s in got]
    print(f"[observe] long/short fill {n}/{total} on {date}"
          + (f"; majors present: {', '.join(majors)}" if majors
             else "; BTC and ETH both absent"))
    if got:
        print(f"[observe]   {', '.join(got[:14])}" + (" ..." if len(got) > 14 else ""))

    for venue in WATCHED:
        streak, nights = blocked_streak(rows, venue)
        tonight = [r for r in rows if r["date"] == date and r["venue"] == venue]
        code = tonight[0].get("http_status") if tonight else ""
        print(f"[observe] {venue}: policy-blocked {streak} consecutive night(s) "
              f"of {nights} recorded"
              + (f" (tonight HTTP {code})" if code else " (tonight: not blocked)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
