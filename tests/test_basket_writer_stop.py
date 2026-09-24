"""The retired basket's writer is gone, its record is archival, and the macro regime it
used to compute now comes from ledger/macro.csv (2026-09-24).

docs/DECISION-2026-09-24-model-layer.md §B.7 enumerated what stopping the writer would
change; this pins each answer:

  * nothing writes the basket's record any more — not the nightly's code, not its
    workflow — and the record is byte-for-byte what was published on the last night;
  * the regime the walk-forward study splits on is sourced from macro.csv, with label
    parity against every recorded night the new source can label, and it FAILS rather
    than guesses when the macro history cannot support a label;
  * the walk-forward study's history is unchanged;
  * the words: output that describes the canonical Index says so.
"""
import ast
import hashlib
import importlib.util
import json
import re
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("writer_stop_nightly", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)
SRC = (ROOT / "nightly.py").read_text(encoding="utf-8")
WF = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")

# The archived record as the 2026-09-24 nightly (commit 207ea9e) left it.
ARCHIVE_SHA256 = {
    "basket.json": "da3778bf112cfa41919bacbdfcfa02ea100823fd4b531121eabcd70131a27493",
    "index.csv": "be69877946e0751252daf063cee0b9940512e496c060568df7e00a3f62061525",
    "index.legacy.csv": "fa7b9b4f7bc5970ac0a3d4a05220f0b7eb5555e63f7963520ed789f50ed32b8c",
}
# Every key of index.json except `canonical` (the live mirror), canonical JSON.
INDEX_JSON_ARCHIVE_SHA256 = "e2f89dd4bf4937827dbedcd41a98ee37b7a46983c24651ebd23405a4c2a6169b"
# walkforward_report over the cross-section through the retirement night, with the
# recorded regimes, generated_at removed: identical to the committed 2026-09-24
# ledger/walkforward.json, which was built with the old index.csv-sourced regimes.
WALKFORWARD_THROUGH_RETIREMENT_SHA256 = (
    "df2d57964455f085b7f1e3bc4f7fc0c7ddcc9034ceb4ed3ffab878387ff721cc")

REMOVED = ("build_basket", "_write_index_row", "_persist_index_row", "_normalize_live",
           "_macro_regime_from_ledger", "fetch_global_market_cap", "_risk_stats",
           "REBALANCE_DAYS", "EJECT_RANK", "EJECT_GAP")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True).encode()


# ---------------------------------------------------------------------------
# the basket no longer mutates
# ---------------------------------------------------------------------------
def test_the_writer_is_gone_and_main_does_not_reach_it():
    for name in REMOVED:
        assert not hasattr(nightly, name), f"nightly.{name} still exists"
    tree = ast.parse(SRC)
    main_src = "".join(ast.get_source_segment(SRC, n) for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name in ("main", "_main"))
    assert "basket" not in re.sub(r"#.*", "", main_src).lower().replace("basket_retired_on", ""), (
        "main() still names the basket")


def test_no_code_path_writes_the_archived_record():
    """No function but the archive reader names index.csv, and nothing names basket.json
    or the legacy file at all. index.json is touched by one function, which rewrites the
    canonical key only (pinned below)."""
    tree = ast.parse(SRC)
    uses = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            for const in ("INDEX_CSV", "BASKET_JSON", "INDEX_LEGACY_CSV", "INDEX_JSON"):
                if const in names:
                    uses.setdefault(const, set()).add(node.name)
    assert uses.get("INDEX_CSV", set()) <= {"read_index_rows"}, uses
    assert "BASKET_JSON" not in uses and "INDEX_LEGACY_CSV" not in uses, uses
    assert uses.get("INDEX_JSON", set()) <= {"_refresh_index_canonical"}, uses


def test_refreshing_the_canonical_mirror_leaves_the_archive_untouched(tmp_path, monkeypatch):
    shutil.copy(ROOT / "ledger" / "index.json", tmp_path / "index.json")
    monkeypatch.setattr(nightly, "INDEX_JSON", tmp_path / "index.json")
    nightly._refresh_index_canonical({"legs": 99, "to": "2099-01-01"})
    doc = json.loads((tmp_path / "index.json").read_text())
    assert doc["canonical"] == {"legs": 99, "to": "2099-01-01"}
    archive = {k: v for k, v in doc.items() if k != "canonical"}
    assert _sha(_canon(archive)) == INDEX_JSON_ARCHIVE_SHA256


def test_the_workflow_never_stages_the_archive_and_the_guard_would_catch_a_write():
    added = {p for line in re.findall(r"^\s*git add (.+)$", WF, re.M)
             for p in line.replace("|| true", "").replace("2>/dev/null", "").split()}
    for name in nightly.ARCHIVED_LEDGER_FILES:
        assert f"ledger/{name}" not in added, f"the nightly stages ledger/{name}"
    assert "git status --porcelain -- ledger/" in WF


# ---------------------------------------------------------------------------
# the archived history is unchanged
# ---------------------------------------------------------------------------
def test_the_archived_files_are_byte_identical_to_the_last_published_night():
    for name, sha in ARCHIVE_SHA256.items():
        assert _sha((ROOT / "ledger" / name).read_bytes()) == sha, f"ledger/{name} changed"
    doc = json.loads((ROOT / "ledger" / "index.json").read_text(encoding="utf-8"))
    archive = {k: v for k, v in doc.items() if k != "canonical"}
    assert _sha(_canon(archive)) == INDEX_JSON_ARCHIVE_SHA256, (
        "a key of index.json other than `canonical` changed")
    assert set(nightly.ARCHIVED_LEDGER_FILES) == set(ARCHIVE_SHA256)


# ---------------------------------------------------------------------------
# regime-source parity
# ---------------------------------------------------------------------------
def _old_rule(caps):
    """_macro_regime_from_ledger()'s arithmetic as it stood at commit 18eb514, applied
    to (date, cap) pairs — frozen here so the parity claim has something to be checked
    against once the function itself is gone."""
    if len(caps) < 2:
        return "N/A", None
    today_cap = caps[-1][1]
    latest = date.fromisoformat(caps[-1][0])
    target = latest - timedelta(days=7)
    past = min(caps, key=lambda c: abs(date.fromisoformat(c[0]) - target))
    if past[1] <= 0:
        return "N/A", None
    return ("RISK-OFF" if today_cap / past[1] < 0.92 else "RISK-ON"), past[0]


def _index_rows():
    return nightly.read_index_rows(ROOT / "ledger" / "index.csv")


def test_the_old_rule_reproduces_every_recorded_label():
    """The baseline: the frozen rule over index.csv's own history gives back every label
    the basket's writer recorded (it labelled night D from the nights before D)."""
    rows = _index_rows()
    caps = [(r["date"], float(r["global_market_cap"])) for r in rows if r["global_market_cap"]]
    for r in rows:
        assert _old_rule([c for c in caps if c[0] < r["date"]])[0] == r["macro_regime"], r["date"]


def test_macro_csv_labels_every_night_it_can_exactly_as_recorded():
    """The parity the move rests on. Over every recorded night, macro.csv either gives
    the recorded label or refuses (MacroDataMissing) — it never disagrees. It refuses
    only at the start of its own history (it begins 2026-08-19), where a seven-day
    comparison has no night seven days back."""
    labelled, refused = [], []
    for r in _index_rows():
        try:
            got = nightly.macro_regime_for(r["date"], ROOT / "ledger" / "macro.csv")
        except nightly.MacroDataMissing:
            refused.append(r["date"])
            continue
        assert got["macro_regime"] == r["macro_regime"], (r["date"], got, r["macro_regime"])
        labelled.append(r["date"])
    assert len(labelled) >= 30, f"only {len(labelled)} overlapping nights were compared"
    assert all(d <= "2026-08-24" for d in refused), refused


def test_both_sources_measure_the_same_ratio_where_they_compare_the_same_nights():
    """Label parity alone is weak on a one-class history (every recorded label is
    RISK-ON). Where both sources pick the same pair of nights, the ratio they compare
    against 0.92 is the same number: index.csv rounded the cap to the dollar."""
    rows = _index_rows()
    icaps = [(r["date"], float(r["global_market_cap"])) for r in rows if r["global_market_cap"]]
    compared = 0
    for r in rows:
        prior = [c for c in icaps if c[0] < r["date"]]
        if len(prior) < 2:
            continue
        _, old_ref = _old_rule(prior)
        try:
            new = nightly.macro_regime_for(r["date"], ROOT / "ledger" / "macro.csv")
        except nightly.MacroDataMissing:
            continue
        if (prior[-1][0], old_ref) != (new["latest_date"], new["ref_date"]):
            continue
        old_ratio = prior[-1][1] / dict(prior)[old_ref]
        assert old_ratio == pytest.approx(new["ratio"], abs=2e-6), r["date"]
        compared += 1
    assert compared >= 25, compared


def test_missing_macro_data_fails_explicitly(tmp_path):
    fields = nightly.MACRO_FIELDS

    def write(rows):
        p = tmp_path / "macro.csv"
        p.write_text(",".join(fields) + "\r\n" + "".join(
            f"{d},{v}" + "," * (len(fields) - 2) + "\r\n" for d, v in rows))
        return p

    with pytest.raises(nightly.MacroDataMissing, match="missing"):
        nightly.macro_regime_for("2026-10-01", tmp_path / "absent.csv")
    with pytest.raises(nightly.MacroDataMissing, match="usable night"):
        nightly.macro_regime_for("2026-10-01", write([("2026-09-30", 1e12)]))
    ok = [(f"2026-09-{d:02d}", 1e12) for d in range(20, 31)]
    assert nightly.macro_regime_for("2026-10-01", write(ok))["macro_regime"] == "RISK-ON"
    # the latest night is five days old: no label, not yesterday's
    with pytest.raises(nightly.MacroDataMissing, match="latest night"):
        nightly.macro_regime_for("2026-10-06", write(ok))
    # no night within three days of seven days back
    with pytest.raises(nightly.MacroDataMissing, match="no night near"):
        nightly.macro_regime_for("2026-10-01", write([("2026-09-18", 1e12), ("2026-09-30", 1e12)]))
    # and a real drawdown is labelled
    dd = [(f"2026-09-{d:02d}", 1e12 if d < 25 else 0.9e12) for d in range(20, 31)]
    assert nightly.macro_regime_for("2026-10-01", write(dd))["macro_regime"] == "RISK-OFF"


def test_a_night_without_a_label_writes_nothing_and_turns_the_run_red(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "MACRO_CSV", tmp_path / "absent.csv")
    monkeypatch.setattr(nightly, "REGIME_CSV", tmp_path / "regime.csv")
    with pytest.raises(nightly.MacroDataMissing):
        nightly.record_macro_regime("2026-10-01")
    assert not (tmp_path / "regime.csv").exists()
    # main() records the reason instead of a label; the workflow's last steps read it.
    assert 'run["macro_regime"] = f"missing: {e}"' in SRC
    step = WF[WF.index("Fail the run when the macro regime could not be recorded"):]
    assert 'reg.startswith("missing")' in step and "sys.exit(1)" in step
    assert WF.index("Commit ledger") < WF.index("Fail the run when the macro regime")


def test_the_regime_is_recorded_before_the_walkforward_study_reads_it(tmp_path, monkeypatch):
    main_src = SRC[SRC.index("def _main("):]
    assert main_src.index("record_macro_regime(today)") < main_src.index("recorded_regimes()")
    monkeypatch.setattr(nightly, "REGIME_CSV", tmp_path / "regime.csv")
    row = nightly.record_macro_regime("2026-09-25")
    again = nightly.record_macro_regime("2026-09-25")          # a same-day re-run replaces
    rows = nightly._read_csv_rows(tmp_path / "regime.csv", nightly.REGIME_FIELDS)
    assert len(rows) == 1 and rows[0]["macro_regime"] == row["macro_regime"] == again["macro_regime"]
    assert rows[0]["latest_date"] == "2026-09-24" and rows[0]["ref_date"] == "2026-09-17"


# ---------------------------------------------------------------------------
# the walk-forward study's history is unchanged
# ---------------------------------------------------------------------------
def test_recorded_regimes_through_retirement_are_the_archived_labels(tmp_path, monkeypatch):
    archived = {r["date"]: r["macro_regime"] for r in _index_rows()
                if r["macro_regime"] in ("RISK-ON", "RISK-OFF")}
    got = nightly.recorded_regimes()
    assert {d: v for d, v in got.items() if d <= nightly.BASKET_RETIRED_ON} == archived
    # A regime.csv row can never overwrite an archived night, and later nights join.
    monkeypatch.setattr(nightly, "REGIME_CSV", tmp_path / "regime.csv")
    nightly._append_context_rows(tmp_path / "regime.csv", nightly.REGIME_FIELDS, "2026-09-20",
                                 [{"date": "2026-09-20", "macro_regime": "RISK-OFF"}])
    nightly._append_context_rows(tmp_path / "regime.csv", nightly.REGIME_FIELDS, "2026-09-25",
                                 [{"date": "2026-09-25", "macro_regime": "RISK-OFF"}])
    got = nightly.recorded_regimes()
    assert got["2026-09-20"] == "RISK-ON" and got["2026-09-25"] == "RISK-OFF"


def test_the_walkforward_study_through_retirement_is_unchanged():
    by = {d: v for d, v in nightly.xsec_by_date().items() if d <= nightly.BASKET_RETIRED_ON}
    wf = nightly.walkforward_report(by, nightly.recorded_regimes())
    wf.pop("generated_at", None)
    assert _sha(_canon(wf)) == WALKFORWARD_THROUGH_RETIREMENT_SHA256


# ---------------------------------------------------------------------------
# terminology: output about the canonical Index says so
# ---------------------------------------------------------------------------
def test_output_that_describes_the_canonical_index_names_it():
    v = (ROOT / "scripts" / "validate_ledger.py").read_text(encoding="utf-8")
    perf = [l for l in v.splitlines() if "f\"performance: {perf['legs']} leg(s)," in l]
    assert len(perf) == 2 and all("canonical Index" in l and "basket" not in l for l in perf), perf
    assert "leg(s), canonical Index {pf['book_total']" in SRC
    assert "whether the canonical Index beat the " in SRC
    assert "Paper return of the published basket" not in SRC
    # The retired basket's own files keep their name: that output is about the basket.
    assert "basket weights sum to" in v


def test_the_validator_prints_canonical_index(capsys, monkeypatch):
    spec = importlib.util.spec_from_file_location("writer_stop_v", ROOT / "scripts" / "validate_ledger.py")
    v = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(v)
    monkeypatch.setattr("sys.argv", ["v", "--ledger", str(ROOT / "ledger"), "--scope", "crypto"])
    assert v.main() == 0
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("performance:"))
    assert "canonical Index" in line and "basket" not in line, line
