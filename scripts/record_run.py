"""Append this nightly run to ledger/runs.csv. Provenance only — never a gate.

Runs after every gate, whatever they decided (`if: always()` in nightly.yml), so a
refused or crashed night is recorded as exactly that. It merges three sources, none of
which it infers:

  * what nightly.main() reported about itself (the hand-off at nightly.run_info_path():
    snapshot timestamp, markets fetched, rows scored and written, budget skips, outcome);
  * what the workflow reported about each step (`--step name=outcome`, GitHub's own
    success / failure / skipped / cancelled);
  * what the artifacts on disk say about their own dates, read from the files that are
    about to be committed rather than from memory.

Exit 0 unless it cannot write the file at all. It must never be the reason a ledger is
not published, so the workflow also runs it with continue-on-error.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("nightly_runs", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

CRYPTO_STEPS = ("nightly_step", "parity_gate", "crypto_gate", "atr_gate")


def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def artifact_dates(ledger: Path) -> dict:
    """The date each artifact claims for itself, from the file on disk."""
    out = {}
    sig = ledger / "signals.csv"
    if sig.exists():
        with sig.open(newline="", encoding="utf-8") as fh:
            dates = {r.get("date") for r in csv.DictReader(fh) if r.get("date")}
        out["signals_latest"] = max(dates) if dates else None
    shards = sorted((ledger / "xsec").glob("*.csv")) if (ledger / "xsec").is_dir() else []
    if shards:
        with shards[-1].open(newline="", encoding="utf-8") as fh:
            dates = {r.get("date") for r in csv.DictReader(fh) if r.get("date")}
        out["xsec_latest"] = max(dates) if dates else None
    out["perp_as_of"] = _json(ledger / "perp.json").get("as_of")
    out["walkforward_to"] = _json(ledger / "walkforward.json").get("to")
    out["rwa_date"] = _json(ledger / "rwa.json").get("date")
    return out


def build_row(info: dict, steps: dict, ledger: Path, env=None) -> dict:
    env = os.environ if env is None else env
    row = dict(info)
    row.setdefault("outcome", "no run record: nightly.py did not report (crashed before "
                              "its first line, or was never started)")
    row.update({k: v for k, v in steps.items()})
    row.update(artifact_dates(ledger))
    row["recorded_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    row["event"] = env.get("GITHUB_EVENT_NAME") or "local"
    row["run_id"] = env.get("GITHUB_RUN_ID")
    row["run_attempt"] = env.get("GITHUB_RUN_ATTEMPT")
    row["sha"] = (env.get("GITHUB_SHA") or "")[:12] or None
    row.setdefault("spec_hash", nightly.SPEC_HASH)
    # What the Commit steps will do with this run, derived from the same outcomes they
    # read. A push can still fail after this; the next run's row and git history say so.
    crypto_ok = all(steps.get(k) == "success" for k in CRYPTO_STEPS)
    row["crypto_publish"] = "commit" if crypto_ok else "withheld"
    row["rwa_publish"] = ("commit" if steps.get("rwa_gate") == "success"
                          and steps.get("nightly_step") == "success" else "withheld")
    return row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", default=str(ROOT / "ledger"))
    ap.add_argument("--info", default=None, help="the run hand-off (default: nightly's)")
    ap.add_argument("--step", action="append", default=[],
                    help="name=outcome, e.g. crypto_gate=success (repeatable)")
    args = ap.parse_args(argv)
    ledger = Path(args.ledger)
    info = _json(Path(args.info) if args.info else nightly.run_info_path())
    steps = {}
    for s in args.step:
        k, _, v = s.partition("=")
        if k not in nightly.RUN_FIELDS:
            print(f"[runs] unknown step {k!r}", file=sys.stderr)
            return 2
        steps[k] = v or "unknown"
    row = build_row(info, steps, ledger)
    path = nightly.append_run_row(row, ledger / "runs.csv")
    print(f"[runs] {row.get('date') or '?'} {row.get('outcome')} · crypto "
          f"{row['crypto_publish']} · rwa {row['rwa_publish']} · perp {row.get('perp_as_of')} "
          f"· walkforward {row.get('walkforward_to')} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
