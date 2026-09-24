"""Tests for nightly.py engine: rebalance hysteresis (A), ejection delta (C),
execution-adjusted counterfactual (B), and macro regime (D, passive).

These run without network: fetch_global_market_cap and fetch_markets are
monkeypatched. The hysteresis test is the key regression guard for the
#10<->#11 churn fix.
"""
import importlib.util
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
NIGHTLY = ROOT / "nightly.py"

spec = importlib.util.spec_from_file_location("nightly_test_mod", NIGHTLY)
assert spec is not None, f"could not load spec for {NIGHTLY}"
nightly = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nightly)


# ---------------------------------------------------------------------------
# the commit step stages everything the run writes
# ---------------------------------------------------------------------------
def _staged_by(workflow_text):
    import re
    return {p.rstrip("/") for line in re.findall(r"^\s*git add (.+)$", workflow_text, re.M)
            for p in line.replace("|| true", "").replace("2>/dev/null", "").split()
            if p.startswith("ledger/")}


def test_every_ledger_artifact_is_staged_by_the_nightly_commit():
    """The Commit step stages an explicit allowlist, so a new artifact is invisible to it
    until someone names it. ledger/perp.json (2026-09-17) and ledger/walkforward.json
    (2026-09-18) were written and validated on the runner every night and discarded with
    it: the published board ran on a six-night-old funding transport, which the browser
    refuses whole, while every gate passed. Every entry under ledger/ must be named —
    except the retired basket's archived record, which must NOT be: it is never written,
    so leaving it unstaged makes the leftover guard fail any run that writes it."""
    wf = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    staged = _staged_by(wf)
    archived = {f"ledger/{n}" for n in nightly.ARCHIVED_LEDGER_FILES}
    on_disk = {f"ledger/{p.name}" for p in (ROOT / "ledger").iterdir()
               if not p.name.startswith(".")}
    missing = sorted(p for p in on_disk - archived if p not in staged)
    assert not missing, f"written under ledger/ but never committed by the nightly: {missing}"
    assert not (staged & archived), f"the nightly stages the archived basket record: {sorted(staged & archived)}"
    assert "ledger/regime.csv" in staged, "the macro regime ledger is never committed"


def test_the_rwa_rollback_reaches_nested_shards(tmp_path):
    """The RWA gate's rollback and the stage guard were written for flat ledger/rwa_*
    files. The flow, wrapper and observation ledgers are now month shards under
    ledger/rwa/<kind>/. Executed against a real git repository rather than read: a
    tracked shard modified tonight is restored, a new month's untracked shard is removed,
    and the guard that keeps a refused-crypto night's commit to RWA files lets a nested
    shard through and nothing else."""
    import re
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    wf = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    restore = re.search(r"^\s*(git ls-files -z -- 'ledger/rwa\*' \| xargs .+)$", wf, re.M).group(1)
    remove = re.search(r"^\s*(git ls-files -z --others -- 'ledger/rwa\*' \| xargs .+)$", wf, re.M).group(1)
    guard = re.search(r"(git diff --cached --name-only \| grep -v '\^ledger/rwa'[^;\n]*?grep -q \.)", wf).group(1)

    def sh(cmd):
        return subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True,
                              env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    sh("git init -q .")
    shard = tmp_path / "ledger" / "rwa" / "flow" / "2026-09.csv"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"date,underlying_id\r\n2026-09-30,a\r\n")
    (tmp_path / "ledger" / "signals.csv").write_text("date\n")
    assert sh("git add -A && git commit -q -m base").returncode == 0
    committed = shard.read_bytes()
    shard.write_bytes(committed + b"2026-09-30,b\r\n")                  # tonight's refused rows
    new_month = shard.with_name("2026-10.csv")
    new_month.write_bytes(b"date,underlying_id\r\n2026-10-01,a\r\n")  # and a new month's shard
    assert sh(restore).returncode == 0 and sh(remove).returncode == 0
    assert shard.read_bytes() == committed, "the rollback did not restore a nested shard"
    assert not new_month.exists(), "the rollback left a new month's untracked shard behind"
    # The guard: a staged nested shard is RWA; a staged crypto file is not.
    new_month.write_bytes(b"date,underlying_id\r\n2026-10-01,a\r\n")
    sh("git add ledger/rwa")
    assert sh(guard).returncode != 0, "the guard refused a nested RWA shard"
    (tmp_path / "ledger" / "signals.csv").write_text("date\n2026-10-01\n")
    sh("git add ledger/signals.csv")
    assert sh(guard).returncode == 0, "the guard let a crypto file through"


def test_the_nightly_fails_loudly_on_an_unstaged_ledger_write():
    """The allowlist test above covers files that exist in the checkout. A brand-new
    artifact does not exist here until its first night, so the workflow also checks the
    runner's own tree after the push and turns the run red if anything under ledger/ is
    still modified or untracked."""
    wf = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    guard = "git status --porcelain -- ledger/"
    assert guard in wf
    assert wf.index("git commit") < wf.index(guard)


def test_ledger_writers_are_serialised_and_a_moved_main_does_not_lose_the_night():
    nightly_wf = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    release_wf = (ROOT / ".github" / "workflows" / "rwa_release.yml").read_text(encoding="utf-8")
    for text in (nightly_wf, release_wf):
        assert "group: ledger-writer" in text
        assert "cancel-in-progress: false" in text
    assert "git pull --rebase" in nightly_wf
    assert nightly_wf.index("git push") < nightly_wf.index("git pull --rebase")


def test_an_rwa_refusal_cannot_stop_the_crypto_commit_or_vice_versa():
    """Two models, one commit, and until now one gate: an RWA-only defect lost two
    nights of a healthy crypto ledger (2026-09-21, -22)."""
    yaml = pytest.importorskip("yaml")  # installed by tests.yml
    wf_text = (ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    steps = yaml.safe_load(wf_text)["jobs"]["ledger"]["steps"]
    by_name = {s.get("name"): s for s in steps}
    order = [s.get("name") for s in steps]
    rwa_gate = by_name["RWA ledger gate"]
    assert rwa_gate["id"] == "rwa_gate" and rwa_gate.get("continue-on-error") is True
    # Gated on their own evidence even when nightly.py failed after writing them.
    assert rwa_gate.get("if") == "always()"
    # A diagnostic can never cost the night.
    assert by_name["Record the observations under watch"].get("continue-on-error") is True
    # A fallback commit with refused crypto files still unstaged must be able to rebase.
    assert wf_text.count("git pull --rebase --autostash") == 2 and "git pull --rebase origin" not in wf_text
    rel = (ROOT / ".github" / "workflows" / "rwa_release.yml").read_text(encoding="utf-8")
    assert "python scripts/validate_ledger.py --scope rwa" in rel
    assert "--scope rwa" in rwa_gate["run"]
    assert "--scope crypto" in by_name["Ledger integrity gate"]["run"]
    # Taken before any blocking gate, so its verdict exists whichever of them fails.
    assert order.index("RWA ledger gate") < order.index("Parity gate (frontend <-> backend must agree)")
    commit = by_name["Commit ledger"]
    assert commit["id"] == "commit" and "steps.rwa_gate.outcome" in commit["env"]["RWA_GATE"]
    assert commit["run"].index('if [ "$RWA_GATE" != "success" ]') < commit["run"].index("git add")
    alone = by_name["Commit what passed its own gate"]
    assert "failure()" in alone["if"] and "steps.commit.outcome == 'skipped'" in alone["if"]
    assert 'if [ "$RWA_GATE" = "success" ]' in alone["run"]
    assert "grep -v '^ledger/rwa' | grep -v '^ledger/runs\\.csv$'" in alone["run"]
    # The run is recorded whatever the gates decided, before either commit step, and
    # can never block one.
    rec = by_name["Record the run"]
    assert rec["if"] == "always()" and rec.get("continue-on-error") is True
    assert order.index("ATR eligibility gate") < order.index("Record the run") < order.index("Commit ledger")
    for sid in ("nightly", "rwa_gate", "parity", "integrity", "atr"):
        assert f"steps.{sid}.outcome" in rec["run"], sid
    # Smoke steps run after both commits: a stall there can no longer cost the night.
    for smoke in ("Cryptometer smoke test", "CoinGecko smoke test", "RWA smoke test"):
        assert order.index(smoke) > order.index("Commit what passed its own gate"), smoke
        assert by_name[smoke]["if"] == "always()" and by_name[smoke]["continue-on-error"]
    last = steps[-1]
    assert "steps.rwa_gate.outcome == 'failure'" in last["if"] and "exit 1" in last["run"]
