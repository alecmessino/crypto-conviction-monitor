#!/usr/bin/env python3
"""Write ``ledger/manifest.json``: what is on disk, about to be committed.

The browser fetched 2.0 MB of ``rwa.json`` and 3.26 MB of ``signals.json`` on every page
load and again every 120 seconds, with ``cache: "no-store"``, for artifacts that change
once a night. A tab left open for a working day pulled hundreds of megabytes of files
that had not changed. This is the small file the page reads instead, so it can tell
whether a refetch is worth doing.

It is deliberately DERIVED, never declared. Every field is read out of the artifacts
themselves at the moment they are staged, so the manifest cannot describe a night that
was not published:

* ``rwa.py`` runs ``continue-on-error`` in the nightly. On a night it fails, ``rwa.json``
  on disk is still last night's, and a manifest that reported today's date because today
  is when the job ran would tell every browser to refetch 2 MB it already had — or worse,
  tell it not to when the file HAD changed.
* the RWA release workflow commits ``rwa.json`` on its own schedule, without touching the
  crypto ledger. Two writers, one manifest; reading the files is the only way both stay
  described.

The cache key is a content hash rather than a date, because a date cannot distinguish a
re-run from a new night. ``sha256`` over the exact bytes served is the only key that
cannot disagree with what the browser receives.

An artifact that is missing is NAMED in ``absent`` rather than dropped from the map. A
consumer that finds no entry for a file must not be able to read that as "unchanged" —
it is the difference between a gate that fails open and one that silently serves a stale
board.

Run it after the build gates and before the commit, and stage the file it writes:

    python scripts/write_manifest.py
    python scripts/write_manifest.py --ledger ledger
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

# Every committed artifact the browser fetches. Ordered as the page reads them, so a
# diff of this list against loadLedger() in index.html is a one-screen review.
ARTIFACTS = [
    "signals.json", "index.json", "market_breadth.json", "monitor.json",
    "parity.json", "funding.json", "market_intel.json", "rwa.json",
]

# 12 hex characters of sha256. The collision probability across one file per night for a
# century is far below the probability of the ledger itself being wrong, and the whole
# manifest has to stay small enough that fetching it every 120s is cheaper than not.
HASH_CHARS = 12


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:HASH_CHARS]


def _load(path: Path):
    """Parsed JSON, or None. A file that exists but does not parse is reported absent:
    the browser cannot render it either, and calling it present would be a manifest
    describing something nobody can read."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def build(ledger: Path) -> dict:
    artifacts: dict[str, str] = {}
    absent: list[str] = []
    for name in ARTIFACTS:
        p = ledger / name
        key = f"{ledger.name}/{name}"
        if p.is_file():
            artifacts[key] = _sha(p)
        else:
            absent.append(key)

    man = {
        # When the MANIFEST was written, which is not when either artifact was built.
        # Both of those are carried below, read from the artifacts themselves.
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifacts": artifacts,
        "absent": absent,
    }

    sig = _load(ledger / "signals.json")
    if sig is not None:
        rows = sig.get("rows") or []
        dates = [r.get("date") for r in rows if r.get("date")]
        man["signals"] = {
            # signals.json carries no top-level date; the ledger's own latest row is the
            # night it describes.
            "date": max(dates) if dates else None,
            "generated_at": sig.get("generated_at"),
            "rows": len(rows),
            "sha256": artifacts.get(f"{ledger.name}/signals.json"),
        }
    else:
        man["signals"] = None

    rwa = _load(ledger / "rwa.json")
    if rwa is not None:
        man["rwa"] = {
            "date": rwa.get("date"),
            "spec_hash": rwa.get("spec_hash"),
            "generated_at": rwa.get("generated_at"),
            "status": rwa.get("status"),
            "sha256": artifacts.get(f"{ledger.name}/rwa.json"),
        }
    else:
        man["rwa"] = None

    return man


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", default="ledger", help="ledger directory (default: ledger)")
    args = ap.parse_args()

    ledger = Path(args.ledger)
    if not ledger.is_dir():
        print(f"no ledger directory at {ledger}")
        return 1

    man = build(ledger)
    out = ledger / "manifest.json"
    out.write_text(json.dumps(man, indent=1, sort_keys=False) + "\n", encoding="utf-8")

    print(f"wrote {out} ({out.stat().st_size} B)")
    print(f"  described: {len(man['artifacts'])}   absent: {len(man['absent'])}")
    for key in ("signals", "rwa"):
        blk = man.get(key)
        print(f"  {key}: " + ("ABSENT" if blk is None
              else f"{blk.get('date')} {blk.get('sha256')}"))
    if man["absent"]:
        print("  absent: " + ", ".join(man["absent"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
