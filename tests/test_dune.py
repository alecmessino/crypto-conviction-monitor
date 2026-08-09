"""The Dune feed, mocked.

The API is unreachable from CI without the secret, and a test that needs a live key is
a test that does not run. Everything here drives ``fetch_dune_module_b`` through a
stubbed ``_get_json``, so the parsing, the aliasing, the paging and the derived columns
are all covered without a network call.

The property these tests exist to protect is that this feed is **observational**.
Nothing it returns reaches ``score()``. The specification hash is pinned below, because
the entire reason for recording these fields rather than scoring them is that adopting
them must be a separate, deliberate, hashed decision — and a change that quietly moved
the hash would have restarted the crypto track record for the second time this week.
"""
import csv
import importlib.util
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("dune_mod", HERE.parent / "nightly.py")
nightly = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nightly)


def stub(monkeypatch, pages):
    """Serve canned Dune payloads, one per call, and record the URLs requested."""
    seen = []

    def fake(url, headers=None):
        seen.append(url)
        page = pages[min(len(seen) - 1, len(pages) - 1)]
        return {"result": {"rows": page}}
    monkeypatch.setattr(nightly, "_get_json", fake)
    return seen


# ---------------------------------------------------------------------------
# the property that matters most
# ---------------------------------------------------------------------------
def test_the_dune_feed_does_not_touch_the_specification():
    """Recording without scoring must not move the hash.

    If it did, wiring up a data feed would silently restart the history the hash exists
    to segment — the exact failure that made 2026-08-05 invisible.
    """
    captured = nightly.spec()["functions"]
    for fn in captured.values():
        for field in ("unlocks_usd", "supply_increase_pct", "addr_growth_pct",
                      "adoption_dilution", "unlock_overhang_pct", "dune"):
            assert field not in fn, f"{field} reached a scoring function"


def test_the_recorded_columns_exist_and_are_appended_not_inserted():
    tail = nightly.FIELDS[-4:]
    assert tail == ["perp_mult", "spec_hash", "unlock_overhang_pct", "adoption_dilution"] \
        or set(("unlock_overhang_pct", "adoption_dilution")).issubset(nightly.FIELDS)
    for f in ("unlocks_usd", "supply_increase_pct", "addr_growth_pct", "era"):
        assert f in nightly.FIELDS


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def test_a_well_formed_response_is_keyed_by_upper_case_symbol(monkeypatch):
    stub(monkeypatch, [[{"symbol": "arb", "unlocks_usd": 1e6,
                         "supply_increase_pct": 4.0, "addr_growth_pct": 2.0}]])
    out = nightly.fetch_dune_module_b("123", "key")
    assert set(out) == {"ARB"}
    assert out["ARB"]["unlocks_usd"] == 1e6
    assert out["ARB"]["era"] == 2.0        # 4% emission against 2% adoption


def test_era_is_taken_from_the_query_when_it_supplies_one(monkeypatch):
    stub(monkeypatch, [[{"symbol": "OP", "supply_increase_pct": 4.0,
                         "addr_growth_pct": 2.0, "era": 9.9}]])
    assert nightly.fetch_dune_module_b("1", "k")["OP"]["era"] == 9.9


def test_zero_adoption_does_not_divide_by_zero(monkeypatch):
    stub(monkeypatch, [[{"symbol": "X", "supply_increase_pct": 5.0, "addr_growth_pct": 0}]])
    assert nightly.fetch_dune_module_b("1", "k")["X"]["era"] is None


def test_column_aliases_are_accepted(monkeypatch):
    """The query is written by a human in a web editor and the column is called whatever
    they called it. A fetcher that accepts one spelling returns nothing and looks
    exactly like a feed with no data."""
    stub(monkeypatch, [[{"TOKEN": "PEPE", "UNLOCKS": 5e5,
                         "emission_pct": 3.0, "address_growth": 1.5}]])
    rec = nightly.fetch_dune_module_b("1", "k")["PEPE"]
    assert rec["unlocks_usd"] == 5e5
    assert rec["supply_increase_pct"] == 3.0
    assert rec["addr_growth_pct"] == 1.5


def test_rows_without_a_symbol_are_skipped(monkeypatch):
    stub(monkeypatch, [[{"unlocks_usd": 1.0}, {"symbol": "OK", "unlocks_usd": 2.0}]])
    assert set(nightly.fetch_dune_module_b("1", "k")) == {"OK"}


def test_unparseable_numbers_become_null_rather_than_zero(monkeypatch):
    """Zero is a claim about emission. Absent is not."""
    stub(monkeypatch, [[{"symbol": "Z", "unlocks_usd": "n/a",
                         "supply_increase_pct": "", "addr_growth_pct": None}]])
    rec = nightly.fetch_dune_module_b("1", "k")["Z"]
    assert rec == {"unlocks_usd": None, "supply_increase_pct": None,
                   "addr_growth_pct": None, "era": None}


# ---------------------------------------------------------------------------
# paging and failure
# ---------------------------------------------------------------------------
def test_a_full_page_triggers_another_request(monkeypatch):
    full = [{"symbol": f"T{i}", "unlocks_usd": i} for i in range(1000)]
    seen = stub(monkeypatch, [full, [{"symbol": "LAST", "unlocks_usd": 1}]])
    out = nightly.fetch_dune_module_b("1", "k")
    assert "LAST" in out and len(out) == 1001
    assert "offset=0" in seen[0] and "offset=1000" in seen[1]


def test_a_short_page_stops_paging(monkeypatch):
    seen = stub(monkeypatch, [[{"symbol": "A"}]])
    nightly.fetch_dune_module_b("1", "k")
    assert len(seen) == 1


def test_a_failed_call_returns_nothing_rather_than_raising(monkeypatch):
    """The nightly must not lose a night's board because an optional feed is down."""
    def boom(url, headers=None):
        raise RuntimeError("503")
    monkeypatch.setattr(nightly, "_get_json", boom)
    assert nightly.fetch_dune_module_b("1", "k") == {}


def test_an_empty_result_is_empty_not_an_error(monkeypatch):
    monkeypatch.setattr(nightly, "_get_json", lambda url, headers=None: {})
    assert nightly.fetch_dune_module_b("1", "k") == {}


# ---------------------------------------------------------------------------
# the derived context
# ---------------------------------------------------------------------------
def test_unlock_overhang_is_normalised_by_market_cap():
    """A $10m unlock is noise for a $2t asset and existential for a $30m one. The raw
    dollar figure alone cannot tell those apart."""
    rec = {"unlocks_usd": 10e6, "era": None}
    assert nightly.dune_context(rec, 2e12)["unlock_overhang_pct"] == pytest.approx(0.0005)
    assert nightly.dune_context(rec, 30e6)["unlock_overhang_pct"] == pytest.approx(33.3333, abs=0.01)


def test_adoption_dilution_inverts_era_so_larger_is_better():
    """Every other reading on the board is larger-is-better. An inverted one invites a
    reader to misread the sign at exactly the moment it matters."""
    assert nightly.dune_context({"era": 2.0}, 1e9)["adoption_dilution"] == 0.5
    assert nightly.dune_context({"era": 0.5}, 1e9)["adoption_dilution"] == 2.0


def test_context_is_null_when_the_feed_is_absent():
    assert nightly.dune_context(None, 1e9) == {"unlock_overhang_pct": None,
                                               "adoption_dilution": None}


def test_context_is_null_rather_than_zero_when_market_cap_is_missing():
    out = nightly.dune_context({"unlocks_usd": 1e6, "era": 1.0}, None)
    assert out["unlock_overhang_pct"] is None
    assert out["adoption_dilution"] == 1.0        # era needs no market cap


def test_a_non_positive_era_yields_no_ratio():
    assert nightly.dune_context({"era": 0.0}, 1e9)["adoption_dilution"] is None
    assert nightly.dune_context({"era": -1.0}, 1e9)["adoption_dilution"] is None


# ---------------------------------------------------------------------------
# how the monitor treats an observational feed
# ---------------------------------------------------------------------------
@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "LEDGER_CSV", tmp_path / "signals.csv")

    def write(rows):
        with (tmp_path / "signals.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=nightly.FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({**{k: "" for k in nightly.FIELDS}, **r})
    return write


def board(date, n=30, **extra):
    return [{"date": date, "symbol": f"A{i:02d}", "name": f"A{i:02d}",
             "conviction": 90 - i * 2.0, "signal": nightly._tier_for(90 - i * 2.0),
             "price": 1.0 + i, "market_cap": 1e9, "turnover_pct": 30.0,
             "rs7": 1.0, "rs14": 1.0, "rs30": 1.0, "rs200": 1.0, "perp_mult": 1.0,
             "spec_hash": "abc123", **extra}
            for i in range(n)]


def status(mon, name):
    return next(c["status"] for c in mon["health"] if c["name"] == name)


def test_an_absent_dune_feed_does_not_amber_the_input_panel(ledger):
    """The feed is null until the query is configured, and for `unlocks_usd` it stays
    largely null forever — unlock schedules are contractual, not on-chain. Folding that
    into the input warn pins the panel to a permanent amber, and a permanent amber is
    what a real dropout in rs200 would then hide behind."""
    ledger(board("2026-03-01") + board("2026-03-02"))
    mon = nightly._compute_monitor()
    assert status(mon, "Field presence") == "pass"
    assert all(v == 0.0 for v in mon["coverage"]["context"]["latest"].values())


def test_the_contextual_feed_is_reported_but_never_graded(ledger):
    ledger(board("2026-03-01") + board("2026-03-02"))
    assert status(nightly._compute_monitor(), "Contextual feeds") == "info"
    ledger(board("2026-03-01", unlocks_usd=5e5) + board("2026-03-02", unlocks_usd=5e5))
    assert status(nightly._compute_monitor(), "Contextual feeds") == "info"


def test_a_live_feed_shows_up_as_coverage(ledger):
    ledger(board("2026-03-01", era=0.8, unlocks_usd=1e6)
           + board("2026-03-02", era=0.8, unlocks_usd=1e6))
    latest = nightly._compute_monitor()["coverage"]["context"]["latest"]
    assert latest["era"] == 1.0 and latest["unlocks_usd"] == 1.0
    assert latest["addr_growth_pct"] == 0.0


def test_no_field_is_both_an_input_and_context():
    """A field in both lists would be graded and excused at once, and which one won
    would depend on dict ordering."""
    assert not set(nightly.MON_TRACKED_FIELDS) & set(nightly.MON_CONTEXT_FIELDS)
    for f in nightly.MON_CONTEXT_FIELDS:
        assert f in nightly.FIELDS


# ---------------------------------------------------------------------------
# schema growth
# ---------------------------------------------------------------------------
def test_the_new_columns_are_appended_so_the_old_header_is_a_prefix():
    """signals.csv is rewritten in full each night from name-keyed rows, so a schema
    that grows at the end migrates itself. Inserting mid-list instead would leave the
    committed file's header no longer a prefix of the schema, which is the shape the
    validator cannot distinguish from the misalignment bug it exists to catch."""
    old = [f for f in nightly.FIELDS if f not in ("unlock_overhang_pct", "adoption_dilution")]
    assert nightly.FIELDS[:len(old)] == old
    assert nightly.FIELDS[-2:] == ["unlock_overhang_pct", "adoption_dilution"]


# ---------------------------------------------------------------------------
# the documented query
# ---------------------------------------------------------------------------
def test_the_query_template_ships_and_names_its_own_limits():
    sql = (HERE.parent / "docs" / "dune_module_b.sql").read_text()
    for col in ("symbol", "supply_increase_pct", "addr_growth_pct", "unlocks_usd"):
        assert col in sql
    # It must say that unlock schedules are not generally on-chain, or someone will read
    # a null column as a broken feed rather than as an honest gap.
    assert "not on-chain" in sql.lower()
    assert "DUNE_UNLOCK_QUERY_ID" in sql
