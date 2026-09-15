"""The cross-sectional research ledger: schema, ranks, idempotence, and the invariant.

signals.csv records the top fifty names by conviction. Measured on the recorded ledger,
that window decides the answer: paired leg by leg over the same forty legs, moving it
from k=40 to k=50 shifts the information coefficient by -0.0576 (SE 0.0168, t = -3.43).
ledger/xsec/ records the whole scored cross-section so the statistic can be computed over
the population it claims to describe.

The tests that matter most here are the last two kinds. One asserts that the narrow
ledger is a PROVABLE SUBSET of the wide one on every shared column, because two writers
emitting the same quantity are two writers that can disagree, and a silent divergence
between them would reproduce the exact defect that AUDIT-2026-09 1.0 exists to fix. The
other asserts the legacy series did not move: same columns, same rows, same hash.
"""
import csv
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_spec = importlib.util.spec_from_file_location("xsec_mod", ROOT / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


def _row(sym, conv, mcap, **kw):
    """A scored row in the shape the nightly's row loop produces."""
    base = {"date": "2026-09-14", "symbol": sym, "conviction": conv,
            "market_cap": mcap, "price": 1.0, "turnover_pct": 5.0,
            "rs7": 1.0, "rs14": 2.0, "rs30": 3.0, "rs200": 4.0, "rs_blend": 2.5,
            "rs_windows_n": 4, "c_depth": 20.0, "c_momentum": 11.0,
            "c_liquidity": 30.0, "emission_mult": 1.0, "perp_mult": 1.0,
            "fdv_usd": mcap, "funding_apr": None, "rsi7": None, "beta_btc": None,
            "spec_hash": nightly.SPEC_HASH}
    base.update(kw)
    return base


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Point the module's ledger paths at a scratch directory."""
    monkeypatch.setattr(nightly, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(nightly, "XSEC_DIR", tmp_path / "xsec")
    monkeypatch.setattr(nightly, "XSEC_SCHEMA_JSON", tmp_path / "xsec" / "SCHEMA.json")
    return tmp_path / "xsec"


# --------------------------------------------------------------------------- schema

def test_the_schema_is_locked():
    """Twenty-five columns, in this order, at version 1.

    Pinned as a literal rather than derived, because Phase 2's backfill writes into this
    shape and a column appearing or moving silently would leave two files that parse and
    disagree. Changing this list is a deliberate act that fails here first.
    """
    assert nightly.XSEC_SCHEMA_VERSION == 1
    assert nightly.XSEC_FIELDS == [
        "date", "symbol", "rank_mcap", "rank_conv",
        "conviction", "price", "market_cap", "turnover_pct",
        "rs7", "rs14", "rs30", "rs200", "rs_blend", "rs_windows_n",
        "c_depth", "c_momentum", "c_liquidity", "emission_mult", "perp_mult",
        "fdv_usd", "funding_apr", "rsi7", "beta_btc",
        "spec_hash", "src",
    ]
    assert len(nightly.XSEC_FIELDS) == 25
    assert len(set(nightly.XSEC_FIELDS)) == 25
    assert nightly.XSEC_SOURCES == ("live", "backfill")
    # Every shared column must actually exist in both schemas, or the invariant below
    # would pass by checking nothing.
    for f in nightly.XSEC_SHARED_FIELDS:
        assert f in nightly.XSEC_FIELDS, f
        assert f in nightly.FIELDS, f


def test_the_sidecar_describes_the_shards_it_sits_beside(ledger):
    nightly.write_xsec([_row("BTC", 55, 1e12)], "2026-09-14")
    doc = json.loads((ledger / "SCHEMA.json").read_text())
    assert doc["schema_version"] == nightly.XSEC_SCHEMA_VERSION
    assert doc["fields"] == list(nightly.XSEC_FIELDS)
    assert doc["shared_with_signals_csv"] == list(nightly.XSEC_SHARED_FIELDS)
    assert doc["row_key"] == ["date", "symbol", "src"]
    assert doc["sources"] == ["live", "backfill"]
    # The sidecar is rewritten every run, so it is either absent or current.
    nightly.write_xsec([_row("BTC", 55, 1e12)], "2026-09-14")
    assert json.loads((ledger / "SCHEMA.json").read_text()) == doc


def test_every_shard_on_disk_carries_the_current_header():
    """The CSV header IS the schema. A shard whose header drifted is unreadable data."""
    d = ROOT / "ledger" / "xsec"
    if not d.exists():
        pytest.skip("no shard written yet — the first one lands with the next nightly")
    shards = sorted(d.glob("*.csv"))
    assert shards, "ledger/xsec exists but holds no shard"
    for path in shards:
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert header == nightly.XSEC_FIELDS, f"{path.name} header drifted"


# --------------------------------------------------------------------------- ranks

def test_ranks_are_dense_deterministic_and_order_independent():
    """Ranks must be a function of the data, never of the order the rows arrived in.

    signals.csv's fifty-row cut comes from a STABLE sort on conviction, so it inherits
    whatever order CoinGecko returned. A rank that did the same could not be reproduced
    from the recorded file, which is the whole reason both ranks are written down.
    """
    rows = [_row("AAA", 50, 3e9), _row("BBB", 70, 1e9),
            _row("CCC", 50, 2e9), _row("DDD", 10, 9e9)]
    out = {r["symbol"]: r for r in nightly.xsec_rows(rows)}

    assert [out[s]["rank_mcap"] for s in ("DDD", "AAA", "CCC", "BBB")] == [1, 2, 3, 4]
    # Ties on conviction (AAA and CCC at 50) break by symbol ascending.
    assert out["BBB"]["rank_conv"] == 1
    assert out["AAA"]["rank_conv"] == 2
    assert out["CCC"]["rank_conv"] == 3
    assert out["DDD"]["rank_conv"] == 4

    # Dense and complete: every row ranked, 1..n, no gaps and no repeats.
    for field in ("rank_mcap", "rank_conv"):
        assert sorted(r[field] for r in out.values()) == list(range(1, len(rows) + 1))

    # Same data, every input order, same answer.
    import itertools
    for perm in itertools.permutations(rows):
        again = {r["symbol"]: (r["rank_mcap"], r["rank_conv"])
                 for r in nightly.xsec_rows(list(perm))}
        assert again == {s: (r["rank_mcap"], r["rank_conv"]) for s, r in out.items()}


def test_an_unrankable_value_sorts_last_and_is_still_ranked():
    """A scored row with no market cap is still a scored row.

    Dropping it would make the rank column describe a different set from the one the
    file holds, which is a subtler version of the mislabelling this ledger exists to fix.
    """
    rows = [_row("AAA", 50, 1e9), _row("ZZZ", 40, None), _row("MMM", 30, None)]
    out = {r["symbol"]: r for r in nightly.xsec_rows(rows)}
    assert out["AAA"]["rank_mcap"] == 1
    assert out["MMM"]["rank_mcap"] == 2     # both null, symbol ascending
    assert out["ZZZ"]["rank_mcap"] == 3
    assert len(out) == 3


def test_the_rows_are_written_in_conviction_rank_order(ledger):
    nightly.write_xsec([_row("AAA", 10, 1e9), _row("BBB", 90, 2e9)], "2026-09-14")
    with (ledger / "2026-09.csv").open(newline="", encoding="utf-8") as f:
        got = [r["symbol"] for r in csv.DictReader(f)]
    assert got == ["BBB", "AAA"]


# ------------------------------------------------------------------- rs_windows_n

def test_rs_windows_n_counts_what_the_payload_carried():
    """Not four by convention. Nineteen of tonight's markets publish no 200-day change.

    Both legs are required because relative strength is a difference: a window the
    benchmark did not publish is a window with nothing to subtract.
    """
    full = {f"price_change_percentage_{tf}d_in_currency": 1.0
            for tf in nightly.RS_WINDOWS}
    btc = dict(full)
    assert nightly.observed_rs_windows(full, btc) == 4

    young = {k: v for k, v in full.items()
             if "200d" not in k}
    assert nightly.observed_rs_windows(young, btc) == 3

    explicit_null = dict(full, price_change_percentage_200d_in_currency=None)
    assert nightly.observed_rs_windows(explicit_null, btc) == 3

    # The benchmark's own gap removes the window for everyone, not just for itself.
    btc_short = {k: v for k, v in btc.items() if "200d" not in k}
    assert nightly.observed_rs_windows(full, btc_short) == 3
    assert nightly.observed_rs_windows({}, btc) == 0
    assert nightly.observed_rs_windows(full, None) == 0


def test_the_nightly_records_the_window_count_without_scoring_it():
    """The provenance column exists; score() still reads a missing window as 0.0.

    Recording the count and fixing the zero-fill are separate decisions, and this change
    is only the first. If score() ever starts consulting the count, the specification
    hash moves and this assertion is the reminder that it should.
    """
    src = nightly.spec()["functions"]["score"]
    assert "rs_windows_n" not in src
    assert "observed_rs_windows" not in src
    assert "rs_windows_n" not in nightly.FIELDS
    assert "rs_windows_n" in nightly.XSEC_FIELDS


# --------------------------------------------------------------------- persistence

def test_a_date_lands_in_its_month_shard(ledger):
    nightly.write_xsec([_row("BTC", 55, 1e12, date="2026-09-14")], "2026-09-14")
    nightly.write_xsec([_row("BTC", 56, 1e12, date="2026-10-01")], "2026-10-01")
    assert sorted(p.name for p in ledger.glob("*.csv")) == ["2026-09.csv", "2026-10.csv"]
    assert nightly.xsec_shard_path("2026-09-14").name == "2026-09.csv"
    with pytest.raises(ValueError):
        nightly.xsec_shard_path("not-a-date")


def test_a_same_night_rerun_is_byte_identical(ledger):
    """A re-run that changes nothing must produce a file that changes nothing.

    Otherwise every re-run is a commit, and the ledger's history stops meaning "the
    board moved" and starts meaning "the job ran twice".
    """
    rows = [_row("AAA", 10, 1e9), _row("BBB", 90, 2e9), _row("CCC", 50, 5e9)]
    path, n = nightly.write_xsec(rows, "2026-09-14")
    first = path.read_bytes()
    assert n == 3
    path, n = nightly.write_xsec(list(reversed(rows)), "2026-09-14")
    assert n == 3
    assert path.read_bytes() == first, "a re-run rewrote the shard"

    keys = [(r["date"], r["symbol"], r["src"])
            for r in csv.DictReader(path.open(newline="", encoding="utf-8"))]
    assert len(keys) == len(set(keys)) == 3


def test_a_rerun_replaces_only_its_own_date(ledger):
    nightly.write_xsec([_row("AAA", 10, 1e9, date="2026-09-13")], "2026-09-13")
    nightly.write_xsec([_row("BBB", 20, 2e9, date="2026-09-14")], "2026-09-14")
    nightly.write_xsec([_row("CCC", 30, 3e9, date="2026-09-14")], "2026-09-14")
    rows = list(csv.DictReader((ledger / "2026-09.csv").open(newline="", encoding="utf-8")))
    assert [(r["date"], r["symbol"]) for r in rows] == [
        ("2026-09-13", "AAA"), ("2026-09-14", "CCC")]


def test_live_and_backfill_coexist_and_never_clobber_each_other(ledger):
    """The row key is (date, symbol, src), not (date, symbol).

    Phase 2 will write reconstructed rows for dates that already carry live ones. With a
    date-only key the second writer would silently delete the first writer's work, and
    the file would look complete.
    """
    nightly.write_xsec([_row("AAA", 10, 1e9)], "2026-09-14", src="live")
    nightly.write_xsec([_row("AAA", 11, 1e9)], "2026-09-14", src="backfill")
    rows = list(csv.DictReader((ledger / "2026-09.csv").open(newline="", encoding="utf-8")))
    assert {(r["src"], r["conviction"]) for r in rows} == {("live", "10"), ("backfill", "11")}

    # Re-running the backfill leaves the live row untouched.
    nightly.write_xsec([_row("AAA", 12, 1e9)], "2026-09-14", src="backfill")
    rows = list(csv.DictReader((ledger / "2026-09.csv").open(newline="", encoding="utf-8")))
    assert {(r["src"], r["conviction"]) for r in rows} == {("live", "10"), ("backfill", "12")}

    with pytest.raises(ValueError):
        nightly.write_xsec([_row("AAA", 10, 1e9)], "2026-09-14", src="observed")


# ----------------------------------------------------------------- the invariant

def test_the_narrow_ledger_is_a_provable_subset_of_the_wide_one():
    """Every signals.csv row must appear in its shard with equal values everywhere.

    This is the assertion the whole design rests on. Two writers emitting the same
    quantity is two writers that can disagree, and a silent divergence here would
    reproduce exactly the defect AUDIT-2026-09 1.0 was written about — a file whose
    contents did not match what the code said they were.

    Deliberately NOT asserted: that rank_conv <= 50 selects the persisted fifty. The
    legacy cut is a stable sort inheriting the payload's order and rank_conv breaks ties
    by symbol, so at a tie on the fiftieth conviction — which 2026-09-14 has, two names
    at 24 — they can name different rows. Only the values are claimed.
    """
    d = ROOT / "ledger" / "xsec"
    if not d.exists():
        pytest.skip("no shard written yet — the first one lands with the next nightly")
    wide = {}
    for path in sorted(d.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("src") == "live":
                    wide[(r["date"], r["symbol"])] = r
    assert wide, "shards exist but hold no live rows"

    with (ROOT / "ledger" / "signals.csv").open(newline="", encoding="utf-8") as f:
        narrow = list(csv.DictReader(f))
    covered = [r for r in narrow if (r["date"], r["symbol"]) in wide]
    if not covered:
        # Both files exist and share no night. Benign causes are real — a shard covering
        # a month signals.csv no longer holds — and there is nothing to compare either
        # way, so this is an absence of evidence rather than a disagreement. The
        # round-trip test above still exercises the formatting path on every run.
        pytest.skip("no night appears in both signals.csv and a shard yet")

    dates = {r["date"] for r in covered}
    for r in narrow:
        if r["date"] not in dates:
            continue
        key = (r["date"], r["symbol"])
        assert key in wide, f"{key} is in signals.csv and missing from the cross-section"
        for field in nightly.XSEC_SHARED_FIELDS:
            assert wide[key][field] == r[field], (
                f"{key} disagrees on {field}: "
                f"signals.csv {r[field]!r} vs xsec {wide[key][field]!r}")


def test_the_invariant_holds_over_the_real_recorded_values(ledger):
    """The same check, run now, against values this repository actually recorded.

    The two tests above skip until the first nightly writes a shard, which would leave
    the central claim of this design unexercised in the meantime. This one does not
    wait: it takes the real signals.csv rows for the most recent night, embeds them in a
    wider cross-section the way the nightly's row loop would, writes a shard through the
    real writer, and asserts every shared value survives the round trip unchanged.

    It is the formatting path that is under test. Both files go through csv.DictWriter
    over the same Python objects, so they agree by construction — and "by construction"
    is a claim worth executing, because it stops holding the moment either writer starts
    formatting a value on its way out.
    """
    with (ROOT / "ledger" / "signals.csv").open(newline="", encoding="utf-8") as f:
        recorded = list(csv.DictReader(f))
    day = max(r["date"] for r in recorded)
    narrow = [r for r in recorded if r["date"] == day]
    assert len(narrow) == 50, f"expected the recorded fifty for {day}, got {len(narrow)}"

    # The full scored set: the recorded fifty, plus the names the nightly scores and
    # then discards at write time.
    padding = [_row(f"PAD{i:03d}", 20 - (i % 15), 1e8 + i, date=day)
               for i in range(185)]
    path, n = nightly.write_xsec(narrow + padding, day)
    assert n == 235

    with path.open(newline="", encoding="utf-8") as f:
        wide = {(r["date"], r["symbol"]): r for r in csv.DictReader(f)}
    assert len(wide) == 235

    checked = 0
    for r in narrow:
        key = (r["date"], r["symbol"])
        assert key in wide, f"{key} was dropped by the writer"
        for field in nightly.XSEC_SHARED_FIELDS:
            assert wide[key][field] == r[field], (
                f"{key} disagrees on {field}: "
                f"signals.csv {r[field]!r} vs xsec {wide[key][field]!r}")
            checked += 1
    assert checked == 50 * len(nightly.XSEC_SHARED_FIELDS)

    # And the wide file really is wider: every recorded row is in it, and so are the
    # ones signals.csv never held.
    assert len(wide) > len(narrow)
    assert {r["symbol"] for r in narrow} < set(s for _, s in wide)


def test_the_wide_ledger_is_wider_than_the_narrow_one():
    """A cross-section that is also fifty rows would be the same defect with a new path."""
    d = ROOT / "ledger" / "xsec"
    if not d.exists():
        pytest.skip("no shard written yet — the first one lands with the next nightly")
    per_night = {}
    for path in sorted(d.glob("*.csv")):
        with path.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("src") == "live":
                    per_night[r["date"]] = per_night.get(r["date"], 0) + 1
    for day, n in per_night.items():
        assert n > 50, f"{day} recorded {n} rows — the cross-section was truncated"


# ------------------------------------------------------------- the legacy series

def test_the_legacy_ledger_did_not_move():
    """signals.csv keeps its columns and its rows, and the score keeps its hash."""
    assert len(nightly.FIELDS) == 71
    assert nightly.FIELDS[:9] == ["date", "symbol", "name", "price", "market_cap",
                                  "turnover_pct", "erosion_ratio", "conviction", "signal"]
    with (ROOT / "ledger" / "signals.csv").open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == nightly.FIELDS
    # This change is persistence only. If it moved the hash, something scoring-shaped
    # was edited by accident.
    assert nightly.SPEC_HASH == "1a4ea6e4d77e"


def test_nothing_in_the_page_reads_the_research_ledger():
    """The browser fetches signals.json whole on every load. This must not join it."""
    page = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "ledger/xsec" not in page
    assert "xsec" not in (ROOT / "methodology.html").read_text(encoding="utf-8")


def test_the_writer_is_not_part_of_the_specification():
    """Recording more rows is not a re-valuation of any of them."""
    captured = nightly.spec()["functions"]
    for name in ("write_xsec", "xsec_rows", "_xsec_rank", "observed_rs_windows",
                 "write_xsec_schema", "xsec_shard_path"):
        assert name not in captured
        for body in captured.values():
            assert name not in body, f"{name} reached a scoring function"
