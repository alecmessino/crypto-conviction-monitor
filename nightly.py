#!/usr/bin/env python3
"""Standalone nightly signal persistence for the public monitor.

Self-contained so it runs in CI without the full launch_skew package.
Appends one row per (date, symbol) to ledger/signals.csv + ledger/signals.json
so a REAL backtest ledger accumulates over time. No fabricated history.

Dune: if DUNE_API_KEY + DUNE_UNLOCK_QUERY_ID are set, unlock context is
fetched; otherwise Module B columns stay null (never fabricated).
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

LEDGER_DIR = Path(__file__).resolve().parent / "ledger"
LEDGER_CSV = LEDGER_DIR / "signals.csv"
LEDGER_JSON = LEDGER_DIR / "signals.json"
FIELDS = ["date", "symbol", "name", "price", "market_cap", "turnover_pct",
          "erosion_ratio", "conviction", "signal",
          "rs7", "rs14", "rs30", "rs200", "rs_blend",
          "c_liquidity", "c_era", "c_depth", "c_momentum",
          "unlocks_usd", "supply_increase_pct", "addr_growth_pct", "era",
          "roi_30d", "roi_90d", "survived", "perp_mult",
          # Appended, never inserted: the columns are positional in every reader, and
          # inserting one mid-list would reinterpret every row already written.
          # Historical rows carry an empty value — their specification is genuinely
          # unknown and must report as unknown rather than be assumed to match today's.
          "spec_hash",
          # Derived from the Dune columns above. Observational: score() reads neither,
          # and adopting them would be a separate, hashed decision. Appended at the end
          # for the reason stated above — the file on disk was written under the shorter
          # header, and a strict prefix is what lets the next run widen it cleanly.
          "unlock_overhang_pct", "adoption_dilution",
          # Module 1 — derivatives. funding_rate already reaches score() through
          # lavl_perp_mult and has done since before this column existed; recording it
          # changes nothing about that. Everything else here is observational, on the
          # same terms as the Dune columns: score() reads none of it, and adopting any
          # of it would be a separate decision that moves the specification hash.
          # oi_usd in particular was already being fetched from Bybit on every run and
          # thrown away — the ingestion cost was being paid and the data discarded.
          "funding_rate", "funding_ann_pct", "oi_usd", "oi_chg_24h_pct",
          "oi_to_mcap", "long_short_ratio", "oi_price_divergence",
          # Module 2 — the daily bar. Recorded so ATR and the choppiness index can be
          # computed from accumulated history; CoinGecko has no daily-granularity OHLC
          # endpoint, but high_24h/low_24h are already in the markets response that
          # every run makes, so this costs nothing and back-fills nothing.
          "high_24h", "low_24h",
          # Module 3 — cross-venue funding. Appended for the reason stated above.
          #
          # `funding_ann_pct` above is RETIRED — historical values stand, nothing new is
          # written into it. It annualised at a fixed three settlements a day, which was
          # correct for the single Bybit feed that produced it and wrong for any venue on
          # another clock. That is not hypothetical: on 2026-08-17 every rate in
          # production came from an hourly venue, and that column would have understated
          # all 26 of them eightfold. `funding_apr` beside `funding_interval_h` says the
          # same thing without the ambiguity, so keeping both would be keeping one that
          # can only ever be right by coincidence.
          #
          # `funding_interval_h` is the field that makes the rate a measurement rather
          # than a number: 0.0001 at Bybit's 8h and 0.0001 at Hyperliquid's 1h are
          # 10.95% and 87.6% a year, and without the interval the column holds two
          # units. `rsi7` and `funding_regime` are recorded because they are inputs to
          # the modifier and a score whose inputs are not recorded cannot be audited
          # after the fact.
          "funding_apr", "funding_interval_h", "funding_venue", "funding_venues_n",
          "funding_apr_spread", "funding_regime", "rsi7",
          # Trailing funding, over the nights actually recorded. One settlement print is
          # a noisy estimate of what a position would earn holding the asset: funding
          # mean-reverts, and a single hot night is not a carry regime. These columns say
          # whether it has been paying, and how consistently.
          #
          # Observational for now, deliberately. Substituting a trailing figure into the
          # modifier would make the score lag a genuine regime change, and choosing
          # between those is a decision that should be made against recorded evidence
          # rather than asserted — which is what recording these makes possible.
          "funding_apr_trail", "funding_trail_n", "funding_pos_share",
          # The counterfactual: what the modifier WOULD have been had it read the
          # trailing carry instead of tonight's print. Recorded, never applied — so the
          # comparison that decides whether to adopt it is a query over this column in a
          # month's time rather than a re-run, and the decision is made against evidence
          # rather than asserted now on two nights of data.
          #
          # Adopting it is deliberately a one-line edit inside lavl_perp_mult — a
          # captured SPEC_FUNCTION — so the switch moves the specification hash and
          # draws its own boundary, which is the whole point of having one.
          "perp_mult_trail",
          # Module 4 — Cryptometer. Forced selling, which nothing else in this pipeline
          # can see: a conviction score cannot tell an orderly decline from a cascade,
          # and that distinction is what decides whether to wait for a better fill.
          # Observational, like every other derivatives column except funding.
          "liq_longs_usd", "liq_shorts_usd", "liq_imbalance",
          # How the interval behind funding_apr was arrived at: reported by the venue,
          # fixed by its protocol, or assumed. Recorded because it is the difference
          # between a measured carry and a plausible one, and only the first belongs in
          # a study.
          "funding_interval_basis",
          # Module F — supply overhang. The FIRST of these is scoring: `emission_mult`
          # is what score() multiplied the risk term by tonight, and `emission_drag` is
          # the severity it came from. Both are recorded because a multiplier whose
          # input is not on the row cannot be audited after the fact, and because
          # `emission_drag` empty means the FDV was not published while 0.0 means the
          # token is fully circulating — two different facts that a single column would
          # collapse.
          "emission_drag", "emission_mult", "fdv_usd",
          # Module G — trend structure, from bars this pipeline recorded itself. A
          # 14-period ADX needs 29 nights (Wilder consumes the period twice); until then
          # every one of these is empty and `adx_bars` says how far off it is. Purely
          # observational: score() reads none of it, and the strategy label is a
          # statement about which book an asset suits, not about its rank.
          "adx", "plus_di", "minus_di", "adx_regime", "adx_bars", "atr14", "strategy",
          # Module H — systemic risk. Correlation and beta against BTC over the trailing
          # window, computed from recorded closes. The sizer reads these to cap a book
          # that is fifteen names and one bet; nothing else does.
          "corr_btc", "beta_btc", "corr_obs",
          # Module I — the asset against its own liquidity history rather than against
          # the universe. A cross-sectional screen flags the same illiquid names every
          # night; this flags the night a normally-liquid name stops trading.
          "turnover_z", "liq_shock",
          # Module J — search attention against model conviction. `tmd_divergence` is
          # positive when the crowd ranks a name higher than this model does. Empty for
          # anything not on tonight's trending list, which is most of the board.
          "trending_rank", "tmd_divergence", "tmd_label"]

# Dune Analytics (Module B: vesting / emission-vs-adoption ERA).
# Key is read ONLY from env DUNE_API_KEY (supplied by the CI secret). Never hardcoded.
# A public unlock-schedule query is configured via DUNE_UNLOCK_QUERY_ID.
DUNE_BASE = "https://api.dune.com/api/v1"
# CoinGecko free markets endpoint (separate host from Dune).
CG_BASE = "https://api.coingecko.com/api/v3"

STABLES = {"USDT", "USDC", "DAI", "BUSD", "TUSD", "USDD", "FDUSD", "USDE",
           "USD1", "USDS", "PYUSD", "GUSD", "USDG", "FRAX", "USDD", "TUSD",
           "XAUT", "PAXG"}


def _load_sibling(filename: str, modname: str):
    """Load a sibling module by path, compiling its source in process.

    Same reasoning as _load_funding below, which this generalises: by-name imports fail
    under pytest, and going through the normal loader can execute a stale .pyc whose
    constants disagree with the source spec() parses.
    """
    import importlib.util
    path = Path(__file__).resolve().parent / filename
    src = path.read_text(encoding="utf-8")
    spec_ = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec_)
    exec(compile(src, str(path), "exec"), mod.__dict__)  # noqa: S102
    return mod


def _load_funding():
    """Load funding.py by path rather than by name.

    This module is executed three different ways — as a script, by importlib under a
    made-up module name from the test suite, and by the validator from another
    directory — and only one of those puts the repository root on ``sys.path``. A plain
    ``import funding`` works when run as a script and raises ImportError under pytest,
    which is a failure mode that shows up as "the tests are broken" rather than as what
    it is. Loading relative to ``__file__`` works in all three.

    The source is compiled in process rather than executed through the normal loader,
    which is not fastidiousness. ``spec()`` parses funding.py *from disk* to capture the
    scoring functions, and reads the modifier constants off this module *object*. Those
    two have to come from the same bytes or the specification hash describes code that
    did not run.

    They can diverge, and it is not exotic. CPython reuses a cached ``.pyc`` whenever the
    source's size and mtime match the cache header, and mtime there has one-second
    granularity — so two edits of equal length within the same second, or a fresh CI
    checkout that stamps files identically, load the old bytecode while ``read_text``
    returns the new source. That was observed here: after editing MOD_MAX_PENALTY and
    restoring it, the loaded module still reported the edited value, and three different
    threshold edits all produced one identical hash. A hash mechanism that reports the
    file it did not execute is worse than no hash mechanism.
    """
    path = Path(__file__).resolve().parent / "funding.py"
    src = path.read_text(encoding="utf-8")
    import importlib.util
    spec_ = importlib.util.spec_from_file_location("cm_funding", path)
    mod = importlib.util.module_from_spec(spec_)
    exec(compile(src, str(path), "exec"), mod.__dict__)  # noqa: S102 - see above
    return mod


funding = _load_funding()
cryptometer = _load_sibling("cryptometer.py", "cm_client")
# Neither of these reaches score(), so neither is captured in the specification hash.
# coingecko.py supplies market context (trending, sectors, macro anchors, on-chain
# depth); quant.py derives readings from bars this pipeline already recorded. Loaded the
# same way as the other two so a fresh CI checkout cannot serve a stale .pyc for them
# either — the reasoning in _load_funding applies to any sibling, not just the scored one.
coingecko = _load_sibling("coingecko.py", "cm_coingecko")
quant = _load_sibling("quant.py", "cm_quant")
# The RWA workspace. Loaded the same way and, like the two above, captured in NO part of
# SPEC_HASH — not because it has no thresholds, but because it has its own. rwa.spec_hash()
# segments the RWA track record and nightly.SPEC_HASH segments the crypto one; a shared
# digest would make every edit to either invalidate the history of both, which is the
# opposite of what a specification hash is for.
rwa = _load_sibling("rwa.py", "cm_rwa")


# ---------------------------------------------------------------------------
# specification identity
# ---------------------------------------------------------------------------
# Every function whose text can change a published score. Named here rather than
# inferred, so adding a scoring function is a deliberate act that shows up in review.
SPEC_FUNCTIONS = ("score", "_lavl_regime", "lavl_perp_mult", "_tier_for",
                  # Module F. Captured for the same reason the funding curve is:
                  # emission_mult multiplies the published score, so an edit to
                  # either the severity curve or its envelope must re-segment the
                  # track record rather than quietly reinterpret it.
                  "emission_drag", "emission_mult")
SPEC_CONSTANTS = ("TIER_CUTS", "STABLES",
                  "EMISSION_FREE_RATIO", "EMISSION_ANCHOR_RATIO",
                  "EMISSION_ANCHOR_SEVERITY", "EMISSION_MAX_PENALTY")

# The same, for funding.py. lavl_perp_mult is a two-line delegation, so without this the
# specification would capture the *call* and none of the arithmetic behind it: the
# regime boundaries, the severity curve, the confirmation weights and the envelope all
# live in the other file, and every one of them changes published scores.
#
# This was a live hole for exactly one commit. Moving the modifier into funding.py left
# the hash reading 872935361713 both before and after the step function was replaced by
# a continuous surface — a rewrite of the entire scoring curve that the mechanism built
# to notice scoring rewrites reported as no change at all. A specification that stops at
# a module boundary is not a specification, it is a description of one file.
SPEC_FUNDING_FUNCTIONS = ("annualize", "classify_regime", "funding_severity",
                          "regime_modifier", "rsi", "_ramp", "_atanh_scale",
                          "_num_or_none")
SPEC_FUNDING_CONSTANTS = (
    "REGIME_OVERHEATED", "REGIME_ELEVATED", "REGIME_NEUTRAL_FLOOR", "REGIME_SQUEEZE",
    "MOD_MAX_PENALTY", "MOD_MAX_BOOST", "MOD_HOT_ANCHOR", "MOD_COLD_ANCHOR",
    "MOD_SQUEEZE_SATURATION", "MOD_OVERHEATED_PRICE_CHG", "MOD_UNCONFIRMED_WEIGHT",
    "MOD_SQUEEZE_RSI", "MOD_SQUEEZE_RSI_FULL",
    # Derived from the anchors above. Captured anyway rather than trusted to follow,
    # so a change to the derivation is caught even if every anchor stays put.
    "MOD_HOT_SCALE", "MOD_COLD_SCALE")


def spec() -> dict:
    """A canonical form of everything that can change a score.

    The sibling equity project enumerates named constants because its thresholds *are*
    named constants. Here they are literals inside the scoring curves — ``turnover <=
    0.30``, ``era < 0.7`` — so there is nothing to list. The specification is therefore
    derived from the code itself: each scoring function is parsed, its docstrings
    stripped, and unparsed back to canonical source. Editing a threshold changes the
    result; editing a comment or reflowing a line does not.

    Without this the history is not segmentable, and an Information Coefficient or a
    track record computed across a silent threshold change is a number about two
    different models.
    """
    import ast

    here = Path(__file__).resolve().parent

    def capture(path: Path, wanted: set, prefix: str = "") -> dict:
        """Canonical source for each named function in one file.

        Read from disk rather than via sys.modules: the validator and the tests load
        these modules through importlib under names that are never registered there,
        and a specification that only computes when imported normally is not a
        specification.
        """
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in wanted:
                continue
            body = node.body
            # Drop the docstring so prose edits do not falsely segment the series.
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            stripped = ast.FunctionDef(
                name=node.name, args=node.args, body=body or [ast.Pass()],
                decorator_list=[], returns=None, type_comment=None,
                type_params=getattr(node, "type_params", []))
            ast.fix_missing_locations(stripped)
            found[prefix + node.name] = ast.unparse(stripped)
        missing = {prefix + n for n in wanted} - set(found)
        if missing:
            # A renamed scoring function must not silently drop out of the specification.
            raise RuntimeError(
                f"spec() cannot find scoring function(s) in {path.name}: {sorted(missing)}")
        return found

    parts = capture(Path(__file__), set(SPEC_FUNCTIONS))
    parts.update(capture(here / "funding.py", set(SPEC_FUNDING_FUNCTIONS), "funding."))

    consts = {}
    for name in SPEC_CONSTANTS:
        value = globals().get(name)
        consts[name] = sorted(value) if isinstance(value, set) else value
    for name in SPEC_FUNDING_CONSTANTS:
        if not hasattr(funding, name):
            raise RuntimeError(f"spec() cannot find funding constant: {name}")
        consts["funding." + name] = getattr(funding, name)
    return {"functions": parts, "constants": consts}


def spec_hash() -> str:
    """Short stable digest of spec(), recorded on every row.

    Tied to the interpreter's ``ast.unparse`` output, so a Python upgrade can in
    principle shift it without the model changing. That is visible rather than silent —
    the monitor reports the hash per day and a spurious change shows up as a boundary
    with no accompanying code change.
    """
    import hashlib
    blob = json.dumps(spec(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


# The value is computed at the BOTTOM of this module, not here. See the block above the
# assignment for why — it is not a style preference, it was a hole in the specification.


# ---------------------------------------------------------------------------
# specification equivalence
# ---------------------------------------------------------------------------
# Exactly one entry, and it is a correction to the INSTRUMENTATION rather than to the
# model. It exists so twenty nights of otherwise-comparable history are not split into
# two track records by a bug in the thing that measures track records.
#
# What happened: SPEC_HASH used to be assigned near the top of this module, before
# TIER_CUTS and the four emission anchors were executed. spec() reads constants with
# globals().get(), which returns None for anything not yet defined, so those five were
# hashed as null on every row this repository has ever written.
#
# What was audited before adding this entry — every commit touching nightly.py from
# 2026-08-02 to 2026-08-20, values extracted by AST rather than by diffing text:
#
#   TIER_CUTS      introduced 2026-08-08 (1e7a24b) as ((80,STRONG),(70,BUY),(55,HOLD),
#                  (40,WATCH),(0,AVOID)) — the same cuts score() already applied as
#                  literals, so a refactor rather than a re-valuation. NEVER changed
#                  afterwards. It predates both SPEC_CONSTANTS (b0497e3, 2026-08-09)
#                  and the first hashed night (2026-08-09).
#   EMISSION_*     introduced 2026-08-19 (cd6db73) with Module F. NEVER changed since.
#                  Their introduction is a real scoring change and is already segmented
#                  by the function capture: e65f7dc59d55 -> 2da60f7efd7b.
#
# So across the entire hashed period no captured constant ever changed value, and the
# null capture therefore hid nothing. The only pair of digests that describes one body
# of scoring code twice is the one below, and the equality is proved rather than
# asserted: 43f4b3f's only non-comment change to this file is the relocation of the
# SPEC_HASH assignment, and spec()["functions"] is byte-identical either side of it.
# tests/test_persistence.py re-derives 2da60f7efd7b from today's source on every run.
#
# This is NOT a general amnesty. d600984ec00b and e65f7dc59d55 were computed under the
# same buggy instrumentation but describe genuinely different scoring code (no emission
# multiplier at all), so they remain distinct segments and are deliberately absent here.
SPEC_EQUIVALENT = {
    "2da60f7efd7b": {
        "canonical": "6f98778fa627",
        "reason": "instrumentation",
        # The hash HEAD produced when the equivalence was verified. The re-derivation
        # test only runs while SPEC_HASH still equals this; past that point the entry is
        # a frozen historical record and is asserted to be unmodified instead.
        "verified_against": "6f98778fa627",
        # The five the old ordering captured as None. Nulling exactly these in today's
        # specification reproduces the superseded digest.
        "null_constants": ("TIER_CUTS", "EMISSION_FREE_RATIO", "EMISSION_ANCHOR_RATIO",
                           "EMISSION_ANCHOR_SEVERITY", "EMISSION_MAX_PENALTY"),
        "detail": ("SPEC_HASH was computed before five captured constants were defined, "
                   "so they hashed as null. Same scoring functions, same constant "
                   "values, different instrumentation — scoring-equivalent."),
    },
}


def canonical_spec_hash(h):
    """Collapse a superseded digest onto the one describing the same scoring code.

    Identity for everything not in the table, which is every hash except the single
    audited pair. A history spanning two hashes really is two datasets — that rule is
    not being softened here, only applied to the model rather than to a defect in the
    ruler.
    """
    key = (h or "").strip()
    entry = SPEC_EQUIVALENT.get(key)
    return entry["canonical"] if entry else key


def spec_hash_as_recorded_before(null_constants) -> str:
    """Today's specification, re-hashed with `null_constants` forced to None.

    This is what makes the equivalence claim checkable rather than a comment. The old
    ordering produced exactly this: the same functions and the same constant dict, with
    the late-defined entries still unset at the moment the digest was taken.
    """
    import hashlib
    sp = spec()
    doctored = {"functions": sp["functions"],
                "constants": {k: (None if k in set(null_constants) else v)
                              for k, v in sp["constants"].items()}}
    blob = json.dumps(doctored, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def _get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "conviction-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec
        return json.loads(resp.read().decode())


# Column-name tolerance. A Dune query is written by a human in a web editor and the
# column will be called whatever they called it; a fetch that only accepts one spelling
# silently returns nothing and looks identical to "no data".
DUNE_ALIASES = {
    "symbol": ("symbol", "token", "ticker", "asset"),
    "unlocks_usd": ("unlocks_usd", "unlocks", "unlock_usd", "unlock_value_usd",
                    "upcoming_unlocks_usd"),
    "supply_increase_pct": ("supply_increase_pct", "supply_increase", "emission_pct",
                            "inflation_pct", "supply_growth_pct"),
    "addr_growth_pct": ("addr_growth_pct", "address_growth", "address_growth_pct",
                        "active_address_growth_pct", "adoption_pct"),
    "era": ("era", "erosion_ratio", "emission_adoption_ratio"),
}


def _pick(row: dict, field: str):
    """First alias present in the row, matched case-insensitively."""
    lower = {str(k).lower(): v for k, v in row.items()}
    for alias in DUNE_ALIASES[field]:
        if alias in lower and lower[alias] not in (None, ""):
            return lower[alias]
    return None


def fetch_dune_report(query_id: str, api_key: str) -> dict:
    """The Module B fetch, plus why it produced what it produced.

    ``{"data": {SYMBOL: {...}}, "status": ..., "detail": ..., "columns": [...]}``.

    The status matters because four different situations all end in a table of nulls,
    and a single "no data" message cannot tell a reader which one they are in. The one
    that actually happened here: a valid key and a real query id pointing at a query
    about something else entirely — it returned ``cryptocurrency`` and
    ``volume_24h_usd``, every row was dropped for want of a symbol column, and the
    dashboard would have said "unconfigured or down" while being configured and up.

      unconfigured  no key or no query id
      unreachable   the call failed — wrong key, missing query, never executed
      unusable      rows came back but nothing in them was recognisable
      partial       symbols recognised, some fields absent (the expected steady state:
                    unlock schedules are contractual, so that column is mostly null)
      live          symbols and every expected field present

    Nothing here reaches ``score()``, which computes its own ERA proxy from 24-hour
    stability and has never read these columns. Recording without scoring is deliberate:
    the fields accumulate history now, and adopting them later is a separate, hashed
    decision that will draw its own specification boundary.

    ERA = supply_increase_pct / addr_growth_pct — emission against adoption. Above 1 the
    token is diluting faster than it is being adopted.
    """
    out: dict = {}
    columns: list = []
    seen = 0
    if not (query_id and api_key):
        return {"data": out, "status": "unconfigured", "columns": columns,
                "detail": "no query id or key configured"}
    try:
        # Paged: a saved query over the full token universe exceeds one page, and a
        # silently truncated result is indistinguishable from a partial feed.
        offset, limit = 0, 1000
        for _ in range(10):
            url = f"{DUNE_BASE}/query/{query_id}/results?limit={limit}&offset={offset}"
            data = _get_json(url, headers={"X-Dune-Api-Key": api_key})
            rows = (data.get("result") or {}).get("rows") or []
            # Union across rows, not rows[0]: Dune omits keys whose value is null, so
            # any single row understates the result set. Reading the first row alone is
            # what made a feed enriching 99 tokens look like it returned nothing
            # recognisable.
            columns = sorted(set(columns) | {k for r in rows for k in r})
            seen += len(rows)
            for r in rows:
                sym = str(_pick(r, "symbol") or "").upper().strip()
                if not sym:
                    continue
                rec = {k: _num(_pick(r, k)) for k in
                       ("unlocks_usd", "supply_increase_pct", "addr_growth_pct", "era")}
                if rec["era"] is None:
                    sup, addr = rec["supply_increase_pct"], rec["addr_growth_pct"]
                    if sup is not None and addr:
                        rec["era"] = round(sup / addr, 3)
                out[sym] = rec
            if len(rows) < limit:
                break
            offset += limit
    except Exception as e:  # noqa: BLE001
        print(f"[dune] fetch failed, Module B -> null: {e}", file=__import__("sys").stderr)
        return {"data": {}, "status": "unreachable", "columns": columns,
                "detail": f"the call failed ({e}) — wrong key, no such query, or a "
                          f"query that has never been executed"}

    if not out:
        return {"data": out, "status": "unusable", "columns": columns,
                "detail": "the query returned %d row(s) but no column this feed "
                          "recognises" % seen}

    covered = {f: sum(1 for r in out.values() if r.get(f) is not None)
               for f in ("unlocks_usd", "supply_increase_pct", "addr_growth_pct")}
    missing = [f for f, n in covered.items() if n == 0]
    return {"data": out, "columns": columns,
            "status": "partial" if missing else "live",
            "detail": ("%d token(s) enriched" % len(out))
                      + ("; no values for " + ", ".join(missing) if missing else
                         "; every expected field present")}


def fetch_dune_module_b(query_id: str, api_key: str) -> dict:
    """Just the data. See :func:`fetch_dune_report` for why it is what it is."""
    return fetch_dune_report(query_id, api_key)["data"]


def dune_context(rec: dict | None, market_cap: float | None) -> dict:
    """The two derived readings, recorded alongside the raw fields.

    ``unlock_overhang_pct`` normalises the dollar unlock by market cap, which is the
    only form in which it means anything: a $10m unlock is noise for a $2t asset and an
    existential event for a $30m one.

    ``adoption_dilution`` inverts ERA so that larger is better, matching the direction
    of every other reading on the board. Both are context, not inputs — nothing here is
    read by ``score()``.
    """
    out = {"unlock_overhang_pct": None, "adoption_dilution": None}
    if not rec:
        return out
    unl = rec.get("unlocks_usd")
    if unl is not None and market_cap:
        out["unlock_overhang_pct"] = round(100.0 * unl / market_cap, 4)
    era = rec.get("era")
    if era is not None and era > 0:
        out["adoption_dilution"] = round(1.0 / era, 4)
    return out


def _median(values):
    """Median of the readable values, or None when none are. Not a mean, deliberately.

    Used for the board's funding temperature, where the distribution has a fat right
    tail: one asset printing a 900% annualised squeeze would drag a mean far enough to
    describe a board that does not exist.
    """
    vals = sorted(v for v in (_num(x) for x in (values or [])) if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 4)


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def fetch_markets(total: int = 250, per_page: int = 125, delay: float = 3.5,
                  session: dict | None = None) -> list[dict]:
    """Fetch the full universe in chunked pages with exponential backoff on 429.

    Splits `total` coins across multiple /coins/markets pages (CoinGecko caps
    per_page at 250 and rate-limits free keys), sleeping between calls so the
    job stays under the per-IP budget. On HTTP 429 it backs off and retries.

    ``session`` is a resolved credential from ``coingecko.open_session``. Passing one
    sends the key on every page, which is the entire point of configuring it: the
    backoff loop below exists because the keyless tier answers 429, and a page that
    exhausts its retries is silently dropped — the board is then scored over half a
    universe and nothing in the artifact says so. Omitting the session keeps the old
    keyless behaviour exactly, so this is additive and a missing secret degrades rather
    than breaks.
    """
    import time
    out: list[dict] = []
    host = (session or {}).get("host") or CG_BASE
    headers = {"User-Agent": "conviction-monitor/1.0", **((session or {}).get("headers") or {})}
    pages = max(1, (total + per_page - 1) // per_page)
    for page in range(1, pages + 1):
        url = (f"{host}/coins/markets?vs_currency=usd"
               f"&order=market_cap_desc&per_page={per_page}&page={page}"
               f"&price_change_percentage=24h,7d,14d,30d,200d")
        backoff = 5.0
        for attempt in range(4):
            try:
                data = _get_json(url, headers)
                if isinstance(data, list):
                    out.extend(data)
                break
            except urllib.error.HTTPError as e:
                if getattr(e, "code", None) == 429:
                    print(f"[429] page {page} attempt {attempt+1}: backing off {backoff:.0f}s",
                          file=__import__("sys").stderr)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                else:
                    print(f"[warn] page {page} HTTP {getattr(e, 'code', '?')}; skipping",
                          file=__import__("sys").stderr)
                    break
        else:
            print(f"[warn] page {page} gave up after retries", file=__import__("sys").stderr)
        time.sleep(delay)
    return out


# ---------------------------------------------------------------------------
# supply overhang (Module F) — the second thing that reaches the score
# ---------------------------------------------------------------------------
# Until now the only non-market input to conviction was the funding modifier. This is
# the second, and it is here because the defect it removes is structural rather than
# incidental: a low-float token and a fully-circulating one with the same market cap,
# the same turnover and the same relative strength scored identically, and they are not
# the same asset. One of them has its entire remaining supply still to issue.
#
# The gate already knew this. `_conjunctive_gate` refuses anything with FDV/MC above
# 2.0, which is a cliff at exactly the wrong place: 1.99 was waved through unpenalised
# and 2.01 was excluded outright, and nothing in between was expressed at all. A binary
# admission test is not a way of pricing a continuous risk.
#
# What is being priced: FDV/MC is the multiple by which supply expands if every
# scheduled token is issued. It is not a forecast of when. That distinction is why the
# penalty is capped well below the funding modifier's — dilution is a headwind with an
# unknown date, and an unknown date is not worth a large multiplier.
EMISSION_FREE_RATIO = 1.10      # at or below: effectively fully circulating, no drag
EMISSION_ANCHOR_RATIO = 3.0     # a 3x float expansion carries EMISSION_ANCHOR_SEVERITY
EMISSION_ANCHOR_SEVERITY = 0.75
EMISSION_MAX_PENALTY = 0.90     # the whole envelope: at most a 10% haircut, ever


def emission_drag(fdv, market_cap) -> float | None:
    """Supply overhang as a 0..1 severity, or None when it was not observed.

    ``None`` and ``0.0`` are different readings and must not collapse. A token whose FDV
    CoinGecko does not publish — which is every token with no fixed maximum supply, and
    a good number with one — has an *unknown* overhang, and scoring it as though it were
    fully circulating would hand the least transparent assets on the board the best
    supply grade. Unknown returns None here and a neutral 1.0 downstream, which is the
    only reading the data supports.

    The curve is tanh over the log of the expansion multiple, scaled from the two
    anchors above rather than fitted by hand, so moving an anchor moves the whole curve
    coherently. Log because the difference between 1.2x and 2.4x float expansion is the
    same *kind* of difference as between 4x and 8x, and a linear read makes a 40x
    vesting cliff forty times worse than a 1x one instead of asymptotically worse.
    """
    fdv = _num(fdv)
    mc = _num(market_cap)
    if fdv is None or mc is None or fdv <= 0 or mc <= 0:
        return None
    ratio = fdv / mc
    if ratio <= EMISSION_FREE_RATIO:
        return 0.0
    scale = (math.log(EMISSION_ANCHOR_RATIO / EMISSION_FREE_RATIO)
             / math.atanh(EMISSION_ANCHOR_SEVERITY))
    return round(math.tanh(math.log(ratio / EMISSION_FREE_RATIO) / scale), 6)


def emission_mult(fdv, market_cap) -> float:
    """The conviction multiplier the overhang earns. Neutral 1.0 when unobserved.

    Deliberately a separate function from :func:`emission_drag` rather than one that
    returns both, because this is the half that is captured in the specification hash
    and the half whose edit re-segments the track record. Keeping the severity curve and
    the envelope in one function would mean a change to either could not be told from a
    change to the other by reading the diff.
    """
    d = emission_drag(fdv, market_cap)
    if d is None:
        return 1.0
    return round(1.0 - (1.0 - EMISSION_MAX_PENALTY) * d, 4)


def score(t: dict, perps_map: dict | None = None,
          btc: dict | None = None) -> tuple[float, int, str, dict]:
    """Conviction score (0-100) with multi-timeframe relative strength.

    Momentum (Module C) now uses RELATIVE STRENGTH vs BTC across 7d/14d/30d/200d
    instead of 24h price change alone — 24h is mostly noise; a coin quietly
    outperforming BTC over 30-200d is the structural signal. Returns
    (era, total, sig, components) where components decomposes the score for
    attribution (liquidity/era/depth/momentum/rs_blend/perp_mult).
    """
    mc = t.get("market_cap") or 0
    vol = t.get("total_volume") or 0
    chg = t.get("price_change_percentage_24h") or 0.0
    turnover = (vol / mc) if mc else 0.0

    # --- Multi-timeframe relative strength vs BTC (7/14/30/200d; 90d is null
    # on the free tier, so it is intentionally excluded — no fabrication) ---
    def _pct(tf: int) -> float:
        return float(t.get(f"price_change_percentage_{tf}d_in_currency") or 0.0)
    def _btc(tf: int) -> float:
        return float((btc or {}).get(f"price_change_percentage_{tf}d_in_currency") or 0.0)
    rs = {tf: _pct(tf) - _btc(tf) for tf in (7, 14, 30, 200)}
    # Blended RS: weight recent Horizons but keep the long window loud enough
    # to surface quiet 60-200d outperformers. Volatility-adjustment deferred
    # (needs a return series the free tier doesn't provide).
    rs_blend = 0.30 * rs[7] + 0.25 * rs[14] + 0.25 * rs[30] + 0.20 * rs[200]

    # Module A (0-30): liquidity fit, soft curve, peak 30-60%
    if turnover <= 0:
        a = 0
    elif turnover <= 0.30:
        a = 10 + (turnover / 0.30) * 20
    elif turnover <= 0.60:
        a = 30 - abs(turnover - 0.45) / 0.15 * 6
    elif turnover <= 1.20:
        a = 20 - (turnover - 0.60) / 0.60 * 12
    else:
        a = max(2, 8 - (turnover - 1.20) * 4)

    # Module B (0-30): ERA proxy from 24h stability (Dune overrides if present)
    import math
    ag = 15 if abs(chg) < 5 else 10 if abs(chg) < 15 else 5
    era = 5.0 / ag
    b = 20 if era < 0.7 else 15 if era < 1.0 else 10 if era < 1.5 else 5 if era < 2.0 else 0

    # v2 MULTIPLICATIVE conviction = Quality x Confirmation x RiskAdjustment.
    # This replaces the additive model that saturated momentum at a hard clamp
    # (PUMP and HYPE collided at c_momentum=20). Composition:
    #   Q (Structural Quality)   = log-mcap depth, 0-1        (thin caps shrink the floor)
    #   C (Market Confirmation)  = soft sigmoid over RS_blnd, 0-1  (NO clamp -> rankable within tier)
    #   R (Risk Adjustment)      = mcap-aware liquidity, 0.4-1.0  (low turnover does NOT punish blue-chips)
    # A high-mcap asset (depth~1) with low turnover keeps R=1.0; only micro-caps /
    # low-depth names get the liquidity haircut. This is what pushes PUMP below HYPE/ADA.
    depth = max(0.0, min(1.0, (math.log10(mc) - 6) / 4.0)) if mc else 0
    cm = 0.10 + 0.90 * ((math.tanh(rs_blend / 25.0) + 1.0) / 2.0)  # confirmation, [0.10,0.91]
    # Risk: liquidity-fit fraction, but floored to 1.0 (no penalty) for established
    # depth (>=0.90 log-mcap) so blue-chip perps are not punished for low %.
    if depth >= 0.90:
        a_frac = 1.0
    else:
        if turnover <= 0:
            a_frac = 0.0
        elif turnover <= 0.30:
            a_frac = (10 + (turnover / 0.30) * 20) / 30.0
        elif turnover <= 0.60:
            a_frac = (30 - abs(turnover - 0.45) / 0.15 * 6) / 30.0
        elif turnover <= 1.20:
            a_frac = (20 - (turnover - 0.60) / 0.60 * 12) / 30.0
        else:
            a_frac = max(2.0, 8 - (turnover - 1.20) * 4) / 30.0
        a_frac = max(0.4, a_frac)  # never zero out a name; cap the haircut at 60%
    risk = a_frac
    # Module F. Applied unconditionally, unlike the perp overlay: supply overhang is a
    # property of the token's own schedule and does not depend on a derivatives feed
    # existing. Unobserved FDV returns exactly 1.0, so an asset is never marked down for
    # a disclosure CoinGecko does not publish.
    em = emission_mult(t.get("fully_diluted_valuation"), mc)
    risk *= em
    if perps_map is not None:
        risk *= lavl_perp_mult((t.get("symbol") or "").upper(), perps_map)

    total = max(0, min(100, int(round(100 * depth * cm * risk))))
    sig = "STRONG" if total >= 80 else "BUY" if total >= 70 else "HOLD" if total >= 55 \
        else "WATCH" if total >= 40 else "AVOID"
    comp = {
        "liquidity": round(a_frac * 30, 1), "era": round(b, 1),
        "depth": round(depth * 20, 1), "momentum": round(cm * 20, 1),  # confirmation, display-scaled
        "risk_adjustment": round(risk, 3), "rs_blend": round(rs_blend, 2),
        "rs7": round(rs[7], 2), "rs14": round(rs[14], 2),
        "rs30": round(rs[30], 2), "rs200": round(rs[200], 2),
        "perp_mult": round(lavl_perp_mult((t.get("symbol") or "").upper(),
                                           perps_map or {}), 3),
        # Published beside the multiplier it produced. A 0.94 on screen cannot
        # distinguish "a 2.6x float expansion" from "the feed was absent", and those are
        # opposite facts about a token's supply.
        "emission_mult": em,
        "emission_drag": emission_drag(t.get("fully_diluted_valuation"), mc),
    }
    return era, total, sig, comp


def _lavl_regime(t: dict) -> str:
    """Lightweight LAVL regime (mirrors lavl.py) for the conjunctive gate.

    Uses only free-payload fields: 24h change, 24h range, vol/mc, range-tightness.
    Perp multiplier is neutral (1.0) until a derivatives feed is wired.
    """
    price = t.get("current_price") or 0
    chg = t.get("price_change_percentage_24h") or 0.0
    high = t.get("high_24h") or price or 1
    low = t.get("low_24h") or price or 1
    vol = t.get("total_volume") or 0
    mc = t.get("market_cap") or 0
    if price <= 0 or mc <= 0:
        return "COMPRESS"
    spread = (high - low) / price if price else 1
    if spread <= 0:
        spread = 0.01
    depth = vol / mc if mc else 0
    if depth <= 0:
        depth = 1e-6
    velo = (abs(chg) / 100.0 / spread) * math.log(max(2.0, depth * 1e6)) if spread else 0
    velo = min(4.0, velo)
    range_tight = 1 - (high - low) / high if high else 0
    diverge = (vol / mc) * max(0.0, range_tight)
    diverge = min(2.3, diverge)
    lavl = 0.6 * velo + 0.4 * diverge  # risk_mult neutral (1.0)
    if lavl > 2.5:
        return "ALPHA RUSH"
    if lavl >= 0.5:
        return "STABLE"
    return "COMPRESS"


def _conjunctive_gate(t: dict, conv: int) -> bool:
    """Replicates the front-end gated flag so the basket uses the same universe."""
    mc = t.get("market_cap") or 0
    vol = t.get("total_volume") or 0
    turnover = (vol / mc) if mc else 0.0
    fdv = t.get("fully_diluted_valuation") or 0
    dilution = (fdv / mc) if mc else None
    gate_a = 0.30 <= turnover <= 0.60
    gate_b = (dilution is None) or (dilution <= 2.0)
    band = _lavl_regime(t)
    gate_c = band not in ("COMPRESS", "LIQ TRAP") and turnover < 0.90
    return bool(gate_a and gate_b and gate_c)


def fetch_long_short(perps_map: dict, symbols: set[str] | None = None,
                     limit: int = 60) -> int:
    """Binance's global long/short account ratio, merged into `perps_map` in place.

    Keyless and public, but one request per symbol, so it is bounded to the symbols
    actually being scored and capped. Anything not fetched keeps a null ratio rather
    than a neutral 1.0 — "half the accounts are long" is a real reading and must not be
    manufactured by a failed request.

    Returns how many symbols were enriched, so the caller can log coverage instead of
    reporting a silent partial.
    """
    if not symbols:
        return 0
    got = 0
    for base in sorted(symbols)[:limit]:
        try:
            data = _get_json(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
                f"?symbol={base}USDT&period=1d&limit=1")
            row = (data or [{}])[0]
            ratio = float(row.get("longShortRatio"))
        except Exception:  # noqa: BLE001
            continue      # per-asset null; never a fabricated neutral
        perps_map.setdefault(base, {})["long_short_ratio"] = round(ratio, 4)
        got += 1
    return got


def lavl_perp_mult(ticker: str, perps_map: dict) -> float:
    """LAVL leverage-micro-regime multiplier (RiskMult_perp).

    Delegates to :func:`funding.regime_modifier`, which classifies the annualised carry
    and requires a confirming input before it adjusts anything. See that function for
    the thresholds and for why each adjustment is conditional.

    **This function's text is hashed into the specification.** The version it replaces
    read the raw per-interval rate directly:

        funding > +0.0005 per interval  ->  0.85     (54.75% APR at an 8h clock)
        funding < 0                     ->  1.15     (any inversion at all)

    Three things were wrong with that, and all three are why this edit is worth a
    specification boundary rather than being deferred:

    1. It compared a *rate* against a threshold without knowing the *interval*. The
       constant 0.0005 encodes an 8-hour settlement clock that was never stated. Point
       the same code at Hyperliquid, which settles hourly, and the penalty triggers at
       roughly 8% APR instead of 55% — the threshold silently means something different
       per venue.
    2. Both adjustments fired on funding alone. Positive carry says longs are paying for
       leverage; it does not say the move is extended, and an orderly funded market was
       being charged a crowding penalty. Symmetrically, any negative print at all earned
       a 15% boost, including assets where shorts are paying because they are right.
    3. The boost was a step. Funding at -0.1% and at -8% earned the identical 1.15.

    The replacement keeps the same shape — a multiplier on the risk term, base maths
    untouched — and gates each adjustment on the confirmation the original assumed.

    Reads ``funding_apr``, ``price_chg_24h`` and ``rsi7`` from the perps map. When only
    a raw rate is present the APR is derived at the venue's interval, defaulting to the
    8-hour clock the previous version assumed, so a caller passing the old shape gets a
    defined reading rather than a crash.
    """
    info = perps_map.get(ticker)
    if not info:
        return 1.0
    apr = info.get("funding_apr")
    if apr is None:
        apr = funding.annualize(info.get("funding_rate"),
                                info.get("interval_hours") or 8.0)
    mult, _reason = funding.regime_modifier(apr, info.get("price_chg_24h"),
                                            info.get("rsi7"))
    return mult

# The legacy annualisation constant, for the Bybit feed only: that venue quotes per
# 8-hour interval, so three settlements a day. It is NOT a general constant — Hyperliquid
# settles hourly and some Binance symbols every four hours, and applying this to either
# understates the carry. Cross-venue rates go through funding.annualize, which takes the
# interval as an argument because the interval is part of the unit.
FUNDING_INTERVALS_PER_YEAR = 3 * 365
# Below this a "divergence" is rounding, on either axis.
DIVERGENCE_EPS_PRICE = 0.5      # percent
DIVERGENCE_EPS_OI = 1.0         # percent


def funding_ann_pct(funding_rate) -> float | None:
    """Funding as an annualised percentage carry.

    The raw 8-hour rate is unreadable at a glance — 0.0005 and 0.01 both look like
    small numbers and mean 55% and 1,095% a year. Annualising is the only form in which
    the figure can be compared against anything else on the board.
    """
    if funding_rate is None:
        return None
    try:
        return round(float(funding_rate) * FUNDING_INTERVALS_PER_YEAR * 100.0, 4)
    except (TypeError, ValueError):
        return None


def oi_price_divergence(price_chg_pct, oi_chg_pct) -> str | None:
    """Where open interest went while price went somewhere.

    The four states are the standard reading of positioning against direction:

      price up,   OI up    ACCUMULATION   new money backing the move
      price up,   OI down  SHORT_SQUEEZE  the rally is shorts closing, not buyers
      price down, OI up    SHORT_BUILD    new money positioned against it
      price down, OI down  LONG_FLUSH     leverage being unwound, not distribution

    None when either leg is missing or too small to be a direction. Guessing a quadrant
    from a 0.1% drift would produce a confident badge on noise, and a badge is read as
    a claim.
    """
    if price_chg_pct is None or oi_chg_pct is None:
        return None
    try:
        p, o = float(price_chg_pct), float(oi_chg_pct)
    except (TypeError, ValueError):
        return None
    if abs(p) < DIVERGENCE_EPS_PRICE or abs(o) < DIVERGENCE_EPS_OI:
        return "FLAT"
    if p > 0:
        return "ACCUMULATION" if o > 0 else "SHORT_SQUEEZE"
    return "SHORT_BUILD" if o > 0 else "LONG_FLUSH"


def _prev_oi_by_symbol() -> dict:
    """Yesterday's recorded open interest, for the 24h delta.

    Read from the ledger rather than fetched, because Bybit's tickers endpoint reports
    a point-in-time value with no history. On the first night after this column lands
    there is nothing to compare against and the delta is null — which is the honest
    reading, and specifically not zero, since zero is a claim that OI did not move.
    """
    rows = _read_signals_rows()
    if not rows:
        return {}
    dates = sorted({r.get("date") for r in rows if r.get("date")})
    if len(dates) < 2:
        return {}
    prev = dates[-1]
    out = {}
    for r in rows:
        if r.get("date") != prev:
            continue
        v = _num(r.get("oi_usd"))
        if v:
            out[(r.get("symbol") or "").upper()] = v
    return out


def _rsi_by_symbol(live_prices: dict | None = None, period: int = 7) -> dict:
    """7-period RSI per symbol, from recorded closes plus tonight's live price.

    There is no free daily-close series for this universe, so the ledger is the series —
    one close per symbol per night, accumulated since the first run. Tonight's price is
    appended before computing because the reading that gates the squeeze boost has to
    include today; an RSI as of yesterday would let an asset that reversed hard this
    afternoon still collect the boost.

    Symbols with fewer than ``period + 1`` recorded closes get None, not a partial
    figure. That is a real constraint with a real consequence and it is worth stating
    plainly: the short-squeeze boost cannot fire for a symbol until it has eight nights
    of history, and a symbol that drops off the board and returns has a gap that this
    function does not interpolate across — the closes are taken in date order and a
    missing night simply is not there. Both cases fail closed, to the neutral 1.0.
    """
    rows = _read_signals_rows()
    if not rows:
        return {}
    series: dict = {}
    for r in sorted(rows, key=lambda x: (x.get("date") or "")):
        sym = (r.get("symbol") or "").upper()
        px = _num(r.get("price"))
        if sym and px:
            series.setdefault(sym, []).append(px)
    out = {}
    for sym, closes in series.items():
        live = (live_prices or {}).get(sym)
        full = closes + [live] if live else closes
        out[sym] = funding.rsi(full, period)
    return out


FUNDING_TRAIL_NIGHTS = 7


def _funding_trail_by_symbol(nights: int = FUNDING_TRAIL_NIGHTS) -> dict:
    """Trailing funding per symbol: mean APR, nights covered, share of them positive.

    Reads ``funding_apr``, falling back to the retired ``funding_ann_pct`` for rows
    written before it existed. That fallback is correct rather than approximate: every
    such row came from Bybit's 8-hour clock, which is exactly the basis the old column
    assumed, so over that history the two are arithmetically the same number. Without it
    this reading would start from one night and discard the seventeen already on disk.

    ``pos_share`` is the reading that matters for a carry: a 30% mean built from one
    +200% night and six flat ones is not a 30% carry, and the mean alone cannot tell
    those apart.

    Returns per symbol ``{"mean": float|None, "n": int, "pos_share": float|None}``.
    None rather than 0.0 for an unrecorded mean — a symbol with no funding history has
    no trailing carry, and 0.0 would claim it was measured and found flat.
    """
    rows = _read_signals_rows()
    if not rows:
        return {}
    dates = sorted({r.get("date") for r in rows if r.get("date")})[-nights:]
    keep = set(dates)
    series: dict = {}
    for r in rows:
        if r.get("date") not in keep:
            continue
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        v = _num(r.get("funding_apr"))
        if v is None:
            v = _num(r.get("funding_ann_pct"))
        if v is not None:
            series.setdefault(sym, []).append(v)
    out = {}
    for sym, vals in series.items():
        out[sym] = {
            "mean": round(sum(vals) / len(vals), 4),
            "n": len(vals),
            "pos_share": round(sum(1 for v in vals if v > 0) / len(vals), 3),
        }
    return out


def perp_context(ticker: str, perps_map: dict, market_cap, price_chg_pct,
                 prev_oi: dict) -> dict:
    """The recorded derivatives columns for one asset. Observational throughout."""
    info = perps_map.get(ticker) or {}
    fr = info.get("funding_rate")
    oi = info.get("oi_usd")
    p0 = prev_oi.get(ticker)
    oi_chg = round(100.0 * (oi / p0 - 1.0), 4) if (oi and p0) else None
    return {
        "funding_rate": fr,
        # Retired, not removed: FIELDS is append-only, so the column stays for the
        # rows already written under it. Nothing new is written into it. It annualised
        # at a fixed three settlements a day, which was right for the single Bybit feed
        # that produced it and is wrong for every hourly venue — and on 2026-08-17 every
        # rate in production came from an hourly venue. `funding_apr` beside
        # `funding_interval_h` supersedes it and cannot carry the same ambiguity.
        "funding_ann_pct": None,
        "oi_usd": oi,
        "oi_chg_24h_pct": oi_chg,
        # Leverage relative to the size of the asset. A $1bn book is enormous on a
        # $2bn token and unremarkable on a $200bn one.
        "oi_to_mcap": round(oi / market_cap, 6) if (oi and market_cap) else None,
        "long_short_ratio": info.get("long_short_ratio"),
        "oi_price_divergence": oi_price_divergence(price_chg_pct, oi_chg),
    }


# The credential main() resolved, for the two call sites buried inside build_basket and
# _write_index_row. Threading a session argument down through both would change three
# public signatures that the test suite and the validator already call positionally, to
# carry one optional header — so it is a module global that main() sets once and that
# stays None everywhere else. None means keyless, which is exactly what those functions
# did before this existed.
CG_SESSION: dict | None = None


def fetch_global_market_cap(session: dict | None = None) -> float | None:
    """Total crypto market cap (USD) from CoinGecko's free /global endpoint.

    Used as the apples-to-apples macro benchmark: benchmark_total_return is
    current_global / entry_global, computed over the same window as the basket.
    Returns None on failure so the benchmark falls back to neutral (no fabrication).
    """
    try:
        sess = session if session is not None else CG_SESSION
        host = (sess or {}).get("host") or CG_BASE
        data = _get_json(f"{host}/global",
                         {"User-Agent": "conviction-monitor/1.0",
                          **((sess or {}).get("headers") or {})})
        return float(data.get("data", {}).get("total_market_cap", {}).get("usd", 0) or 0) or None
    except Exception as e:  # noqa: BLE001
        print(f"[global] fetch failed: {e}", file=__import__("sys").stderr)
        return None


def _risk_stats(daily_returns: list[float]) -> dict | None:
    """Risk statistics from a series of OVERNIGHT returns.

    Takes the returns explicitly rather than reading index.csv, because that file's
    return columns are cumulative since the basket's cost basis, not daily. Feeding them
    to a Sharpe ratio computes the dispersion of a running total, which rises with the
    length of the series rather than with the volatility of anything. Gated at 30
    observations, so it had never fired — a landmine rather than a live wrong number.

    Sharpe: rf=0, mean(daily) / std(daily) * sqrt(365).
    Max drawdown: largest peak-to-trough decline of the compounded curve.
    """
    if len(daily_returns) < 30:
        return None
    rets = list(daily_returns)
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    std = math.sqrt(var) if var > 0 else 0.0
    sharpe = (mean / std) * math.sqrt(365) if std > 0 else 0.0
    cum = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in rets:
        cum *= (1 + r)
        peak = max(peak, cum)
        if peak > 0:
            max_dd = min(max_dd, (cum - peak) / peak)
    return {
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "n_days": len(rets),
        "convention": "rf=0; annualized daily x sqrt(365)",
    }


# A tier change caused by a move no larger than this is the label crossing a threshold,
# not a change of view. Matched to the equity terminal, where the same diagnostic
# measured 47 of 73 overnight tier changes falling inside it — a "new BUY" list built
# without the distinction is wrong in detail most mornings.
MARGINAL_MOVE = 2.0

# The same cuts _score() applies. Used only to fill a tier for a historical row that
# predates the signal column; a row that recorded its own signal keeps it, so an old
# night is never silently reinterpreted under today's thresholds.
TIER_CUTS = ((80, "STRONG"), (70, "BUY"), (55, "HOLD"), (40, "WATCH"), (0, "AVOID"))


def _tier_for(conviction: float) -> str:
    for cut, name in TIER_CUTS:
        if conviction >= cut:
            return name
    return "AVOID"


def _compute_tier_diff() -> dict:
    """Overnight tier transitions, separating real reclassifications from boundary noise.

    This is a diff, not a rule. Nothing here feeds back into scoring. The alternative
    fix for boundary churn is hysteresis — refusing to change a tier until a name moves
    far enough past the threshold — and that puts a memory of yesterday inside tonight's
    score, so a name's tier would depend on the path it took to reach its conviction
    rather than on the conviction itself. The score stays a pure function of tonight's
    inputs; the presentation carries the caveat.

    Derived entirely from the signals ledger. No new feeds.
    """
    if not LEDGER_CSV.exists():
        return {}
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    by_date: dict[str, dict[str, dict]] = {}
    for r in rows:
        d, sym = r.get("date"), r.get("symbol")
        if not d or not sym:
            continue
        try:
            conv = float(r.get("conviction") or 0)
        except ValueError:
            continue
        by_date.setdefault(d, {})[sym] = {
            "conviction": conv,
            "tier": r.get("signal") or _tier_for(conv),
            "name": r.get("name") or sym,
        }

    dates = sorted(by_date)
    if len(dates) < 2:
        return {"from": None, "to": dates[-1] if dates else None, "pending": True,
                "marginal_move": MARGINAL_MOVE, "changed": [], "marginal": [],
                "counts": {}}

    prev_d, curr_d = dates[-2], dates[-1]
    prev, curr = by_date[prev_d], by_date[curr_d]

    changed, marginal = [], []
    for sym, now in curr.items():
        was = prev.get(sym)
        if not was or was["tier"] == now["tier"]:
            continue
        delta = now["conviction"] - was["conviction"]
        entry = {
            "symbol": sym, "name": now["name"],
            "from": round(was["conviction"], 1), "to": round(now["conviction"], 1),
            "delta": round(delta, 1),
            "from_tier": was["tier"], "to_tier": now["tier"],
            "marginal": abs(delta) <= MARGINAL_MOVE,
        }
        (marginal if entry["marginal"] else changed).append(entry)

    changed.sort(key=lambda e: -abs(e["delta"]))
    marginal.sort(key=lambda e: -abs(e["delta"]))
    total = len(changed) + len(marginal)
    return {
        "from": prev_d, "to": curr_d, "pending": False,
        "marginal_move": MARGINAL_MOVE,
        "names_compared": len(set(prev) & set(curr)),
        "changed": changed,
        "marginal": marginal,
        "counts": {
            "tier_changes": total,
            "real": len(changed),
            "marginal": len(marginal),
            "marginal_share": round(len(marginal) / total, 3) if total else None,
        },
    }


# Snapshot days required before the paper curve is drawn. Below this it would be one or
# two segments, a shape the eye reads as a trend and which is nothing of the kind.
PERF_MIN_DAYS = 5

# Basket size and weighting mirror build_basket(): top 10 by conviction, weighted in
# proportion to it. The hysteresis buffer is deliberately *not* replicated — this curve
# answers "what did the score say to hold", and reconstructing the ejection rules from a
# ledger that never recorded which names were actually held would be a guess dressed as
# a record.
PERF_TOP_N = 10

# A leg that loses more than this share of the book to unpriceable or departed names is
# not a return, it is a data outage wearing one.
PERF_MAX_WEIGHT_LOSS = 0.10

# The benchmark. Present on every recorded day, and the closest crypto analogue to the
# index the equity terminal measures against.
PERF_BENCHMARK = "BTC"

# ---------------------------------------------------------------------------
# Selection edge
# ---------------------------------------------------------------------------
# Does conviction predict the next day's return? This is the only question that decides
# whether the score is worth acting on, and it is a different question from "is the
# basket beating the benchmark".
#
# The distinction matters because the basket IS losing to equal weight — about -283bp
# over the six legs since the 2026-08-05 boundary — and the obvious reading of that is
# "the selection is subtracting value". The measurement does not support that reading.
# The information coefficient over the same legs is +0.006 with a 95% interval of
# roughly [-0.09, +0.10]: indistinguishable from zero in either direction. A
# concentrated book with no measurable edge underperforms an equal-weight control as a
# matter of course, because concentration adds variance without adding expected return.
# That is the honest description of the current state, and it is neither "the model
# works" nor "the model is broken".
#
# So this panel leads with the interval and the sample size, not with a ranked list of
# which names hurt. Ranking six observations by contribution produces a confident-looking
# table of noise, and acting on it is how a model gets fitted to its own sampling error.
# The per-name accounting is computed and shown, clearly labelled as arithmetic rather
# than evidence.
EDGE_MIN_NAMES = 10        # below this a rank correlation is not worth computing
EDGE_QUINTILE = 5
# Targets used to state how much history is still needed. 0.03 is a respectable
# cross-sectional signal; 0.05 would be a strong one.
EDGE_TARGET_ICS = (0.02, 0.03, 0.05)
# Legs needed before the mean IC is worth reading at all. Chosen so the standard error
# of the mean is at most about half a plausible true signal.
EDGE_MIN_LEGS = 40


def _edge_legs(by_date: dict, boundary: str | None) -> list[dict]:
    """Per-leg information coefficient and quintile spread, after the boundary.

    Legs before a specification change are excluded rather than blended: an IC averaged
    across two different scoring functions is a number about a model that never existed.
    """
    out = []
    dates = sorted(by_date)
    for a, b in zip(dates, dates[1:]):
        if boundary and a < boundary:
            continue
        prev, curr = by_date[a], by_date[b]
        pairs = []
        for sym, row in prev.items():
            nxt = curr.get(sym)
            conv, p0 = _mon_float(row, "conviction"), _mon_float(row, "price")
            p1 = _mon_float(nxt, "price") if nxt else None
            if conv is None or not p0 or not p1:
                continue
            pairs.append((conv, p1 / p0 - 1.0))
        if len(pairs) < EDGE_MIN_NAMES:
            continue
        rho = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
        ranked = sorted(pairs, key=lambda p: -p[0])
        k = max(3, len(ranked) // EDGE_QUINTILE)
        top = sum(p[1] for p in ranked[:k]) / k
        bot = sum(p[1] for p in ranked[-k:]) / k
        out.append({"from": a, "to": b, "ic": rho, "names": len(pairs),
                    "top_quintile": round(top * 100, 4),
                    "bottom_quintile": round(bot * 100, 4),
                    "spread_bp": round((top - bot) * 1e4, 1)})
    return out


_ATTRIB_BASIS = (
    "Arithmetic, not evidence. Contribution = active weight x (return - equal-weight "
    "return) per leg, linked across legs by the Carino method so the parts sum to the "
    "chained gap exactly rather than approximately. An unlinked sum does not: it is a "
    "sum of arithmetic pieces set against a difference of geometric wholes, and the "
    "error grows with the number of legs. Over this many legs the ordering is still "
    "mostly sampling noise, so it is a description of what happened and not a list of "
    "names to act on. An overweight name that fell is a selection error; an underweight "
    "name that rose is an omission."
)


def _carino_k(p: float, b: float) -> float:
    """Carino's per-period linking coefficient.

    k = [ln(1+p) - ln(1+b)] / (p - b), which is the slope of ln(1+x) averaged over the
    interval between the two returns. As p approaches b the expression is 0/0 and the
    limit is the derivative at that point, 1/(1+p). Computed by the limit rather than by
    a nudge, because a leg where the book exactly matched the benchmark is an ordinary
    outcome, not a degenerate one, and an epsilon there would put a small fabricated
    number into every contribution on that day.
    """
    if p <= -1.0 or b <= -1.0:
        # Total loss. ln(0) is undefined and no linking coefficient exists; the caller
        # reports the leg rather than substituting a number for it.
        raise ValueError(f"return of -100% or worse cannot be log-linked (p={p}, b={b})")
    if abs(p - b) < 1e-12:
        return 1.0 / (1.0 + p)
    return (math.log1p(p) - math.log1p(b)) / (p - b)


def _active_contributions(usable_legs: list, limit: int = 8) -> dict:
    """Where the basket-minus-equal-weight gap came from, name by name.

    **Carino-linked, and this is the correction that matters.** The previous version
    summed single-period active contributions arithmetically and claimed to reconcile to
    the realised gap "by construction". That claim is true for one leg and false for
    every leg after it: the gap it reconciles to is chained geometrically, and a sum of
    arithmetic parts does not equal a difference of geometric wholes. On twenty legs the
    residual had reached 312.9bp against a gap of -1266.0bp, 24.7% of the number it was
    said to explain, while the panel went on calling itself exact.

    The arithmetic, per leg t:

        p_t = book return, b_t = equal-weight return, a_t = p_t - b_t
        c_i,t = active_i,t x (r_i,t - b_t),  and  sum_i c_i,t = a_t exactly
        k_t   = [ln(1+p_t) - ln(1+b_t)] / (p_t - b_t)
        K     = [ln(1+P)   - ln(1+B)]   / (P - B)          over the chained totals
        C_i   = sum_t c_i,t x k_t / K

    Then sum_i C_i = sum_t a_t k_t / K = [ln(1+P) - ln(1+B)] / K = P - B, exactly, which
    is what "reconciles" has to mean if the word is going to appear beside the number.

    `active_i,t` is the leg's own weight arithmetic, not a re-derivation of it: the book
    weight is w_i / kept over the names that stayed priceable, and the benchmark weight
    is 1/|shared| over the names present on both nights. Those two sets are not the same
    set, and the difference is exactly why this is computed from the leg rather than
    from the dates the leg covers.

    Still arithmetic, not evidence. Over this many legs the ordering is dominated by
    sampling noise, and treating it as a list of names to drop is how a model gets
    fitted to its own error. Split by stance because the two mistakes differ: an
    overweight name that fell is a selection error, an underweight name that rose is an
    omission, and they have different remedies.
    """
    # A leg with no equal-weight reading has no active return to decompose. It is
    # excluded here AND reported, because the performance curve chains `book` through
    # such a leg while `equal_weight` skips it, so a silent exclusion would leave the
    # attribution reconciling to a gap built over a different set of days.
    legs = [l for l in usable_legs if l.get("equal_weight") is not None]
    skipped = len(usable_legs) - len(legs)

    per_leg = []          # [{k, contrib:{sym: c}}]
    held: dict[str, float] = {}
    book_chain = eq_chain = 1.0
    for l in legs:
        p_t, b_t = l["book"], l["equal_weight"]
        weights, kept, shared = l["weights"], l["kept"], l["shared"]
        prev, curr = l["_prev"], l["_curr"]
        rets = {}
        for sym in set(list(weights) + list(shared)):
            p0 = (prev.get(sym) or {}).get("price")
            p1 = (curr.get(sym) or {}).get("price")
            if p0 and p1:
                rets[sym] = p1 / p0 - 1.0
        n_shared = len(shared) or 1
        contrib = {}
        for sym, r in rets.items():
            # The book's weight on this name, renormalised over what stayed priceable,
            # minus the benchmark's. Zero on either side where the name is absent there.
            wb = (weights.get(sym, 0.0) / kept) if (sym in weights and kept) else 0.0
            wq = (1.0 / n_shared) if sym in shared else 0.0
            active = wb - wq
            if active == 0.0:
                continue
            contrib[sym] = active * (r - b_t)
            held[sym] = held.get(sym, 0.0) + active
        per_leg.append({"k": _carino_k(p_t, b_t), "contrib": contrib,
                        "a": p_t - b_t, "from": l["from"], "to": l["to"]})
        book_chain *= (1.0 + p_t)
        eq_chain *= (1.0 + b_t)

    P, B = book_chain - 1.0, eq_chain - 1.0
    if not per_leg:
        return {"legs": 0, "total_bp": 0.0, "detractors": [], "contributors": [],
                "linking": "carino", "reconciles_to_bp": 0.0, "residual_bp": 0.0,
                "legs_without_benchmark": skipped,
                "basis": _ATTRIB_BASIS}
    K = _carino_k(P, B)

    contrib: dict[str, float] = {}
    for leg in per_leg:
        scale = leg["k"] / K
        for sym, c in leg["contrib"].items():
            contrib[sym] = contrib.get(sym, 0.0) + c * scale

    total = sum(contrib.values())
    target = P - B
    ranked = sorted(contrib.items(), key=lambda kv: kv[1])

    def row(sym, v):
        return {"symbol": sym, "bp": round(v * 1e4, 1),
                "stance": "overweight" if held.get(sym, 0.0) > 0 else "underweight"}

    return {
        "legs": len(per_leg),
        "legs_without_benchmark": skipped,
        "linking": "carino",
        "total_bp": round(total * 1e4, 1),
        # The gap this decomposition is a decomposition OF, carried beside the sum so
        # the two can be compared without recomputing either.
        "reconciles_to_bp": round(target * 1e4, 1),
        "residual_bp": round((total - target) * 1e4, 6),
        "detractors": [row(s, v) for s, v in ranked[:limit]],
        "contributors": [row(s, v) for s, v in ranked[-limit:][::-1]],
        "basis": _ATTRIB_BASIS,
    }


def _compute_edge() -> dict:
    """Whether the conviction score has demonstrable predictive power yet.

    Reports the interval, not just the point estimate. A mean IC quoted alone invites
    the reader to treat +0.006 as "slightly positive" when the honest statement is
    "cannot be distinguished from nothing with the data on hand".
    """
    by_date, _ = _perf_by_date()
    perf = _compute_performance()
    boundary = perf.get("spec_boundary")
    legs = _edge_legs(by_date, boundary)
    # The attribution decomposes the SAME legs the performance curve chains, handed over
    # rather than rebuilt from the dates: rebuilding is what let the two drift apart
    # under different filters while both reported twenty legs.
    _, usable_legs, _, _ = _perf_legs(by_date, sorted(by_date))
    attribution = _active_contributions(usable_legs)
    ics = [l["ic"] for l in legs if l["ic"] is not None]
    spreads = [l["spread_bp"] for l in legs]
    base = {"legs": len(legs), "min_legs": EDGE_MIN_LEGS, "boundary": boundary,
            "spec_hash": SPEC_HASH, "series": legs, "attribution": attribution,
            # The realised gap the attribution reconciles to. Carried here so the panel
            # can state the underperformance and the null result side by side, which is
            # the pairing that stops either being misread on its own.
            "book_total": perf.get("book_total"),
            "equal_weight_total": perf.get("equal_weight_total"),
            "benchmark_total": perf.get("benchmark_total"),
            "basis": ("Information coefficient = rank correlation between tonight's "
                      "conviction and tomorrow's return, across the assets scored on "
                      "both nights. It answers whether the ordering is informative, "
                      "which is a different question from whether the basket beat the "
                      "benchmark — a concentrated book with no edge underperforms an "
                      "equal-weight control as a matter of course.")}
    if len(ics) < 2:
        return {**base, "measurable": False, "mean_ic": None, "ci": None,
                "verdict": "Not enough legs to measure anything."}

    mean = sum(ics) / len(ics)
    var = sum((i - mean) ** 2 for i in ics) / (len(ics) - 1)
    se = (var / len(ics)) ** 0.5
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    measurable = len(ics) >= EDGE_MIN_LEGS and (lo > 0 or hi < 0)
    # Per-leg IC noise is about 1/sqrt(n-3); the legs needed to resolve a given true
    # signal follows from wanting a standard error of half that signal.
    nbar = sum(l["names"] for l in legs) / len(legs)
    per_leg = 1.0 / max(1.0, (nbar - 3)) ** 0.5
    needed = {f"{t:.2f}": int(round((per_leg / (t / 2.0)) ** 2)) for t in EDGE_TARGET_ICS}
    return {
        **base,
        "measurable": measurable,
        "mean_ic": round(mean, 4),
        "median_ic": round(sorted(ics)[len(ics) // 2], 4),
        "ic_sd": round(var ** 0.5, 4),
        "ic_se": round(se, 4),
        "ci": [round(lo, 4), round(hi, 4)],
        "t_stat": round(mean / se, 3) if se else None,
        "legs_positive": sum(1 for i in ics if i > 0),
        "mean_spread_bp": round(sum(spreads) / len(spreads), 1) if spreads else None,
        "spreads_positive": sum(1 for s in spreads if s > 0),
        "per_leg_noise": round(per_leg, 3),
        "legs_needed": needed,
        "verdict": (
            "Conviction orders the universe informatively."
            if measurable and mean > 0 else
            "Conviction orders the universe backwards — the ranking is inverted."
            if measurable else
            "No measurable relationship between conviction and next-day return. The "
            "interval spans zero, so this is neither evidence the score works nor "
            "evidence it does not — there is simply not enough history yet."),
    }


def _perf_by_date() -> tuple[dict, int]:
    """signals.csv as {date: {symbol: row}}, latest run per (date, symbol) winning.

    The ledger has been appended to more than once on some days — 2026-08-02 carries
    nine runs — so a naive read counts one day's board nine times and computes returns
    between a day and itself. Collapsing on (date, symbol) is the only reading that
    makes a daily series daily; the count of collapsed rows is reported rather than
    swallowed, because a rising number means the workflow is firing more than once.
    """
    if not LEDGER_CSV.exists():
        return {}, 0
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    by_date: dict[str, dict[str, dict]] = {}
    collapsed = 0
    for r in rows:
        d, sym = r.get("date"), (r.get("symbol") or "").upper()
        if not d or not sym:
            continue
        try:
            price = float(r.get("price") or 0)
            conv = float(r.get("conviction") or 0)
        except ValueError:
            continue
        if price <= 0:
            continue
        day = by_date.setdefault(d, {})
        if sym in day:
            collapsed += 1
        day[sym] = {"price": price, "conviction": conv}
    return by_date, collapsed


def _perf_weights(day: dict) -> dict:
    """Conviction-weighted top N, as build_basket() would have published it."""
    ranked = sorted(day.items(), key=lambda kv: -kv[1]["conviction"])[:PERF_TOP_N]
    total = sum(v["conviction"] for _, v in ranked) or 1.0
    return {sym: v["conviction"] / total for sym, v in ranked if v["conviction"] > 0}


def _perf_legs(by_date: dict, dates: list) -> tuple:
    """The legs the performance curve is chained from. One definition, two consumers.

    Extracted because the attribution used to build its own leg set from the same dates
    under *different* filters: the curve dropped a leg that lost more than
    PERF_MAX_WEIGHT_LOSS of its book, the attribution did not, and the attribution
    applied an EDGE_MIN_NAMES floor the curve did not. On the ledger as it stands both
    happen to land on twenty legs, which is a coincidence and not a coupling. An
    attribution that reconciles to a curve built from a different set of days is not an
    attribution of that curve, and nothing in the output would have said so.

    Returns ``(all_legs, usable_after_boundary, boundary, dropped_pre_break)``.
    """
    legs = []
    for a, b in zip(dates, dates[1:]):
        prev, curr = by_date[a], by_date[b]
        weights = _perf_weights(prev)
        num = held = missing = 0.0
        names = 0
        for sym, w in weights.items():
            held += w
            p1 = (curr.get(sym) or {}).get("price")
            if not p1:
                missing += w      # left the universe, or unpriceable tonight
                continue
            num += w * (p1 / prev[sym]["price"] - 1.0)
            names += 1
        kept = held - missing
        if held <= 0 or kept <= 0:
            continue

        shared = [s for s in prev if s in curr]
        eq = (sum(curr[s]["price"] / prev[s]["price"] - 1.0 for s in shared) / len(shared)
              if shared else None)
        bp0 = (prev.get(PERF_BENCHMARK) or {}).get("price")
        bp1 = (curr.get(PERF_BENCHMARK) or {}).get("price")
        legs.append({
            "from": a, "to": b,
            "book": num / kept,
            "benchmark": (bp1 / bp0 - 1.0) if (bp0 and bp1) else None,
            "equal_weight": eq,
            "names": names,
            "weight_lost": missing / held,
            "usable": (missing / held) <= PERF_MAX_WEIGHT_LOSS,
            # Carried so the attribution can reproduce this leg's arithmetic exactly
            # rather than approximating it from the same dates. Underscored keys are
            # in-memory only: nothing here is serialised into the ledger.
            "kept": kept, "weights": weights, "shared": shared,
            "_prev": prev, "_curr": curr,
        })

    usable = [l for l in legs if l["usable"]]

    # Start after the most recent specification boundary. A leg that straddles one
    # chains a book chosen by one model onto returns scored by another, and averaging
    # across that is not a track record for either, it is a number about a model that
    # never existed. The legs before the boundary are still real measurements of the
    # model that produced them; they are excluded from *this* curve, not deleted, and
    # the count is reported so the exclusion is visible rather than implied.
    breaks = sorted(b["to"] for b in _spec_breaks())
    boundary = breaks[-1] if breaks else None
    dropped_pre_break = 0
    if boundary:
        dropped_pre_break = len([l for l in usable if l["from"] < boundary])
        usable = [l for l in usable if l["from"] >= boundary]
    return legs, usable, boundary, dropped_pre_break


def _compute_performance() -> dict:
    """Paper return of the published basket, chained across recorded days.

    The same three rules as the equity terminal, for the same reasons:

    * **Weights from the earlier night, prices from both.** Using tonight's weights
      against tonight's prices prints alpha every day forever and looks entirely
      plausible doing it.
    * **Only recorded dates.** No back-fill. A day that was not recorded is gone.
    * **A missing benchmark is a gap, not a flat segment.** Flat reads as "the market
      did not move" where the truth is "it was not recorded".

    Deliberately NOT built on ledger/index.json. That file's row series is unusable: its
    header declares seven columns while eight of ten rows carry thirteen, so a DictReader
    silently files the alpha figure under n_holdings and a dollar amount under
    rebalanced; several dates repeat; and benchmark_return is identically 0.0 on every
    row, which makes its "alpha" the raw return under another name. signals.csv is
    structurally sound and is the source here.
    """
    by_date, collapsed = _perf_by_date()
    dates = sorted(by_date)
    if len(dates) < 2:
        return {"days": len(dates), "legs": 0, "min_days": PERF_MIN_DAYS,
                "renderable": False, "series": [], "duplicates_collapsed": collapsed,
                "benchmark": PERF_BENCHMARK}

    legs, usable, boundary, dropped_pre_break = _perf_legs(by_date, dates)

    series, book, bench, eqc = [], 1.0, 1.0, 1.0
    bench_live = False
    # The origin is where measurement starts, which is the first *usable* leg's earlier
    # date — not the first date on file. Anchoring at dates[0] when the opening legs
    # were dropped draws the first segment from 08-01 to 08-05 and attributes one
    # night's return to four days of chart.
    if usable:
        series.append({"date": usable[0]["from"], "book": 0.0,
                       "benchmark": 0.0, "equal_weight": 0.0})
    for l in usable:
        book *= (1.0 + l["book"])
        if l["benchmark"] is not None:
            bench *= (1.0 + l["benchmark"]); bench_live = True
        if l["equal_weight"] is not None:
            eqc *= (1.0 + l["equal_weight"])
        series.append({
            "date": l["to"],
            "book": round((book - 1.0) * 100, 4),
            "benchmark": round((bench - 1.0) * 100, 4) if l["benchmark"] is not None else None,
            "equal_weight": round((eqc - 1.0) * 100, 4) if l["equal_weight"] is not None else None,
        })
    # The origin is only a real point on a line that has real points.
    if series and not bench_live:
        series[0]["benchmark"] = None
    if series and not any(p["equal_weight"] is not None for p in series[1:]):
        series[0]["equal_weight"] = None

    # A leg that straddles a specification boundary chains a book chosen by one model
    # onto returns scored by another. Reported rather than hidden, exactly as the equity
    # terminal reports a curve spanning two spec hashes: it is two series drawn end to
    # end, and the reader has to know which.
    breaks = set(b["to"] for b in _spec_breaks())
    crossed = sorted({l["to"] for l in usable if l["to"] in breaks})

    return {
        "days": len(dates),
        "min_days": PERF_MIN_DAYS,
        "spec_breaks_crossed": crossed,
        "spec_stable": not crossed,
        "spec_boundary": boundary,
        "legs_before_boundary": dropped_pre_break,
        "renderable": len(usable) >= PERF_MIN_DAYS - 1,
        "legs": len(usable),
        "legs_dropped": len(legs) - len(usable),
        "duplicates_collapsed": collapsed,
        "from": usable[0]["from"] if usable else dates[0],
        "to": usable[-1]["to"] if usable else dates[-1],
        "recorded_from": dates[0], "recorded_to": dates[-1],
        "benchmark": PERF_BENCHMARK,
        "benchmark_available": bench_live,
        "top_n": PERF_TOP_N,
        "book_total": round((book - 1.0) * 100, 4) if usable else None,
        "benchmark_total": round((bench - 1.0) * 100, 4) if bench_live else None,
        "equal_weight_total": round((eqc - 1.0) * 100, 4) if usable else None,
        "series": series,
    }


# ---------------------------------------------------------------------------
# model monitoring
# ---------------------------------------------------------------------------
# Every threshold below is tuned against the days actually on file, not carried over
# from the equity project. Two of its panels do not transfer at all and are deliberately
# replaced rather than copied:
#
#   * Equity ranks a fixed index constituent list, so its rank correlation is a
#     statement about scoring stability. This universe is "top N by market cap from
#     CoinGecko" and turns over 8-20% a night by construction, so the same statistic
#     across all names would mostly report which coins were large that morning.
#     Stability is therefore measured on the surviving cohort only, with the churn
#     reported beside it so the number is never read without its context.
#
#   * Equity's coverage panel measures the share of inputs that came from a filing
#     rather than a sector-median substitution. Nothing here imputes, so that has no
#     analogue. The equivalent early warning is field presence: if CoinGecko stops
#     returning rs200, or Dune's Module B flatlines, a factor quietly goes to zero and
#     the only symptom is a column of nulls.
#
# Dispersion is a warn, never a fail. Equity's percentiles guarantee dispersion by
# construction, so a collapse there is always a pipeline defect. These are absolute
# thresholds over raw turnover and momentum, so a collapse can be a genuine market —
# everything correlated, or liquidity gone. It is a regime reading, not a bug report.
MON_MIN_ASSETS = 25
MON_DISPERSION_WARN = 8.0        # observed 17.9 on the latest recorded board
MON_STABILITY_WARN = 0.70
MON_CHURN_WARN = 0.35            # settled nights run 8-20%; the first two were 44-63%
MON_STALE_HOURS = 36
# Fields whose disappearance would silently zero a factor rather than raise anything.
# These are model inputs: an absence here is a defect, and it sets the check's status.
MON_TRACKED_FIELDS = ("price", "market_cap", "turnover_pct", "conviction",
                      "rs7", "rs14", "rs30", "rs200", "perp_mult",
                      # Genuine model inputs, not context: _lavl_regime — a captured
                      # SPEC_FUNCTION — reads high_24h/low_24h off the live payload and
                      # always has. Recording them as columns did not put them into the
                      # specification; they were already in it. If CoinGecko stops
                      # returning them the LAVL band silently changes for every asset,
                      # which is precisely the dropout this panel exists to catch.
                      "high_24h", "low_24h")
# Recorded-but-not-scored context: the Dune feed, and the emission/adoption ratio beside
# it — score() computes its own internal adoption proxy and never reads any of these.
# Coverage is reported so a dead feed stays visible, but it must not set the status.
# `unlocks_usd` is null for most tokens *by construction* — unlock schedules are not
# on-chain in the general case — so folding it into the warn would pin this panel to a
# permanent amber and hide a real dropout in price or rs200 behind it.
MON_CONTEXT_FIELDS = ("era", "unlocks_usd", "supply_increase_pct", "addr_growth_pct",
                      # Derivatives and the daily bar. Observational on the same terms:
                      # score() reads none of them. `funding_rate` used to be the
                      # exception worth naming — lavl_perp_mult read the raw rate — but
                      # since Module 3 the modifier reads the annualised, interval-aware
                      # `funding_apr` instead, and this column is now purely the record
                      # of what the legacy Bybit feed returned.
                      "funding_rate", "oi_usd", "long_short_ratio",
                      # Module 3. `funding_apr` and `rsi7` are genuine model inputs —
                      # lavl_perp_mult reads both — and the first draft of this change
                      # filed them as tracked for exactly that reason. That was wrong,
                      # and the dry run showed why within one board: funding_apr is null
                      # for every spot-only asset, which is most of the long tail, and
                      # rsi7 is null until a symbol has eight recorded closes. Grading
                      # them pinned field presence to a permanent amber at 60% coverage
                      # — which is the failure `unlocks_usd` is already filed here to
                      # avoid, and a permanent amber is what a real dropout in price or
                      # rs200 would then hide behind.
                      #
                      # Structural nullity is the test, not model relevance. The dropout
                      # that actually matters for these — every venue going dark at
                      # once, which silently collapses every modifier to 1.0 — is not
                      # visible in a per-column coverage ratio anyway, and is checked
                      # directly by "Funding feed" in _compute_monitor instead.
                      "funding_apr", "rsi7",
                      # Provenance: which book the rate came from, how many listed the
                      # asset, how far apart they were. The modifier reads the
                      # consolidated APR and never the spread.
                      "funding_venue", "funding_venues_n", "funding_apr_spread",
                      "funding_regime",
                      # Module 4. Null for any symbol Cryptometer has no book for, and
                      # null for every symbol when the key is unconfigured, so grading
                      # them would pin this panel amber for a feed that is optional by
                      # design.
                      "liq_longs_usd", "liq_shorts_usd", "liq_imbalance",
                      "funding_interval_basis")
MON_FIELD_PRESENCE_WARN = 0.90


def _spearman(a: list, b: list):
    """Rank correlation, ties averaged. None below three pairs."""
    n = len(a)
    if n < 3:
        return None

    def ranks(xs):
        order = sorted(range(n), key=lambda i: xs[i])
        out = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((y - mb) ** 2 for y in rb) ** 0.5
    return round(num / (da * db), 4) if da and db else None


def _mon_check(name, status, detail, value=None) -> dict:
    return {"name": name, "status": status, "detail": detail, "value": value}


def _mon_float(r, k):
    try:
        v = r.get(k)
        return float(v) if v not in (None, "", "None") else None
    except (TypeError, ValueError):
        return None


# A suspected specification change, detected from the data rather than declared.
# The hash makes a change visible going forward; this catches one that predates the
# hash, or one shipped by someone who edited a threshold without the hash noticing —
# and it is the backstop that found the 2026-08-05 break in this ledger, where the
# median conviction moved 36 points on a night the median price moved 0.00%.
BREAK_SCORE_MOVE = 10.0      # median |delta conviction|, points
BREAK_PRICE_MOVE = 0.02      # median |price move| below which the market cannot explain it


def _spec_breaks() -> list:
    """Nights where the scores moved and the market did not.

    Conviction is a function of price and liquidity inputs. If the median asset's score
    moves double digits while the median asset's price barely moves, the function
    changed — that is a specification boundary regardless of whether anything recorded
    one, and any return study or Information Coefficient spanning it is a number about
    two different models.
    """
    rows = _read_signals_rows()
    by_date = {}
    for r in rows:
        d, sym = r.get("date"), (r.get("symbol") or "").upper()
        if d and sym:
            by_date.setdefault(d, {})[sym] = r
    dates = sorted(by_date)
    out = []
    for a, b in zip(dates, dates[1:]):
        prev, curr = by_date[a], by_date[b]
        shared = sorted(set(prev) & set(curr))
        dconv, dpx = [], []
        for s in shared:
            c0, c1 = _mon_float(prev[s], "conviction"), _mon_float(curr[s], "conviction")
            p0, p1 = _mon_float(prev[s], "price"), _mon_float(curr[s], "price")
            if c0 is not None and c1 is not None:
                dconv.append(abs(c1 - c0))
            if p0 and p1:
                dpx.append(abs(p1 / p0 - 1.0))
        if len(dconv) < 5 or len(dpx) < 5:
            continue
        med_c = sorted(dconv)[len(dconv) // 2]
        med_p = sorted(dpx)[len(dpx) // 2]
        if med_c >= BREAK_SCORE_MOVE and med_p <= BREAK_PRICE_MOVE:
            out.append({
                "from": a, "to": b, "shared": len(shared),
                "median_score_move": round(med_c, 1),
                "median_price_move": round(med_p * 100, 2),
                "detail": ("the median asset's score moved %.0f points on a night its "
                           "price moved %.2f%% — the scoring function changed"
                           % (med_c, med_p * 100)),
            })
    return out


def _compute_monitor(dune_report: dict | None = None) -> dict:
    """Operational condition of the pipeline. Never a claim about predictive power.

    A board can be fresh, dispersed, fully covered and perfectly stable while
    forecasting nothing at all. Whether high-conviction assets outperform is a separate
    measurement needing months inside one specification hash — which is what the
    spec_hash column exists to make possible, and why it is reported here.

    ``dune_report`` is this run's fetch report, when there was one. Without it the
    contextual feed can only be described from the columns on disk, which cannot tell a
    feed that is switched off from one that is switched on and pointing at the wrong
    query.
    """
    rows = _read_signals_rows()
    if not rows:
        return {}

    by_date = {}
    for r in rows:
        d, sym = r.get("date"), (r.get("symbol") or "").upper()
        if d and sym:
            by_date.setdefault(d, {})[sym] = r
    dates = sorted(by_date)
    if not dates:
        return {}
    latest = by_date[dates[-1]]

    # --- stability, on the surviving cohort ---------------------------------
    stability = None
    if len(dates) >= 2:
        prev, curr = by_date[dates[-2]], by_date[dates[-1]]
        shared = sorted(set(prev) & set(curr))
        pairs = [(x, y) for x, y in
                 ((_mon_float(prev[s], "conviction"), _mon_float(curr[s], "conviction"))
                  for s in shared)
                 if x is not None and y is not None]
        entered, left = sorted(set(curr) - set(prev)), sorted(set(prev) - set(curr))
        moves = [abs(y - x) for x, y in pairs]
        stability = {
            "from": dates[-2], "to": dates[-1],
            "shared": len(pairs),
            "rank_correlation": _spearman([p[0] for p in pairs], [p[1] for p in pairs]),
            "mean_abs_move": round(sum(moves) / len(moves), 2) if moves else None,
            "max_abs_move": round(max(moves), 1) if moves else None,
            # Reported with the correlation, always. A stability reading on a population
            # that replaced a fifth of itself overnight is not a statement about scoring.
            "entered": entered, "left": left,
            "churn": round(len(entered) / len(curr), 3) if curr else None,
            "universe_prev": len(prev), "universe_curr": len(curr),
        }

    # --- field presence, and its trend --------------------------------------
    def presence(day, fields=MON_TRACKED_FIELDS):
        return {f: (round(sum(1 for r in day.values() if _mon_float(r, f) is not None)
                          / len(day), 4) if day else 0.0)
                for f in fields}

    coverage_series = [dict(date=d, n=len(by_date[d]), **presence(by_date[d])) for d in dates]
    latest_presence = presence(latest)
    context_presence = presence(latest, MON_CONTEXT_FIELDS)
    context_series = [dict(date=d, **presence(by_date[d], MON_CONTEXT_FIELDS)) for d in dates]

    # --- dispersion, as a regime reading ------------------------------------
    def sigma(day):
        cs = [c for c in (_mon_float(r, "conviction") for r in day.values()) if c is not None]
        if len(cs) < 2:
            return None, None
        m = sum(cs) / len(cs)
        return (round((sum((c - m) ** 2 for c in cs) / (len(cs) - 1)) ** 0.5, 2),
                round(sorted(cs)[len(cs) // 2], 1))

    dispersion, _median = sigma(latest)
    disp_series = []
    for d in dates:
        s, med = sigma(by_date[d])
        if s is not None:
            disp_series.append({"date": d, "dispersion": s, "median": med})
    convs = [c for c in (_mon_float(r, "conviction") for r in latest.values()) if c is not None]

    # --- specification continuity -------------------------------------------
    # Spans are built on the CANONICAL hash, so the one audited instrumentation
    # correction does not split twenty otherwise-comparable nights into two track
    # records. Every other digest is its own identity — see SPEC_EQUIVALENT, which holds
    # exactly one entry and says what was audited to justify it. The raw hash recorded
    # on the rows travels alongside, so nothing is rewritten and the collapse is
    # visible rather than silent.
    spec_spans, unknown_days, aliased_days = [], 0, 0
    for d in dates:
        hashes = {r.get("spec_hash") for r in by_date[d].values() if r.get("spec_hash")}
        raw = sorted(hashes)[0] if hashes else None
        h = canonical_spec_hash(raw) if raw else None
        if raw and h != raw:
            aliased_days += 1
        if not h:
            unknown_days += 1
        if spec_spans and spec_spans[-1]["spec_hash"] == h:
            spec_spans[-1]["to"] = d
            spec_spans[-1]["days"] += 1
            if raw and raw not in spec_spans[-1]["recorded_as"]:
                spec_spans[-1]["recorded_as"].append(raw)
        else:
            spec_spans.append({"spec_hash": h, "from": d, "to": d, "days": 1,
                               "recorded_as": [raw] if raw else []})

    # --- health -------------------------------------------------------------
    checks = []
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(dates[-1] + "T00:00:00+00:00")).total_seconds() / 3600
        checks.append(_mon_check(
            "Data freshness", "pass" if age_h <= MON_STALE_HOURS else "fail",
            "latest board %s (%.0fh old)" % (dates[-1], age_h), round(age_h, 1)))
    except ValueError:
        checks.append(_mon_check("Data freshness", "fail", "unparseable latest date"))

    checks.append(_mon_check(
        "Universe size", "pass" if len(latest) >= MON_MIN_ASSETS else "fail",
        "%d assets scored on %s" % (len(latest), dates[-1]), len(latest)))

    tiers = {r.get("signal") for r in latest.values() if r.get("signal")}
    checks.append(_mon_check(
        "Signal tiers populated", "pass" if len(tiers) >= 3 else "warn",
        "%d of 5 tiers in use" % len(tiers), len(tiers)))

    # Warn, never fail — see the note above the thresholds.
    ok = (dispersion or 0) >= MON_DISPERSION_WARN
    checks.append(_mon_check(
        "Score dispersion", "pass" if ok else "warn",
        "sigma = %s across %d assets" % (dispersion, len(convs))
        + ("" if ok else " — compressed. On absolute thresholds this can be the market, "
                         "not the pipeline"),
        dispersion))

    if latest_presence:
        worst = min(latest_presence.items(), key=lambda kv: kv[1])
        checks.append(_mon_check(
            "Field presence", "pass" if worst[1] >= MON_FIELD_PRESENCE_WARN else "warn",
            "weakest input `%s` present for %.0f%% of the board" % (worst[0], worst[1] * 100),
            round(worst[1], 4)))

    if context_presence:
        live = [f for f, v in context_presence.items() if v > 0]
        # Informational by design — see MON_CONTEXT_FIELDS. Never graded: a null here is
        # the expected state, not a defect. But *why* it is null is worth saying, which
        # is what the fetch report supplies — a query id pointing at the wrong query
        # produces exactly the same empty columns as no configuration at all.
        why = {
            "unconfigured": "no Dune query configured",
            "unreachable": "the Dune call failed — wrong key, no such query, or a query "
                           "that has never been executed",
            "unusable": "the configured Dune query returned rows this feed cannot read "
                        "— it is answering a different question",
        }.get((dune_report or {}).get("status"))
        # Only where the reason needs evidence. "no Dune query configured" is complete
        # on its own; the other two are claims about a remote system and have to show
        # what came back. Columns come from the structured field rather than being read
        # back out of the prose, so the two cannot drift apart.
        if why and (dune_report or {}).get("status") != "unconfigured":
            bits = [b for b in (dune_report.get("detail"),
                                "returned: " + ", ".join(dune_report["columns"])
                                if dune_report.get("columns") else None) if b]
            if bits:
                why += " (%s)" % "; ".join(bits)
        checks.append(_mon_check(
            "Contextual feeds", "info",
            ("%d of %d recorded-not-scored fields carry values (%s) — observational only, "
             "nothing here reaches score()"
             % (len(live), len(context_presence), ", ".join(live)))
            if live else
            (why or "no contextual fields carry values") +
            ". Nothing scored depends on it",
            round(len(live) / len(context_presence), 4)))

    if stability and stability["rank_correlation"] is not None:
        rc, churn = stability["rank_correlation"], stability["churn"] or 0
        checks.append(_mon_check(
            "Ranking stability", "pass" if rc >= MON_STABILITY_WARN else "warn",
            "rank correlation %s across %d names held in common" % (rc, stability["shared"]),
            rc))
        checks.append(_mon_check(
            "Universe churn", "pass" if churn <= MON_CHURN_WARN else "warn",
            "%d of %d assets are new (%.0f%%) — the ranking above is measured only on the rest"
            % (len(stability["entered"]), stability["universe_curr"], churn * 100),
            round(churn, 3)))
    else:
        checks.append(_mon_check("Ranking stability", "pending",
                                 "needs two recorded days; nothing to compare against yet"))

    real_spans = [s for s in spec_spans if s["spec_hash"]]
    if unknown_days and not real_spans:
        checks.append(_mon_check(
            "Specification history", "pending",
            "%d day(s) recorded before the specification was hashed — unknown, not "
            "assumed to match today's" % unknown_days, 0))
    else:
        multi = len(real_spans) > 1
        checks.append(_mon_check(
            "Specification history", "warn" if multi else "pass",
            "%d specification(s) across %d recorded day(s)"
            % (len(real_spans), sum(s["days"] for s in real_spans))
            + ("; %d earlier day(s) unhashed" % unknown_days if unknown_days else "")
            + ("; %d day(s) carry a superseded digest folded onto its canonical one "
               "(instrumentation correction, scoring-equivalent)" % aliased_days
               if aliased_days else "")
            + ("  — a series spanning two hashes is two datasets" if multi else ""),
            len(real_spans)))

    breaks = _spec_breaks()
    if breaks:
        # Fails only when this run introduced one. A boundary in the recorded past is an
        # immutable fact about the history — failing on it forever would block every
        # future deploy and train everyone to ignore the gate, which is worse than not
        # having it. A fresh one means tonight's run changed the scoring, and that
        # should stop the build.
        fresh = [b for b in breaks if b["to"] == dates[-1]]
        checks.append(_mon_check(
            "Undeclared specification change", "fail" if fresh else "warn",
            ("this run changed the scoring: " + fresh[0]["detail"]) if fresh else
            ("%d boundary in the recorded past (%s) — history either side of it is two "
             "datasets, not one, and any return study has to segment there"
             % (len(breaks), ", ".join(b["to"] for b in breaks))),
            len(breaks)))
    else:
        checks.append(_mon_check(
            "Undeclared specification change", "pass",
            "no night where the median score moved without the median price", 0))

    # --- the funding feed, checked as a feed rather than as a column ---------
    # `funding_apr` is null for every spot-only asset, so its coverage ratio says more
    # about how many of the board have perpetual markets than about whether the feed
    # works — which is why it is filed as context above. The dropout that actually
    # matters is different in kind: if every venue goes dark at once, lavl_perp_mult
    # falls to a neutral 1.0 for every asset, every score shifts, and nothing anywhere
    # throws. That is a scoring event disguised as a quiet night, and it is what this
    # check is for.
    #
    # The comparison is against the recorded past rather than an absolute floor. How
    # much of this universe has a perp market is a fact about the universe and drifts
    # slowly; a collapse against that baseline is a fact about the pipeline.
    perp_cover = []
    for d in dates:
        day = by_date[d]
        n_f = sum(1 for r in day.values() if _mon_float(r, "funding_apr") is not None)
        perp_cover.append((d, n_f / len(day) if day else 0.0))
    today_cover = perp_cover[-1][1]
    prior = [c for _, c in perp_cover[:-1] if c > 0]
    baseline = (sum(prior) / len(prior)) if prior else None
    if not prior:
        checks.append(_mon_check(
            "Funding feed", "pending",
            "no prior night has a funding column to compare against — the baseline "
            "starts accumulating tonight", round(today_cover, 3)))
    elif today_cover == 0:
        checks.append(_mon_check(
            "Funding feed", "fail",
            "no asset carries a funding rate tonight against a %.0f%% baseline — every "
            "score modifier fell to a neutral 1.0, which moves the board without "
            "raising anything" % (baseline * 100), 0.0))
    elif today_cover < baseline * 0.5:
        checks.append(_mon_check(
            "Funding feed", "warn",
            "funding present for %.0f%% of the board against a %.0f%% baseline — at "
            "least one venue is down and the modifiers it would have set are neutral"
            % (today_cover * 100, baseline * 100), round(today_cover, 3)))
    else:
        checks.append(_mon_check(
            "Funding feed", "pass",
            "funding present for %.0f%% of the board, in line with the %.0f%% baseline "
            "— the rest are spot-only, which is not a dropout"
            % (today_cover * 100, baseline * 100), round(today_cover, 3)))

    dupes = len(rows) - len({(r.get("date"), r.get("symbol")) for r in rows})
    checks.append(_mon_check(
        "Ledger integrity", "pass" if dupes == 0 else "fail",
        "one row per (date, symbol)" if dupes == 0
        else "%d duplicate row(s) — the daily series is not daily" % dupes, dupes))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_hash": SPEC_HASH,
        "observations": len(dates),
        "from": dates[0], "to": dates[-1],
        "health": checks,
        "stability": stability,
        "dispersion": {"latest": dispersion, "series": disp_series,
                       "warn_below": MON_DISPERSION_WARN},
        "coverage": {"latest": latest_presence, "series": coverage_series,
                     "warn_below": MON_FIELD_PRESENCE_WARN,
                     "basis": ("Share of the board carrying a usable value for each input. "
                               "Nothing here is imputed, so this measures feed health rather "
                               "than substitution: a field going to zero is an upstream "
                               "dropout, and the factor it feeds quietly stops contributing."),
                     "context": {"latest": context_presence, "series": context_series,
                                 "feed": {k: v for k, v in (dune_report or {}).items()
                                          if k != "data"} or None,
                                 "basis": ("Recorded, not scored. These columns are logged "
                                           "beside every board so they can be studied "
                                           "against realised returns before anyone argues "
                                           "for adopting them; adoption would change the "
                                           "specification hash and start a new track "
                                           "record. Nulls here are expected — unlock "
                                           "schedules are contractual, not on-chain, and "
                                           "are recorded only for tokens whose vesting "
                                           "contracts are enumerated in the query.")}},
        "specification": {"spans": spec_spans, "unknown_days": unknown_days,
                          "suspected_breaks": breaks,
                          # Published so the terminal can explain a folded span rather
                          # than showing two dates under one digest with no account of
                          # why. Empty for every hash outside the audited pair.
                          "aliased_days": aliased_days,
                          "equivalence": {k: {"canonical": v["canonical"],
                                              "reason": v["reason"],
                                              "detail": v["detail"]}
                                          for k, v in SPEC_EQUIVALENT.items()}},
        "scope": ("Operational condition only. Whether these scores predict returns is a "
                  "separate question needing months of history inside one specification "
                  "hash. Nothing here is evidence that the conviction score forecasts "
                  "anything; it is evidence that the pipeline producing it is behaving."),
    }


CHOP_PERIOD = 14
CHOP_TRENDING = 38.2
CHOP_CHOPPY = 61.8


def choppiness(bars: list) -> float | None:
    """The 14-period Choppiness Index over accumulated daily bars.

        CHOP = 100 * log10(sum(ATR1) / (maxHigh - minLow)) / log10(period)

    Low means directional, high means range-bound. It is deliberately computed from
    bars this pipeline recorded itself: CoinGecko's /ohlc endpoint has no daily
    granularity at all (30-minute at 1 day, 4-hour to 30 days, 4-day beyond), so a
    "14-day" CHOP taken from it would be a 14-bar CHOP over 56 hours wearing the wrong
    label. high_24h/low_24h are already in the markets response, so the honest version
    costs no extra request and simply needs fourteen nights to exist.

    Returns None below `CHOP_PERIOD + 1` bars — the panel says how many it has rather
    than showing a number computed from a shorter window.
    """
    if len(bars) < CHOP_PERIOD + 1:
        return None
    window = bars[-(CHOP_PERIOD + 1):]
    trs = []
    for prev, cur in zip(window, window[1:]):
        h, lo, pc = cur.get("high"), cur.get("low"), prev.get("close")
        if h is None or lo is None or pc is None:
            return None
        trs.append(max(h - lo, abs(h - pc), abs(lo - pc)))
    recent = window[1:]
    highs = [b["high"] for b in recent if b.get("high") is not None]
    lows = [b["low"] for b in recent if b.get("low") is not None]
    if len(highs) < CHOP_PERIOD or len(lows) < CHOP_PERIOD:
        return None
    rng = max(highs) - min(lows)
    total = sum(trs)
    if rng <= 0 or total <= 0:
        return None
    return round(100.0 * math.log10(total / rng) / math.log10(CHOP_PERIOD), 2)


def chop_regime(chop) -> str | None:
    if chop is None:
        return None
    return ("TRENDING" if chop < CHOP_TRENDING
            else "RANGE-BOUND" if chop > CHOP_CHOPPY
            else "TRANSITIONAL")


def _chop_by_symbol() -> dict:
    """Choppiness per asset from the recorded bars, plus how far off it is otherwise.

    ``{SYMBOL: {"chop": float|None, "regime": str|None, "bars": int}}`` — the bar count
    travels with the value so the terminal can render "accumulating (10/15)" instead of
    an empty cell that reads as a broken column.
    """
    rows = _read_signals_rows()
    bars: dict[str, list] = {}
    for r in sorted(rows, key=lambda x: (x.get("date") or "")):
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        h, lo, c = _num(r.get("high_24h")), _num(r.get("low_24h")), _num(r.get("price"))
        if h is None or lo is None or c is None:
            continue
        bars.setdefault(sym, []).append({"high": h, "low": lo, "close": c})
    out = {}
    for sym, seq in bars.items():
        ch = choppiness(seq)
        out[sym] = {"chop": ch, "regime": chop_regime(ch), "bars": len(seq)}
    return out


# ---------------------------------------------------------------------------
# ledger -> series (the inputs every quant.py reading takes)
# ---------------------------------------------------------------------------
# One pass over signals.csv builds all of them. The first version of this read the file
# once per indicator, which on a 940-row ledger is cheap and on a 90-night one is four
# full scans a night for no reason; more to the point, four readers can disagree about
# which rows are usable and produce indicators computed over different subsets of the
# same history.
def _series_from_ledger(rows: list[dict] | None = None) -> dict:
    """Bars, closes, turnover, ranks and RSI per symbol, oldest first.

    Returns ``{"bars":, "closes":, "turnover":, "quality":, "dates":}``.

    ``bars`` requires high/low/close all present, which is a stricter test than the
    others and deliberately so: a bar missing its range is not a bar, and admitting it
    with the close substituted for both would feed ADX a zero true range and quietly
    depress every reading downstream. That gap is real in this ledger — high_24h and
    low_24h were appended as columns partway through, so the bar series is shorter than
    the close series and the indicators that need bars say so.
    """
    rows = _read_signals_rows() if rows is None else rows
    rows = sorted(rows, key=lambda r: (r.get("date") or "", r.get("symbol") or ""))
    bars: dict[str, list] = {}
    closes: dict[str, list] = {}
    turnover: dict[str, list] = {}
    quality: dict[str, list] = {}
    by_date: dict[str, list] = {}
    for r in rows:
        d = r.get("date") or ""
        if d:
            by_date.setdefault(d, []).append(r)
    # Market-cap rank is recomputed per night rather than read: the ledger stores caps,
    # not ranks, and a rank taken from row order would be a conviction rank wearing a
    # market-cap label — the rows are written sorted by conviction.
    rank_of = {}
    for d, day in by_date.items():
        ordered = sorted(day, key=lambda r: -(_num(r.get("market_cap")) or 0))
        for i, r in enumerate(ordered, start=1):
            rank_of[(d, (r.get("symbol") or "").upper())] = i
    for r in rows:
        sym = (r.get("symbol") or "").upper()
        if not sym:
            continue
        d = r.get("date") or ""
        h, lo, c = _num(r.get("high_24h")), _num(r.get("low_24h")), _num(r.get("price"))
        if None not in (h, lo, c):
            bars.setdefault(sym, []).append({"high": h, "low": lo, "close": c, "date": d})
        if c is not None and c > 0:
            closes.setdefault(sym, []).append(c)
            quality.setdefault(sym, []).append(
                {"date": d, "close": c, "rank": rank_of.get((d, sym)),
                 "rsi7": _num(r.get("rsi7"))})
        t = _num(r.get("turnover_pct"))
        if t is not None:
            turnover.setdefault(sym, []).append(t)
    return {"bars": bars, "closes": closes, "turnover": turnover,
            "quality": quality, "dates": sorted(by_date)}


def _trend_structure(series: dict) -> dict:
    """ADX, ATR and the strategy label per symbol, joined to the choppiness reading.

    ``{SYMBOL: {adx, plus_di, minus_di, regime, bars, needed, atr14, strategy, ...}}``.
    Every entry is present even when nothing could be computed, because the terminal
    needs to render "accumulating (9/29)" for those and cannot do that from an absent
    key.
    """
    chop = _chop_by_symbol()
    out = {}
    for sym, seq in (series.get("bars") or {}).items():
        a = quant.adx(seq)
        st = quant.strategy_for((chop.get(sym) or {}).get("regime"), a)
        out[sym] = {**a, "atr14": quant.atr(seq),
                    "chop": (chop.get(sym) or {}).get("chop"),
                    "chop_regime": (chop.get(sym) or {}).get("regime"),
                    "strategy": st["strategy"], "strategy_basis": st["basis"],
                    "strategy_confidence": st["confidence"]}
    return out


def _read_csv_rows(path: Path, fields: list) -> list[dict]:
    """Rows of one of the append-only context ledgers, normalised onto its fields."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: r.get(k) for k in fields} for r in csv.DictReader(f)]


def _append_context_rows(path: Path, fields: list, today: str, rows: list[dict]) -> int:
    """Replace today's rows in an append-only context ledger and rewrite it.

    Replace rather than append, for the reason main() already documents for
    signals.csv: a second run on the same day otherwise records that day twice, and
    anything reading the file as a daily series then computes a 7-day flow across a
    window that contains one day nine times. Prior days are untouched.
    """
    kept = [r for r in _read_csv_rows(path, fields) if r.get("date") != today]
    fresh = [{k: r.get(k) for k in fields} for r in rows]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(kept + fresh)
    return len(fresh)


# ---------------------------------------------------------------------------
# market intelligence artifact
# ---------------------------------------------------------------------------
# How many names the correlation matrix covers. The matrix is O(n^2) to compute and to
# render, and its purpose is to say whether the *book* is one bet — so it is taken over
# the names the book could hold, not over the 250-name universe where most pairs are
# between two things nobody would own together.
CORR_BOARD_N = 20


def _compute_market_intel(feeds: dict, series: dict, conviction_by_symbol: dict,
                          today: str, board_syms: list | None = None) -> dict:
    """Everything the terminal's context panels read, in one artifact.

    Assembled here rather than in main() because every piece of it is a join between a
    feed report and the recorded ledger, and those joins have rules — which symbols the
    correlation matrix covers, what the sector flow's lookback actually spans, whether
    the stablecoin regime has a prior night to compare against. Rules belong somewhere a
    test can call them with fixtures.

    Every section carries the status of the feed behind it. A panel whose feed failed
    renders as unavailable and says which failure it was; it does not render as empty,
    which is indistinguishable from a market where nothing is happening.
    """
    def rep(name):
        return (feeds or {}).get(name) or {"status": "absent", "detail": "feed not run",
                                           "data": {}}

    trending_rep, cats_rep = rep("trending"), rep("categories")
    global_rep, stable_rep, dex_rep = rep("global"), rep("stablecoins"), rep("dex")
    gdata = global_rep.get("data") or {}
    sdata = stable_rep.get("data") or {}

    # --- sectors -----------------------------------------------------------
    sector_hist = _read_csv_rows(SECTORS_CSV, SECTOR_FIELDS)
    for r in sector_hist:
        r["mcap"] = _num(r.get("mcap"))
    rotation = quant.sector_rotation(cats_rep.get("data") or {}, sector_hist,
                                     gdata.get("mcap_chg_24h"))

    # --- macro: today against the recorded macro ledger ---------------------
    macro_hist = _read_csv_rows(MACRO_CSV, MACRO_FIELDS)
    prior = [r for r in macro_hist if (r.get("date") or "") != today]
    prior.sort(key=lambda r: r.get("date") or "")
    # Seven nights back where seven exist, otherwise the oldest recorded. The lookback
    # actually used is reported, never assumed: "stable float +2% over 7d" and the same
    # number over 2d are different claims and only one of them is the label.
    lookback = prior[-7] if len(prior) >= 7 else (prior[0] if prior else None)
    lookback_days = (len(prior) - prior.index(lookback)) if lookback else 0
    stable_float_chg = None
    if lookback and sdata.get("total_mcap"):
        base = _num(lookback.get("stable_mcap"))
        if base and base > 0:
            stable_float_chg = round((sdata["total_mcap"] / base - 1.0) * 100, 3)
    stable = quant.stablecoin_regime(sdata.get("velocity"), stable_float_chg,
                                     lookback_days)

    # --- correlation over the names the book could hold ---------------------
    ranked = sorted(((v, k) for k, v in (conviction_by_symbol or {}).items()
                     if v is not None), reverse=True)
    universe = list(board_syms) if board_syms else [sym for _, sym in ranked[:CORR_BOARD_N]]
    universe = [s for s in universe if s in (series.get("closes") or {})]
    if "BTC" not in universe and "BTC" in (series.get("closes") or {}):
        # The benchmark has to be in the matrix for a beta to exist at all. Added
        # explicitly rather than assumed present: BTC is not always inside the top 20 by
        # conviction, and on the nights it is not, every beta silently became None.
        universe.append("BTC")
    corr = quant.correlation_report({s: series["closes"][s] for s in universe})

    # --- trending divergence ------------------------------------------------
    tmd = quant.trending_divergence(trending_rep.get("data") or {}, conviction_by_symbol)

    # --- fallen kings -------------------------------------------------------
    kings = quant.fallen_kings(series.get("quality") or {})

    # --- on-chain rotation --------------------------------------------------
    dex_hist = _read_csv_rows(DEX_CSV, DEX_FIELDS)
    dex_by_net: dict[str, list] = {}
    for r in dex_hist:
        if (r.get("date") or "") != today and r.get("network"):
            dex_by_net.setdefault(r["network"], []).append(r)
    dex_rows = []
    for net, rec in (dex_rep.get("data") or {}).items():
        hist = sorted(dex_by_net.get(net) or [], key=lambda r: r.get("date") or "")
        window = hist[-7:]
        base = next((_num(r.get("volume_24h")) for r in window
                     if _num(r.get("volume_24h"))), None)
        flow = (round((rec["volume_24h"] / base - 1.0) * 100, 2)
                if (base and base > 0 and rec.get("volume_24h")) else None)
        dex_rows.append({**rec, "volume_flow_pct": flow, "flow_days": len(window)})
    dex_rows.sort(key=lambda r: -(r.get("volume_24h") or 0))

    # --- liquidity shocks ---------------------------------------------------
    shocks = {}
    for sym, vals in (series.get("turnover") or {}).items():
        rec = quant.liquidity_shock(vals)
        if rec["z"] is not None:
            shocks[sym] = rec

    return {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_hash": SPEC_HASH,
        "session": (feeds or {}).get("_session") or {},
        "feeds": {n: {"status": r["status"], "detail": r["detail"],
                      "http_status": r.get("http_status")}
                  for n, r in (("trending", trending_rep), ("categories", cats_rep),
                               ("global", global_rep), ("stablecoins", stable_rep),
                               ("dex", dex_rep))},
        "macro": {
            **{k: gdata.get(k) for k in
               ("total_mcap", "total_mcap_ex_btc", "total_volume", "btc_dominance",
                "eth_dominance", "eth_btc_dominance_ratio", "mcap_chg_24h")},
            "stable_mcap": sdata.get("total_mcap"),
            "stable_volume": sdata.get("total_volume"),
            "stable_velocity": sdata.get("velocity"),
            "stable_issuers": sdata.get("issuers") or [],
            "stable_regime": stable,
            "lookback_days": lookback_days,
            "history_nights": len(prior) + 1,
        },
        "sectors": rotation,
        "correlation": corr,
        "trending": tmd,
        "fallen_kings": kings,
        "dex": {"networks": dex_rows, "status": dex_rep["status"],
                "detail": dex_rep["detail"]},
        "liquidity_shocks": shocks,
        "thresholds": {
            "adx_trending": quant.ADX_TRENDING, "adx_weak": quant.ADX_WEAK,
            "adx_min_bars": quant.ADX_MIN_BARS,
            "corr_cluster": quant.CORR_CLUSTER, "corr_min_obs": quant.CORR_MIN_OBS,
            "tmd_crowded_gap": quant.TMD_CROWDED_GAP,
            "tmd_quiet_gap": quant.TMD_QUIET_GAP,
            "liq_shock_z": quant.LIQ_SHOCK_Z,
            "fallen_min_dd": quant.FALLEN_MIN_DD, "fallen_max_dd": quant.FALLEN_MAX_DD,
            "stable_velocity_hot": quant.STABLE_VELOCITY_HOT,
            "emission_free_ratio": EMISSION_FREE_RATIO,
            "emission_anchor_ratio": EMISSION_ANCHOR_RATIO,
            "emission_max_penalty": EMISSION_MAX_PENALTY,
            "impact_coeff": quant.IMPACT_COEFF,
            "spread_bps": quant.DEFAULT_SPREAD_BPS,
        },
    }


# Conviction at or above this counts as the model backing a name. Matches the BUY cut in
# TIER_CUTS; kept as its own constant so a change here is a deliberate reporting choice
# rather than something that follows silently from a scoring edit.
PERSIST_LEVEL = 70.0
# A name whose best night clears the level but whose typical night does not. The gap is
# what separates a position from a headline.
PERSIST_SPIKE_GAP = 15.0
PERSIST_MAX_NAMES = 40
# Nights added to the denominator when ranking. A name seen once and backed once has a
# raw share of 1.0 and would outrank one backed on nine nights of eleven, which is the
# opposite of what persistent means. Shrinking toward zero costs a well-established name
# almost nothing and costs a one-night sample most of its score — exactly the asymmetry
# wanted. Reported as its own field rather than buried in a sort key.
PERSIST_SHRINK = 2.0


def _persistence(series: dict, dates: list) -> dict:
    """Which names hold conviction across nights, and which spike for one.

    This replaced persistent_30d / persistent_90d, which required 30 and 90 *consecutive*
    nights above the level. The ledger has 13, so both were empty lists on every board
    ever recorded — the same starvation that made the change feed look broken. This
    measures over the history that exists and states the window, rather than reporting
    nothing until an arbitrary threshold is crossed.

    Streaks are counted on consecutive *recorded* boards, not calendar days. A night the
    pipeline did not run is a gap in observation, and treating it as a break would
    understate persistence for a reason that has nothing to do with the asset. A name
    absent from a board genuinely breaks its streak — it left the universe.
    """
    idx = {d: i for i, d in enumerate(dates)}
    rows = []
    for sym, seq in series.items():
        pts = sorted(((d, c) for d, c in seq if c is not None), key=lambda x: x[0])
        if not pts:
            continue
        convs = [c for _, c in pts]
        seen = {d for d, _ in pts}
        mean = sum(convs) / len(convs)
        sd = (sum((c - mean) ** 2 for c in convs) / (len(convs) - 1)) ** 0.5 if len(convs) > 1 else 0.0
        # Streaks walk the full date axis so a night the asset was missing breaks the
        # run, which is the honest reading: it was not on the board to be held.
        best = cur = run = 0
        by_date = dict(pts)
        for d in dates:
            c = by_date.get(d)
            run = run + 1 if (d in seen and c is not None and c >= PERSIST_LEVEL) else 0
            best = max(best, run)
        cur = run
        above = sum(1 for c in convs if c >= PERSIST_LEVEL)
        peak = max(convs)
        rows.append({
            "symbol": sym,
            "nights": len(pts), "of": len(dates),
            "mean": round(mean, 1), "sd": round(sd, 1),
            "peak": round(peak, 1), "latest": round(pts[-1][1], 1),
            "nights_above": above,
            "share_above": round(above / len(pts), 3),
            # The ranked figure. See PERSIST_SHRINK: a raw share cannot compare a
            # one-night sample with an eleven-night one.
            "persistence": round(above / (len(pts) + PERSIST_SHRINK), 3),
            "best_streak": best, "current_streak": cur,
            # A one-night wonder: it cleared the level at its best, but its typical
            # night sits well below it. Shown as a flag rather than filtered out,
            # because the interesting question is which of these the reader recognises.
            "spike": peak >= PERSIST_LEVEL and (peak - mean) >= PERSIST_SPIKE_GAP
                     and above <= max(1, len(pts) // 4),
            # The row of the heatmap, aligned to the date axis. None where the asset was
            # not on the board, which is a different cell from a low score.
            "cells": [round(by_date[d], 1) if d in by_date and by_date[d] is not None else None
                      for d in dates],
        })
    # Ranked by how much of its recorded life the name spent backed, then by how
    # convincingly — which puts durable conviction above a single high reading.
    rows.sort(key=lambda r: (-r["persistence"], -r["best_streak"], -r["mean"]))
    return {
        "dates": dates,
        "level": PERSIST_LEVEL,
        "window": len(dates),
        "rows": rows[:PERSIST_MAX_NAMES],
        "n_backed": sum(1 for r in rows if r["nights_above"] > 0),
        "n_spikes": sum(1 for r in rows if r["spike"]),
        "shrink": PERSIST_SHRINK,
        "basis": ("Share of a name's recorded nights at or above conviction "
                  f"{PERSIST_LEVEL:.0f}, with the longest consecutive run beside it. "
                  "Streaks count recorded boards, not calendar days: a night the "
                  "pipeline did not run is a gap in observation, while a night the "
                  "asset was absent genuinely breaks the run because it was not there "
                  "to hold. This is a description of what the score has done, not a "
                  "forecast — a name can be perfectly persistent and still be wrong. "
                  "Ranking uses a shrunk share so a name seen once cannot outrank one "
                  "backed on most of eleven nights."),
    }


# Size of the leading cohort whose retention defines stickiness. Ten is the book the
# basket actually holds, so churn here is churn a holder would have paid for.
HEALTH_COHORT = 10
# Below this share retained night over night the model is reordering its own top book
# faster than a holder could act on it. Not calibrated against a historical norm —
# there is no history to calibrate against yet, and a threshold presented as one would
# be a number invented and then dressed as evidence. It is a stated convention, and the
# ribbon says which.
HEALTH_STICKY_WARN = 0.70


def _model_health(series: dict, dates: list, tier_diff: dict | None) -> dict:
    """Is today's board a trend or a twitch?

    Stickiness is the share of last night's top cohort still in tonight's, averaged over
    every consecutive pair recorded. It answers the question a holder actually has —
    "would I have been churning this book" — which rank correlation across the whole
    universe does not: a board can reorder its tail violently while the top ten sit
    still, and score a low correlation for movements nobody would have traded.

    Reported with the window attached and no comparison to a historical average, because
    eleven nights is not a history to average. A green badge implying "normal for this
    model" would be inventing the baseline it claims to measure against.
    """
    by_date = {}
    for sym, seq in series.items():
        for d, c in seq:
            if c is not None:
                by_date.setdefault(d, {})[sym] = c
    pairs, retained = 0, 0.0
    for a, b in zip(dates, dates[1:]):
        prev, curr = by_date.get(a) or {}, by_date.get(b) or {}
        if len(prev) < HEALTH_COHORT or len(curr) < HEALTH_COHORT:
            continue
        top_prev = {s for s, _ in sorted(prev.items(), key=lambda kv: -kv[1])[:HEALTH_COHORT]}
        top_curr = {s for s, _ in sorted(curr.items(), key=lambda kv: -kv[1])[:HEALTH_COHORT]}
        retained += len(top_prev & top_curr) / HEALTH_COHORT
        pairs += 1
    sticky = round(retained / pairs, 4) if pairs else None

    # Last night alone, for the "what just changed" half of the ribbon.
    latest = None
    if pairs:
        a, b = dates[-2], dates[-1]
        prev, curr = by_date.get(a) or {}, by_date.get(b) or {}
        if len(prev) >= HEALTH_COHORT and len(curr) >= HEALTH_COHORT:
            tp = {s for s, _ in sorted(prev.items(), key=lambda kv: -kv[1])[:HEALTH_COHORT]}
            tc = {s for s, _ in sorted(curr.items(), key=lambda kv: -kv[1])[:HEALTH_COHORT]}
            latest = {"retained": len(tp & tc), "of": HEALTH_COHORT,
                      "entered": sorted(tc - tp), "left": sorted(tp - tc)}

    # Promotions and demotions across the conviction tiers, from the diff already
    # computed. Symbols travel with the counts so the ribbon can filter to them.
    flips = {"into_buy": [], "out_of_buy": [], "into_strong": [], "pending": True}
    if tier_diff and not tier_diff.get("pending"):
        flips["pending"] = False
        rank = {name: i for i, (_, name) in enumerate(TIER_CUTS)}   # 0 = STRONG
        for c in tier_diff.get("changed") or []:
            f, t = c.get("from_tier"), c.get("to_tier")
            if f not in rank or t not in rank:
                continue
            if t == "STRONG" and f != "STRONG":
                flips["into_strong"].append(c["symbol"])
            # Lower index is a better tier, so a fall in index is a promotion.
            if rank[t] < rank[f] and rank[t] <= rank["BUY"]:
                flips["into_buy"].append(c["symbol"])
            elif rank[f] <= rank["BUY"] < rank[t]:
                flips["out_of_buy"].append(c["symbol"])
    return {
        "cohort": HEALTH_COHORT,
        "stickiness": sticky,
        "sticky_warn": HEALTH_STICKY_WARN,
        "pairs": pairs,
        "window": len(dates),
        "latest": latest,
        "flips": flips,
        "basis": (f"Share of the top {HEALTH_COHORT} by conviction retained from one "
                  f"recorded night to the next, averaged over {pairs} pair(s). It is "
                  "the churn a holder of that book would have paid for, which rank "
                  "correlation across the whole universe does not measure — a board can "
                  "reorder its tail violently while the top sits still. No comparison "
                  "to a historical norm is drawn: this window is too short to be one."),
    }


# Horizons the change feed will use, longest first, with the recorded days each needs.
# A delta over N days needs N+1 recorded boards to have two endpoints.
FEED_HORIZONS = (("d30", 31), ("d10", 11), ("d7", 8), ("d1", 2))
FEED_LIMIT = 8


def _change_feed(trend: dict, days_recorded: int) -> dict:
    """Largest conviction gains and losses, over the longest horizon that has data.

    This used to be hardwired to a 10-day delta, which needs eleven recorded boards.
    The ledger has never had that many, so `d10` was None for every asset on every run
    and the feed serialised as {"gains": [], "losses": []} — rendering as an empty
    panel that looks exactly like a broken one. It would have started working on its
    own eventually, silently, which is its own problem: nobody would have known whether
    it was fixed or still faulty.

    So it degrades instead. It reports the horizon it actually used and what the longer
    ones are still waiting for, and the panel says so rather than showing nothing.
    """
    pending = {h: {"needs": need, "have": days_recorded}
               for h, need in FEED_HORIZONS if days_recorded < need}
    for horizon, need in FEED_HORIZONS:
        if days_recorded < need:
            continue
        movers = [(s, t[horizon]) for s, t in trend.items() if t.get(horizon) is not None]
        if not movers:
            continue
        movers.sort(key=lambda x: x[1], reverse=True)
        return {
            "horizon": horizon,
            "days": int(horizon[1:]),
            "gains": [{"symbol": s, "delta": round(d, 1), horizon: round(d, 1)}
                      for s, d in movers[:FEED_LIMIT] if d > 0],
            "losses": [{"symbol": s, "delta": round(d, 1), horizon: round(d, 1)}
                       for s, d in reversed(movers[-FEED_LIMIT:]) if d < 0],
            "pending": pending,
        }
    return {"horizon": None, "days": None, "gains": [], "losses": [], "pending": pending}


def _compute_market_breadth() -> dict:
    """Conviction as a time-series (reviewer #3) + breadth/dispersion/persistence
    (the three differentiated signals). Purely derived from the signals ledger —
    no new feeds. Returns a dict written to ledger/market_breadth.json.

    Per asset: conviction delta over 10d and 30d (trend/acceleration) from the
    stored daily conviction series.
    Market level: % assets scoring >70 / >80 (breadth), conviction dispersion
    (stdev across the universe, the Conviction Dispersion Index), and persistent
    leadership (assets >=70 for 30d and 90d).
    """
    if not LEDGER_CSV.exists():
        return {}
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # Build per-symbol series of (date, conviction)
    series: dict[str, list] = {}
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        try:
            conv = float(r.get("conviction") or 0)
        except ValueError:
            continue
        series.setdefault(sym, []).append((r.get("date"), conv))

    # Sort each series by date and compute deltas
    def _at(seq, days_back):
        if len(seq) <= days_back:
            return None
        return seq[-1][1] - seq[-(days_back + 1)][1]

    trend = {}
    for sym, seq in series.items():
        seq.sort(key=lambda x: x[0])
        trend[sym] = {
            "conviction": seq[-1][1] if seq else 0,
            # d1 and d7 exist because d10 needs eleven recorded days and the ledger has
            # had fewer for its entire life so far. The feed was serialising as
            # {"gains": [], "losses": []} every night and rendering as nothing, which
            # is indistinguishable from a broken panel — see _change_feed below.
            "d1": _at(seq, 1),
            "d7": _at(seq, 7),
            "d10": _at(seq, 10),
            "d30": _at(seq, 30),
        }

    # Market-level aggregates are computed over the LATEST daily snapshot only
    # (the current investable universe), not the union of all history — otherwise
    # assets that left the universe would distort breadth/dispersion.
    all_dates = [d for seq in series.values() for d, _ in seq]
    latest_date = max(all_dates) if all_dates else None
    latest_conv = [
        seq[-1][1] for seq in series.values()
        if seq and seq[-1][0] == latest_date and seq[-1][1] is not None
    ]
    n = len(latest_conv) or 1
    above70 = sum(1 for c in latest_conv if c >= 70)
    above80 = sum(1 for c in latest_conv if c >= 80)
    dispersion = (sum((c - (sum(latest_conv) / n)) ** 2 for c in latest_conv) / n) ** 0.5 if n > 1 else 0.0

    # persistent_30d / persistent_90d used to be computed here: assets at or above 70 on
    # the last 30 and 90 *consecutive* boards. Both were empty lists on every night the
    # ledger has ever recorded, because they need 30 and 90 nights and there are 13. A
    # field that is structurally empty is not a null reading — it is a zero the terminal
    # rendered as a fact ("Persistent 30d: 0"), which is a stronger and wronger claim
    # than saying nothing. `persistence` below measures the same idea over the history
    # that exists and ships its window with it.

    change_feed = _change_feed(trend, len(set(all_dates)))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_assets": n,  # latest-day investable universe (not the historical union)
        "breadth_above70": above70,
        "breadth_above80": above80,
        "breadth_pct_above70": round(100 * above70 / n, 1),
        "dispersion": round(dispersion, 2),
        "conviction_change_feed": change_feed,
        # Regime per asset, plus the bar count so the terminal can say what it is still
        # waiting for rather than rendering an empty cell.
        "chop": _chop_by_symbol(),
        # Whether the ordering is informative at all — the question that decides
        # whether any of the rest is worth acting on.
        "edge": _compute_edge(),
        # Which names hold conviction across nights versus spike for one.
        "persistence": _persistence(series, sorted(set(all_dates))),
        # Model health, for the ribbon: is tonight a trend or a twitch.
        "health": _model_health(series, sorted(set(all_dates)), _compute_tier_diff()),
        "chop_period": CHOP_PERIOD,
        "trend": {s: {"conviction": round(t["conviction"], 1),
                      **{h: (round(t[h], 1) if t[h] is not None else None)
                         for h in ("d1", "d7", "d10", "d30")}}
                 for s, t in sorted(trend.items(), key=lambda x: -x[1]["conviction"])},
    }


def _macro_regime_from_ledger() -> str:
    """Macro Liquidity Gate (D) — PASSIVE / display-only.

    Reads the stored index.csv and compares today's global mcap to the value
    ~7 days ago. RISK-OFF if global mcap is down > 8% over that window.
    Returns "RISK-OFF" / "RISK-ON" / "N/A" (no history). This is a logged
    context signal only — it NEVER changes basket holdings.
    """
    try:
        # Through the schema-checked reader: a mismatched header yields no rows rather
        # than a column of misread values.
        rows = read_index_rows()
        if not rows:
            return "N/A"
        caps = [(r["date"], float(r["global_market_cap"])) for r in rows
                if r.get("global_market_cap") not in (None, "", "None")]
        if len(caps) < 2:
            return "N/A"
        today_cap = caps[-1][1]
        # find the cap closest to 7 days before the latest date
        latest = date.fromisoformat(caps[-1][0])
        target = latest - timedelta(days=7)
        past = min(caps, key=lambda c: abs(date.fromisoformat(c[0]) - target))
        past_cap = past[1]
        if past_cap <= 0:
            return "N/A"
        if today_cap / past_cap < 0.92:
            return "RISK-OFF"
        return "RISK-ON"
    except Exception:  # noqa: BLE001
        return "N/A"


BASKET_JSON = LEDGER_DIR / "basket.json"
INDEX_CSV = LEDGER_DIR / "index.csv"
# Where a file whose header cannot be reconciled with INDEX_FIELDS is set aside. Kept
# rather than deleted: it is the only record of what the pipeline actually emitted, and
# the malformed rows are evidence even though they are not data.
INDEX_LEGACY_CSV = LEDGER_DIR / "index.legacy.csv"

# The schema, in one place. Previously the header was `list(row.keys())` written only
# when the file did not yet exist, so when six columns were added to `row` the existing
# file kept its seven-column header while every new line appended thirteen values. A
# DictReader then filed the alpha figure under n_holdings and a dollar amount under
# rebalanced, with no error and no visible symptom — the same class of failure the
# equity ledger's append-only column check exists to catch.
INDEX_FIELDS = [
    "date", "global_market_cap",
    "basket_return_since_entry", "benchmark_return_since_entry", "alpha_since_entry",
    "exec_adjusted_return_since_entry", "turnover_bps", "macro_regime",
    "eject_delta", "ejected_syms", "entrants_syms", "n_holdings", "rebalanced",
]


def _read_signals_rows() -> list[dict]:
    """Existing signals rows, normalised onto the current FIELDS."""
    if not LEDGER_CSV.exists():
        return []
    with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
        return [{k: r.get(k) for k in FIELDS} for r in csv.DictReader(f)]


def dedupe_signals() -> int:
    """Collapse duplicate (date, symbol) rows, latest occurrence winning.

    A one-shot repair for the history a blind append already produced. Returns the
    number of rows removed. Not run automatically — a caller decides, because silently
    rewriting a ledger is the kind of thing this project is meant to be careful about.
    """
    rows = _read_signals_rows()
    if not rows:
        return 0
    seen: dict[tuple, dict] = {}
    for r in rows:
        seen[(r.get("date"), r.get("symbol"))] = r
    out = sorted(seen.values(), key=lambda r: (r.get("date") or "", r.get("symbol") or ""))
    removed = len(rows) - len(out)
    if removed:
        with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(out)
    return removed


def read_index_rows(path=None) -> list[dict]:
    """Rows from index.csv, or [] if its header does not match the current schema.

    Refusing to read a mismatched file is the point. Parsing it anyway is what produced
    a published chart of fabricated alpha.
    """
    path = path or INDEX_CSV
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return []
        if header != INDEX_FIELDS:
            return []
        return [dict(zip(header, r)) for r in reader if len(r) == len(header)]


def _persist_index_row(row: dict, path=None) -> list[dict]:
    """Write today's row and return the full series.

    Rewrites the whole file every run rather than appending, which fixes three things at
    once: the header can never drift from the rows, a re-run on the same date replaces
    that date instead of adding a duplicate, and a file whose header no longer matches
    the schema is moved aside instead of being appended to. At ledger scale — one row a
    day — a full rewrite costs nothing.
    """
    path = path or INDEX_CSV
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != INDEX_FIELDS:
            legacy = INDEX_LEGACY_CSV
            path.replace(legacy)
            print(f"[index] header did not match the schema ({len(header)} columns vs "
                  f"{len(INDEX_FIELDS)}); moved the old file to {legacy.name} and started "
                  f"a clean series. Those rows are not recoverable: their values are "
                  f"positionally misaligned against the header they were written under.")

    rows = [r for r in read_index_rows(path) if r.get("date") != row["date"]]
    rows.append({k: ("" if row.get(k) is None else row.get(k)) for k in INDEX_FIELDS})
    rows.sort(key=lambda r: r.get("date") or "")

    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INDEX_FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)
    return rows
INDEX_JSON = LEDGER_DIR / "index.json"
MARKET_BREADTH_JSON = LEDGER_DIR / "market_breadth.json"
MONITOR_JSON = LEDGER_DIR / "monitor.json"
# Cross-venue funding, in its own artifact rather than folded into signals.json. The
# per-venue detail is a nested object per symbol and the carry screen is a ranked list;
# neither fits the flat one-row-per-(date, symbol) shape the ledger holds, and widening
# the CSV to carry them would put JSON inside a CSV cell. The flat, per-asset fields
# that *do* fit are in FIELDS as columns; this file is the structured view beside them.
FUNDING_JSON = LEDGER_DIR / "funding.json"
# Market context, same reasoning as FUNDING_JSON above: the sector matrix is a ranked
# list, the correlation matrix is a square of squares, and the trending panel is keyed
# on symbols that are mostly NOT on the board. None of that is one row per (date,
# symbol), and forcing it into the CSV would mean JSON inside CSV cells.
MARKET_INTEL_JSON = LEDGER_DIR / "market_intel.json"
# Three append-only ledgers, because three of the new readings are multi-day and their
# sources publish no history at all. /coins/categories has a 24h column and nothing
# else; /global is a snapshot; GeckoTerminal's pool volume is a rolling 24h window.
# A "7d sector rotation" therefore cannot be fetched — it can only be accumulated, one
# night at a time, exactly as the choppiness bars were. These files are that
# accumulation, and until they are a week long every multi-day column reads as
# accumulating rather than as a number.
SECTORS_CSV = LEDGER_DIR / "sectors.csv"
MACRO_CSV = LEDGER_DIR / "macro.csv"
DEX_CSV = LEDGER_DIR / "dex.csv"
SECTOR_FIELDS = ["date", "category_id", "name", "mcap", "volume_24h", "turnover",
                 "chg24h", "rs24h", "coins_count"]
MACRO_FIELDS = ["date", "total_mcap", "total_mcap_ex_btc", "total_volume",
                "btc_dominance", "eth_dominance", "eth_btc_dominance_ratio",
                "mcap_chg_24h", "stable_mcap", "stable_volume", "stable_velocity",
                "stable_regime", "funding_heat_apr", "board_mean_conviction"]
DEX_FIELDS = ["date", "network", "pools", "reserve_usd", "volume_24h", "vlr"]
REBALANCE_DAYS = 7
# Rebalance hysteresis (A): don't eject a holding just because it slipped one
# rank. Keep it unless it drops to rank >= EJECT_RANK or its score falls more
# than EJECT_GAP below the marginal entrant. Controls turnover drag.
EJECT_RANK = 13
EJECT_GAP = 5.0
# Execution friction (B): one-way cost (bps) applied to turnover at rebalance to
# build an execution-adjusted COUNTERFACTUAL track record. Does NOT mutate the
# raw paper return. Calibrated from measured turnover once history accrues.
EXEC_BPS_ONEWAY = 25.0


def build_basket(markets: list[dict], today: str, btc: dict | None = None) -> dict:
    """Conviction-weighted Top-10 basket with score-proportional target weights.

    Tracks, per day:
      #2  benchmark via total crypto market cap (entry->now), horizon-matched.
      #5  live-drifted weights (target * price move) vs target weights.
      #6  per-asset audit trail (entry/current price, target/live weight, return).
    Rebalances weekly or when a holding drops out of the gated Top 10.
    """
    scored = []
    for t in markets:
        sym = (t.get("symbol") or "").upper()
        if not sym or sym in STABLES:
            continue
        era, conv, _, _ = score(t, None, btc)
        if _conjunctive_gate(t, conv):
            scored.append((sym, t, conv))
    scored.sort(key=lambda x: x[2], reverse=True)
    # The strict conjunctive gate rarely fires on real large-cap data (turnover
    # for top caps is usually < 0.30). Fall back to Top-N by conviction so the
    # basket always reflects the strongest non-stable assets when the gate is empty.
    if not scored:
        for t in markets:
            sym = (t.get("symbol") or "").upper()
            if not sym or sym in STABLES:
                continue
            era, conv, _, _ = score(t, None, btc)
            scored.append((sym, t, conv))
        scored.sort(key=lambda x: x[2], reverse=True)
    top = scored[:10]
    # Rank 11..N (marginal entrants) — used by the hysteresis buffer below.
    rest = scored[10:]
    if not top:
        # Truly empty universe — still record an empty-basket index row so the
        # workflow's git add never fails on a missing file.
        _write_index_row(today, [], {}, False)
        return {"holdings": [], "rebalanced": today, "note": "no assets"}

    # --- Rebalance hysteresis (A) ---
    # On a non-calendar rebalance, only eject a prior holding if it dropped to
    # rank >= EJECT_RANK, OR its score fell more than EJECT_GAP below the
    # marginal entrant (rank 11). This stops #10<->#11 flips from rebalancing
    # daily and burning alpha in turnover.
    keep = set()
    prev_syms = set()
    prev = {}
    if BASKET_JSON.exists():
        try:
            prev = json.loads(BASKET_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            prev = {}
    prev_syms = {h["symbol"] for h in prev.get("holdings", [])}
    if prev_syms:
        rest_conv = {sym: c for sym, _, c in rest}
        for h in prev.get("holdings", []):
            sym = h["symbol"]
            if sym in {s for s, _, _ in top}:
                keep.add(sym); continue
            rank = next((i for i, (s, _, _) in enumerate(scored, 1) if s == sym), None)
            # marginal entrant = best of rank 11+ (or rank 10 if basket < 10)
            margin_conv = rest[0][2] if rest else (top[-1][2] if top else 0)
            if rank is not None and rank >= EJECT_RANK:
                pass  # eject (do not keep)
            elif (h.get("conviction") or 0) + EJECT_GAP < margin_conv:
                pass  # structurally outclassed — eject
            else:
                keep.add(sym)
    # Build holdings: keep previous holdings still retained, add new entrants.
    kept_holdings = [h for h in prev.get("holdings", []) if h["symbol"] in keep]
    new_top = [x for x in top if x[0] not in prev_syms]
    holdings = []
    for h in kept_holdings:
        sym = h["symbol"]
        # refresh conviction from current scoring
        nh = next((x for x in top if x[0] == sym), None)
        conv = nh[2] if nh else h.get("conviction", 0)
        holdings.append({
            "symbol": sym, "conviction": conv, "weight": 0.0,
            "entry_price": h.get("entry_price") or 0,
            "current_price": (next((t.get("current_price") for _, t, _ in top if _ and (t.get("symbol") or "").upper() == sym), None)) or h.get("current_price") or 0,
        })
    for sym, t, conv in new_top:
        holdings.append({
            "symbol": sym, "conviction": conv, "weight": 0.0,
            "entry_price": t.get("current_price") or 0,
            "current_price": t.get("current_price") or 0,
        })

    # Normalise over the holdings that actually exist, not over the top N.
    #
    # The denominator used to be the sum of the top ten's convictions while the weights
    # were applied to kept + new entrants — a superset whenever the hysteresis buffer
    # retains a name that has slipped out of the top ten. The two sets only coincide
    # when nothing is being held over, so the weights stopped summing to 1 the moment
    # the buffer did its job. On 2026-08-08 the denominator was 9 (a single name in
    # `top`) against eleven holdings, and the weights summed to 76.6 — so every
    # `wret = sum(weight * return)` in the index was inflated roughly seventy-six-fold,
    # which is where the published "daily" returns of 87% and 123% came from.
    total_conv = sum(h["conviction"] for h in holdings) or 1
    for h in holdings:
        h["weight"] = round(h["conviction"] / total_conv, 4)

    # Rebalance decision: calendar OR membership changed vs the (hysteresis-
    # filtered) previous basket. A pure #10<->#11 flip no longer triggers.
    prev_date = prev.get("rebalanced", today)
    cur_syms = {h["symbol"] for h in holdings}
    try:
        days_since = (date.fromisoformat(today) - date.fromisoformat(prev_date)).days
    except ValueError:
        days_since = REBALANCE_DAYS
    rebalanced = (days_since >= REBALANCE_DAYS) or (cur_syms != prev_syms)
    if rebalanced:
        # The benchmark baseline is carried over, not re-snapshotted. Resetting it here
        # while kept holdings keep their original entry_price measured the two legs over
        # different horizons: the basket accumulated from its first entry while the
        # benchmark restarted from zero on every rebalance. Since `rebalanced` was true
        # on nine of the first ten runs, benchmark_return was 0.0 on every row and the
        # reported alpha was the basket's raw return under another name. Only a genuinely
        # new basket — no prior baseline — takes today's reading.
        gmc = prev.get("entry_global_mcap") or fetch_global_market_cap()
        basket = {"rebalanced": today, "entry_global_mcap": gmc, "holdings": holdings}
        BASKET_JSON.write_text(json.dumps(basket, indent=2))
    else:
        # keep entry prices + entry_global_mcap from prev basket for unchanged holdings
        basket = prev
        for h in basket.get("holdings", []):
            if h["symbol"] in cur_syms:
                nh = next(x for x in holdings if x["symbol"] == h["symbol"])
                h["weight"] = nh["weight"]
                h["conviction"] = nh["conviction"]

    # Live prices + benchmark
    live = {(tk.get("symbol") or "").upper(): tk.get("current_price") or 0 for tk in markets}
    gmc_now = fetch_global_market_cap()
    entry_gmc = basket.get("entry_global_mcap") or gmc_now

    # --- Ejection Alpha Delta (C) + Execution turnover (B) ---
    # Computed only on rebalance days (when holdings actually change).
    ejected_syms, entrants_syms = [], []
    ejected_avg_return = 0.0
    if rebalanced:
        prev_h = {h["symbol"]: h for h in prev.get("holdings", [])}
        ejected_syms = [s for s in prev_h if s not in cur_syms]
        entrants_syms = [s for s in cur_syms if s not in prev_h]
        ej_rets = []
        for s in ejected_syms:
            ep = prev_h[s].get("entry_price") or 0
            cur = live.get(s) or 0
            if ep and cur:
                ej_rets.append((cur - ep) / ep)
        ejected_avg_return = (sum(ej_rets) / len(ej_rets)) if ej_rets else 0.0
    # Δ_eject = R_entrant(0 at entry) - R_ejected(realized). Full R_entrant
    # window requires the next rebalance's entry prices; we store the symbol
    # lists so it can be refined once >=2 rebalances exist. Honest, no fabrication.
    eject_delta = -ejected_avg_return
    # One-way turnover (bps) = Σ |new_w - old_w| over changed holdings.
    # Genesis (no prior basket) is the baseline — no execution cost is charged.
    turnover_bps = 0.0
    if rebalanced and prev.get("holdings"):
        old_w = {h["symbol"]: h.get("weight", 0) for h in prev.get("holdings", [])}
        for h in holdings:
            s = h["symbol"]
            turnover_bps += abs(h.get("weight", 0) - old_w.get(s, 0)) * 1e4

    # Per-asset audit trail + live-drift weights (#5, #6)
    audit = []
    vals = []
    for h in basket.get("holdings", []):
        cur = live.get(h["symbol"]) or 0
        ep = h.get("entry_price") or 0
        tw = h.get("weight", 0)
        if cur and ep:
            ret = (cur - ep) / ep
            v = tw * (cur / ep)          # drifted value
        else:
            ret = 0.0
            v = tw
        vals.append(v)
        audit.append({
            "ticker": h["symbol"], "entry_price": round(ep, 8), "current_price": round(cur, 8),
            "target_weight": round(tw, 4), "return": round(ret, 4),
        })
    vtot = sum(vals) or 1
    for a, v in zip(audit, vals):
        a["live_weight"] = round(v / vtot, 4)

    # Basket return = Σ target_weight * asset_return (target-weighted, not drifted)
    wret = sum(a["target_weight"] * a["return"] for a in audit)
    # Execution-adjusted counterfactual return (B): raw wret minus one-way cost
    # on turnover. Never mutates the raw paper return stored alongside it.
    exec_adjusted_return = wret - (EXEC_BPS_ONEWAY / 1e4) * (turnover_bps / 1e4)
    # Benchmark: total crypto market cap, entry -> now (horizon-matched) (#2)
    bench_total = (gmc_now / entry_gmc) if (gmc_now and entry_gmc) else 1.0

    # Append daily row + recompute cumulative from stored rows
    _write_index_row(today, audit, basket, rebalanced, live,
                     macro_regime=None, eject_delta=eject_delta,
                     ejected_syms=ejected_syms, entrants_syms=entrants_syms,
                     exec_adjusted_return=exec_adjusted_return, turnover_bps=turnover_bps)
    return basket


def _normalize_live(audit: list, live_holdings: list) -> list:
    """Active-basket slice of the audit trail with target weights Σ=1.0.

    ``current_holdings`` (the audit trail) can carry stale rows from ejected
    symbols whose ``target_weight`` no longer represents the live basket — that
    made the terminal's allocation column sum to >100% (e.g. 383.3%). This
    returns only the symbols currently in the live basket, with their
    target weights re-normalised so Σ target_weight == 1.0, and synthesises a
    parallel ``live_weight`` (drifted by price) and ``return`` so the terminal
    needs zero schema changes.
    """
    live_syms = {h.get("symbol") for h in live_holdings}
    active = [h for h in audit if h.get("ticker") in live_syms]
    if not active:
        return []
    raw_sum = sum(h.get("target_weight", 0) for h in active) or 1.0
    out = []
    for h in active:
        tw = h.get("target_weight", 0) / raw_sum
        ep = h.get("entry_price") or 0
        cur = h.get("current_price") or 0
        ret = ((cur - ep) / ep) if (cur and ep) else 0.0
        live_w = tw * (cur / ep) if (cur and ep) else tw
        out.append({"ticker": h.get("ticker"), "entry_price": ep,
                    "current_price": cur, "target_weight": round(tw, 4),
                    "live_weight": round(live_w, 4), "return": round(ret, 4)})
    return out


def _write_index_row(today: str, audit: list, basket: dict, rebalanced: bool,
                     live: dict | None = None, macro_regime: str | None = None,
                     eject_delta: float = 0.0, ejected_syms: list | None = None,
                     entrants_syms: list | None = None,
                     exec_adjusted_return: float = 0.0, turnover_bps: float = 0.0) -> None:
    """Append today's basket/index row and rewrite index.json.

    Always writes ledger/index.csv + ledger/index.json (creating them on first
    run) so the CI workflow's `git add` never fails on a missing file, even when
    the basket is empty. `live` maps symbol->current price for non-rebalance-day
    return computation.

    New columns (this build):
      macro_regime      (D) RISK-ON / RISK-OFF from 7d global mcap trend (passive)
      eject_delta       (C) R_entrant(0) - R_ejected(realized) on rebalance days
      ejected_syms / entrants_syms (C) the rotation that occurred
      exec_adjusted_return (B) paper return minus one-way execution cost on turnover
      turnover_bps      (B) one-way turnover at rebalance
    """
    gmc_now = fetch_global_market_cap()
    entry_gmc = basket.get("entry_global_mcap") or gmc_now
    # Basket return = Σ target_weight * asset_return (target-weighted, not drifted)
    wret = 0.0
    for h in basket.get("holdings", []):
        tw = h.get("weight", 0) or 0
        ep = h.get("entry_price") or 0
        if live:
            cur = live.get(h.get("symbol"), h.get("current_price")) or 0
        else:
            cur = h.get("current_price") or 0
        ret = ((cur - ep) / ep) if (cur and ep) else 0.0
        wret += tw * ret
    bench_total = (gmc_now / entry_gmc) if (gmc_now and entry_gmc) else 1.0

    # --- Macro Liquidity Gate (D): PASSIVE / display-only ---
    # 7-day total mcap trend from the stored ledger. RISK-OFF if down > 8%.
    # Logged + shown in the panel; never alters holdings (no premature overlay).
    if macro_regime is None:
        macro_regime = _macro_regime_from_ledger()

    row = {
        "date": today,
        "global_market_cap": round(gmc_now, 0) if gmc_now else None,
        # Renamed from basket_return / benchmark_return. Both are cumulative since the
        # basket's cost basis, never overnight, and the old names invited exactly the
        # mistake the consumer made: compounding ten since-entry figures as if they were
        # ten daily ones. Overnight return lives in market_breadth.json's `performance`
        # block, which chains it from the signals ledger.
        "basket_return_since_entry": round(wret * 100, 3),
        "benchmark_return_since_entry": round((bench_total - 1) * 100, 3),
        "alpha_since_entry": round((wret - (bench_total - 1)) * 100, 3),
        "exec_adjusted_return_since_entry": round(exec_adjusted_return * 100, 3),
        "turnover_bps": round(turnover_bps, 1),
        "macro_regime": macro_regime,
        "eject_delta": round(eject_delta * 100, 3) if rebalanced else "",
        "ejected_syms": ",".join(ejected_syms) if (rebalanced and ejected_syms) else "",
        "entrants_syms": ",".join(entrants_syms) if (rebalanced and entrants_syms) else "",
        "n_holdings": len(audit),
        "rebalanced": rebalanced,
    }
    idx_rows = _persist_index_row(row)
    # The latest reading, not a product of every reading. These columns are cumulative
    # since the basket's cost basis, so compounding the series multiplied one number by
    # its own history — ten rows each already up 80-120% chained into a total return of
    # 4.53x. A cumulative figure is read, not accumulated.
    def _last(field):
        vals = [r.get(field) for r in idx_rows if r.get(field) not in (None, "", "None")]
        return 1 + float(vals[-1]) / 100.0 if vals else 1.0
    cum_basket = _last("basket_return_since_entry")
    cum_exec = _last("exec_adjusted_return_since_entry")
    gcaps = [float(r["global_market_cap"]) for r in idx_rows if r.get("global_market_cap") not in (None, "", "None")]
    bench_cum = (gcaps[-1] / gcaps[0]) if len(gcaps) >= 2 and gcaps[0] else 1.0
    # Macro regime summary (D, passive) + ejection-alpha tally (C)
    regimes = [r.get("macro_regime") for r in idx_rows if r.get("macro_regime") in ("RISK-ON", "RISK-OFF")]
    macro_now = regimes[-1] if regimes else "N/A"
    macro_riskoff_days = sum(1 for x in regimes if x == "RISK-OFF")
    eject_deltas = [float(r["eject_delta"]) for r in idx_rows
                    if r.get("eject_delta") not in (None, "", "None")]
    eject_alpha_cum = round(sum(eject_deltas) / 100.0, 4) if eject_deltas else None
    # Risk stats (Sharpe / max drawdown) — derived from the ledger once >=30 days
    # of history exist. Convention: rf=0, daily returns annualized x sqrt(365).
    # From the overnight series in market_breadth.json, the only daily one we have.
    perf_legs = _compute_performance()
    daily = [l for l in (perf_legs.get("series") or [])]
    dailies = []
    prev_cum = 0.0
    for pt in daily:
        cum = (pt.get("book") or 0.0) / 100.0
        dailies.append((1 + cum) / (1 + prev_cum) - 1)
        prev_cum = cum
    risk = _risk_stats(dailies[1:])
    INDEX_JSON.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest": row,
        "basket_total_return": round(cum_basket, 4),
        "benchmark_total_return": round(bench_cum, 4),
        "exec_adjusted_total_return": round(cum_exec, 4),
        "macro_regime": macro_now,
        "macro_riskoff_days": macro_riskoff_days,
        "eject_alpha_cumulative": eject_alpha_cum,
        "sharpe_convention": "rf=0; daily returns annualized x sqrt(365); computed when len(rows)>=30",
        "risk": risk,
        "current_holdings": audit,
        "latest_holdings": _normalize_live(audit, basket.get("holdings", [])),
        "rows": idx_rows,
    }, indent=2))


def main() -> int:
    global CG_SESSION
    today = date.today().isoformat()

    # The credential first, because fetch_markets is the very next network call and it
    # is the one that has actually been losing pages to HTTP 429. The plan is probed
    # rather than assumed — see coingecko.open_session — and the result is printed, so a
    # secret that is set but refused reads as "refused" in the log rather than as an
    # unexplained rate limit three weeks later.
    CG_SESSION = coingecko.open_session()
    print(f"[cg] session: {CG_SESSION['plan']} / {CG_SESSION['status']} — "
          f"{CG_SESSION['detail']}", file=__import__("sys").stderr)

    # Module B (Dune). The report, not just the data — a configured query pointing at
    # the wrong thing produces the same empty table as no configuration at all, and the
    # dashboard has to be able to say which.
    dune_report = fetch_dune_report(os.environ.get("DUNE_UNLOCK_QUERY_ID"),
                                    os.environ.get("DUNE_API_KEY"))
    dune_b = dune_report["data"]
    print(f"[dune] {dune_report['status']}: {dune_report['detail']}"
          + (" | returned: " + ", ".join(dune_report["columns"])
             if dune_report.get("columns") else ""),
          file=__import__("sys").stderr)

    markets = fetch_markets(session=CG_SESSION)
    scored_syms = {(t.get("symbol") or "").upper() for t in markets
                   if (t.get("symbol") or "").upper() and (t.get("symbol") or "").upper() not in STABLES}
    # One funding fetch, across four venues. There used to be two: fetch_perps_map hit
    # Bybit's tickers endpoint for open interest, and the venue layer hit the same
    # endpoint again for funding. On 2026-08-17 that cost the feed — the first call
    # returned 49 symbols and the second got HTTP 403, so Bybit funding was recorded as
    # unreachable on a night it was reachable. Calling an endpoint twice a night to
    # populate two columns is how you get rate-limited out of one of them.
    venue_reports = funding.fetch_all_venues(scored_syms)
    for name, rep in venue_reports["venues"].items():
        code = rep.get("http_status")
        tag = (f" [HTTP {code}"
               + (" — POLICY BLOCK, this host will not be served" if rep.get("policy_blocked") else "")
               + "]") if code else ""
        print(f"[funding] {name}: {rep['status']} — {rep['detail']}{tag}",
              file=__import__("sys").stderr)
    consolidated = funding.consolidate(venue_reports)
    perps_map = {
        sym: {"funding_rate": rec.get("funding_rate"),
              "interval_hours": rec.get("interval_hours"),
              "funding_apr": rec.get("funding_apr"),
              "oi_usd": rec.get("oi_usd")}
        for sym, rec in consolidated.items()}
    live_venues = [n for n, r in venue_reports["venues"].items() if r["status"] == "live"]
    if consolidated:
        intervals = {}
        for rec in consolidated.values():
            h = rec.get("interval_hours")
            intervals[h] = intervals.get(h, 0) + 1
        print(f"[funding] {len(consolidated)} consolidated market(s) from "
              + ", ".join(live_venues) + "; intervals: "
              + ", ".join(f"{k:g}h x{v}" for k, v in sorted(intervals.items(),
                                                            key=lambda kv: kv[0] or 0)),
              file=__import__("sys").stderr)
    else:
        # Not fatal and not silent. Every modifier falls to the neutral 1.0, which is
        # the correct reading for "no funding was observed" and the wrong thing to
        # discover from a flat column three weeks later.
        print("[funding] no venue returned a usable market — every score modifier is "
              "neutral 1.0 tonight, and the regime column is null rather than NEUTRAL",
              file=__import__("sys").stderr)

    # Module 4. The key is a repository secret and absent locally, so this degrades to
    # "unconfigured" on a developer machine and only ever runs live in CI.
    cm_key = cryptometer.api_key_from_env()
    # Ranked by market cap, not alphabetically. The first version took
    # sorted(scored_syms)[:25], and scored_syms is the FULL ~250-market universe rather
    # than the board — so the sweep started at "A7A5" and never reached BTC or ETH. On
    # 2026-08-18 that spent 25 calls, succeeded on 17, and landed 3 on the board: AAVE,
    # ADA and AKE. Fourteen successful lookups were fetched and discarded, and the names
    # anyone would actually want a positioning read on were never queried.
    #
    # Market cap rather than conviction because conviction does not exist yet here —
    # score() runs further down — and because it is the better key anyway. Positioning
    # is a liquidity-dependent reading: Cryptometer's binance_futures book covers majors
    # and thins out fast, which is where the 8 failures came from. Market cap also
    # churns far less than conviction does night to night, and a column that changes
    # which symbols it covers every evening is a poor time series.
    _by_cap = sorted(
        ((t.get("market_cap") or 0, (t.get("symbol") or "").upper()) for t in markets),
        reverse=True)
    board_syms = [sym for _, sym in _by_cap if sym in scored_syms
                  ][:cryptometer.DEFAULT_SYMBOL_LIMIT]
    print(f"[cryptometer] querying the top {len(board_syms)} by market cap: "
          + ", ".join(board_syms[:8]) + ("..." if len(board_syms) > 8 else ""),
          file=__import__("sys").stderr)
    liq_report = cryptometer.fetch_liquidations(cm_key, board_syms)
    print(f"[cryptometer] liquidations {liq_report['status']}: {liq_report['detail']}",
          file=__import__("sys").stderr)
    liq_map = liq_report["data"] or {}

    # Long/short is Binance-only and one request per symbol. Skipped outright when the
    # Binance venue fetch already failed: on a US-hosted runner that endpoint answers
    # HTTP 451, and firing sixty requests to collect sixty identical refusals is a
    # minute of runner time spent proving something already known.
    if "binance" in live_venues:
        n_ls = fetch_long_short(perps_map, scored_syms)
        print(f"[funding] long/short ratio for {n_ls}/{len(scored_syms)} symbols.",
              file=__import__("sys").stderr)
    else:
        # Binance is the only exchange that publishes this ratio publicly and it answers
        # 451 here, which is why the column has been null on every row since the runner
        # moved. Cryptometer is not geo-blocked, so this restores a reading rather than
        # inventing one — same quantity, different host.
        ls_report = cryptometer.fetch_positioning(cm_key, board_syms)
        for sym, rec in (ls_report["data"] or {}).items():
            perps_map.setdefault(sym, {})["long_short_ratio"] = rec["ratio"]
        print(f"[cryptometer] long/short {ls_report['status']}: {ls_report['detail']}"
              + " — Binance is geo-blocked from this host, so this is the substitute "
                "source rather than an additional one",
              file=__import__("sys").stderr)
    prev_oi = _prev_oi_by_symbol()
    print(f"[perp] prior-night open interest for {len(prev_oi)} symbols"
          + ("" if prev_oi else " — the 24h OI delta is null tonight, not zero"),
          file=__import__("sys").stderr)

    # RSI needs tonight's close, which is the live price — the ledger row for today has
    # not been written yet at this point.
    live_px = {(t.get("symbol") or "").upper(): t.get("current_price")
               for t in markets if t.get("current_price")}
    live_chg = {(t.get("symbol") or "").upper():
                t.get("price_change_percentage_24h") for t in markets}
    rsi_map = _rsi_by_symbol(live_px)
    # Trailing funding, from the nights already on disk. Read before tonight's row is
    # written, so it describes the history a decision would have been made against.
    trail_map = _funding_trail_by_symbol()
    n_trail = sum(1 for v in trail_map.values() if v["n"] >= 3)
    print(f"[funding] trailing carry over <= {FUNDING_TRAIL_NIGHTS} nights for "
          f"{len(trail_map)} symbols, {n_trail} of them with 3+ nights",
          file=__import__("sys").stderr)
    n_rsi = sum(1 for v in rsi_map.values() if v is not None)
    print(f"[funding] 7d RSI for {n_rsi}/{len(rsi_map)} symbols"
          + ("" if n_rsi else " — no symbol has 8 recorded closes yet, so the "
                             "short-squeeze boost cannot fire tonight"),
          file=__import__("sys").stderr)

    # Merge the consolidated funding and its two confirming inputs into perps_map, which
    # is what score() reads. This is the only place the derivatives feed reaches the
    # score, and it does so through lavl_perp_mult — a captured SPEC_FUNCTION — so the
    # decision is recorded in the specification hash on every row written tonight.
    for t in markets:
        sym = (t.get("symbol") or "").upper()
        if sym in perps_map:
            perps_map[sym]["price_chg_24h"] = t.get("price_change_percentage_24h")
            perps_map[sym]["rsi7"] = rsi_map.get(sym)
    # --- market context ----------------------------------------------------
    # Fetched before the row loop because two of its readings are per-row columns, and
    # after the derivatives feed because it is the lower-priority of the two: if the
    # rate limit is going to bite tonight, it should bite the context panels rather than
    # the funding modifier that actually moves scores.
    intel_feeds = coingecko.fetch_all(CG_SESSION)
    for name, rep in intel_feeds["feeds"].items():
        code = f" [HTTP {rep['http_status']}]" if rep.get("http_status") else ""
        print(f"[cg] {name}: {rep['status']} — {rep['detail']}{code}",
              file=__import__("sys").stderr)
    trending_data = (intel_feeds["feeds"].get("trending") or {}).get("data") or {}
    global_data = (intel_feeds["feeds"].get("global") or {}).get("data") or {}

    # Everything quant.py reads, from one pass over the ledger. Read BEFORE tonight's
    # row is written, so every trailing reading describes the history a decision would
    # actually have been made against rather than one that already contains its own
    # outcome.
    series = _series_from_ledger()
    trend = _trend_structure(series)
    n_adx = sum(1 for v in trend.values() if v.get("adx") is not None)
    print(f"[quant] trend structure for {len(trend)} symbol(s); {n_adx} have the "
          f"{quant.ADX_MIN_BARS} bars a {quant.ADX_PERIOD}-period ADX needs",
          file=__import__("sys").stderr)

    # Correlation and beta against BTC across the WHOLE board rather than the top 20 the
    # matrix covers, because these are per-row columns and a column that is populated
    # for twenty rows and empty for thirty looks like a broken feed rather than a
    # deliberate scope.
    btc_rets = quant.log_returns(
        (series["closes"].get("BTC") or [])[-(quant.CORR_WINDOW + 1):])
    corr_btc, beta_btc, corr_obs = {}, {}, {}
    for sym, closes in series["closes"].items():
        r = quant.log_returns(closes[-(quant.CORR_WINDOW + 1):])
        corr_obs[sym] = min(len(r), len(btc_rets))
        if sym == "BTC":
            corr_btc[sym], beta_btc[sym] = 1.0, 1.0
            continue
        corr_btc[sym] = quant.pearson(r, btc_rets)
        beta_btc[sym] = quant.beta(r, btc_rets)
    n_corr = sum(1 for v in corr_btc.values() if v is not None)
    print(f"[quant] BTC correlation for {n_corr}/{len(corr_btc)} symbol(s) over "
          f"<= {quant.CORR_WINDOW} nights ({len(btc_rets)} BTC returns available)",
          file=__import__("sys").stderr)

    # Liquidity shock takes tonight's turnover as the observation and the ledger as the
    # baseline, so it is computed here where tonight's value exists rather than inside
    # _series_from_ledger where it does not.
    live_turnover = {
        (t.get("symbol") or "").upper():
            round((t.get("total_volume") or 0) / t["market_cap"] * 100, 2)
        for t in markets if t.get("market_cap")}
    shock = {sym: quant.liquidity_shock(vals, live_turnover.get(sym))
             for sym, vals in series["turnover"].items()}
    n_shock = sum(1 for v in shock.values() if v.get("shock"))
    print(f"[quant] {n_shock} symbol(s) show a turnover collapse of "
          f"{quant.LIQ_SHOCK_Z} sigma or worse against their own baseline",
          file=__import__("sys").stderr)

    # BTC = market-neutral reference for multi-timeframe relative strength.
    btc = next((m for m in markets if (m.get("symbol") or "").upper() == "BTC"), None)
    basket = build_basket(markets, today, btc)
    rows = []
    seen = set()
    for t in markets:
        sym = (t.get("symbol") or "").upper()
        if not sym or sym in seen or sym in STABLES:
            continue
        seen.add(sym)
        era, conv, sig, comp = score(t, perps_map, btc)
        pm = lavl_perp_mult(sym, perps_map)
        # The same modifier score() applied, with the sentence explaining why. The
        # multiplier is recorded as perp_mult; the reason goes to funding.json, because
        # a 0.85 on screen cannot distinguish "penalised for crowding" from "the feed
        # was absent" and those are opposite facts about an asset.
        fc = funding.funding_context(sym, consolidated,
                                     t.get("price_change_percentage_24h"),
                                     rsi_map.get(sym))
        # Same function, same confirmations, trailing carry instead of tonight's print.
        # Nothing downstream reads this.
        trail_apr = (trail_map.get(sym) or {}).get("mean")
        pm_trail, _ = funding.regime_modifier(
            trail_apr, t.get("price_change_percentage_24h"), rsi_map.get(sym))
        b = dune_b.get(sym)  # real Dune fields if present, else None -> null
        rows.append({
            "date": today, "symbol": sym, "name": t.get("name", ""),
            "price": t.get("current_price") or 0, "market_cap": t.get("market_cap") or 0,
            "turnover_pct": round((t.get("total_volume", 0) / t.get("market_cap", 1)) * 100, 2) if t.get("market_cap") else 0,
            "erosion_ratio": round(era, 3), "conviction": conv, "signal": sig,
            "rs7": comp["rs7"], "rs14": comp["rs14"], "rs30": comp["rs30"],
            "rs200": comp["rs200"], "rs_blend": comp["rs_blend"],
            "c_liquidity": comp["liquidity"], "c_era": comp["era"],
            "c_depth": comp["depth"], "c_momentum": comp["momentum"],
            "unlocks_usd": b["unlocks_usd"] if b else None,
            "supply_increase_pct": b["supply_increase_pct"] if b else None,
            "addr_growth_pct": b["addr_growth_pct"] if b else None,
            "era": b["era"] if b else None,
            **dune_context(b, t.get("market_cap")),
            "roi_30d": None, "roi_90d": None, "survived": None,
            "perp_mult": round(pm, 3),
            "spec_hash": SPEC_HASH,
            **perp_context(sym, perps_map, t.get("market_cap"),
                           t.get("price_change_percentage_24h"), prev_oi),
            # The daily bar, for ATR and choppiness once enough of them exist.
            "high_24h": t.get("high_24h"),
            "low_24h": t.get("low_24h"),
            # Module 3. Only the flat columns: the per-venue breakdown is a nested
            # object and lives in funding.json. `score_modifier` is deliberately NOT a
            # column of its own — it is already recorded as `perp_mult`, which is what
            # score() actually multiplied by, and two columns holding the same number
            # is two columns that can disagree.
            "funding_interval_basis": (consolidated.get(sym) or {}).get("interval_basis"),
            **{k: fc[k] for k in ("funding_apr", "funding_interval_h", "funding_venue",
                                  "funding_venues_n", "funding_apr_spread",
                                  "funding_regime", "rsi7")},
            "funding_apr_trail": trail_apr,
            "funding_trail_n": (trail_map.get(sym) or {}).get("n"),
            "funding_pos_share": (trail_map.get(sym) or {}).get("pos_share"),
            "perp_mult_trail": round(pm_trail, 3),
            "liq_longs_usd": (liq_map.get(sym) or {}).get("longs_usd"),
            "liq_shorts_usd": (liq_map.get(sym) or {}).get("shorts_usd"),
            "liq_imbalance": (liq_map.get(sym) or {}).get("imbalance"),
            # Module F. emission_mult is the multiplier score() applied a few lines
            # above; emission_drag is the severity behind it and fdv_usd is the input
            # both came from. All three, because a modifier recorded without its input
            # cannot be re-derived, and an empty drag has to be distinguishable from a
            # zero one — see the FIELDS comment.
            "emission_drag": comp["emission_drag"],
            "emission_mult": comp["emission_mult"],
            "fdv_usd": t.get("fully_diluted_valuation"),
            # Module G — observational.
            **{k: (trend.get(sym) or {}).get(v) for k, v in
               (("adx", "adx"), ("plus_di", "plus_di"), ("minus_di", "minus_di"),
                ("adx_regime", "regime"), ("adx_bars", "bars"), ("atr14", "atr14"),
                ("strategy", "strategy"))},
            # Module H — observational.
            "corr_btc": corr_btc.get(sym),
            "beta_btc": beta_btc.get(sym),
            "corr_obs": corr_obs.get(sym),
            # Module I — observational.
            "turnover_z": (shock.get(sym) or {}).get("z"),
            "liq_shock": (shock.get(sym) or {}).get("shock"),
            # Module J. Filled after the loop, once the board has been ranked — the
            # divergence is between two RANKINGS and neither exists per row.
            "trending_rank": None, "tmd_divergence": None, "tmd_label": None,
        })
    rows.sort(key=lambda r: r["conviction"], reverse=True)

    # Module J, now that conviction exists for every row. The intel artifact is built
    # from the same call so the per-row columns and the panel can never disagree about
    # which ranking they compared against.
    conv_map = {r["symbol"]: r["conviction"] for r in rows}
    intel = _compute_market_intel(
        {**intel_feeds["feeds"], "_session": intel_feeds["session"]},
        series, conv_map, today, board_syms=[r["symbol"] for r in rows[:CORR_BOARD_N]])
    tmd_by_sym = {a["symbol"]: a for a in (intel["trending"].get("assets") or [])}
    for r in rows:
        a = tmd_by_sym.get(r["symbol"])
        if a:
            r["trending_rank"] = a["trending_rank"]
            r["tmd_divergence"] = a["divergence"]
            r["tmd_label"] = a["label"]
    n_tmd = sum(1 for r in rows if r["trending_rank"] is not None)
    print(f"[quant] {n_tmd} board name(s) also appear on the trending list of "
          f"{intel['trending'].get('n_trending', 0)}",
          file=__import__("sys").stderr)

    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    # Replace today's rows rather than appending them. A blind append made a second run
    # on the same day a second copy of that day's board: 2026-08-02 was recorded nine
    # times and 2026-08-03 twice, 460 duplicate (date, symbol) pairs in all. Anything
    # reading this as a daily series then counts one day nine times and computes returns
    # between a day and itself. Prior days are untouched — this replaces a re-run, it
    # does not rewrite history.
    kept = [r for r in _read_signals_rows() if r.get("date") != today]
    fresh = [{k: r.get(k) for k in FIELDS} for r in rows[:50]]
    with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(kept + fresh)

    # Backfill ROI from today's live prices for aged rows
    live = { (t.get("symbol") or "").upper(): t.get("current_price") or 0 for t in markets }
    all_rows = []
    if LEDGER_CSV.exists():
        with LEDGER_CSV.open(newline="", encoding="utf-8") as f:
            all_rows = list(csv.DictReader(f))
    updated = 0
    for r in all_rows:
        cur = live.get(r["symbol"])
        if not cur or not r["price"] or r["price"] in ("0", "0.0"):
            continue
        age = (date.today() - date.fromisoformat(r["date"])).days
        roi = (cur - float(r["price"])) / float(r["price"])
        if r.get("roi_30d") in (None, "", "None") and age >= 30:
            r["roi_30d"] = round(roi * 100, 2); updated += 1
        if r.get("roi_90d") in (None, "", "None") and age >= 90:
            r["roi_90d"] = round(roi * 100, 2); r["survived"] = "True" if cur > 0 else "False"; updated += 1
    with LEDGER_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
        for r in all_rows:
            # normalize: keep only current FIELDS, fill missing with None
            w.writerow({k: r.get(k) for k in FIELDS})

    # Rewrite JSON summary
    deciles = {i: {"count": 0, "survived": 0, "roi_30d": [], "roi_90d": []} for i in range(1, 11)}
    for r in all_rows:
        try:
            cv = int(r["conviction"] or 0)
        except ValueError:
            cv = 0
        d = deciles[max(1, min(10, (cv // 10) + 1))]
        d["count"] += 1
        if r.get("survived") == "True":
            d["survived"] += 1
        for k, key in (("roi_30d", "roi_30d"), ("roi_90d", "roi_90d")):
            v = r.get(k)
            if v not in (None, "", "None"):
                try:
                    d[key].append(float(v))
                except ValueError:
                    pass
    summary = {
        "total_signals": len(all_rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "by_decile": {str(k): {
            "count": v["count"], "survived": v["survived"],
            "win_rate": round(v["survived"] / v["count"], 3) if v["count"] else 0,
            "avg_roi_30d": round(sum(v["roi_30d"]) / len(v["roi_30d"]), 3) if v["roi_30d"] else None,
            "avg_roi_90d": round(sum(v["roi_90d"]) / len(v["roi_90d"]), 3) if v["roi_90d"] else None,
        } for k, v in deciles.items()},
        "rows": all_rows,
    }
    with LEDGER_JSON.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # Module 3 artifact. Written whatever the venues did — a file that only appears on
    # good nights makes "no funding tonight" indistinguishable from "the step did not
    # run", and the terminal has to be able to say which.
    fund_assets = []
    for r in fresh:
        sym = r["symbol"]
        rec = consolidated.get(sym) or {}
        apr = r.get("funding_apr")
        mult, reason = funding.regime_modifier(
            apr, live_chg.get(sym), rsi_map.get(sym))
        fund_assets.append({
            "symbol": sym,
            "price": r.get("price"),
            # The rate rebased to an 8-hour equivalent, which is the only form in which
            # venues on different clocks can be put in one column. The raw per-interval
            # rate is beside it, with the interval that gives it meaning.
            "funding_rate_8h": (round(apr / (3 * 365 * 100.0), 8)
                                if apr is not None else None),
            "funding_rate_raw": r.get("funding_rate"),
            "interval_hours": r.get("funding_interval_h"),
            "funding_apr": apr,
            "regime": r.get("funding_regime"),
            "score_modifier": mult,
            "modifier_reason": reason,
            "rsi7": r.get("rsi7"),
            "price_chg_24h": live_chg.get(sym),
            "venue": r.get("funding_venue"),
            "venues_n": r.get("funding_venues_n"),
            "apr_spread": r.get("funding_apr_spread"),
            "interval_basis": r.get("funding_interval_basis"),
            "gap_filled": (consolidated.get(sym) or {}).get("gap_filled", False),
            "oi_usd": r.get("oi_usd"),
            # Severity and confirmation, published rather than left to be inferred from
            # the multiplier. Two assets can land on the same 0.93 from very different
            # places — extreme carry barely confirmed, or moderate carry fully confirmed
            # — and the modifier alone cannot distinguish them.
            "severity": round(funding.funding_severity(apr), 4),
            "apr_trail": r.get("funding_apr_trail"),
            "score_modifier_trail": r.get("perp_mult_trail"),
            "trail_n": r.get("funding_trail_n"),
            "pos_share": r.get("funding_pos_share"),
            "by_venue": rec.get("by_venue") or {},
        })
    applied = {}
    for a in fund_assets:
        if a["score_modifier"] != 1.0:
            applied[a["symbol"]] = a["score_modifier"]
    funding_payload = {
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spec_hash": SPEC_HASH,
        # http_status and policy_blocked are recorded per night so "is Binance
        # permanently blocked from this runner or was that one bad evening" becomes a
        # question the ledger answers rather than one someone remembers.
        "venues": {n: {"status": rep["status"], "detail": rep["detail"],
                       "markets": len(rep["data"]),
                       "http_status": rep.get("http_status"),
                       "policy_blocked": rep.get("policy_blocked", False)}
                   for n, rep in venue_reports["venues"].items()},
        "consolidated_markets": len(consolidated),
        # Published so the dashboard reads its colour bands and its badge cut-offs from
        # the engine rather than from a second copy of the numbers in JavaScript. A
        # threshold duplicated in two languages is a threshold that will disagree with
        # itself, which is the exact failure the parity gate exists for on the score.
        "thresholds": {
            "overheated_apr": funding.REGIME_OVERHEATED,
            "elevated_apr": funding.REGIME_ELEVATED,
            "neutral_floor_apr": funding.REGIME_NEUTRAL_FLOOR,
            "squeeze_apr": funding.REGIME_SQUEEZE,
            "overheated_price_chg": funding.MOD_OVERHEATED_PRICE_CHG,
            "squeeze_rsi": funding.MOD_SQUEEZE_RSI,
            "squeeze_rsi_full": funding.MOD_SQUEEZE_RSI_FULL,
            "max_penalty": funding.MOD_MAX_PENALTY,
            "max_boost": funding.MOD_MAX_BOOST,
            "unconfirmed_weight": funding.MOD_UNCONFIRMED_WEIGHT,
            "squeeze_saturation_apr": funding.MOD_SQUEEZE_SATURATION,
        },
        "carry": {
            "taker_fee_pct": funding.CARRY_TAKER_FEE_PCT,
            "slippage_pct": funding.CARRY_SLIPPAGE_PCT,
            "fills": funding.CARRY_FILLS,
            "hold_days": funding.CARRY_DEFAULT_HOLD_DAYS,
        },
        "modifiers_applied": applied,
        "assets": fund_assets,
        "carry_screen": funding.carry_screen(consolidated),
    }
    FUNDING_JSON.write_text(json.dumps(funding_payload, indent=2))
    print(f"[funding] {len(applied)} asset(s) carried a non-neutral score modifier"
          + (": " + ", ".join(f"{k} x{v}" for k, v in sorted(applied.items()))
             if applied else " — every asset scored at 1.0"))

    # --- market context artifacts ------------------------------------------
    # The three append-only ledgers first, then the derived artifact. Order matters
    # only in that the CSVs are what tomorrow's multi-day columns will read; tonight's
    # `intel` was computed against the file as it stood BEFORE this write, which is
    # correct — a 7d sector flow must not include tonight's row at both ends of its own
    # window.
    n_sec = _append_context_rows(SECTORS_CSV, SECTOR_FIELDS, today, [
        {"date": today, "category_id": r["id"], "name": r["name"], "mcap": r["mcap"],
         "volume_24h": r["volume_24h"], "turnover": r["turnover"],
         "chg24h": r["chg24h"], "rs24h": r["rs24h"], "coins_count": r["coins_count"]}
        for r in intel["sectors"]["sectors"]])
    m = intel["macro"]
    _append_context_rows(MACRO_CSV, MACRO_FIELDS, today, [{
        "date": today,
        **{k: m.get(k) for k in ("total_mcap", "total_mcap_ex_btc", "total_volume",
                                 "btc_dominance", "eth_dominance",
                                 "eth_btc_dominance_ratio", "mcap_chg_24h",
                                 "stable_mcap", "stable_volume", "stable_velocity")},
        "stable_regime": (m.get("stable_regime") or {}).get("regime"),
        # The board's own funding temperature, recorded beside the macro anchors so the
        # ribbon reads one file. Median rather than mean: funding APR has a fat right
        # tail and one 900% squeeze print would otherwise define the whole board's heat.
        "funding_heat_apr": _median([r.get("funding_apr") for r in fresh]),
        "board_mean_conviction": (round(sum(r["conviction"] for r in rows) / len(rows), 2)
                                  if rows else None),
    }])
    n_dex = _append_context_rows(DEX_CSV, DEX_FIELDS, today, [
        {"date": today, "network": r["network"], "pools": r["pools"],
         "reserve_usd": r["reserve_usd"], "volume_24h": r["volume_24h"],
         "vlr": r["vlr"]}
        for r in intel["dex"]["networks"]])
    MARKET_INTEL_JSON.write_text(json.dumps(intel, indent=2))
    sec = intel["sectors"]
    lead = sec["leaders"][0] if sec["leaders"] else None
    print(f"[intel] {n_sec} sector(s), {n_dex} network(s), "
          f"{len(intel['fallen_kings'])} fallen king(s), "
          f"{len(intel['liquidity_shocks'])} liquidity reading(s)"
          + (f" | leading sector: {lead['name']} "
             f"{lead['rs24h'] if lead['rs24h'] is not None else lead['chg24h']:+.2f}%"
             if lead else ""),
          file=__import__("sys").stderr)
    c = intel["correlation"]
    if c.get("effective_n") is not None:
        print(f"[intel] the top {c['n']} names behave like {c['effective_n']} "
              f"independent bet(s) (mean pairwise r {c['mean_correlation']})",
              file=__import__("sys").stderr)
    else:
        print(f"[intel] correlation pending — {c['n']} name(s) have "
              f"{quant.CORR_MIN_OBS}+ overlapping returns", file=__import__("sys").stderr)
    sr = intel["macro"]["stable_regime"]
    print(f"[intel] fiat bridge: {sr.get('regime')} — {sr.get('basis')}",
          file=__import__("sys").stderr)

    # Conviction time-series + breadth/dispersion/persistence (reviewer #3 + the
    # three differentiated signals). Derived purely from the signals ledger.
    breadth = _compute_market_breadth()
    if breadth:
        # Overnight tier transitions, split into real reclassifications and boundary
        # crossings. Attached to breadth rather than given its own file: it is the same
        # kind of derived-from-the-ledger diagnostic and the terminal already loads this.
        breadth["tier_diff"] = _compute_tier_diff()
        # Paper return of the published basket, chained across recorded days. Built from
        # signals.csv rather than index.json — see _compute_performance for why.
        breadth["performance"] = _compute_performance()
        MARKET_BREADTH_JSON.write_text(json.dumps(breadth, indent=2))
        # Operational condition of the pipeline, in its own file: it is a monitoring
        # artifact rather than a market view, and pinning it to breadth would couple the
        # two.
        mon = _compute_monitor(dune_report)
        if mon:
            MONITOR_JSON.write_text(json.dumps(mon, indent=2))
            counts = {}
            for c in mon["health"]:
                counts[c["status"]] = counts.get(c["status"], 0) + 1
            print("[monitor] " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                  + f"  (observations={mon['observations']})")
            for c in mon["health"]:
                if c["status"] in ("fail", "warn"):
                    print(f"  {c['status'].upper()}: {c['name']} — {c['detail']}")

        pf = breadth["performance"]
        if pf.get("legs"):
            print(f"[perf] {pf['legs']} leg(s), basket {pf['book_total']:+.2f}%"
                  + (f", {pf['benchmark']} {pf['benchmark_total']:+.2f}%"
                     if pf["benchmark_available"] else ", benchmark missing")
                  + (f", equal-weight {pf['equal_weight_total']:+.2f}%"
                     if pf.get("equal_weight_total") is not None else "")
                  + ("" if pf["renderable"] else
                     f" — below the {pf['min_days']}-day render threshold"))
            if pf.get("duplicates_collapsed"):
                print(f"[perf] collapsed {pf['duplicates_collapsed']} duplicate "
                      f"(date, symbol) rows — the workflow ran more than once on some days")
        td = breadth["tier_diff"]
        if td and not td.get("pending"):
            c = td["counts"]
            print(f"[tiers] {td['from']} -> {td['to']}: {c['tier_changes']} changed "
                  f"({c['real']} real, {c['marginal']} on <= {td['marginal_move']} pts)")

    # ---------------------------------------------------------------------
    # RWA workspace — deliberately LAST
    # ---------------------------------------------------------------------
    # Placement is the whole decision here. This adds about forty CoinGecko calls
    # against a keyless ceiling of roughly ten to fifteen a rolling minute, so it is
    # the feed most likely to meet a 429 tonight. Running it after every crypto
    # artifact is on disk means a rate limit costs the RWA board and can never cost a
    # page of the scored universe — the same priority argument the context feeds are
    # ordered by further up, applied to a larger consumer.
    #
    # It is also why this is urgent rather than merely new: /rwas/{id}/market_chart
    # answers 401 below the Basic plan, so the net-issuance series cannot be
    # backfilled. Every night this does not run is a night that is gone.
    #
    # rwa.snapshot() never raises on a feed failure — every fetch returns a report and
    # a failed report degrades the artifact — but the call is wrapped anyway, because
    # main() has no exception handler and a defect in a module added today must not be
    # able to stop a ledger that has been committing since August. A traceback here is
    # printed and the run still returns 0.
    try:
        rwa_art = rwa.snapshot()
        # .get(), not [] — snapshot()'s designed degradation path returns early when the
        # underlying universe is unavailable, and that payload carries no "graph" or
        # "board_gate". Indexing them turned the module's most careful behaviour into a
        # KeyError, which the except below would then report as though the RWA build had
        # crashed rather than declined.
        g = rwa_art.get("graph") or {}
        bg = rwa_art.get("board_gate") or {}
        if rwa_art.get("status") == "unavailable":
            print(f"[rwa] unavailable — {rwa_art.get('detail')}")
        else:
            print(f"[rwa] {rwa_art['status']} · {bg.get('ranked', 0)} ranked / "
                  f"{bg.get('graded', 0)} graded of {g.get('underlyings_ranked', 0)} "
                  f"underlying(s) · {g.get('wrappers_priced', 0)}/{g.get('wrappers_n', 0)} "
                  f"wrapper(s) priced · {g.get('unresolved_n', 0)} unresolved "
                  f"· spec {rwa_art.get('spec_hash')}")
        for name, rep in (rwa_art.get("feeds") or {}).items():
            if rep["status"] not in ("live", "unavailable"):
                print(f"[rwa] {name}: {rep['status']} — {rep['detail']}")
        if rwa_art.get("written"):
            print("[rwa] ledger: " + ", ".join(f"{k} {v} row(s)"
                                               for k, v in sorted(rwa_art["written"].items())))
        top = [r for r in (rwa_art.get("board") or [])
               if r.get("conviction") is not None][:5]
        if top:
            print("[rwa] " + " | ".join(
                f"{(r['symbol'] or '').upper()} {r['conviction']:.0f} {r['label']}"
                for r in top))
        board = rwa_art.get("board") or []
        moved = [r for r in board
                 if (r.get("flow") or {}).get("impulse") in (rwa.IMPULSE_MINTING,
                                                             rwa.IMPULSE_STRONG,
                                                             rwa.IMPULSE_REDEMPTION)]
        print(f"[rwa] issuance: {len(moved)} underlying(s) minted or redeemed against "
              f"{len(board)} on the board")
    except Exception as e:  # noqa: BLE001
        print(f"[rwa] FAILED — {type(e).__name__}: {e}")
        print("[rwa] the crypto ledger above is unaffected; tonight's issuance row is lost")

    print(f"Nightly {today}: wrote {len(rows[:25])} signals, backfilled {updated}. "
          f"Ledger total: {len(all_rows)}.")
    if basket.get("holdings"):
        hs = ", ".join(f"{h['symbol']} {h['weight']*100:.1f}%" for h in basket["holdings"])
        print(f"[basket] Top-10 conviction-weighted | rebalanced={basket.get('rebalanced')} | {hs}")
        print(f"[index] cumulative return tracked in ledger/index.json")
    return 0


# ---------------------------------------------------------------------------
# specification hash — computed LAST, deliberately
# ---------------------------------------------------------------------------
# This used to sit immediately after spec_hash() was defined, near the top of the file,
# and it was wrong in a way that is invisible until you look for it.
#
# spec() captures FUNCTIONS by parsing this file from disk, so their position does not
# matter. It captures CONSTANTS with globals().get(name) — which returns None for
# anything not yet executed. TIER_CUTS is defined around line 1080 and the emission
# anchors around line 555, both far below where the hash used to be computed, so both
# were captured as null on every row this repository has ever written.
#
# The consequence is precisely the failure the funding.py capture was added to fix:
# editing TIER_CUTS — the BUY/STRONG/HOLD boundaries themselves — would not have moved
# the hash, and the mechanism built to make scoring changes visible would have reported
# no change at all. It was also non-deterministic: SPEC_HASH taken at import and
# spec_hash() called a moment later returned different values, because by the second
# call the constants existed.
#
# Computing it here, after the whole module has executed, is the fix. Every SPEC_CONSTANT
# is defined by this point, and tests/test_persistence.py now asserts both that the
# import-time value equals a later call and that no captured constant is None — either
# check would have caught this on the day it was introduced.
#
# This necessarily moves the published hash, because the specification now contains five
# constants it always should have. That is a real re-segmentation and it is recorded as
# one rather than papered over.
SPEC_HASH = spec_hash()


if __name__ == "__main__":
    raise SystemExit(main())
