#!/usr/bin/env python3
"""Anti-degeneracy and integrity gate for the crypto ledger.

This exists because the pipeline ran green for a week while publishing a chart of
fabricated alpha under the caption "live paper track record". Nothing threw. The
workflow reported success. The site deployed. Four separate bugs in the write path
produced files that still parsed:

* the CSV header was written once and never reconciled, so a later schema change left
  a seven-column header over thirteen-column rows and a reader filed the alpha figure
  under ``n_holdings``;
* rows appended unconditionally, so a re-run duplicated a date — 460 duplicate
  ``(date, symbol)`` pairs accumulated in the signals ledger;
* the benchmark baseline reset on every rebalance while the basket's did not, pinning
  ``benchmark_return`` at exactly 0.0 on every published row and making the reported
  alpha the raw return under another name;
* a since-entry cumulative was written into a column the consumer compounded as if it
  were daily, turning a basket up ~120% into a 4.53x "total return".

Every check below corresponds to one of those, or to a failure the sibling equity
project actually had. A build that produces structurally parseable files full of
meaningless values is a failed build, and this is what says so. Run it between the data
build and the commit; a non-zero exit must block publication.

    python scripts/validate_ledger.py
    python scripts/validate_ledger.py --ledger ledger --min-assets 30
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Import nightly.py for its schema constants rather than restating them. A validator
# with its own copy of the field list is a second source of truth, and the bug this
# file exists to prevent was two sources of truth disagreeing.
_spec = importlib.util.spec_from_file_location("nightly_schema", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

# Thresholds. Each is set to catch structural breakage, not to demand perfection, and
# sits well clear of what a healthy run actually produces.
MIN_ASSETS = 25              # a full run scores ~50
MIN_DISPERSION = 5.0         # every name scoring alike is the failure this catches
MIN_DISTINCT_TIERS = 3       # a board that only ever says HOLD is not a board
MIN_PRICE = 1e-12
MCAP_LOW, MCAP_HIGH = 1e6, 5e13
# A single night's move beyond this is a data error, not a market. Crypto is volatile;
# this is deliberately loose enough that a real 60% day passes.
MAX_OVERNIGHT_MOVE = 1.50
# The benchmark must actually vary. It was identically 0.0 on all ten published rows.
BENCHMARK_CONSTANT_ROWS = 3


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# schema and shape
# ---------------------------------------------------------------------------
def check_headers(ledger: Path) -> list[str]:
    """The header must match the schema the writer uses.

    The original bug in one check: a header written once at file creation, a schema
    that later grew six columns, and every subsequent row appended under a header that
    no longer described it. A DictReader raises nothing — it simply maps values to the
    wrong names.
    """
    problems = []
    for name, fields in (("signals.csv", nightly.FIELDS),
                         ("index.csv", nightly.INDEX_FIELDS)):
        path = ledger / name
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            widths = {len(r) for r in reader if r}
        if header != fields:
            problems.append(
                f"{name}: header does not match the writer's schema "
                f"({len(header)} columns vs {len(fields)}). Rows written under a stale "
                f"header are positionally misaligned and cannot be repaired.")
        bad = {w for w in widths if w != len(fields)}
        if bad:
            problems.append(f"{name}: rows of width {sorted(bad)} against a "
                            f"{len(fields)}-column header")
    return problems


def check_no_duplicates(ledger: Path) -> list[str]:
    """One row per (date, symbol), one row per date.

    A blind append made a re-run a second copy of that day's board. Anything reading
    the result as a daily series then counts one day several times and computes returns
    between a day and itself.
    """
    problems = []
    sig = ledger / "signals.csv"
    if sig.exists():
        rows = list(csv.DictReader(sig.open(newline="", encoding="utf-8")))
        pairs = Counter((r.get("date"), r.get("symbol")) for r in rows)
        dupes = {k: n for k, n in pairs.items() if n > 1}
        if dupes:
            worst = sorted(dupes.items(), key=lambda kv: -kv[1])[:3]
            problems.append(
                f"signals.csv: {len(dupes)} duplicate (date, symbol) pairs, "
                f"{sum(dupes.values()) - len(dupes)} redundant rows "
                f"(worst: {', '.join(f'{k[1]} on {k[0]} x{n}' for k, n in worst)}). "
                f"Run nightly.dedupe_signals() to repair.")

    idx = ledger / "index.csv"
    if idx.exists():
        rows = nightly.read_index_rows(idx)
        dates = Counter(r.get("date") for r in rows)
        dupes = {d: n for d, n in dates.items() if n > 1}
        if dupes:
            problems.append(f"index.csv: duplicate dates {sorted(dupes)}")
    return problems


def check_mirror(ledger: Path) -> list[str]:
    """signals.json must describe the same rows as signals.csv.

    The JSON is the artifact the site and any consumer reads; the CSV is the record.
    They are written in the same run and there is no legitimate reason for them to
    disagree, so a divergence means one of them was rebuilt and the other was not.
    """
    csv_path, json_path = ledger / "signals.csv", ledger / "signals.json"
    if not (csv_path.exists() and json_path.exists()):
        return []
    n_csv = sum(1 for _ in csv.DictReader(csv_path.open(newline="", encoding="utf-8")))
    try:
        payload = json.loads(json_path.read_text())
    except Exception as exc:
        return [f"signals.json unreadable: {exc}"]
    n_json = len(payload.get("rows") or [])
    if n_csv != n_json:
        return [f"signals.json holds {n_json} rows where signals.csv holds {n_csv} — "
                f"one was rebuilt without the other"]
    return []


# ---------------------------------------------------------------------------
# the board itself
# ---------------------------------------------------------------------------
def check_board(ledger: Path, min_assets: int) -> list[str]:
    """The latest day's scores must actually discriminate between assets."""
    path = ledger / "signals.csv"
    if not path.exists():
        return ["signals.csv missing — nothing was recorded"]
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    if not rows:
        return ["signals.csv is empty"]
    latest = max(r.get("date") or "" for r in rows)
    today = [r for r in rows if r.get("date") == latest]

    problems = []
    if len(today) < min_assets:
        problems.append(f"only {len(today)} assets scored on {latest} "
                        f"(expected at least {min_assets})")

    convs = [c for c in (_num(r.get("conviction")) for r in today) if c is not None]
    if len(convs) < 2:
        problems.append(f"{latest}: fewer than two usable conviction scores")
    else:
        mean = sum(convs) / len(convs)
        sd = (sum((c - mean) ** 2 for c in convs) / (len(convs) - 1)) ** 0.5
        if sd < MIN_DISPERSION:
            problems.append(f"{latest}: conviction dispersion {sd:.2f} is below "
                            f"{MIN_DISPERSION} — the board is not separating assets")

    tiers = {r.get("signal") for r in today if r.get("signal")}
    if len(tiers) < MIN_DISTINCT_TIERS:
        problems.append(f"{latest}: only {len(tiers)} distinct tier(s) in use "
                        f"({', '.join(sorted(tiers)) or 'none'})")

    for field, lo, hi, label in (("price", MIN_PRICE, None, "price"),
                                 ("market_cap", MCAP_LOW, MCAP_HIGH, "market cap")):
        vals = [(r.get("symbol"), _num(r.get(field))) for r in today]
        bad = [s for s, v in vals if v is None or v < lo or (hi and v > hi)]
        if bad:
            problems.append(f"{latest}: implausible {label} for "
                            f"{', '.join(bad[:5])}{' …' if len(bad) > 5 else ''}")
    return problems


# ---------------------------------------------------------------------------
# the published return series
# ---------------------------------------------------------------------------
def check_returns(ledger: Path) -> list[str]:
    """The checks that would have caught the fabricated alpha."""
    problems = []
    idx = ledger / "index.csv"
    if idx.exists():
        rows = nightly.read_index_rows(idx)
        bench = [_num(r.get("benchmark_return_since_entry")) for r in rows]
        bench = [b for b in bench if b is not None]
        # The one that mattered. A benchmark that never moves is not a benchmark, and
        # every "alpha" measured against it is the raw return under another name.
        if len(bench) >= BENCHMARK_CONSTANT_ROWS and len(set(bench)) == 1:
            problems.append(
                f"index.csv: benchmark_return_since_entry is constant at {bench[0]} "
                f"across {len(bench)} rows — alpha measured against it is the basket's "
                f"own return renamed")

    breadth = ledger / "market_breadth.json"
    if not breadth.exists():
        return problems
    try:
        payload = json.loads(breadth.read_text())
    except Exception as exc:
        return problems + [f"market_breadth.json unreadable: {exc}"]

    perf = payload.get("performance") or {}
    if perf:
        legs, need = perf.get("legs") or 0, perf.get("min_days") or 0
        if perf.get("renderable") and legs < need - 1:
            problems.append(f"performance: claims renderable on {legs} measured leg(s), "
                            f"below the {need}-day threshold it declares")
        if perf.get("book_total") is not None and not legs:
            problems.append("performance: reports a total return with no measured legs")
        if perf.get("duplicates_collapsed"):
            problems.append(
                f"performance: had to collapse {perf['duplicates_collapsed']} duplicate "
                f"rows to read the ledger as a daily series — the ledger should not "
                f"contain them")
        series = perf.get("series") or []
        prev = None
        for pt in series:
            cur = pt.get("book")
            if prev is not None and cur is not None:
                step = (1 + cur / 100.0) / (1 + prev / 100.0) - 1
                if abs(step) > MAX_OVERNIGHT_MOVE:
                    problems.append(f"performance: overnight move of {step * 100:.0f}% "
                                    f"on {pt.get('date')} — a data error, not a market")
            prev = cur if cur is not None else prev
        if series and not perf.get("benchmark_available"):
            if any(pt.get("benchmark") is not None for pt in series):
                problems.append("performance: carries benchmark points while reporting "
                                "the benchmark unavailable")

    diff = payload.get("tier_diff") or {}
    counts = diff.get("counts") or {}
    if counts:
        listed = len(diff.get("changed") or []) + len(diff.get("marginal") or [])
        if counts.get("tier_changes") != listed:
            problems.append(f"tier_diff: counts say {counts.get('tier_changes')} changes "
                            f"but {listed} are listed")
    return problems


def check_monitor(ledger: Path) -> list[str]:
    """The health report must itself be healthy.

    A monitoring artifact that silently stops updating is worse than none: it keeps
    displaying the last good reading while the thing it watches degrades. So the gate
    checks that it was produced, that it describes the ledger actually on disk, and that
    nothing in it is failing.
    """
    path = ledger / "monitor.json"
    if not path.exists():
        return ["monitor.json was not produced — the health report is the thing that "
                "notices degradation, and it is missing"]
    try:
        mon = json.loads(path.read_text())
    except Exception as exc:
        return [f"monitor.json unreadable: {exc}"]

    problems = []
    sig = ledger / "signals.csv"
    if sig.exists():
        rows = list(csv.DictReader(sig.open(newline="", encoding="utf-8")))
        latest = max((r.get("date") or "" for r in rows), default=None)
        if latest and mon.get("to") != latest:
            problems.append(f"monitor.json reports through {mon.get('to')} while the "
                            f"ledger runs to {latest} — it did not rerun")

    for check in mon.get("health") or []:
        if check.get("status") == "fail":
            problems.append(f"health check failing: {check.get('name')} — {check.get('detail')}")
    return problems


def check_basket(ledger: Path) -> list[str]:
    path = ledger / "basket.json"
    if not path.exists():
        return []
    try:
        basket = json.loads(path.read_text())
    except Exception as exc:
        return [f"basket.json unreadable: {exc}"]
    holdings = basket.get("holdings") or []
    if not holdings:
        return []
    problems = []
    total = sum(h.get("weight") or 0 for h in holdings)
    if not 0.97 <= total <= 1.03:
        problems.append(f"basket weights sum to {total:.3f}, not 1")
    if not basket.get("entry_global_mcap"):
        problems.append("basket has no entry_global_mcap — the benchmark has no baseline")
    missing = [h.get("symbol") for h in holdings if not h.get("entry_price")]
    if missing:
        problems.append(f"holdings without an entry price: {', '.join(map(str, missing[:5]))}")
    return problems


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", default=str(ROOT / "ledger"))
    ap.add_argument("--min-assets", type=int, default=MIN_ASSETS)
    args = ap.parse_args()
    ledger = Path(args.ledger)

    if not ledger.is_dir():
        print(f"FAIL  no ledger directory at {ledger}")
        return 2

    problems: list[str] = []
    problems += check_headers(ledger)
    problems += check_no_duplicates(ledger)
    problems += check_mirror(ledger)
    problems += check_board(ledger, args.min_assets)
    problems += check_returns(ledger)
    problems += check_basket(ledger)
    problems += check_monitor(ledger)

    # Context, printed whether or not the gate passes — a validator that only speaks up
    # on failure teaches nobody what healthy looks like.
    sig = ledger / "signals.csv"
    if sig.exists():
        rows = list(csv.DictReader(sig.open(newline="", encoding="utf-8")))
        dates = sorted({r.get("date") for r in rows if r.get("date")})
        latest = [r for r in rows if r.get("date") == (dates[-1] if dates else None)]
        convs = [c for c in (_num(r.get("conviction")) for r in latest) if c is not None]
        print(f"ledger:      {ledger}")
        print(f"history:     {len(dates)} day(s), {dates[0] if dates else '—'} "
              f"to {dates[-1] if dates else '—'}, {len(rows)} rows")
        if convs:
            mean = sum(convs) / len(convs)
            sd = (sum((c - mean) ** 2 for c in convs) / max(1, len(convs) - 1)) ** 0.5
            tiers = Counter(r.get("signal") for r in latest)
            print(f"latest:      {len(latest)} assets  conviction {min(convs):.0f}-"
                  f"{max(convs):.0f}  dispersion {sd:.1f}")
            print("tiers:       " + "  ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    breadth = ledger / "market_breadth.json"
    if breadth.exists():
        try:
            perf = (json.loads(breadth.read_text()).get("performance") or {})
            if perf.get("legs"):
                print(f"performance: {perf['legs']} leg(s), basket "
                      f"{perf['book_total']:+.2f}%, {perf['benchmark']} "
                      f"{perf['benchmark_total']:+.2f}%"
                      if perf.get("benchmark_available") else
                      f"performance: {perf['legs']} leg(s), basket {perf['book_total']:+.2f}%")
        except Exception:
            pass

    if problems:
        print(f"\nFAIL  {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nPASS  ledger is publishable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
