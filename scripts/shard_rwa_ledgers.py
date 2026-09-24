"""Migrate the RWA monoliths into month shards — docs/DESIGN-RWA-SHARDING.md §4.2.

    python scripts/shard_rwa_ledgers.py            # --check: verify, change nothing
    python scripts/shard_rwa_ledgers.py --apply    # verify, then migrate

For each of flow, wrappers and observed:

  1. read the legacy monolith (and any shard rows already present — the union);
  2. refuse a non-ISO date or a duplicated (date, key) in that union;
  3. render each month's rows with the nightly's own writer (rwa.append_daily_rows)
     into a temporary directory;
  4. verify — the rows read back from the rendered shards equal the source rows; when no
     shard pre-existed, header + the shard bodies in order equal the monolith BYTE FOR
     BYTE; the row counts agree; every shard's dates lie inside its month;
  5. only then move the shards into ledger/rwa/<kind>/, write SCHEMA.json and delete the
     monolith.

It prints each monolith's sha256, row count and last date, which tests/test_rwa.py pins.
Idempotent: with no monolith left it verifies the shards and exits 0 without touching a
byte. On any mismatch it exits non-zero with the monolith still in place and no shard
written.

The flow ledger cannot be re-fetched at any price (/rwas/{id}/market_chart answers 401
below the Basic plan). That is why nothing here writes until everything has verified.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_rwa():
    import importlib.util
    path = ROOT / "rwa.py"
    spec = importlib.util.spec_from_file_location("shard_rwa", path)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), mod.__dict__)  # noqa: S102
    return mod


rwa = _load_rwa()


class MigrationError(RuntimeError):
    pass


def _iso(day) -> bool:
    try:
        rwa.rwa_shard_path(Path("."), "flow", day)
        return True
    except ValueError:
        return False


def _render(rows: list, fields: list) -> dict:
    """{month: bytes} using the nightly's writer, one append per date in recorded order
    — the same sequence of writes that produced the monolith."""
    out = {}
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        by_month: dict[str, list] = {}
        for r in rows:
            by_month.setdefault(r["date"][:7], []).append(r)
        for month, mrows in by_month.items():
            path = td / f"{month}.csv"
            # Group by date preserving order, then append each date as a night would.
            dates: list[str] = []
            for r in mrows:
                if r["date"] not in dates:
                    dates.append(r["date"])
            for d in dates:
                rwa.append_daily_rows(path, fields, d, [r for r in mrows if r["date"] == d])
            out[month] = path.read_bytes()
    return out


def plan_kind(ledger: Path, kind: str) -> dict:
    """Everything the migration of one ledger would do, verified, with nothing written."""
    fields, key, legacy_name = rwa.RWA_SHARDED[kind]
    legacy = ledger / legacy_name
    existing = rwa.rwa_shard_files(ledger, kind)
    info = {"kind": kind, "legacy": legacy, "existing_shards": [p.name for p in existing]}
    if not legacy.exists():
        info["action"] = "none"          # already migrated: verify the shards only
        rows = rwa.read_ledger(ledger, kind)
        for p in existing:
            with p.open(newline="", encoding="utf-8") as f:
                stray = {r["date"] for r in csv.DictReader(f) if (r.get("date") or "")[:7] != p.stem}
            if stray:
                raise MigrationError(f"{kind}/{p.name}: dates outside its month {sorted(stray)[:3]}")
        info["rows"] = len(rows)
        return info

    raw = legacy.read_bytes()
    src = rwa.read_ledger(ledger, kind)            # legacy + any existing shard rows
    bad = sorted({r.get("date") for r in src if not _iso(r.get("date"))}, key=str)
    if bad:
        raise MigrationError(f"{kind}: non-ISO date(s) {bad[:3]} — refusing to guess a shard")
    keys = [(r["date"], r.get(key)) for r in src]
    dup = sorted({k for k in keys if keys.count(k) > 1}) if len(keys) != len(set(keys)) else []
    if dup:
        raise MigrationError(f"{kind}: duplicate (date, {key}) {dup[:3]} — refusing to migrate")

    shards = _render(src, fields)
    # --- verification ------------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        tdl = Path(td)
        d = rwa.rwa_shard_dir(tdl, kind)
        d.mkdir(parents=True)
        for month, body in shards.items():
            (d / f"{month}.csv").write_bytes(body)
        back = rwa.read_ledger(tdl, kind)
    if back != src:
        raise MigrationError(f"{kind}: rows read back from the shards differ from the source")
    if sum(len(list(csv.DictReader(io.StringIO(b.decode("utf-8"), newline="")))) for b in shards.values()) != len(src):
        raise MigrationError(f"{kind}: shard row count differs from the source")
    for month, body in shards.items():
        dates = {r["date"][:7] for r in csv.DictReader(io.StringIO(body.decode("utf-8"), newline=""))}
        if dates != {month}:
            raise MigrationError(f"{kind}/{month}.csv: dates outside its month {sorted(dates)}")
    if not existing:
        header = (",".join(fields) + "\r\n").encode("utf-8")
        joined = header + b"".join(shards[m][len(header):] for m in sorted(shards))
        if joined != raw:
            raise MigrationError(f"{kind}: header + shard bodies is not the monolith byte for byte")
    info.update(action="migrate", shards=shards, rows=len(src),
                sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
                last_date=max(r["date"] for r in src) if src else None)
    return info


def apply_kind(ledger: Path, info: dict) -> None:
    kind = info["kind"]
    d = rwa.rwa_shard_dir(ledger, kind)
    d.mkdir(parents=True, exist_ok=True)
    for month, body in sorted(info["shards"].items()):
        tmp = d / f".{month}.csv.migrating"
        tmp.write_bytes(body)
        os.replace(tmp, d / f"{month}.csv")
    rwa.write_rwa_shard_schema(ledger, kind)
    # Last, and only after every shard is in place: the monolith goes.
    info["legacy"].unlink()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="migrate (default: check only)")
    ap.add_argument("--ledger", default=str(ROOT / "ledger"))
    a = ap.parse_args(argv)
    ledger = Path(a.ledger)
    plans = []
    try:
        for kind in rwa.RWA_SHARDED:
            plans.append(plan_kind(ledger, kind))
    except MigrationError as exc:
        print(f"[shard] REFUSED: {exc}. Nothing was written.", file=sys.stderr)
        return 1
    for p in plans:
        if p["action"] == "none":
            print(f"[shard] {p['kind']}: already sharded ({len(p['existing_shards'])} shard(s), "
                  f"{p['rows']} rows) — nothing to do")
        else:
            print(f"[shard] {p['kind']}: {p['legacy'].name} sha256={p['sha256']} "
                  f"rows={p['rows']} bytes={p['bytes']} last_date={p['last_date']} -> "
                  f"{', '.join(f'{m}.csv' for m in sorted(p['shards']))}"
                  f"{'' if a.apply else ' (check only)'}")
    if a.apply:
        for p in plans:
            if p["action"] == "migrate":
                apply_kind(ledger, p)
        print("[shard] applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
