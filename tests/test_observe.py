"""The observation recorder: durable answers to "has this held for N nights".

The failure this replaces is not a bug in any function. It is that the question needed
three consecutive nights and the mechanism for answering it lived inside a chat session,
which does not last three nights. Anything that has to survive the observer belongs in
the repository.
"""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _load(tmp: Path):
    spec = importlib.util.spec_from_file_location("obs_mod", ROOT / "scripts" / "observe.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.LEDGER = tmp
    m.HEALTH_CSV = tmp / "venue_health.csv"
    return m


def _funding(date, **venues):
    return {"date": date, "venues": venues}


def _blocked(code):
    return {"status": "unreachable", "http_status": code, "policy_blocked": True,
            "markets": 0}


LIVE = {"status": "live", "http_status": None, "policy_blocked": False, "markets": 90}


def test_a_streak_counts_consecutive_nights_not_a_total(tmp_path):
    """Three of the last ten nights is a flaky venue. The last three consecutively is a
    policy. Only the second justifies removing a venue, and a total cannot tell them
    apart."""
    m = _load(tmp_path)
    for d in ("2026-08-16", "2026-08-17", "2026-08-18"):
        rows = m.append_venue_health(_funding(d, binance=_blocked(451)), d)
    assert m.blocked_streak(rows, "binance") == (3, 3)
    # A night where it answered breaks the streak, even with blocks either side.
    rows = m.append_venue_health(_funding("2026-08-19", binance=LIVE), "2026-08-19")
    assert m.blocked_streak(rows, "binance") == (0, 4)


def test_rerunning_a_date_replaces_rather_than_duplicates(tmp_path):
    """The nightly can run twice in a day. A blind append would count one night twice
    and turn a two-night streak into four — the same defect that put 460 duplicate
    (date, symbol) pairs in signals.csv."""
    m = _load(tmp_path)
    m.append_venue_health(_funding("2026-08-18", binance=_blocked(451)), "2026-08-18")
    rows = m.append_venue_health(_funding("2026-08-18", binance=_blocked(451)), "2026-08-18")
    assert len(rows) == 1
    assert m.blocked_streak(rows, "binance") == (1, 1)


def test_a_venue_that_recovers_is_recorded_as_recovered(tmp_path):
    m = _load(tmp_path)
    m.append_venue_health(_funding("2026-08-17", bybit=_blocked(403)), "2026-08-17")
    rows = m.append_venue_health(_funding("2026-08-18", bybit=LIVE), "2026-08-18")
    assert m.blocked_streak(rows, "bybit") == (0, 2)
    latest = [r for r in rows if r["date"] == "2026-08-18"][0]
    assert latest["policy_blocked"] is False and latest["markets"] == 90


def test_the_fill_rate_names_the_majors_because_that_was_the_bug(tmp_path):
    """3/50 was not the problem — the problem was WHICH 3. The sweep selected
    alphabetically from the full 250-market universe and never reached BTC, so a bare
    count would have looked like thin coverage rather than a wrong query."""
    m = _load(tmp_path)
    fields = ["date", "symbol", "long_short_ratio"]
    with (tmp_path / "signals.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader()
        for sym, ratio in (("BTC", "0.79"), ("ETH", "0.53"), ("AAVE", ""), ("ZZZ", "")):
            w.writerow({"date": "2026-08-19", "symbol": sym, "long_short_ratio": ratio})
    n, total, got = m.ls_fill("2026-08-19")
    assert (n, total) == (2, 4)
    assert got == ["BTC", "ETH"]


def test_a_missing_funding_artifact_is_not_an_error(tmp_path):
    """The recorder runs before the gates. It must never be the reason a ledger fails to
    commit — an absent artifact is a quiet no-op, not a crash."""
    m = _load(tmp_path)
    assert m.main() == 0


def test_the_recorder_does_not_touch_the_specification():
    """It reads what the nightly wrote and appends. Nothing here is scored.

    596d414706be -> 2da60f7efd7b: Module F, a scoring change in nightly.py. This
    pin moves with it and the recorder itself is untouched, which is the whole point of
    asserting it here.
    """
    spec = importlib.util.spec_from_file_location("n_obs", ROOT / "nightly.py")
    nightly = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nightly)
    assert nightly.SPEC_HASH == "6f98778fa627"
    src = (ROOT / "scripts" / "observe.py").read_text(encoding="utf-8")
    assert "import nightly" not in src
