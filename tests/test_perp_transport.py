"""ledger/perp.json — the funding transport. AUDIT-PHASE1.6, 8e750228e15a -> ab16684ad5c1.

Phase 1.5 stopped the board applying a STALE multiplier. It did so by reading only rows
carrying the snapshot's own date out of signals.json, and signals.json persists fifty
names a night out of ~235 scored. The board came out correct and under-informed: 184 of
234 rows at a neutral 1.000 while nightly.score() had a live cross-venue reading for 153
of them. One model, two consumers, different information sets — and nothing anywhere
that would have said so.

This file asserts the two properties that close it:

  COVERAGE   the multiplier the browser will apply equals the one score() applied, for
             every scored symbol, not for the persisted fifty.
  AUTHORITY  the artifact says what was AVAILABLE and the consumer decides what is
             VALID. A file on disk can never talk a consumer into a number outside the
             envelope, whatever it claims about it.
"""
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("tr_nightly", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)

DAY = "2026-09-17"


def _rows(n=3):
    return [
        {"symbol": "ZEC", "perp_mult": 1.071, "funding_apr": -25.185,
         "funding_venue": "gateio", "funding_venues_n": 5, "funding_apr_spread": 33.8855,
         "funding_interval_h": 8.0, "funding_regime": "SHORT_SQUEEZE_RISK", "rsi7": 74.96},
        {"symbol": "ETH", "perp_mult": 1.0, "funding_apr": 5.913,
         "funding_venue": "binance", "funding_venues_n": 6, "funding_interval_h": 8.0,
         "funding_regime": "NEUTRAL", "rsi7": 43.68},
        {"symbol": "PONS", "perp_mult": 1.0, "funding_apr": None, "funding_venue": None,
         "funding_venues_n": None, "funding_interval_h": None, "funding_regime": None,
         "rsi7": None},
    ][:n]


# ------------------------------------------------------------------ the artifact

def test_the_artifact_distinguishes_a_neutral_reading_from_no_reading():
    """The distinction the whole phase turns on. x1.000 has two causes."""
    doc = nightly.perp_artifact(_rows(), DAY)
    assert doc["rows"]["ETH"] == {"m": 1.0, "s": "current", "apr": 5.913,
                                  "v": "binance", "n": 6, "ih": 8.0,
                                  "rg": "NEUTRAL", "rsi": 43.68}
    # PONS has no perpetual market at all. Same multiplier, opposite fact.
    assert doc["rows"]["PONS"] == {"m": 1.0, "s": "absent"}
    assert doc["universe"] == 3 and doc["with_reading"] == 2


def test_the_artifact_carries_the_snapshot_and_the_rules_in_force():
    doc = nightly.perp_artifact(_rows(), DAY)
    assert doc["as_of"] == DAY
    assert doc["spec_hash"] == nightly.SPEC_HASH
    assert doc["envelope"] == [nightly.PERP_ENVELOPE_LO, nightly.PERP_ENVELOPE_HI]
    assert doc["max_age_days"] == nightly.PERP_MAX_AGE_DAYS
    assert doc["schema_version"] == nightly.PERP_ARTIFACT_VERSION
    assert set(doc["row_keys"]) == set(nightly.PERP_ROW_KEYS)
    # Every short key a row can carry is documented inside the file itself.
    for row in doc["rows"].values():
        assert set(row) <= set(doc["row_keys"]), row


def test_the_artifact_is_slim():
    """A transport that can grow is a transport that will.

    ledger/funding.json is the rich one — eight venues nested per asset, the carry
    screen, seventy kilobytes, fifty rows. This is fetched on every page load and
    carries every scored symbol, so it holds what the score needs and nothing else.
    """
    path = ROOT / "ledger" / "perp.json"
    if not path.exists():
        pytest.skip("no artifact written yet")
    kb = path.stat().st_size / 1024
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert kb < 64, f"{kb:.1f} KB — the transport is growing"
    assert "by_venue" not in path.read_text(encoding="utf-8")
    assert doc["universe"] > 50, "a transport that is also fifty rows is the same defect"


# ---------------------------------------------------------------- the authority split

def test_the_artifact_may_say_a_reading_was_absent():
    """Availability is the writer's to assert — nothing else can know it."""
    doc = {"as_of": DAY, "rows": {"A": {"m": 1.0, "s": "absent"},
                                  "B": {"m": 1.0, "s": "current"}}}
    fed = nightly.perp_feed(doc, DAY)
    assert fed["states"] == {"A": "absent", "B": "current"}
    assert fed["mults"] == {"A": 1.0, "B": 1.0}


def test_the_artifact_may_not_talk_the_consumer_into_a_bad_value():
    """Validity is never the artifact's to assert, whatever it claims.

    HBAR's 17.4 again, this time arriving through the new transport with the writer
    vouching for it in three different ways. Each one is refused.
    """
    for claimed in ("current", "absent", "verified", None):
        row = {"m": 17.4}
        if claimed is not None:
            row["s"] = claimed
        fed = nightly.perp_feed({"as_of": DAY, "rows": {"HBAR": row}}, DAY)
        assert fed["mults"]["HBAR"] == 1.0, claimed
        assert fed["states"]["HBAR"] == "rejected", claimed
        assert fed["rejected"] == [{"symbol": "HBAR", "value": 17.4, "date": DAY}]
    # And an artifact cannot widen the envelope by declaring a wider one.
    fed = nightly.perp_feed(
        {"as_of": DAY, "envelope": [0.0, 1e9], "rows": {"HBAR": {"m": 17.4}}}, DAY)
    assert fed["states"]["HBAR"] == "rejected"


def test_every_malformed_cell_shape_a_json_writer_can_produce():
    doc = {"as_of": DAY, "rows": {
        "A": {"m": None, "s": "current"}, "B": {"m": "", "s": "current"},
        "C": {"m": "None", "s": "current"}, "D": {"m": "1.07 garbage"},
        "E": {"s": "current"}, "F": {}, "G": {"m": "1.05"}, "H": {"m": True}}}
    fed = nightly.perp_feed(doc, DAY)
    assert fed["states"]["A"] == fed["states"]["B"] == fed["states"]["C"] == "absent"
    assert fed["states"]["E"] == fed["states"]["F"] == "absent"
    assert fed["states"]["D"] == "rejected"      # float() raises, parseFloat coerces
    assert fed["mults"]["G"] == 1.05             # a numeric string is fine
    assert all(v == 1.0 for k, v in fed["mults"].items() if k != "G")


# ------------------------------------------------------------------- no fallback

def test_a_missing_or_stale_artifact_withholds_the_overlay_whole():
    """No fallback to the signals.json path, deliberately.

    A fallback would reintroduce the divergence this phase removes, silently, on exactly
    the nights something is already wrong. Withheld reads as withheld.
    """
    empty = {"mults": {}, "states": {}, "as_of": None, "rejected": [], "n": 0}
    for doc, why in [(None, "no-artifact"), ({}, "no-artifact"),
                     ({"as_of": "not-a-date", "rows": {"A": {"m": 1.1}}}, "no-artifact"),
                     ({"as_of": "2026-09-14", "rows": {"A": {"m": 1.1}}}, "stale"),
                     ({"as_of": "2026-09-20", "rows": {"A": {"m": 1.1}}}, "stale")]:
        fed = nightly.perp_feed(doc, DAY)
        assert {k: fed[k] for k in empty} == empty, doc
        assert fed["withheld"] == why, doc
    # One day late is NOT stale: the nightly may not have run yet today.
    fed = nightly.perp_feed({"as_of": "2026-09-16", "rows": {"A": {"m": 1.1}}}, DAY)
    assert fed["n"] == 1 and fed["withheld"] is None


def test_the_page_has_no_path_back_to_the_signals_overlay():
    """Asserted on the source, because a fallback is a thing someone adds back later."""
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    # The page must build its overlay from the artifact and from nothing else.
    assert page.count("perpFeed(_doc") == 1
    assert "perpOverlay(ALL_ROWS" not in page
    assert 'fetch("ledger/perp.json"' in page


# --------------------------------------------------------------------- coverage

def test_the_transport_covers_every_symbol_the_nightly_scored():
    """The Phase 1.6 property, against the artifact and the cross-section on disk."""
    path = ROOT / "ledger" / "perp.json"
    shard = ROOT / "ledger" / "xsec"
    if not path.exists():
        pytest.skip("no artifact written yet")
    doc = json.loads(path.read_text(encoding="utf-8"))
    day = doc["as_of"]
    csv_path = shard / f"{day[:7]}.csv"
    if not csv_path.exists():
        pytest.skip("no shard covering the artifact's snapshot")
    import csv
    with csv_path.open(newline="", encoding="utf-8") as f:
        scored = {r["symbol"].upper(): r for r in csv.DictReader(f)
                  if r["date"] == day and r.get("src") == "live"}
    if not scored:
        pytest.skip("the shard holds no live rows for the artifact's snapshot")
    fed = nightly.perp_feed(doc, day)
    assert set(fed["mults"]) == set(scored), (
        f"missing {sorted(set(scored) - set(fed['mults']))[:10]} / "
        f"extra {sorted(set(fed['mults']) - set(scored))[:10]}")
    for sym, r in scored.items():
        if r["perp_mult"] in ("", "None"):
            continue
        assert abs(float(r["perp_mult"]) - fed["mults"][sym]) <= 5e-4, sym


def test_the_artifact_on_disk_carries_no_value_the_consumer_refuses():
    path = ROOT / "ledger" / "perp.json"
    if not path.exists():
        pytest.skip("no artifact written yet")
    doc = json.loads(path.read_text(encoding="utf-8"))
    fed = nightly.perp_feed(doc, doc["as_of"])
    assert fed["rejected"] == [], fed["rejected"]
    for sym, v in fed["mults"].items():
        assert nightly.PERP_ENVELOPE_LO - 1e-9 <= v <= nightly.PERP_ENVELOPE_HI + 1e-9, sym


def test_the_round_trip_through_the_writer_is_lossless_for_the_multiplier():
    """perp_artifact -> perp_feed must return exactly what score() applied."""
    rows = _rows() + [{"symbol": "EDGE_LO", "perp_mult": nightly.PERP_ENVELOPE_LO},
                      {"symbol": "EDGE_HI", "perp_mult": nightly.PERP_ENVELOPE_HI},
                      {"symbol": "ODD", "perp_mult": 0.9166}]
    fed = nightly.perp_feed(nightly.perp_artifact(rows, DAY), DAY)
    for r in rows:
        assert fed["mults"][r["symbol"]] == float(r["perp_mult"]), r["symbol"]
    assert fed["rejected"] == []


def test_the_writer_is_not_part_of_the_specification():
    """Recording a number is not valuing one."""
    captured = nightly.spec()["functions"]
    for name in ("perp_artifact", "write_perp_artifact"):
        assert name not in captured
        for body in captured.values():
            assert name not in body, f"{name} reached a scoring function"
