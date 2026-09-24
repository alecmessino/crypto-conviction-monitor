# Month-sharding the RWA ledgers — design for a dedicated follow-up PR

**Status: DESIGN ONLY.** Nothing here is implemented. It is kept out of the
correctness/resilience PR that fixed the nightly's commit, gates and terminal
(docs/AUDIT-2026-09-23.md), because a historical storage migration has a larger blast
radius than those fixes and deserves its own review, its own merge window (§4.1) and its
own post-merge nightly.

Measured 2026-09-24 against the committed ledger (last ledger commit `bc3aa31`,
2026-09-23). Line numbers are as of the design date and will drift; the §2 tables name
the function or step as well, which is what to search for. The measurement scripts
(`measure.py`, `roundtrip.py`, `project.py`) were one-off and are not committed; §3.4
states what each measured and T6/T7 in §5 turn those measurements into tests.

Legend: **[measured]** means a script or command run here. **[inferred]** means read
from code but not executed. **[unverified]** means an assumption.

---

## 1. Current sizes and growth

### 1.1 Per file [measured]

| file | bytes | rows | B/row | rows/night 09-01 to 09-23 | last night's bytes | trend in bytes/night |
|---|---|---|---|---|---|---|
| `rwa_wrappers.csv` | 5,143,724 | 26,113 | 197.0 | 1,071 to **1,790** | 341,752 | +5.9 KB/day |
| `rwa_flow.csv` | 3,315,983 | 14,781 | 224.3 | 648 to 884 | 201,229 | +2.6 KB/day |
| `rwa_observed.csv` | 1,733,156 | 14,781 | 117.3 | 648 to 884 | 103,409 | +1.3 KB/day |
| `rwa_issuers.csv` | 67,312 | 683 | 98.6 | 34 to 32 | 3,122 | flat |
| `rwa_runs.csv` | 4,247 | 22 | 193.0 | 1 per run | ~160 to 180 | flat |
| `rwa_quarantine.csv` | not on disk | 0 | n/a | 1 per refused run | n/a | n/a |

Other facts from the same measurement:

- Every file is CRLF-terminated. In each file the CRLF count equals the LF count, so
  there are no bare LFs.
- Every file's header equals its `RWA_*_FIELDS` list exactly.
- No cell is `None`.
- Row order is date-nondecreasing in all five files.
- There are 21 recorded dates. **2026-09-21 and 2026-09-22 are absent**; these are the
  duplicate-key nights that were lost.
- All rows are in 2026-09, so today a monthly split produces exactly one shard per file.
- The growth is step-shaped rather than smooth: wrappers went up by 323 on 09-15 and by
  231 across the 09-20 to 09-23 gap.
- Git history (25 commits per file, back to creation on 2026-09-01; see `githist.txt`)
  agrees with the per-date bytes to the byte. Example: rwa_wrappers.csv went from
  4,801,972 to 5,143,724 between 8173d89 and bc3aa31, which is +341,752. That is
  exactly the 09-23 rows.

### 1.2 Projected dates [measured rate, extrapolated]

Limits are GitHub's 50 MiB warning and 100 MiB hard reject. Two scenarios are shown:

- **Constant**: every night adds what the last night added.
- **Accelerating**: bytes/night keep growing at the least-squares trend of 09-02 to 09-23.

| file | 50 MiB, constant | 50 MiB, accelerating | 100 MiB, constant | 100 MiB, accelerating |
|---|---|---|---|---|
| rwa_wrappers.csv | **2027-02-09** | 2026-12-13 | **2027-07-12** | 2027-02-05 |
| rwa_flow.csv | 2027-05-26 | 2027-02-03 | 2028-02-10 | 2027-04-25 |
| rwa_observed.csv | 2028-01-27 | 2027-04-21 | 2029-06-17 | 2027-08-14 |
| rwa_issuers.csv | ~2072 | ~2072 | never in practice | never in practice |
| rwa_runs.csv | ~780 years | n/a | n/a | n/a |

The constant-rate column reproduces the audit's "Feb 2027 / mid-2027". The accelerating
column is a pessimistic bound: it assumes the wrapper listing keeps growing linearly,
and that is **[unverified]**.

### 1.3 Monthly shard sizes

At tonight's rate a wrappers month shard is 342 KB × 31 = **10.6 MB**. Flow is 6.2 MB
and observed is 3.2 MB.

A wrappers shard reaches 50 MiB only when a night holds about **8,900 wrappers**
(50 MiB ÷ 31 ÷ 191 B), which is 5× today's count. It reaches 100 MiB at about 17,700.
Under the accelerating scenario that would happen around May 2027, so the validator
should warn well before then (see §4.6). If it ever happens, switching the granularity
to daily files changes only the path function (see §3.2).

---

## 2. Every affected reference

All line numbers are from the current checkout.

### 2.1 `rwa.py` (constants, readers, writers)

| line | what |
|---|---|
| 134 | `LEDGER_DIR = …/ledger` |
| 1735-1760 | `equity_prints()` → `read_rows(ledger/equity_sessions.csv)`. Not an RWA ledger; **unchanged**. |
| 1981 | `RWA_FLOW_CSV = LEDGER_DIR / "rwa_flow.csv"`. Used only as the default `path` at 2235, 2488 and 2508. |
| 1982 | `RWA_ISSUERS_CSV`. **Defined, never read anywhere** (grep). |
| 1983 | `RWA_WRAPPERS_CSV`. **Defined, never read anywhere** (grep). rwa_wrappers.csv has no reader in code except its own rewrite and the validator. |
| 1984 | `RWA_JSON` (unchanged) |
| 1990-1998 | `RWA_FLOW_FIELDS` (27 columns) |
| 1999-2002 | `RWA_ISSUER_FIELDS` |
| 2003-2007 | `RWA_WRAPPER_FIELDS` (15 columns) |
| 2039-2042 | `RWA_OBSERVED_FIELDS` (9 columns) |
| 2043-2049 | `RWA_RUN_FIELDS` (manifest and quarantine) |
| 2052-2063 | `_atomic_write()`. Writes a sibling `<name>.tmp` and then `os.replace`s it. In a shard directory the temp file would be `2026-09.csv.tmp`; see §4.6. |
| 2127-2140 | `prior_run_quality()` → `read_rows(rwa_runs.csv)` |
| 2143-2148 | `prior_run_status()` |
| 2176-2180 | `read_rows(path, fields)`. Projects each row onto `fields`; a column missing from the header yields `None`. |
| 2183-2203 | `append_daily_rows()`. Keeps every row whose date is not today, appends the fresh rows, writes `DictWriter(lineterminator="\r\n")` with the header, publishes atomically. It does **not sort**; order is preserved. |
| 2206-2214 | `_append_manifest()`. Appends and never replaces (runs, quarantine). |
| 2217-2244 | `_prior_flow(path, today)`. Latest row per underlying strictly before today, over the whole file. |
| 2286, 2297, 2400 | `assemble(volume_baseline=…)` consumes the baseline dict |
| 2481-2499 | `flow_series(path, window=30, today)`. Stable sort by date, excludes date ≥ today, keeps the last 30 records per underlying. |
| 2502-2517 | `volume_baseline(path, window=14, today)`. Same pattern, median of the last 14. |
| 2536 | `flow_csv = ledger_dir / "rwa_flow.csv"`. **Hard-coded name; does not use `RWA_FLOW_CSV`.** |
| 2606-2608 | `runs_csv = ledger_dir / "rwa_runs.csv"`; prior quality and status |
| 2612-2614 | `_prior_flow(flow_csv)`, `flow_series(flow_csv)`, `volume_baseline(flow_csv)`. Three full reads of the flow file per night. |
| 2660 | `_append_manifest(ledger_dir / "rwa_runs.csv")` |
| 2666-2672 | **The write loop**: `("rwa_observed.csv", …), ("rwa_flow.csv", …), ("rwa_issuers.csv", …), ("rwa_wrappers.csv", …)` → `append_daily_rows(ledger_dir / name, …)`. `written[name]` uses these file names as keys. |
| 2680, 2691 | `_append_manifest(ledger_dir / "rwa_quarantine.csv")`, `"retained_in": "rwa_quarantine.csv"` |
| 2704 | `equity_prints(ledger_dir)` |
| 2793 | `"written": written` is published in rwa.json. Current value: `{'rwa_runs.csv':1,'rwa_observed.csv':884,'rwa_flow.csv':884,'rwa_issuers.csv':32,'rwa_wrappers.csv':1790}`. index.html does not read it (grep for `.written` gives nothing). |
| 2801-2803 | `_atomic_write(rwa.json` or `rwa.degraded.json)` |
| 2924-2926 | `summarize()` prints the `written` keys |
| 515-547, 602 | `RWA_SPEC_FUNCTIONS` / `spec_hash`. **None of the I/O functions are in the spec**, so changing them does not move `spec_hash`. |

### 2.2 `nightly.py`

| line | what |
|---|---|
| 211 | `rwa = _load_sibling("rwa.py", "cm_rwa")` |
| 3919-3934 | `_append_context_rows()`. tests/test_rwa.py asserts that it and `rwa.append_daily_rows` produce the same bytes (both are CRLF: DictWriter defaults to `\r\n`). **Keep `append_daily_rows` unchanged** and put the shard logic in a wrapper. |
| 4592-4593, 4609-4733 | xsec precedent: `XSEC_DIR`, `XSEC_SCHEMA_JSON`, `XSEC_SCHEMA_VERSION=4`, `XSEC_FIELDS`, `XSEC_V2_FIELDS`, `XSEC_SHARED_FIELDS` |
| 5410-5427 | `xsec_by_date()`. Reads `sorted(d.glob("*.csv"))` and filters by `src`. |
| 5477-5481 | `xsec_shard_path(day)`. Validates `day[4]=="-"` and returns `XSEC_DIR/f"{day[:7]}.csv"`. |
| 5484-5577 | `write_xsec_schema()`. Writes the sidecar every night with `json.dumps(indent=2)+"\n"`. |
| 5580-5614 | `write_xsec()`. Replaces (date, src) inside the month shard, widens kept rows to the current fields, **sorts** deterministically, uses DictWriter with the default `\r\n`, and has no atomic write. |
| 6710 | the nightly calls `write_xsec(rows, today)` |
| 6962-7038 | `rwa.snapshot(getter=budget.getter(...))` under the new run budget, plus the `[rwa]` prints. A budget skip leaves every rwa file untouched. No file paths here; unchanged. |
| 6105-6113 | the new `RUN_FIELDS` for `ledger/runs.csv`, the crypto run manifest. It is a different file from `ledger/rwa_runs.csv`, and the names are close. |

### 2.3 `scripts/validate_ledger.py`

| line | what |
|---|---|
| 45-46 | loads nightly.py, so `nightly.rwa` is reachable |
| 376-500 | `check_xsec()`, the precedent: sidecar present, version and fields match, header == fields, non-empty, (date, symbol, src) unique, **no date outside its own month**, src allowed |
| 558-578 | the new `check_runs()` (ledger/runs.csv). Not RWA. |
| 763-900 | `check_rwa()` |
| 785 | `flow = ledger / "rwa_flow.csv"`; returns early if absent |
| 788-792 | empty-file check |
| 794-800 | duplicate (date, underlying_id) |
| 802-806 | blank key |
| 813-822 | residual ≤ −100 |
| 826-831 | crypto tier in label |
| 836-841 | unstamped spec_hash on the latest date |
| 844-871 | flow ↔ `rwa_runs.csv` manifest cross-check |
| 873-887 | `rwa_observed.csv` leaked-derived-column check, which uses the raw header `obs[0].keys()`, and the vendor-timestamp check |
| 889-898 | duplicate keys for `rwa_issuers.csv` (issuer_id), `rwa_wrappers.csv` (token_id), `rwa_observed.csv` (underlying_id) |
| 902-985 | `_check_rwa_artifact()` (rwa.json; `rwa_runs.csv` at 969). Unchanged. |
| 990-1024 | `main()`: `--scope`, `check_xsec` at 1018, `check_rwa` at 1024 |
| 1045-1063 | xsec summary print (precedent for an RWA shard summary line) |
| 1079-1087 | rwa.json summary print |

### 2.4 Workflows

Line numbers are as of `ed4220f`.

| file:line | what |
|---|---|
| nightly.yml:61-70 | "RWA ledger gate" `validate_ledger.py --scope rwa`, continue-on-error, comment names `rwa_flow.csv` (64) |
| nightly.yml:108-120 | "Record the run" (new): `--step rwa_gate=…` |
| nightly.yml:134-139 | when the RWA gate fails, rollback with `git ls-files -z -- 'ledger/rwa*' \| xargs git checkout HEAD --` (137) and `git ls-files --others -- 'ledger/rwa*' \| xargs rm -f` (138). **Verified in a scratch repo:** `'ledger/rwa*'` matches nested `ledger/rwa/flow/2026-09.csv` (tracked, restored) and a new untracked `ledger/rwa/flow/2026-10.csv` (removed). No change needed. |
| nightly.yml:183 | `git add ledger/runs.csv \|\| true` (new crypto run manifest) |
| nightly.yml:185-211 | comments naming rwa_flow, rwa_issuers, rwa_wrappers, rwa.json, rwa_observed, rwa_runs, rwa_quarantine |
| nightly.yml:212 | `git add ledger/rwa_flow.csv ledger/rwa_issuers.csv ledger/rwa_wrappers.csv ledger/rwa.json` (required) |
| nightly.yml:213-215 | `git add ledger/rwa_observed.csv \|\| true`; `…rwa_runs.csv \|\| true`; `…rwa_quarantine.csv \|\| true` |
| nightly.yml:218-235 | `git add ledger/xsec \|\| true`, the directory-pathspec precedent and its rationale |
| nightly.yml:239-249 | commit, then push with `git pull --rebase` retry (248) |
| nightly.yml:250-262 | leftover guard `git status --porcelain -- ledger/` (257) |
| nightly.yml:264-298 | "Commit what passed its own gate" (renamed from "Commit the RWA ledger alone"). Lines 278-281 repeat the 212-215 set inside `if [ "$RWA_GATE" = "success" ]`. The guard at 284 is `grep -v '^ledger/rwa' \| grep -v '^ledger/runs\.csv$'`; a nested `ledger/rwa/...` passes (verified). |
| nightly.yml:348-375 | "RWA smoke test" `python rwa.py`, moved after the commits. It does not write (checked `_smoke`). |
| nightly.yml:377-381 | fail the run when the RWA gate refused |
| rwa_release.yml:17, 33, 74-75 | comments |
| rwa_release.yml:82-85 | the same four `git add` lines |
| rwa_release.yml:88 | `grep -q '^ledger/rwa_runs.csv$'` (why runs.csv stays whole at its path) |
| rwa_release.yml:96 | `grep -v '^ledger/rwa'` guard |
| tests.yml:101 | `python tests/test_rwa.py` (standalone) |
| tests.yml:142 | comment "commits ledger/rwa*" |

### 2.5 Tests

| file:line | what |
|---|---|
| test_rwa.py:507-516 | same-day replace via `append_daily_rows(tmp/"rwa_flow.csv")` + `read_rows` (keep as is: helper unchanged) |
| test_rwa.py:519-528 | prior days untouched (keep) |
| test_rwa.py:531-546 | **the two append helpers agree** (keep; the wrapper must not change `append_daily_rows`) |
| test_rwa.py:792-795 | asserts `tmp_path/"rwa_flow.csv"`, `rwa_issuers.csv`, `rwa_wrappers.csv`, `rwa.json` exist; `written["rwa_flow.csv"]==1`; `read_rows(tmp/"rwa_flow.csv")`. **Needs an update.** |
| test_rwa.py:829-830 | two-night chain: `read_rows(tmp/"rwa_flow.csv")`. **Needs an update.** |
| test_rwa.py:1011-1034 | promotion invariant: `written["rwa_flow.csv"]`, `(tmp/"rwa_flow.csv").read_text()` before/after, quarantine and runs reads, `rwa.degraded.json`. **Flow paths need an update.** |
| test_rwa.py:1056 | `second["written"]["rwa_flow.csv"]==1` |
| test_rwa.py:1096-1104 | `read_rows(tmp/"rwa_observed.csv")`. **Needs an update.** |
| test_rwa.py:1124-1134 | `_atomic_write` leaves no `*.tmp` (keep) |
| test_rwa.py:1311-1348 | same-day re-run does not zero the impulse: `read_rows(tmp/"rwa_flow.csv")`. **Needs an update.** |
| test_rwa.py:1467, 1484 | `equity_prints` (unchanged) |
| test_rwa.py:1625, 1654-1666 | `summarize` with `written={"rwa_flow.csv":…}`, `retained_in` (unchanged if the logical keys are kept) |
| test_rwa.py:1693-1720 | workflow staging: every added path starts with `ledger/rwa`; one optional path per `\|\| true` line; release set == nightly's `ledger/rwa*` set; `grep -q '^ledger/rwa_runs.csv$'`; guard string |
| test_rwa.py:1734 | docstring naming the three files |
| test_nightly.py:132-150 | `_staged_by` and **every top-level `ledger/` entry must be staged** (`iterdir`, `rstrip("/")`). `ledger/rwa` must appear in a `git add`. |
| test_nightly.py:153-161 | leftover guard present after the commit |
| test_nightly.py:174-207 | RWA gate / rollback / "Commit what passed its own gate" structure: `if [ "$RWA_GATE" = "success" ]`, `grep -v '^ledger/rwa' \| grep -v '^ledger/runs\.csv$'` |
| **test_run_budget.py:155, 169** | `{p.name: p.read_bytes() for p in (work/"ledger").glob("rwa*")}` before and after a zero-budget run. **This breaks after sharding**: `glob("rwa*")` returns the directory `ledger/rwa`, and `read_bytes()` on it raises `IsADirectoryError`. It must become a walk over files keyed by relative path, e.g. `{str(p.relative_to(L)): p.read_bytes() for p in L.rglob("*") if p.is_file() and p.relative_to(L).parts[0].startswith("rwa")}`. **[measured]** in a scratch tree: `glob("rwa*")` returns `rwa` and `rwa.json` and raises `IsADirectoryError`; the rglob form works. |
| test_validate_ledger.py:282-297 | writes `ledger/rwa_flow.csv` with a duplicate key; expects verdict `{"crypto":0,"rwa":1,"all":1}`. It keeps passing only if the validator still reads a legacy monolith (the transitional reader does). |
| test_validate_ledger.py:331-378 | `rwa.json` / `rwa_runs.csv` helpers (unchanged) |
| test_xsec.py:224-259, 448-479, 481-492 | precedents: month-shard path, byte-identical re-run, page never reads the shards, writer not in the spec |
| test_drawer.py:138-143 | `copytree(ledger)` with a comment about ledger subdirectories. It handles a new `ledger/rwa/` tree. |

### 2.6 Browser and docs

The browser never fetches an RWA CSV. The only RWA fetch is `ledger/rwa.json`
(index.html:8205); the CSV names appear in prose only.

| file:line | text |
|---|---|
| index.html:1485 | `<code>ledger/rwa_issuers.csv</code> is for` (issuers stays whole, so no change) |
| index.html:1509 | `anywhere except <code>ledger/rwa_flow.csv</code>` → `ledger/rwa/flow/` |
| index.html:8500 | `<code>ledger/rwa_wrappers.csv</code> and <code>ledger/rwa_issuers.csv</code>` → `ledger/rwa/wrappers/` |
| methodology.html:289, 327 | `ledger/rwa_flow.csv` |
| methodology.html:431 | `rwa_observed.csv` |
| methodology.html:432, 435 | `rwa_runs.csv`, `rwa_quarantine.csv`, `rwa.degraded.json` (unchanged) |
| docs/AUDIT-2026-09-23.md:48, 62, 152-156, 170, 178 | historical record: leave as is, add a closure note |

---

## 3. Proposed layout, read API and write rule

### 3.1 What gets sharded

```
ledger/rwa/flow/YYYY-MM.csv       + ledger/rwa/flow/SCHEMA.json
ledger/rwa/wrappers/YYYY-MM.csv   + ledger/rwa/wrappers/SCHEMA.json
ledger/rwa/observed/YYYY-MM.csv   + ledger/rwa/observed/SCHEMA.json
ledger/rwa_issuers.csv            (whole)
ledger/rwa_runs.csv               (whole, manifest)
ledger/rwa_quarantine.csv         (whole, manifest)
ledger/rwa.json, rwa.degraded.json (unchanged)
```

Why the rest stays whole:

- **rwa_runs.csv and rwa_quarantine.csv** have different write semantics:
  `_append_manifest` never replaces, while daily replace-by-date is the semantics the
  shard rule is built on. They grow about 180 B per run (roughly 66 KB/year).
  `rwa_release.yml:88` hard-codes `^ledger/rwa_runs.csv$` as its provenance check, and
  `prior_run_quality` and the validator cross-check need every run in one place. A
  single file is the audit trail; sharding it buys nothing.
- **rwa_issuers.csv** grows 3.1 KB a night, about 1.1 MB a year, so 50 MiB is decades
  away. It has no reader in code. Leaving it avoids editing the index.html prose at
  1485 and 8500. Re-evaluate if issuers exceed about 1,000 or the file exceeds 10 MB.
  Sharding it later costs one more entry in the table below.

Why one sidecar per kind: it mirrors `ledger/xsec/SCHEMA.json`, so a generic validator
helper can serve xsec and all three RWA kinds.

Why `ledger/rwa/…` and not `ledger/rwa_flow/…`: a single top-level entry for
test_nightly's allowlist check, a single directory pathspec in each workflow, and it
still matches `^ledger/rwa` and `'ledger/rwa*'`, the existing guard and rollback.

### 3.2 API in rwa.py

These functions are not in `RWA_SPEC_FUNCTIONS`, so `spec_hash` does not move.

```python
RWA_SHARD_ROOT = "rwa"
RWA_SHARD_SCHEMA_VERSION = 1
RWA_SHARDED = {                       # kind -> (fields, row key, legacy monolith name)
    "flow":     (RWA_FLOW_FIELDS,     "underlying_id", "rwa_flow.csv"),
    "wrappers": (RWA_WRAPPER_FIELDS,  "token_id",      "rwa_wrappers.csv"),
    "observed": (RWA_OBSERVED_FIELDS, "underlying_id", "rwa_observed.csv"),
}
_SHARD_RE = re.compile(r"^\d{4}-\d{2}\.csv$")

def rwa_shard_path(ledger_dir: Path, kind: str, day: str) -> Path:
    # same validation as nightly.xsec_shard_path; ValueError on a non-ISO day
    return ledger_dir / "rwa" / kind / f"{day[:7]}.csv"

def read_ledger(ledger_dir: Path, kind: str) -> list[dict]:
    """Every recorded row of one RWA ledger, in recorded order.
    1. legacy monolith ledger_dir/<legacy> if it still exists (transitional)
    2. then each shard matching _SHARD_RE in sorted filename order (YYYY-MM sorts
       chronologically as text)
    Each row is projected onto the CURRENT fields. A column absent from a shard's header
    reads "" (not None), which is what the monolith held after its next rewrite."""

def append_daily_shard(ledger_dir, kind, today, rows) -> int:
    n = append_daily_rows(rwa_shard_path(ledger_dir, kind, today), fields, today, rows)
    write_rwa_shard_schema(ledger_dir, kind)   # deterministic bytes, so no diff when unchanged
    return n
```

`_prior_flow`, `flow_series` and `volume_baseline` each gain a `rows=None` keyword. When
it is given they use it instead of `read_rows(path)`, and their body logic is otherwise
unchanged. `snapshot()` calls `flow = read_ledger(ledger_dir, "flow")` **once** and
passes it to all three. That replaces three full reads with one.

In the write loop at 2666-2672, flow, observed and wrappers go through
`append_daily_shard`, and issuers stays on `append_daily_rows`. The `written` dict keeps
its logical keys (`"rwa_flow.csv"`, …) so rwa.json, `summarize()` and 5 test assertions
don't change. Optionally add `written_paths` with the shard paths.

SCHEMA.json per kind contains:

- `schema_version`, `fields`, `row_key: ["date", key]`
- `shard: "one file per calendar month, ledger/rwa/<kind>/YYYY-MM.csv"`
- `line_terminator: "\r\n"`, `header: "every shard starts with the field header"`
- `row_order: "recorded order; within a shard, dates nondecreasing"`
- `append_only` text: a shard is rewritten only to replace its own date's rows, and a
  closed month is never opened again
- for flow, the "cannot be backfilled" provenance note
- `added_at_vN` lists, for later schema growth

### 3.3 Write rule

Tonight's rows go only into `rwa/<kind>/{today[:7]}.csv`, with the existing
`append_daily_rows` semantics: keep rows whose date is not today, append the fresh rows,
CRLF, header first, atomic replace.

`today` comes from `now` in UTC (rwa.py:2534), so a run never writes a past month and
closed shards are never touched. They stay byte-stable, so git records no diff for them.

### 3.4 Proof that reads are unchanged

Claim: for every sequence of nightly writes, the rows read from the shards equal the rows
read from the monolith, in the same order and with the same parsed values.

The proof rests on four facts.

- **(a) Round-trip stability [measured on all 5 real files].**
  `DictWriter(fields, lineterminator="\r\n")` over `read_rows(path, fields)` reproduces
  each committed file **byte for byte** (`roundtrip.py`: all True). A rewrite therefore
  never changes the bytes of rows it merely keeps.
- **(b) Partition.** Each row's shard is a function of its own `date` (`date[:7]`). So a
  date's rows live in exactly one shard, and `append_daily_rows` on the shard for `today`
  keeps and discards exactly the rows the monolith rewrite would, restricted to that
  month. Rows of other months are untouched in both layouts; by (a) the monolith's
  rewrite re-emits them unchanged.
- **(c) Order.** The monolith is kept rows (in order) followed by fresh rows. Kept rows
  keep their relative order and fresh rows carry `today`. As long as `today` does not go
  backwards across runs, the monolith is date-nondecreasing. That is measured on all 5
  files and is to be asserted by a test. Month blocks are then contiguous and in
  chronological order, and shard filenames `YYYY-MM.csv` sort chronologically.
  Therefore header + concat(shard bodies in filename order) == monolith. **[measured]**
  on the real files: `header+concat(bodies)==monolith bytes` is True for all five.
- **(d) Reader invariance even without (c).** `_prior_flow` takes the max date per
  underlying; only its tie behaviour depends on order, and ties are duplicate keys, which
  the validator forbids. `flow_series` and `volume_baseline` do a *stable* sort by date,
  so they depend only on within-date relative order, and (b) preserves that. So even a
  hypothetical out-of-order monolith gives identical reader outputs.

Caveats to state in code:

- **CRLF.** Both helpers write `\r\n`: rwa explicitly, nightly and xsec through the
  DictWriter default. Shards must be read with `newline=""`. They are never
  `read_text()`-compared; tests compare bytes (see test_rwa.py:1129-1133).
- **Header.** Every shard carries the header, so each shard is independently valid CSV.
  "Concatenation" means header once plus bodies.
- **Schema widening.** A monolith rewrite fills a new column with `""` on all old rows. A
  closed shard keeps its older header forever. `read_ledger` normalises the missing
  column to `""`, so parsed values match the monolith's steady state. Unlike
  `check_xsec`, which fails any closed shard whose header differs, the RWA validator
  should accept a closed shard whose header is a prefix of the current fields listed in
  the sidecar's `added_at_vN`.

---

## 4. Cutover and backward compatibility

### 4.1 Timing (recommended)

**Merge before 2026-10-01.** Every row today is dated 2026-09, so the migration is a pure
rename: `2026-09.csv` is byte-identical to the monolith (measured). Git records R100
renames and `git log --follow` keeps history.

Merge in a window when no ledger-writer is running. The nightly runs 06:17 UTC by cron
but has landed between 11:00 and 13:00 (and as late as 18:46); tests run at 15:40. If
the merge lands while a nightly is in flight, that run, on old code, rewrites
`ledger/rwa_flow.csv`. Its `git pull --rebase` then meets a modify/delete conflict, all 4
push attempts fail, and the night is lost, including an irreplaceable flow night.
**[inferred]** from nightly.yml:239-249 (and 293-298).

Safest option: after the day's nightly has pushed, dispatch nothing, merge, then watch
the next nightly.

### 4.2 Migration script: `scripts/shard_rwa_ledgers.py`

It has `--check` (default, read-only) and `--apply` modes. For each kind:

1. `src = read_rows(legacy, fields)`, plus any shard rows already present (the union, in
   the transitional case), and `raw = legacy.read_bytes()`.
2. Refuse if any date is non-ISO, or if any (date, key) is duplicated in the union.
3. Group by `date[:7]`, preserving order, and render each shard with the
   `append_daily_rows` writer into a temp directory.
4. Verify all of the following:
   - `read_ledger(tmp)` == `src` (list-of-dict equality)
   - if no shard pre-existed, `header + concat(bodies)` == `raw` (bytes)
   - sum of shard rows == `len(src)`
   - each shard's dates are within its month
   - sha256 of the monolith, the row count and the last date are printed so they can be
     pinned in a test (§5, T7)
5. Only then `os.replace` the shards into `ledger/rwa/<kind>/`, write SCHEMA.json and
   delete the monolith. The PR commits this as `git rm` plus `git add ledger/rwa`.
6. Idempotent: with no legacy file present it verifies the shards and exits 0 without
   changes.

### 4.3 Reader during the transition

`read_ledger` returns **legacy + shards** (the union):

| state | reader behaviour |
|---|---|
| legacy present, no shards (code merged, migration not yet run) | full history from legacy; tonight's write goes to the shard; the union stays correct |
| both present | union; an overlapping date shows up as a duplicate key and the validator fails |
| shards only | the steady state |

The validator must **not** fail the RWA gate merely because a monolith coexists. That
would withhold and roll back tonight's rows, and flow nights cannot be re-fetched. It
prints a notice instead. The "no monolith" invariant is enforced in the CI suite
(tests.yml), not the nightly gate (T12).

### 4.4 Workflow changes

These apply to nightly.yml:212-215, nightly.yml:278-281 (inside `if [ "$RWA_GATE" = "success" ]`) and rwa_release.yml:82-85, and
must be identical in all three places.

```
git add ledger/rwa ledger/rwa_issuers.csv ledger/rwa.json   # required; the directory exists after migration
git add ledger/rwa_runs.csv || true
git add ledger/rwa_quarantine.csv || true
```

- Remove `git add ledger/rwa_observed.csv || true`, since observed is now under `ledger/rwa/`.
- A directory pathspec also stages new month shards and SCHEMA.json. `git add <dir>`
  stages deletions too.
- test_rwa.py:1693-1720 keeps passing: all paths start with `ledger/rwa`, there is one
  optional path per `|| true` line, and the release set equals the nightly's set.
- test_nightly.py:139 passes because the top-level entry `ledger/rwa` is staged. The
  monolith names no longer exist on disk.
- Rollback (nightly.yml:137-138) and both `grep -v '^ledger/rwa'` guards need **no
  change**; verified in a scratch repo.
- Optional: `rwa_release.yml` keeps its `^ledger/rwa_runs.csv$` check unchanged.
- The guard in "Commit what passed its own gate" (nightly.yml:284),
  `grep -v '^ledger/rwa' | grep -v '^ledger/runs\.csv$'`, lets `ledger/rwa/<kind>/*.csv`
  through unchanged.
- Update the comments at nightly.yml:64, 185-211 and 267-270 and rwa_release.yml:74-75.

### 4.5 Month rollover and same-day re-run

**Rollover.** Say the night of 2026-10-01 UTC:

1. `rwa_shard_path` returns `rwa/flow/2026-10.csv`, which doesn't exist yet.
2. `append_daily_rows` reads nothing and writes header + rows.
3. `_prior_flow` reads 2026-09 and 2026-10 and finds the 09-30 rows. The chain continues.
4. `git add ledger/rwa` stages the new untracked shard.
5. If the RWA gate fails, rollback removes it (verified).
6. The 2026-09 shard is not rewritten, so it has no diff.

**Same-day re-run** (workflow_dispatch or rwa_release):

- Only the current month's shard is rewritten, and only today's rows are replaced.
- The readers exclude `date >= today` across all shards, which is the same as today.
- The promotion invariant (`rwa_runs.csv`) is unchanged.
- A re-run on the 1st reads the prior month's shard for its prior value (T5).

### 4.6 Validator: `check_rwa` over shards

Reuse the xsec pattern through a helper `_check_shard_dir(d, fields, key, version)`:

- The sidecar is present and parses, with the right version and fields.
- The directory holds only `^\d{4}-\d{2}\.csv$` files plus SCHEMA.json. This catches a
  stray `2026-09.csv.tmp` from `_atomic_write`, which the directory pathspec would
  otherwise commit.
- Each header equals the fields (or is an allowed older prefix).
- No shard is empty (the writer never writes an empty row set).
- **No date lies outside its own month.**

Then run the existing checks over `read_ledger(ledger, kind)`, which is the union:

- duplicate (date, key), blank key, residual ≤ −100, crypto tier, latest-date spec_hash
- the manifest cross-check and the observed timestamp check
- the leaked-derived-column check runs against **each shard's header**

**Duplicates across shards.** They cannot straddle shards: the key contains `date` and
the shard is a function of `date`. So once the stray-date check passes, a per-shard
duplicate check is equivalent to a global one. The global check over the union is still
cheap, and it also covers legacy/shard overlap during the transition.

Add a size warning, not a failure: any shard over 25 MB. That is the early signal from
§1.3 that monthly granularity is running out.

The summary print should follow the xsec model: `rwa shards: flow 1 shard(s) 14,781
rows; …`.

---

## 5. Migration tests to add

Put them in tests/test_rwa.py, above the final `if __name__` block (it must stay last,
see test_rwa.py:1767-1772), so the release workflow's standalone gate runs them.
Validator tests go in tests/test_validate_ledger.py.

- **T1 `test_a_date_lands_in_its_month_shard`**
  - `rwa_shard_path(tmp,"flow","2026-09-14") == tmp/"rwa"/"flow"/"2026-09.csv"`
  - `ValueError` for `"not-a-date"`, `""` and `"2026/09/14"`
- **T2 `test_a_same_day_rerun_rewrites_only_the_current_shard`**
  - Write 2026-08-31, then 2026-09-01 twice (residual 1.0, then 2.0).
  - `2026-08.csv` bytes are identical before and after both 09-01 writes.
  - `2026-09.csv` holds exactly one row, with `residual_pct == "2.0"`.
- **T3 `test_the_month_rollover_opens_a_new_shard_and_leaves_the_old_one`**
  - Write 09-30, then 10-01.
  - The directory holds exactly `{"2026-09.csv","2026-10.csv","SCHEMA.json"}`.
  - The 09 bytes are unchanged by the 10-01 write.
  - `[r["date"] for r in read_ledger(tmp,"flow")] == ["2026-09-30","2026-10-01"]`
- **T4 `test_the_chain_extends_across_a_month_boundary`**
  - Clone of test_rwa.py:801-830 with `now=2026-09-30T03:00Z`, then +1 day.
  - `residual_pct ≈ 12.381 (abs .01)`, `impulse == IMPULSE_MINTING`, `supply_index ≈ 112.381`
  - `rwa/flow/2026-09.csv` and `2026-10.csv` each hold 1 row.
- **T5 `test_a_same_day_rerun_on_the_first_of_the_month_does_not_zero_the_impulse`**
  - Clone of test_rwa.py:1311-1348 with n2 = 2026-10-01.
  - `again.residual_pct ≈ 20.0`, `again.impulse == IMPULSE_STRONG`.
  - `again.chain_days == first.chain_days`
  - Exactly one 2026-10-01 row across the shards.
- **T6 `test_shard_writes_equal_monolith_writes_byte_for_byte`**
  - Drive one script of (today, rows) through both `append_daily_rows(mono)` and
    `append_daily_shard(dir)`. The script spans 08-30 to 10-02, includes a same-day
    re-run in each month, and includes a name containing a comma, which gets quoted.
  - After every step: `header + b"".join(bodies in sorted(shard names)) == mono.read_bytes()`.
  - After every step: `read_ledger(dir,"flow") == read_rows(mono, RWA_FLOW_FIELDS)`.
- **T7 `test_the_real_committed_ledger_round_trips`** (the key test)
  - Pre-migration, for each kind: migrate `ROOT/ledger` into tmp.
    - `read_ledger(tmp,k) == read_rows(ROOT/ledger/legacy, fields)`
    - `header+concat(bodies) == legacy.read_bytes()`
    - each shard's dates `[:7] == stem`
  - Post-migration: pin `MIGRATION = {kind: (sha256, n_rows, last_date)}` as printed by
    the script, and re-render the rows of `read_ledger(ROOT/ledger,k)` with
    `date <= last_date` using the append writer:
    - `sha256(rendered) == pinned`
    - `len == n_rows`
  - This proves the migrated history is the old file, byte for byte. It needs no git
    history (checkout is shallow at depth 1) because of (a) in §3.4.
- **T8 `test_the_readers_see_the_same_series_either_way`**
  - Fixture: 3 months, 4 underlyings, gaps and one re-run. For each `today` in a
    handful of dates, including the 1st of a month:
    - `_prior_flow(mono,today) == _prior_flow(rows=read_ledger(dir),today)`
    - the same for `flow_series` and `volume_baseline`
- **T9 `test_no_real_shard_holds_a_date_outside_its_month`**
  - For every `ledger/rwa/*/*.csv`, `{r["date"][:7]} == {path.stem}`.
- **T10 `test_every_real_shard_is_crlf_with_the_current_header`**
  - `raw.count(b"\n") == raw.count(b"\r\n")`
  - the first line == `",".join(fields)`
  - dates are nondecreasing within the file
- **T11 `test_the_sidecars_describe_their_shards`**
  - `schema_version == RWA_SHARD_SCHEMA_VERSION`, `fields == list(fields)`,
    `row_key == ["date", key]`, `line_terminator == "\r\n"`
  - two consecutive `write_rwa_shard_schema` calls produce identical bytes
- **T12 `test_no_legacy_monolith_remains_and_no_workflow_names_one`**
  - `ledger/rwa_{flow,wrappers,observed}.csv` do not exist.
  - Neither nightly.yml nor rwa_release.yml has a `git add` line containing
    `rwa_flow.csv`, `rwa_wrappers.csv` or `rwa_observed.csv`.
  - `ledger/rwa` is staged by both.
- **T13 `test_the_shard_dirs_hold_only_shards_and_a_sidecar`**
  - Every file under `ledger/rwa/<kind>/` matches `^\d{4}-\d{2}\.csv$` or is `SCHEMA.json`.
- **T14 `test_the_transitional_reader_unions_legacy_and_shards`**
  - A monolith with 09-01 and 09-02 plus a shard with 09-03 reads as dates `[01,02,03]`.
  - Adding 09-02 to the shard makes `check_rwa` report `duplicate (date, underlying_id)`.
- **T15 `test_the_shard_helpers_are_not_part_of_the_specification`**
  - For `read_ledger`, `append_daily_shard`, `rwa_shard_path` and `write_rwa_shard_schema`:
    each is not in `rwa.spec()["functions"]`, and its name appears in no captured body.
- **T16 `test_the_rwa_rollback_reaches_nested_shards`** (test_nightly.py)
  - Uses subprocess git in tmp: a tracked modified `ledger/rwa/flow/2026-09.csv` is
    restored by the nightly.yml:137 command.
  - An untracked `2026-10.csv` is removed by the :138 command.
  - `grep -v '^ledger/rwa'` accepts `ledger/rwa/flow/2026-10.csv`.
- **Migration script tests**
  - `test_the_migration_is_idempotent`: a second `--apply` is a no-op, with bytes and
    mtimes unchanged.
  - `test_the_migration_refuses_and_keeps_the_monolith_on_mismatch`: monkeypatch the
    renderer to drop a row; the exit code is non-zero, the legacy file is still present,
    and no shard is written.
- **Validator tests** (test_validate_ledger.py)
  - `test_a_stray_date_in_a_shard_fails_the_rwa_scope`: the message contains
    "outside its own month".
  - `test_an_empty_shard_fails`
  - `test_a_tmp_file_in_a_shard_dir_fails`
  - `test_legacy_beside_disjoint_shards_is_reported_not_failed`: `--scope rwa` returns 0
    and prints a notice.
  - Update test_validate_ledger.py:282-297 to write a shard. Keep a legacy variant to
    cover T14.
- **Existing tests to update**
  - test_rwa.py:792-795, 829, 1012-1022, 1096 and 1345: change their paths to
    `read_ledger(tmp_path, kind)` or the shard path.
  - Keep 507-546 unchanged, since that helper is untouched.
  - **tests/test_run_budget.py:155 and 169**: replace `glob("rwa*")` + `p.name` with a
    recursive, file-only walk keyed by relative path. Otherwise the test raises
    `IsADirectoryError` the moment `ledger/rwa/` exists.

---

## 6. `ledger/signals.json` (recommend, do not implement)

### 6.1 Measured

**Size and growth.**
- The file is 5,079,074 B pretty-printed (`indent=2`, nightly.py:6702-6703) and
  358,436 B gzipped.
- It grows about **101.5 KB a night** (5,079,074 − 4,368,477 over the 7 commits from 09-14
  to 09-23).
- That rate reaches 50 MiB around Jan 2028. This is not the urgent file.

**Composition.**
- `rows` is 4,699,462 B pretty (92.5%): 2,590 rows × 71 keys, 52 dates from 2026-08-01
  to 2026-09-23, 50 rows a night.
- `by_decile` is 1.2 KB. `total_signals` and `generated_at` are trivial.

**`rows` duplicates signals.csv exactly.**
- The key set is identical and there are **0 differing cells** out of 2,590 × 71
  compared as strings.
- 145 cells are floats: tonight's fresh values, which are string-equal to the CSV.

**Minified** (`separators=(',',':')`):
- **3,581,521 B (−29.5%)**; gzipped 337,342 B (−5.9% on the wire).
- About 68.9 KB a night of growth instead of about 90 to 101 KB.
- The 50 MiB horizon moves to about Sep 2028.

**Windowed (minified)**:

| window | bytes | gzipped |
|---|---|---|
| 1 night | 73 KB | 9.5 KB |
| 7 nights | 507 KB | 58 KB |
| 14 nights | 1.01 MB | 114 KB |
| 30 nights | 2.15 MB | 236 KB |

Without rows the file is 932 B. For comparison, signals.csv is 744,509 B raw and 213,621 B
gzipped: the full history as CSV is smaller on the wire than minified JSON.

### 6.2 Consumers

**index.html** reads only `j.rows`. There are no references to `by_decile`,
`total_signals` or `generated_at`.

| line | use |
|---|---|
| 1963 | `fetch("ledger/signals.json", {cache:"no-cache"})` |
| 1970-1975 | `ALL_ROWS = j.rows` (the **full** history), `LEDGER_BY_DATE`, `REWIND_DATES` (every date; the rewind scrubber), `LEDGER = rows with era` (the Dune LIVE count at 2032) |
| 2026-2030 | `PERPS` from **every** row with no date filter, last write in file order wins: `funding_ann_pct, oi_usd, oi_chg_24h_pct, oi_to_mcap, long_short_ratio, oi_price_divergence`. This is the same stale-carry pattern as the retired PERP overlay (rows persist 50 names a night). **[inferred]** as a latent staleness bug; a window would incidentally fix it. |
| 2084-2101 | latest row per symbol → `STRAT` (strategy, adx, adx_bars, adx_regime), `ATR14` (atr14, adx_bars, date), `TURN_Z` (turnover_z) |
| 4633 | drawer: `ALL_ROWS.find(symbol, date==obsDate)` → era, perp_mult, unlocks_usd |
| 7741-7790 | `rewindRows(d)` / `rowToState`: date, symbol, name, price, market_cap, turnover_pct, fdv_usd, conviction, high_24h, low_24h, rs7/14/30/200, rs_blend, c_liquidity, c_era, c_depth, c_momentum, emission_mult, emission_drag, funding_apr, funding_regime, strategy, adx, adx_bars |
| 4683 | a link to `ledger/signals.json` |

Roughly 40 of the 71 keys are used. Unused examples: roi_30d/90d, survived, spec_hash,
the liq_* fields, the funding trail fields, corr_* and tmd_*. This list comes from grep
and is not exhaustive. **[unverified]**

**Python and tests**

| file:line | use |
|---|---|
| nightly.py:28 | `LEDGER_JSON` |
| nightly.py:6673-6703 | the writer |
| validate_ledger.py:158-177 | `check_mirror`: `len(rows) == len(signals.csv)` |
| test_atr_eligibility.py:54, 73-76, 240-245, 471-476 | the **nightly ATR gate**. It needs the full history (`_series_from_ledger(prior)`) and the fields on `rows[0]`. |
| test_render.py:65-68 | latest night |
| test_drawer.py:67-68, 144-150 | full history; also writes a fixture copy |
| test_parity.py:409-420 | the last 6 dates plus 09-18 and 10-01 replayed |
| test_perp_overlay.py:188, 199, 210 | needs the **2026-08-03** rows (evidence) and all dates |
| test_validate_ledger.py:45, 135-137 | fixtures |
| test_rwa.py:1713 | the name must not appear in rwa_release |
| test_xsec.py:458 | prose |

**Docs:** DECISION-1.0-universe.md:63-75 and 284-286; AUDIT-2026-09-23.md:157;
AUDIT-PHASE1-2026-09-17.md:8, 73 and others; methodology.html:144.

### 6.3 Recommendation

1. **Now, zero consumer change:** write it minified with
   `json.dump(summary, f, separators=(",", ":"))`. That is −1.5 MB on disk and −31% growth
   per night. Every consumer uses `json.load` or `JSON.parse`. Grep found no test that
   asserts indentation, though that is **[unverified]** beyond grep.
   - Cost: GitHub's web diff of one 3.5 MB line is unreadable. Git's pack delta is
     byte-based and still compresses. Per DECISION-1.0 §0.3, do not gzip a committed file.
   - Wire savings are small (−6% gzipped). This mostly helps the repository and parse
     time, not the download.
2. **Later, the real fix for page load:** keep signals.json as summary plus a rolling
   14-night `rows` window (1.0 MB, 114 KB gzipped). That covers the board, the latest
   per-symbol maps, the drawer's current snapshot and a two-week rewind.
   - Lazily fetch the full `signals.csv` (214 KB gzipped) the first time the rewind
     scrubber leaves the window.
   - Move the Python tests to `csv.DictReader(signals.csv)`. The cells are identical as
     strings, measured. This matters most for test_atr_eligibility, the ATR gate, and
     test_perp_overlay's 2026-08-03 evidence rows.
   - Change `check_mirror` to "window ⊆ csv".
   - Re-read PERPS against a window and confirm the product intent.
3. Do **not** drop `rows` outright without step 2. Rewind, the drawer, STRAT/ATR14/TURN_Z
   and the nightly ATR gate all read it.

---

## 7. Side findings

Found while designing this. Status as of the correctness PR:

- **`rwa.degraded.json` was never staged — FIXED in the correctness PR.** rwa.py writes
  it on every refused (quarantined) run and methodology.html promises it is retained;
  no `git add` named it, so the new leftover guard would have turned such a night red and
  the degraded board would have died with the runner. Both writers now stage it. After
  sharding it stays at `ledger/rwa.degraded.json` (not append-only; not sharded).
- **`PERPS` stale carry — FIXED in the correctness PR.** The drawer's recorded
  derivatives map is now built from the newest recorded night only (§6.2 line
  2026-2030 described the old behaviour).
- **`ledger/rwa.json` (2.2 MB) — lazy-loaded in the correctness PR** on the first RWA
  reveal. It is rewritten whole each night and scales with the wrapper count; it is not
  append-only, so sharding does not apply, but its size belongs on the same watch list.
- **`RWA_ISSUERS_CSV` and `RWA_WRAPPERS_CSV` are dead constants**, and `snapshot()`
  hard-codes `ledger_dir / "rwa_flow.csv"` instead of `RWA_FLOW_CSV`. The sharding PR
  should derive every path from `RWA_SHARDED`.
