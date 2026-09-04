#!/usr/bin/env python3
"""Tokenized real-world assets: the wrapper graph, supply impulse, and market quality.

Why this is a separate module and a separate model
--------------------------------------------------
``nightly.score()`` measures a speculative crypto asset: turnover against a float that
can be inflated, emission drag against network adoption, perpetual funding as a
positioning read. Not one of those inputs means anything for a token whose entire job
is to be worth exactly one share of Nvidia. Turnover on a wrapper is a function of how
many people wanted equity exposure on-chain that day, not of how liquid the asset is;
"momentum" is the underlying company's momentum arriving through a pipe; and there is
no perpetual market. The crypto model already refuses pegged metals for this reason —
XAUT and PAXG are excluded from the scored universe — so extending it to 643 tokenized
equities would be extending a refusal.

So this is a second model, with its own inputs, its own thresholds, its own labels and
its own specification hash. Nothing here reaches ``nightly.score()`` and nothing there
reaches this. The two share a terminal and a ledger directory; they do not share a
number.

The one thing worth knowing before reading further
--------------------------------------------------
The residual this module records is the IMPLIED change in tokenized supply. It is a
derived quantity, not an observed one, and the distinction is the whole reason this
paragraph is long.

CoinGecko publishes a tokenized price and a tokenized market cap for each underlying and
no supply series at all. Market cap is price times units outstanding, so::

      MC_t                    U_t * P_t
    ---------------- = ------------------------------- = U_t / U_{t-1}
    MC_{t-1} * P_t/P_{t-1}    U_{t-1} * P_{t-1} * P_t/P_{t-1}

The price term cancels completely and what is left is the ratio of IMPLIED units — the
units the vendor's own two series imply, recovered from a pair neither of which is a
supply series.

WHAT THIS IS NOT. It is not a verified on-chain issuance fact, and this module must never
describe it as mint minus redemption, as net issuance, or as exact. That identity holds
only if CoinGecko's historical market cap is contemporaneous circulating units times the
same published price, with no supply revisions, no reclassification of what counts as
circulating, and no backfill. None of that is documented and none of it has been checked
against an issuance source, because there is no free issuance source for these tokens. A
vendor restating a supply figure would move this series without a single token being
minted, and nothing here could tell the difference.

So: the arithmetic is exact, the interpretation is an inference, and the label says
IMPULSE rather than ISSUANCE. Promoting it would require a token-supply or mint/burn feed
to corroborate against — which is a real and worthwhile next step, and is not this.

Read that way, every reading in the brief still falls out of the arithmetic:

    P +5% / MC +5%    ->  implied units unchanged  ->  no incremental adoption
    P +5% / MC +18%   ->  implied units +12.4%     ->  supply expanding
    P  flat / MC +12% ->  implied units +12%       ->  strong adoption
    P +8% / MC -3%    ->  implied units -10.2%     ->  supply contracting

Two consequences follow, and both are load-bearing below. Because it behaves as a supply
series it is cumulative and path-dependent, so a missing day breaks the chain and the
chain has to record its own gaps rather than compound across them. And because
``/rwas/{id}/market_chart`` answers HTTP 401 on every plan below Basic, the only history
that will ever exist for this series is the history this pipeline records. A night not
snapshotted is a night that cannot be recovered later at any price.

What the free tier can and cannot see
-------------------------------------
Verified against the live API on 2026-09-01, keyless, not from documentation:

    /rwas/list                    200   643 rows, symbol is a unique key
    /rwas/markets                 200   tokenized_market_data, 168-point hourly
                                        sparkline, ids= filter, issuer= filter
    /rwas/{id}                    200   metadata only — NO tokens[] without a plan
    /rwas/{id}/tickers            401   Basic plan or above
    /rwas/{id}/market_chart       401   Basic plan or above
    /rwas/issuers/list            200   34 issuers
    /rwas/issuers/{id}            200   tokens[] WITH platforms — the wrapper graph
    /coins/markets?ids=<wrapper>  200   every wrapper id resolves as an ordinary coin

That last line is the one that decides what this module can be. The paid ``/tickers``
endpoint was supposed to be the only source of per-wrapper prices; it is not, because
wrapper tokens are first-class coins. Per-wrapper price, 24h volume, market cap, chains
and a 7d sparkline are all free. What ``/tickers`` alone would add is per-venue bid/ask
and depth — so execution cost is the one component of this model that is declared and
unavailable rather than computed, and it says so on every row.

The trap in the aggregate
-------------------------
``tokenized_market_data.current_price`` is NOT a TradFi quote. It is a blend of the
tokenized wrappers themselves. Measured: for 18 underlyings the wrapper price equals
the published aggregate to the cent, and where only one wrapper is known the aggregate
collapses onto that wrapper and any "basis" against it is 0.00% by construction. IBM's
aggregate read 282.91 while both its wrappers sat at 233.43 and 238.57 — the aggregate
was being dragged by stale members.

So token-vs-equity dislocation cannot be computed from CoinGecko at all, and this
module does not pretend to. What it computes instead is wrapper-versus-wrapper: two
tokens that are each redeemable for the same share, priced against the median of the
*live* wrappers on that underlying rather than against the contaminated aggregate.
That is a real basis between two things a person can actually hold, and it is the half
of the question that does not need a second vendor.

Standard library only, matching nightly.py: this runs in CI with no install step.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_sibling(filename: str, modname: str):
    """Load a sibling module by path, compiling its source in process.

    Same reasoning as ``nightly._load_sibling``, which this mirrors: a plain
    ``import coingecko`` works when this file is run as a script and raises
    ImportError under pytest, and only one of the three ways this module is executed
    puts the repository root on ``sys.path``.
    """
    import importlib.util
    path = Path(__file__).resolve().parent / filename
    src = path.read_text(encoding="utf-8")
    spec_ = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec_)
    exec(compile(src, str(path), "exec"), mod.__dict__)  # noqa: S102
    return mod


cg = _load_sibling("coingecko.py", "rwa_coingecko")
quant = _load_sibling("quant.py", "rwa_quant")

LEDGER_DIR = Path(__file__).resolve().parent / "ledger"

# ---------------------------------------------------------------------------
# universe and pagination
# ---------------------------------------------------------------------------
# 643 underlyings at 250 a page is three calls. Measured: pages 1-3 return 250/250/142
# and page 4 returns []. `sparkline=true` at that page size costs 380KB and 0.94s, which
# is nothing, and the sparkline is the entire input to the off-hours reading — so it is
# always requested rather than made conditional.
MARKETS_PER_PAGE = 250
MARKETS_MAX_PAGES = 4          # one more than needed, so a growing universe is not silently truncated
# Spacing between calls, chosen from the PROBED plan rather than fixed. Keyless
# CoinGecko tolerates roughly 10-15 requests a rolling minute; a Demo key is documented
# at 30. One constant for both means either the keyed run wastes four minutes of the
# job's budget or the keyless run spends it in 429 backoff, and this module makes about
# forty-five calls a night so the difference is the whole runtime.
#
# The 429 itself is a proper JSON body with a retry-after header — the plain-text
# "Throttled" body at HTTP 200 that an early probe warned about did not reproduce across
# sixty calls including a deliberate burst, so nothing here keys off it. What does guard
# against it is that every parser checks the shape it got before indexing.
FETCH_DELAY_KEYLESS = 6.5
FETCH_DELAY_KEYED = 2.5
FETCH_DELAY = FETCH_DELAY_KEYLESS      # the safe default when no session is in hand


def fetch_delay(session: dict | None) -> float:
    """Seconds to wait between calls for this session's plan."""
    plan = (session or {}).get("plan")
    return FETCH_DELAY_KEYED if plan in ("demo", "pro") else FETCH_DELAY_KEYLESS

# /rwas/list reports 643 rows; markets pagination exposes 642. One underlying is
# reachable by name and not by the ranked feed. That is recorded as a named absence in
# the artifact rather than reconciled away, because a count that silently disagrees with
# its own source is the kind of thing that is discovered six months later.
LIST_MARKETS_GAP_EXPECTED = 1

# ---------------------------------------------------------------------------
# the wrapper -> underlying join
# ---------------------------------------------------------------------------
# The conclusion this block used to carry was "resolution is by SYMBOL, never by id",
# and it was measured — on 262 tokens from two issuers. It was also wrong, and the way it
# was wrong is worth keeping: the sample was xStocks and Dinari, whose conventions are
# opposite enough to feel like a population, and against them the ticker rules resolved
# 260 of 262 while the id rules managed 76/130 and 121/132. Every id rule was discarded
# on that evidence.
#
# The live graph is 1,073 tokens from 34 issuers. The ticker rules resolve 460 of them.
# What the sample could not show is that Ondo alone is 438 tokens and Anchored 80, each
# with its own affix, and that the id carries the answer for 930 of the 1,073. Nothing
# raised when the rules were applied to the whole graph — the join simply returned
# nothing for 613 wrappers, the graph built without them, and every dispersion,
# integrity score and basis computed afterwards was over the subset that happened to
# match two issuers' naming.
#
# Resolution is TWO-SIDED, and that is a measurement rather than a preference. Two
# independent sources are consulted — the token's ticker and the token's id — and where
# both fire they must agree. Across the whole live graph of 1,073 tokens from 34 issuers:
#
#   ticker rules alone                      460/1073   42.9%
#   id-prefix rule alone                     930/1073   86.7%
#   both, where both fire                    383/385    99.5% agreement
#   combined, with the shelf and name rules 1063/1073   99.1%
#
# The first number is why one source is not enough. A single-issuer sample makes the
# ticker rules look complete — they resolve 260 of 262 tokens across xStocks and Dinari —
# and they collapse to 43% the moment Ondo's 438 tokens and Anchored's 80 are included,
# because every issuer has its own affix. Nothing raises when that happens: the graph
# still builds and 613 wrappers are simply missing from it.
#
# THE ORDER IS LOAD-BEARING TWICE OVER.
#
# First, within the ticker rules, the exact symbol is always tried before any stripped
# form. 32 of the 643 underlyings have tickers that themselves end in 'x' (CVX, EQIX,
# FCX, COPX, BITX, ARKX, BOXX) and 42 begin with 'b'. Strip first and Dinari's BITX
# token silently becomes 'bit' forever.
#
# Second, the name rule for commodities runs LAST, after every other rule has failed.
# Sixteen tokens in the live graph would be captured by it otherwise — "SPDR Gold Shares
# aStock", "iShares Silver Trust (Dinari Tokenized ETF)", "VanEck Gold Miners ETF" — and
# every one of them is a wrapper of an ETF, not of the metal. Pricing them against spot
# gold would put two different instruments in the same dispersion.
JOIN_EXACT = "exact"           # symbol is the underlying ticker verbatim (Dinari)
JOIN_X_SUFFIX = "x_suffix"     # ticker + 'x' (xStocks)
JOIN_W_PREFIX = "w_prefix"     # 'w' + ticker (wrapped variants)
JOIN_W_X = "w_prefix_x_suffix"  # 'w' + ticker + 'x'
JOIN_B_PREFIX = "b_prefix"     # 'b' + ticker (Backed Finance: bIB01, bIBTA, bCSPX)
JOIN_ID_PREFIX = "id_prefix"   # the underlying id is a prefix of the token id
JOIN_SHELF = "shelf_affix"     # an issuer's affix, gated on that issuer's id suffix
JOIN_COMMODITY = "commodity_name"  # last resort, and only for the two metals
JOIN_CONFLICT = "conflict"     # both sources fired and disagreed
JOIN_UNRESOLVED = "unresolved"
JOIN_RULES = (JOIN_EXACT, JOIN_X_SUFFIX, JOIN_W_PREFIX, JOIN_W_X, JOIN_B_PREFIX,
              JOIN_ID_PREFIX, JOIN_SHELF, JOIN_COMMODITY)

# Each issuer brands its tokens with its own affix, and the affix is only safe to apply
# to that issuer's tokens. Gating each one on the id suffix that identifies the shelf is
# what makes a single-letter affix safe: 'a' can only be stripped from a token whose id
# ends in '-astock', so it can never fire on an unrelated ticker. Read off the live
# graph, not from documentation.
SHELF_AFFIXES = (
    ("-astock", "prefix", "a"),                       # Anchored:  aAAPL, aGOOGL   (80)
    ("-bstocks-tokenized-stock", "suffix", "b"),      # bStocks:   GOOGLB, TSMB    (52)
    ("-coinbase-tokenized-stock", "suffix", "c"),     # Coinbase:  GOOGLC, METAC    (4)
    ("-st0x-tokenized-etf", "prefix", "wt"),          # ST0x:      wtQQQM, wtSGOV   (6)
    ("-st0x", "prefix", "wt"),
    ("-ondo-tokenized-stock", "suffix", "on"),        # Ondo:      ABTON, SPYON   (438)
    ("-ondo-tokenized-etf", "suffix", "on"),
    ("-ondo-tokenized", "suffix", "on"),
)

# The metals, and only the metals. /rwas/list carries exactly two commodities — gold
# (XAU) and silver (XAG) — and their wrappers are brand names no affix rule can reach:
# PAXG, XAUT, KAU, XAUM, CGO, GGBR, JPGC, VNXAU. Their NAMES, however, all contain the
# metal. Word-boundary anchored so "Goldman Sachs" and "B2Gold" cannot match, and applied
# only after every other rule has failed, so a gold ETF's wrapper resolves to the ETF.
COMMODITY_NAME_PATTERNS = (("gold", r"\bgold\b"), ("silver", r"\bsilver\b"))

# A trailing exchange suffix is a rule, not an exception: /rwas/list carries LSE lines
# as 'ib01.l' and 'ibta.l' while the wrapper tokens spell them BIB01 and BIBTA. Stripping
# the suffix is what makes those two resolve, and it is the same transformation any
# future non-US listing will need.
EXCHANGE_SUFFIXES = (".l",)

# ---------------------------------------------------------------------------
# liveness gates
# ---------------------------------------------------------------------------
# A wrapper below this much 24h volume is not a market. Measured: 60 of 130 xStocks
# wrappers clear it, and Dinari's inventory sits at $8.59 of daily volume against rows
# last updated three weeks ago. Those tokens are real and belong in the graph — they are
# how you see that an issuer's shelf is dead — but they must never price a dislocation
# or move a score.
WRAPPER_LIVE_VOL_USD = 10_000.0
# Freshness. A price stamped a day and a half ago is a historical fact, not a quote.
WRAPPER_STALE_HOURS = 36.0
# Below two live wrappers there is no cross-section: one wrapper is its own median and
# its dispersion is 0.00% by construction, which is the same defect the published
# aggregate has.
DISLOCATION_MIN_LIVE = 2

# WRAPPERS DO NOT ALL DENOMINATE THE SAME QUANTITY, and CoinGecko publishes no unit
# metadata anywhere in the RWA endpoints. This was found by running the engine over the
# live graph and reading what it produced:
#
#   gold      PAXG   $4,435.90   one troy ounce
#             XAUM   $4,430.30   one troy ounce
#             XGZ      $142.70   one gram      (4435.90 / 31.1035 = 142.62)
#             KAU      $142.95   one gram
#             GGBR       $4.43   one thousandth of an ounce
#   netflix   NFLXON   $810.92   one share
#             NFLXB     $80.73   one tenth of a share
#             NFLXX     $81.56   one tenth of a share
#
# Priced naively against a common median, PAXG reported a +300,311bp "dislocation" at
# confidence 100 against a market that is functioning perfectly. That is precisely the
# class of number this repository exists to not publish: nothing threw, the row looked
# like every other row, and it was the largest signal on the board.
#
# There is no honest way to recover the denominations from this data — a ratio near
# 31.1035 is suggestive and a ratio near 10 could be a unit or a genuine mispricing, and
# guessing would be inventing the metadata the vendor does not supply. So the engine
# refuses instead: wrappers are compared only against others within a tolerance band of
# the DEEPEST live wrapper, and everything outside it is reported as a different
# denomination rather than as a basis. The band is wide enough that no plausible real
# dislocation is discarded and far narrower than the smallest unit ratio seen (10x).
DENOMINATION_TOLERANCE = 0.25

# ...and a ratio outside the band is NOT automatically a unit either. That was the first
# version of this guard and it was too generous: a wrapper that had genuinely broken its
# peg by 40% would have been filed as "a different denomination, not a basis" and
# disappeared from the tape — the guard against a fabricated signal quietly suppressing a
# real one.
#
# So a ratio is only called a denomination when a UNIT CONVENTION explains it. These are
# facts rather than fitted parameters: 31.1035 grams to a troy ounce, and the decimal
# splits every fractional-share token uses. Anything else is reported as an unexplained
# divergence and says so — the honest answer, because from price alone a 0.7x ratio could
# be a 0.7-unit token or a token 30% off its peg, and CoinGecko publishes nothing that
# separates them.
GRAMS_PER_TROY_OUNCE = 31.1035
UNIT_RATIOS = (10.0, 100.0, 1000.0, 10000.0, GRAMS_PER_TROY_OUNCE,
               GRAMS_PER_TROY_OUNCE * 10.0, GRAMS_PER_TROY_OUNCE * 1000.0)
# How close a ratio must sit to a convention to be called one. 2% absorbs the real basis
# between two tokens on the same underlying without reaching the next convention.
UNIT_RATIO_TOLERANCE = 0.02


def unit_explanation(ratio):
    """The unit convention that accounts for ``ratio``, or None if none does.

    Checked both ways round, because which side is the smaller unit depends only on
    which wrapper happened to be deepest.
    """
    r = _num(ratio)
    if r is None or r <= 0:
        return None
    for u in UNIT_RATIOS:
        for candidate in (u, 1.0 / u):
            if abs(r / candidate - 1.0) <= UNIT_RATIO_TOLERANCE:
                return (f"1/{u:g}" if candidate < 1 else f"{u:g}x") + (
                    " (grams per troy ounce)" if abs(u - GRAMS_PER_TROY_OUNCE) < 1e-6
                    else "")
    return None
# A basis narrower than this is not worth a row. Wrapper prices are published to the
# cent, so on a $90 share one tick is already ~1bp; 25bp is comfortably above the
# quantisation and below any spread a person would actually cross.
DISLOCATION_MIN_BPS = 25.0

# What this engine emits, named for what it is. A price gap between two wrappers of the
# same underlying is a PRE-EXECUTION observation: it is measured on last prices, and last
# prices say nothing about whether the gap survives a spread, a depth-limited fill or a
# transfer between chains. Calling it an executable dislocation would be claiming the
# three things this plan cannot see.
#
# A row is only ever promoted to EXECUTABLE by a future run that has /rwas/{id}/tickers
# AND passes an explicit friction gate. Nothing in this module can set that state, which
# is why `executable_after_friction` is None here rather than False — False would assert
# a test was run and failed; None says it was never run.
DIVERGENCE_STAGE = "PRE_EXECUTION"
EXECUTABLE_STAGE = "EXECUTABLE"          # reserved; unreachable without a ticker feed

# The board ranks underlyings that have at least one wrapper anyone is trading. Without
# this the board is a 642-row dump in which the top of the ranking is decided by tokens
# with three digits of daily volume.
BOARD_MIN_LIVE_WRAPPERS = 1

# The floor the live smoke test fails below. 99.3% of 1,073 tokens resolved when this was
# measured, and the eight that did not wrap underlyings /rwas/list does not carry.
JOIN_MIN_RESOLUTION_PCT = 97.0

# Wrapper ids go into a query string, and they are SLUGS rather than tickers: a mean of
# 33 characters and a maximum of 83, so 250 of them is a 9,200-character URI and the
# server answers `414 Request-URI Too Large` in HTML rather than JSON. That is what it
# actually did — a whole batch of 250 wrappers vanished from a run that reported itself
# as merely "partial", and the underlyings they belonged to dropped off the board with
# no indication that the cause was a URL length rather than a rate limit.
#
# So batches are cut by CHARACTER BUDGET, not by count. 3,000 leaves generous room under
# the 4KB that is the smallest limit in common use, and per_page is derived from the
# batch that results rather than assumed.
WRAPPER_QUERY_BUDGET = 3000
WRAPPER_CHUNK_MAX = 250          # CoinGecko's own per_page ceiling


def chunk_ids(ids: list, budget: int = WRAPPER_QUERY_BUDGET,
              cap: int = WRAPPER_CHUNK_MAX) -> list:
    """Split ids into batches that fit a query string. Never an empty batch."""
    out, cur, used = [], [], 0
    for i in ids:
        cost = len(i) + 1
        if cur and (used + cost > budget or len(cur) >= cap):
            out.append(cur)
            cur, used = [], 0
        cur.append(i)
        used += cost
    if cur:
        out.append(cur)
    return out

# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------
# Deliberately NOT the crypto vocabulary. STRONG / BUY / HOLD / WATCH / AVOID is an
# action ladder: it tells you what to do with a position. This model answers a different
# question — how healthy, liquid, distributed and coherently priced is this tokenized
# market — and an answer to that is a description of market structure, not an
# instruction. Reusing the crypto words would invite exactly the reading the product
# decision forbids, which is that an RWA score and a crypto score are the same number
# measured twice.
#
# So the vocabulary is states, and every word is one a market can be in:
RWA_DEEP = "DEEP"           # many live wrappers, tight cross-section, real volume
RWA_SOUND = "SOUND"
RWA_THIN = "THIN"
RWA_FRAGILE = "FRAGILE"     # one venue deep, or a cross-section that disagrees with itself
RWA_DORMANT = "DORMANT"     # tokenized in name only
RWA_UNRATED = "UNRATED"     # not a grade of zero — a refusal to grade
RWA_LABELS = (RWA_DEEP, RWA_SOUND, RWA_THIN, RWA_FRAGILE, RWA_DORMANT, RWA_UNRATED)

RWA_T_DEEP = 80.0
RWA_T_SOUND = 65.0
RWA_T_THIN = 50.0
RWA_T_FRAGILE = 35.0

# Tokenization impulse. These are readings of the IMPLIED supply change, not of price,
# and the words say so — none of them names a mint or a redemption, because this module
# cannot observe one. NEUTRAL is not "no signal": it is the specific and informative
# finding that market cap moved as much as price did, so the implied unit count did not
# move and the day was repricing rather than adoption.
IMPULSE_MINTING = "MINTING"
IMPULSE_STRONG = "STRONG_ADOPTION"    # supply grew while price did not
IMPULSE_NEUTRAL = "NEUTRAL"
IMPULSE_REDEMPTION = "REDEMPTION"
IMPULSE_UNREADABLE = "UNREADABLE"     # below the noise floor of the published inputs
IMPULSE_LABELS = (IMPULSE_MINTING, IMPULSE_STRONG, IMPULSE_NEUTRAL,
                  IMPULSE_REDEMPTION, IMPULSE_UNREADABLE)

# A daily impulse above this is a change the published inputs can actually resolve.
# CoinGecko rounds market cap to whole dollars and price to the cent, so on a $200 wrapper
# the price quantisation alone is 2.5bp; 0.5% a day is twenty times that and is still a
# small number against the 12% daily prints the brief's own worked examples contain.
IMPULSE_MIN_PCT = 0.5
# "Strong adoption" is the case where supply grew and price did not explain it. Below
# this much price movement the day is a supply event rather than a repricing.
IMPULSE_FLAT_PRICE_PCT = 1.0

# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
# Four components, weights summing to 100, and one fifth component that is declared and
# unavailable. Execution cost — bid/ask, cost-to-move-up, cost-to-move-down — needs
# per-venue depth from /rwas/{id}/tickers, which answers 401 below the Basic plan. It is
# named here rather than quietly omitted, because a model whose absent inputs are
# invisible is a model nobody can audit. When a key arrives it takes weight from the
# others and the specification hash moves, which is exactly the signal that should fire.
W_LIQUIDITY = 30.0
W_DISTRIBUTION = 25.0
W_IMPULSE = 25.0
W_INTEGRITY = 20.0
# Execution carries REAL WEIGHT and is currently unpriceable. That is the correction: an
# earlier version set this to 0.0 and left execution out of COMPONENT_WEIGHTS entirely,
# which silently redistributed its share across the other four and let a board with no
# execution evidence at all report 100% coverage. A component nobody can measure is not a
# component worth nothing — it is a hole, and the denominator has to know about it.
W_EXECUTION = 20.0
# Per-wrapper weights, same shape and the same rule: execution is declared and absent.
W_W_LIQ, W_W_INT, W_W_DIST, W_W_ISS, W_W_EXEC = 35.0, 30.0, 20.0, 15.0, 20.0
DECLARED_WEIGHTS = {"liquidity": W_LIQUIDITY, "distribution": W_DISTRIBUTION,
                    "impulse": W_IMPULSE, "integrity": W_INTEGRITY,
                    "execution": W_EXECUTION}
# What can be priced without /rwas/{id}/tickers. The gap between this and DECLARED_WEIGHTS
# is exactly what "coverage" reports, and on the free tier it can never close.
COMPONENT_WEIGHTS = {k: v for k, v in DECLARED_WEIGHTS.items() if k != "execution"}
EXECUTION_UNAVAILABLE = "UNAVAILABLE — venue depth/spread feed not connected"
SCORE_BASIS = "available_evidence_normalized"
SCORE_DEFINITION = {
    "score": ("AVAILABLE-EVIDENCE NORMALIZED: weighted mean of the components that "
              "produced a value, rescaled to 0-100 over the weight of those components "
              "only. A 94 is 94 on the evidence that was priced, not 94 of a fully "
              "evidenced 100."),
    "coverage": ("MODEL COVERAGE: priced declared weight / total declared weight. "
                 "Execution (20) is in the denominator and is never priced on this plan, "
                 "so the ceiling is 83.3% and the first night, with no impulse history, "
                 "reads 62.5%."),
    "effective": ("COVERAGE-ADJUSTED: score x coverage / 100. A plain product, published "
                  "beside the score for anyone who wants absent evidence to count against "
                  "the number. Not what the board ranks by."),
    "label": ("RWA SIGNAL: the band of the normalized score. DEEP / SOUND / THIN / FRAGILE "
              "/ DORMANT describe the tokenized market's structure and are the final "
              "signal — not a liquidity sub-classification. UNRATED is a refusal."),
    "wrapper_price_coverage": ("WRAPPER PRICE COVERAGE: wrappers priced / wrappers in the "
                               "graph, per run. A different denominator from model "
                               "coverage and reported separately."),
    "execution_evidence": EXECUTION_UNAVAILABLE,
}

# The score is a weighted mean over the components that actually produced a value,
# rescaled to 0-100, and stamped with the share of DECLARED weight that was priced. That
# is what lets the board be honest on night one, when the impulse component cannot exist
# because it needs a previous night AND execution cannot exist at all: coverage reads 60%,
# every row is affected identically, and the ranking is internally comparable.
#
# The floor is 50 rather than 60 because execution is 20 points of permanent absence on
# this plan. A 60 floor would have refused every row on night one — which is not caution,
# it is a model that cannot run in the only configuration it has.
RWA_MIN_COVERAGE = 50.0

# The published tape is the widest divergences, not every leg. Named here rather than
# written inline at the slice, so the artifact can report the same number it applied.
TAPE_CAP = 200

# WHICH KIND OF EVIDENCE EACH PUBLISHED FIELD IS, keyed by the field's own path on a board
# row. The terminal marks its column headers from this map.
#
# It is published rather than inferred because the distinction is one only this file can
# make. `price` and `market_cap` arrive from the vendor; `dispersion_bps` is computed here
# from a peer set this file assembled; `conviction` is rescaled over whichever components
# produced a value. Nothing in the shape of the numbers says which is which, and a
# column-to-evidence map written in JavaScript would be a second opinion about this file's
# own provenance — the exact substitution the evidence vocabulary exists to prevent.
#
# The vocabulary is the five states the terminal already uses. Only three appear here:
# `unavailable` and `unexplained` are properties of a particular row, never of a column.
EVIDENCE_OBSERVED = "observed"      # the vendor published this number
EVIDENCE_DERIVED = "derived"        # computed here from observed inputs
EVIDENCE_NORMALIZED = "normalized"  # rescaled or banded by the model
FIELD_EVIDENCE = {
    "symbol": EVIDENCE_OBSERVED,
    "name": EVIDENCE_OBSERVED,
    "asset_type": EVIDENCE_OBSERVED,
    "price": EVIDENCE_OBSERVED,
    "price_chg_pct_24h": EVIDENCE_OBSERVED,
    "market_cap": EVIDENCE_OBSERVED,
    "total_volume": EVIDENCE_OBSERVED,
    # Recovered from two observed series by an identity that is exact; reading it as
    # issuance is the inference, and impulse_provenance carries that argument in full.
    "flow.residual_usd": EVIDENCE_DERIVED,
    "flow.residual_pct": EVIDENCE_DERIVED,
    "flow.residual_pct_daily": EVIDENCE_DERIVED,
    "flow.expected_mcap": EVIDENCE_DERIVED,
    "flow.supply_index": EVIDENCE_DERIVED,
    "flow.impulse": EVIDENCE_DERIVED,
    "flow.span_days": EVIDENCE_DERIVED,
    "flow.chain_days": EVIDENCE_DERIVED,
    # Counts against a liveness gate this file applies, not a figure anyone published.
    "wrappers_live": EVIDENCE_DERIVED,
    "wrappers_n": EVIDENCE_DERIVED,
    "dislocation.dispersion_bps": EVIDENCE_DERIVED,
    "dislocation.median_price": EVIDENCE_DERIVED,
    "conviction": EVIDENCE_NORMALIZED,
    "conviction_effective": EVIDENCE_NORMALIZED,
    "coverage": EVIDENCE_NORMALIZED,
    "label": EVIDENCE_NORMALIZED,
}

# Liquidity is scored on a log scale because tokenized volume spans six orders of
# magnitude — $8 of Dinari inventory and $12M of wrapped NVDA are both in the universe —
# and a linear scale would put 640 rows in the bottom percent of the axis.
LIQ_FLOOR_USD = 10_000.0        # scores 0
LIQ_CEIL_USD = 25_000_000.0     # scores full marks
# Turnover: 24h wrapper volume over wrapper market cap. A tokenized share that turns over
# its whole float daily is not deep, it is a hot potato; the curve rewards the middle.
TURNOVER_HEALTHY_LO = 0.02
TURNOVER_HEALTHY_HI = 0.60

# Distribution. Chains and issuers count for more than raw wrapper count: five wrappers
# from one issuer on one chain is one point of failure wearing five hats.
DIST_ISSUERS_FULL = 3
DIST_CHAINS_FULL = 3
DIST_WRAPPERS_FULL = 4
# Herfindahl over wrapper volume share. At or above this one wrapper IS the market.
DIST_CONCENTRATED_HHI = 0.75

# Integrity. Cross-wrapper dispersion is the standard deviation of live wrapper prices
# around their own median, in basis points. Two tokens redeemable for the same share
# should not disagree; when they do, either one of them is stale or one of them is not
# really redeemable.
INTEGRITY_TIGHT_BPS = 20.0      # full marks at or below
INTEGRITY_BROKEN_BPS = 500.0    # zero at or above

# ---------------------------------------------------------------------------
# specification identity
# ---------------------------------------------------------------------------
# This model gets its OWN hash, separate from nightly.SPEC_HASH, and the separation is
# the point rather than an oversight. A crypto threshold change must not segment the RWA
# track record and an RWA threshold change must not segment the crypto one; a single
# shared hash would make every edit to either invalidate the history of both. The
# mechanism is the same one nightly.spec() uses and the reasoning it gives applies
# unchanged: without it, an information coefficient computed across a silent threshold
# change is a number about two different models.
RWA_SPEC_FUNCTIONS = (
    "comparable_set", "score_liquidity", "score_distribution", "score_impulse", "score_integrity",
    "rwa_conviction", "rwa_label", "flow_residual", "impulse_label",
    "wrapper_score", "dislocations",
    # The join is captured too, which nightly.spec() has no equivalent of. It is not a
    # scoring function, but it decides WHICH prices are compared to which — a change to
    # a rule here silently re-points a wrapper at a different company, and every
    # dispersion, basis and integrity score computed after it describes a different
    # question. That belongs in the digest for exactly the reason the thresholds do.
    "join_by_symbol", "join_by_id", "join_by_shelf", "join_by_commodity_name",
    "join_wrapper",
)
RWA_SPEC_CONSTANTS = (
    "W_LIQUIDITY", "W_DISTRIBUTION", "W_IMPULSE", "W_INTEGRITY", "W_EXECUTION",
    "RWA_MIN_COVERAGE", "RWA_T_DEEP", "RWA_T_SOUND", "RWA_T_THIN", "RWA_T_FRAGILE",
    "IMPULSE_MIN_PCT", "IMPULSE_FLAT_PRICE_PCT",
    "LIQ_FLOOR_USD", "LIQ_CEIL_USD", "TURNOVER_HEALTHY_LO", "TURNOVER_HEALTHY_HI",
    "DIST_ISSUERS_FULL", "DIST_CHAINS_FULL", "DIST_WRAPPERS_FULL",
    "DIST_CONCENTRATED_HHI", "INTEGRITY_TIGHT_BPS", "INTEGRITY_BROKEN_BPS",
    "WRAPPER_LIVE_VOL_USD", "WRAPPER_STALE_HOURS",
    "DISLOCATION_MIN_LIVE", "DISLOCATION_MIN_BPS", "BOARD_MIN_LIVE_WRAPPERS",
    "W_W_LIQ", "W_W_INT", "W_W_DIST", "W_W_ISS", "W_W_EXEC",
    "DENOMINATION_TOLERANCE",
    "SHELF_AFFIXES", "COMMODITY_NAME_PATTERNS", "EXCHANGE_SUFFIXES", "SCORE_BASIS",
)


def spec() -> dict:
    """A canonical form of everything that can change an RWA score.

    Same construction as ``nightly.spec()``: each scoring function is parsed from disk,
    its docstring stripped, and unparsed back to canonical source, so editing a
    threshold changes the digest and reflowing a comment does not. Read from disk rather
    than from ``sys.modules`` for the reason that file gives — the validator and the
    tests load this module under names that are never registered there.
    """
    import ast

    path = Path(__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = set(RWA_SPEC_FUNCTIONS)
    found = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in wanted:
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        stripped = ast.FunctionDef(
            name=node.name, args=node.args, body=body or [ast.Pass()],
            decorator_list=[], returns=None, type_comment=None,
            type_params=getattr(node, "type_params", []))
        ast.fix_missing_locations(stripped)
        found[node.name] = ast.unparse(stripped)
    missing = wanted - set(found)
    if missing:
        # A renamed scoring function must not silently drop out of the specification.
        raise RuntimeError(f"rwa.spec() cannot find scoring function(s): {sorted(missing)}")
    consts = {name: globals().get(name) for name in RWA_SPEC_CONSTANTS}
    return {"functions": found, "constants": consts}


def spec_hash() -> str:
    """Short stable digest of ``spec()``, recorded on every row this model writes."""
    import hashlib
    blob = json.dumps(spec(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------
def _num(v):
    """Float or None. Never 0.0 for an unparseable value.

    Same rule as ``coingecko._num`` and for the same reason: a missing market cap and a
    market cap of zero are different facts, and a residual computed against a zero that
    was really a None is a fabricated redemption event.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _iso(ts) -> datetime | None:
    """Parse the ISO-8601 stamps CoinGecko returns, tolerating the trailing Z."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_hours(ts, now: datetime | None = None):
    """Hours since ``ts``, floored at zero. None when the stamp is missing.

    None is not "fresh". Every caller treats an unknown age as failing the freshness
    gate, because a token whose feed stopped publishing a timestamp is exactly the token
    whose price should not be trusted.

    The floor is not cosmetic. ``now`` is captured once when a run starts, and the run
    takes several minutes — so a wrapper price fetched at the end of it carries a vendor
    timestamp AHEAD of the run's own clock, and the raw subtraction gives a negative age.
    Ninety-five of ninety-six tape legs on the first full run read -0.1h that way, which
    is not a price from the future: it is the ordering of a multi-minute fetch, and the
    honest report of it is "as fresh as this run can tell", which is zero.

    What this deliberately does NOT do is hide a real clock problem. A stamp hours ahead
    of the run still floors to zero and still passes the freshness gate — the gate is
    about staleness — but ``_clock_skew_hours`` below reports the largest such gap so a
    genuinely wrong clock is visible in the manifest rather than absorbed here.
    """
    dt = _iso(ts)
    if dt is None or now is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def _clock_skew_hours(stamps, now: datetime | None = None) -> float:
    """The largest amount by which a source timestamp leads ``now``, in hours.

    Expected to be a few minutes on any real run, because the run is not instantaneous.
    A large value means the vendor's clock or ours is wrong, and that is worth seeing.
    """
    if now is None:
        return 0.0
    lead = 0.0
    for ts in stamps:
        dt = _iso(ts)
        if dt is not None:
            lead = max(lead, (dt - now).total_seconds() / 3600.0)
    return round(lead, 3)


def normalise_symbol(sym: str) -> str:
    """Lowercase, trimmed, with any exchange suffix removed.

    /rwas/list carries London lines as 'ib01.l' while the wrapper token spells the same
    instrument BIB01. Stripping the suffix is a rule rather than a patch for those two
    rows: any future non-US listing arrives in the same shape.
    """
    s = (sym or "").strip().lower()
    for suf in EXCHANGE_SUFFIXES:
        if s.endswith(suf):
            return s[: -len(suf)]
    return s


# ---------------------------------------------------------------------------
# fetch layer
# ---------------------------------------------------------------------------
def fetch_list(session: dict, getter=None) -> dict:
    """``/rwas/list`` — the underlying universe. Free on every plan."""
    getter = getter or cg.get
    rep = getter(session, "/rwas/list")
    if rep["status"] != "live":
        return rep
    rows = rep["data"] if isinstance(rep["data"], list) else []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        out.append({"id": r["id"], "symbol": (r.get("symbol") or "").lower(),
                    "name": r.get("name"), "asset_type": r.get("asset_type")})
    if not out:
        return cg._report("empty", "/rwas/list returned no usable rows", [], 200)
    return cg._report("live", f"{len(out)} underlying(s)", out, 200)


def fetch_markets(session: dict, getter=None, sleep=None,
                  per_page: int = MARKETS_PER_PAGE, max_pages: int = MARKETS_MAX_PAGES,
                  ids: list | None = None) -> dict:
    """``/rwas/markets`` — the tokenized tape, with the 7d hourly sparkline.

    ``ids`` takes a comma-separated list and is undocumented but real: it was verified
    live, it composes with ``sparkline=true``, and unknown ids are silently dropped
    rather than erroring. That is what makes a cheap watchlist refresh possible; it is
    also why the caller must compare what came back against what it asked for rather
    than assuming a one-to-one response.

    Paged rather than fetched whole because 643 rows do not fit one response. The loop
    stops on the first short page, so a universe that grows past ``max_pages`` reports
    a truncation instead of quietly ranking a prefix.
    """
    getter = getter or cg.get
    import time as _time
    sleep = sleep or _time.sleep
    delay = fetch_delay(session)
    rows, pages_read, truncated = [], 0, False
    for page in range(1, max_pages + 1):
        params = {"vs_currency": "usd", "per_page": per_page, "page": page,
                  "sparkline": "true"}
        if ids:
            params["ids"] = ",".join(ids)
        rep = getter(session, "/rwas/markets", params)
        if rep["status"] != "live":
            # A partial universe is still worth recording — but only with the failure
            # attached, so tomorrow's residual is not computed against a page that was
            # missing for reasons nobody wrote down.
            if rows:
                return cg._report("partial",
                                  f"{len(rows)} row(s) over {pages_read} page(s); "
                                  f"page {page} failed: {rep['detail']}",
                                  rows, rep.get("http_status"))
            return rep
        page_rows = rep["data"] if isinstance(rep["data"], list) else []
        pages_read += 1
        rows.extend(r for r in page_rows if isinstance(r, dict) and r.get("id"))
        if len(page_rows) < per_page:
            break
        if page == max_pages:
            # Still a full page at the limit: there is more universe than was asked for.
            # Reported rather than absorbed, because a silently truncated ranking looks
            # exactly like a complete one.
            truncated = True
        if ids:
            break
        sleep(delay)
    if not rows:
        return cg._report("empty", "/rwas/markets returned no rows", [], 200)
    detail = f"{len(rows)} underlying(s) over {pages_read} page(s)"
    if truncated:
        detail += (f" — page {max_pages} came back full at the {max_pages}-page limit, "
                   f"so this is a prefix of the universe and not all of it")
    return cg._report("partial" if truncated else "live", detail, rows, 200)


def fetch_issuers(session: dict, getter=None, sleep=None) -> dict:
    """``/rwas/issuers/list`` then ``/rwas/issuers/{id}`` for each.

    The per-issuer call is the only free source of ``tokens[]`` — ``/rwas/{id}`` returns
    metadata and no token array below the Basic plan — so this loop IS the wrapper
    graph. One call per issuer, 34 of them today.

    ``updated_at`` on these responses is hourly ('2026-09-01T01:00:00Z') where the
    markets feed refreshes about every ten minutes. The graph is therefore rebuilt daily
    and never per-tick, and a wrapper listed in the last hour legitimately lags.
    """
    getter = getter or cg.get
    import time as _time
    sleep = sleep or _time.sleep
    delay = fetch_delay(session)
    rep = getter(session, "/rwas/issuers/list")
    if rep["status"] != "live":
        return rep
    listed = [r for r in (rep["data"] if isinstance(rep["data"], list) else [])
              if isinstance(r, dict) and r.get("id")]
    issuers, failures = [], []
    for i, row in enumerate(listed):
        if i:
            sleep(delay)
        one = getter(session, f"/rwas/issuers/{row['id']}")
        if one["status"] != "live" or not isinstance(one["data"], dict):
            failures.append(f"{row['id']}: {one['detail']}")
            continue
        d = one["data"]
        tokens = [t for t in (d.get("tokens") or []) if isinstance(t, dict) and t.get("id")]
        issuers.append({
            "id": d.get("id") or row["id"], "name": d.get("name") or row.get("name"),
            "market_cap": _num(d.get("market_cap")),
            "market_cap_change_24h": _num(d.get("market_cap_change_24h")),
            "volume_24h": _num(d.get("volume_24h")),
            "updated_at": d.get("updated_at"),
            "tokens": [{"id": t["id"], "symbol": (t.get("symbol") or "").lower(),
                        "name": t.get("name"),
                        "platforms": {k: v for k, v in (t.get("platforms") or {}).items() if k}}
                       for t in tokens],
        })
    if not issuers:
        return cg._report("empty",
                          "no issuer detail resolved (" + "; ".join(failures[:3]) + ")",
                          [], 200)
    status = "live" if not failures else "partial"
    detail = f"{len(issuers)}/{len(listed)} issuer(s), {sum(len(i['tokens']) for i in issuers)} token(s)"
    if failures:
        detail += f" — {len(failures)} issuer(s) failed: " + "; ".join(failures[:3])
    rep = cg._report(status, detail, issuers, 200)
    # The DENOMINATOR, carried alongside the data. Completeness is a fraction and the
    # caller cannot compute it from the returned list alone — 31 issuers and 33 issuers
    # both report "partial", and without this the two are indistinguishable to the
    # promotion rule that is supposed to prefer the fuller one.
    rep["listed_n"] = len(listed)
    return rep


def fetch_wrapper_coins(session: dict, coin_ids: list, getter=None, sleep=None,
                        chunk: int = WRAPPER_CHUNK_MAX,
                        budget: int = WRAPPER_QUERY_BUDGET) -> dict:
    """``/coins/markets?ids=`` over the wrapper tokens.

    This is the call that removes the paid dependency. Every wrapper id returned by
    ``/rwas/issuers/{id}`` is a first-class id in the ordinary coins namespace — 130/130
    resolved on the xStocks shelf, zero missing — so per-wrapper price, 24h volume,
    market cap and sparkline are free. What ``/rwas/{id}/tickers`` would add on top is
    per-venue bid/ask and depth, which is why execution cost is the one declared and
    unavailable component of this model.
    """
    getter = getter or cg.get
    import time as _time
    sleep = sleep or _time.sleep
    ids = [i for i in dict.fromkeys(coin_ids) if i]
    if not ids:
        return cg._report("empty", "no wrapper ids to price", {}, None)
    delay = fetch_delay(session)
    out, failures = {}, []
    batches = chunk_ids(ids, budget, chunk)

    def _run(batch, first):
        if not first:
            sleep(delay)
        rep = getter(session, "/coins/markets",
                     {"vs_currency": "usd", "ids": ",".join(batch),
                      "per_page": len(batch), "page": 1, "sparkline": "false"})
        if rep["status"] != "live":
            return rep["detail"]
        for r in (rep["data"] if isinstance(rep["data"], list) else []):
            if not isinstance(r, dict) or not r.get("id"):
                continue
            out[r["id"]] = {
                "id": r["id"], "symbol": (r.get("symbol") or "").lower(),
                "price": _num(r.get("current_price")),
                "market_cap": _num(r.get("market_cap")),
                "volume_24h": _num(r.get("total_volume")),
                "price_change_pct_24h": _num(r.get("price_change_percentage_24h")),
                "circulating_supply": _num(r.get("circulating_supply")),
                "last_updated": r.get("last_updated"),
            }
        return None

    retry = []
    for i, batch in enumerate(batches):
        err = _run(batch, first=(i == 0))
        if err:
            retry.append(batch)

    # One sweep back over the batches that failed, after a long pause. These are 250-id
    # calls and they are the heaviest thing this module asks for, so they are also the
    # first to meet a 429 — and losing one costs 250 wrappers, which drops every
    # underlying they belonged to off the board even though the tape for those
    # underlyings was fetched successfully. Observed keyless: two lost batches took the
    # board from 247 ranked to 48. One retry, not a loop, so a genuine outage still ends.
    if retry:
        sleep(delay * 4)
        still = []
        for batch in retry:
            err = _run(batch, first=True)
            if err:
                still.append(err)
        failures = still
    if not out:
        return cg._report("unreachable",
                          "no wrapper priced (" + "; ".join(failures[:2]) + ")", {}, None)
    status = "live" if not failures else "partial"
    detail = f"{len(out)}/{len(ids)} wrapper(s) priced"
    if failures:
        detail += f" — {len(failures)} batch(es) failed: " + "; ".join(failures[:2])
    return cg._report(status, detail, out, 200)


# ---------------------------------------------------------------------------
# the wrapper graph
# ---------------------------------------------------------------------------
def join_by_symbol(token_symbol: str, sym_index: dict) -> tuple:
    """Resolve a wrapper by its TICKER. Returns ``(underlying_id, rule)``.

    ``sym_index`` maps a normalised underlying symbol to its id. Keying on symbol is
    safe: all 643 underlying symbols were checked for collisions and there are none.

    The candidate order is the whole algorithm — see the note above SHELF_AFFIXES. The
    exact symbol is always first because 32 underlyings have tickers ending in 'x' and
    42 begin with 'b', and a strip-first implementation mis-joins them silently.
    """
    s = normalise_symbol(token_symbol)
    if not s:
        return None, JOIN_UNRESOLVED
    candidates = [(s, JOIN_EXACT)]
    if len(s) > 1 and s.endswith("x"):
        candidates.append((s[:-1], JOIN_X_SUFFIX))
    if len(s) > 1 and s.startswith("w"):
        candidates.append((s[1:], JOIN_W_PREFIX))
    if len(s) > 2 and s.startswith("w") and s.endswith("x"):
        candidates.append((s[1:-1], JOIN_W_X))
    if len(s) > 1 and s.startswith("b"):
        # Backed Finance prefixes every token with 'b' — bIB01, bIBTA, bCSPX. Last,
        # because it is the loosest of these: plenty of real tickers begin with b, and
        # every one of them resolves on the exact rule above before this is reached.
        candidates.append((s[1:], JOIN_B_PREFIX))
    for key, rule in candidates:
        hit = sym_index.get(key)
        if hit:
            return hit, rule
    return None, JOIN_UNRESOLVED


def join_by_id(token_id: str, ids_longest_first: list) -> tuple:
    """Resolve a wrapper by its ID: the longest underlying id that prefixes it.

    Longest-first is not a tie-break, it is the share-class guard. 'alphabet-class-a'
    must be tried before anything shorter, and the reason this rule is safe at all is
    that plain 'alphabet' is NOT an underlying: Anchored's ``alphabet-astock`` resolves
    to nothing here and is left to the ticker side, which reads aGOOGL and gets Class A.
    A rule that matched loosely would have silently assigned it to whichever share class
    sorted first.
    """
    tid = (token_id or "").strip().lower()
    if not tid:
        return None, JOIN_UNRESOLVED
    for uid in ids_longest_first:
        if tid == uid or tid.startswith(uid + "-"):
            return uid, JOIN_ID_PREFIX
    return None, JOIN_UNRESOLVED


def join_by_shelf(token_id: str, token_symbol: str, sym_index: dict) -> tuple:
    """An issuer's own affix, applied only to that issuer's tokens.

    The gate is what makes a one-letter affix safe. 'a' is stripped only from a token
    whose id ends '-astock', so it cannot fire on an unrelated ticker no matter how many
    real symbols begin with a. Without this rule Ondo's 438 tokens and Anchored's 80 are
    absent from the graph, which is 48% of every wrapper in existence.
    """
    tid = (token_id or "").strip().lower()
    sym = normalise_symbol(token_symbol)
    if not tid or not sym:
        return None, JOIN_UNRESOLVED
    for suffix, kind, affix in SHELF_AFFIXES:
        if not tid.endswith(suffix):
            continue
        if kind == "prefix" and sym.startswith(affix) and len(sym) > len(affix):
            hit = sym_index.get(sym[len(affix):])
            if hit:
                return hit, JOIN_SHELF
        if kind == "suffix" and sym.endswith(affix) and len(sym) > len(affix):
            hit = sym_index.get(sym[:-len(affix)])
            if hit:
                return hit, JOIN_SHELF
    return None, JOIN_UNRESOLVED


def join_by_commodity_name(token_name: str, known_ids: set) -> tuple:
    """The metals, last, and only after every other rule has failed.

    Gold and silver wrappers carry brand names no affix reaches — PAXG, XAUT, KAU, XAUM,
    CGO, GGBR, JPGC, VNXAU — but their names all contain the metal. Word-boundary
    anchored so "Goldman Sachs" and "B2Gold" cannot match.

    LAST is the whole safety argument. Sixteen tokens in the live graph would be captured
    here otherwise — "SPDR Gold Shares aStock", "iShares Silver Trust (Dinari Tokenized
    ETF)", "VanEck Gold Miners ETF aStock" — and every one is a wrapper of an ETF rather
    than of the metal. Pricing those against spot gold would put two different
    instruments in one dispersion and call the difference a dislocation.
    """
    import re
    name = (token_name or "").lower()
    if not name:
        return None, JOIN_UNRESOLVED
    for uid, pattern in COMMODITY_NAME_PATTERNS:
        if uid in known_ids and re.search(pattern, name):
            return uid, JOIN_COMMODITY
    return None, JOIN_UNRESOLVED


def join_wrapper(token: dict, index: dict) -> tuple:
    """Resolve one wrapper to its underlying, from both sides. ``(underlying_id, rule)``.

    ``token`` is ``{id, symbol, name}``; ``index`` is what ``build_index`` returns.

    The ticker and the id are independent evidence. Where both resolve and AGREE the
    edge is as good as this data gets. Where both resolve and DISAGREE the edge is kept
    and marked ``conflict`` — flag, never pick — and everything downstream that prices a
    basis excludes it. Two such cases exist in the live graph and both are genuinely
    ambiguous rather than defects: ``gold-xstock`` reads GLDX -> GLD -> the SPDR ETF by
    ticker and plain ``gold`` by id, and ``spacex-prestocks-2`` reads ``spacex-pre-ipo``
    by ticker and ``spacex`` by id. Picking either would be inventing a fact.
    """
    if not isinstance(token, dict):
        return None, JOIN_UNRESOLVED
    sym_index = index.get("by_symbol") or {}
    by_ticker, ticker_rule = join_by_symbol(token.get("symbol"), sym_index)
    by_slug, _ = join_by_id(token.get("id"), index.get("ids_longest_first") or [])
    if by_slug is None:
        by_slug, _ = join_by_shelf(token.get("id"), token.get("symbol"), sym_index)
        corroborating_rule = JOIN_SHELF
    else:
        corroborating_rule = JOIN_ID_PREFIX

    if by_ticker and by_slug:
        if by_ticker != by_slug:
            return by_ticker, JOIN_CONFLICT
        return by_ticker, ticker_rule
    if by_ticker:
        return by_ticker, ticker_rule
    if by_slug:
        return by_slug, corroborating_rule
    return join_by_commodity_name(token.get("name"), index.get("ids") or set())


def build_index(underlyings: list) -> dict:
    """The lookup structures every join rule reads. Built once per night."""
    by_symbol, ids = {}, set()
    for u in underlyings:
        uid = u.get("id")
        if not uid:
            continue
        ids.add(uid)
        key = normalise_symbol(u.get("symbol"))
        if key:
            by_symbol.setdefault(key, uid)
    return {"by_symbol": by_symbol, "ids": ids,
            "ids_longest_first": sorted(ids, key=len, reverse=True)}


def build_graph(underlyings: list, issuers: list, issuer_markets: dict | None = None) -> dict:
    """Explode issuers into ``underlying -> wrappers``, recording how each edge was made.

    The graph is MANY-to-one and the schema has to allow it: NVIDIA, Tesla, IBM and Meta
    each carry both a plain and a 'wrapped-' xStock, so a structure assuming one wrapper
    per underlying drops half the tradeable set. Measured over the whole live graph,
    1,065 of 1,073 tokens resolve; the eight that do not wrap underlyings CoinGecko does
    not carry at all, which is an absence to report rather than a join to force.

    ``issuer_markets`` is optional: ``{issuer_id: [underlying_id, ...]}`` from
    ``/rwas/markets?issuer=``. Where it is supplied it is used as a CHECK and never as
    the answer. The two sources disagree in both directions — xStocks publishes 130
    tokens against 114 market rows, and Dinari's SLX token points at an underlying its
    own market listing omits — so an edge the ticker resolves but the issuer feed does
    not corroborate is marked ``conflict`` and has its dislocation suppressed. Flag,
    never pick.
    """
    index = build_index(underlyings)
    by_id = {u["id"]: u for u in underlyings if u.get("id")}

    wrappers, unresolved = [], []
    for iss in issuers:
        claimed = set((issuer_markets or {}).get(iss["id"]) or [])
        for tok in iss.get("tokens") or []:
            uid, rule = join_wrapper(tok, index)
            if not uid:
                unresolved.append({"token_id": tok["id"], "symbol": tok.get("symbol"),
                                   "issuer_id": iss["id"]})
                continue
            if claimed and uid not in claimed and rule != JOIN_CONFLICT:
                # The ticker says one thing and CoinGecko's own issuer mapping says
                # another. IEMGX -> IEMG by ticker, while markets?issuer= lists EEM.
                # The ticker is probably right; "probably" is not good enough to price
                # a basis against, so the edge is kept and the reading is not.
                rule = JOIN_CONFLICT
            wrappers.append({
                "token_id": tok["id"], "symbol": tok.get("symbol"), "name": tok.get("name"),
                "underlying_id": uid, "issuer_id": iss["id"], "issuer_name": iss.get("name"),
                "join_rule": rule,
                "chains": sorted((tok.get("platforms") or {}).keys()),
            })

    by_underlying = {}
    for w in wrappers:
        by_underlying.setdefault(w["underlying_id"], []).append(w)
    return {
        "wrappers": wrappers,
        "by_underlying": by_underlying,
        "unresolved": unresolved,
        "underlyings_indexed": len(by_id),
        "underlyings_with_wrappers": len(by_underlying),
        "issuers_n": len(issuers),
        "join_rule_counts": {r: sum(1 for w in wrappers if w["join_rule"] == r)
                             for r in JOIN_RULES + (JOIN_CONFLICT,)},
    }


# ---------------------------------------------------------------------------
# 1 — tokenization flow residual  ("tokenization impulse")
# ---------------------------------------------------------------------------
def flow_residual(prev_price, prev_mcap, price, mcap) -> dict:
    """Implied change in tokenized supply between two consecutive snapshots.

    ``Expected_MC = MC_prev * P / P_prev`` is the market cap the same units outstanding
    would have carried at today's price. What is left after dividing it out is the change
    in IMPLIED units — see the module docstring for the cancellation, and for why implied
    is the operative word. The arithmetic is exact; the reading of it as issuance is an
    inference that no available feed corroborates, so ``residual_pct`` is published as a
    tokenization impulse and never as a mint or a redemption.

    Returns ``residual_usd`` alongside it because the dollar figure is what says whether
    a 4% supply change was four million dollars of demand or four hundred.

    Every degenerate input returns ``None`` rather than a neutral-looking zero. A
    previous price of zero makes the ratio undefined, not flat, and a residual of 0.0
    published where the arithmetic did not resolve is a fabricated "no adoption" reading.
    """
    p0, m0 = _num(prev_price), _num(prev_mcap)
    p1, m1 = _num(price), _num(mcap)
    # m1 <= 0, not m1 < 0. A published market cap of exactly zero is a feed that stopped
    # reporting, not a token whose entire supply was redeemed overnight — and the second
    # reading is what `m1 < 0` let through, as a -100% residual with a REDEMPTION label.
    if None in (p0, m0, p1, m1) or p0 <= 0 or m0 <= 0 or p1 <= 0 or m1 <= 0:
        return {"expected_mcap": None, "residual_usd": None, "residual_pct": None,
                "price_chg_pct": None}
    expected = m0 * (p1 / p0)
    if expected <= 0:
        return {"expected_mcap": None, "residual_usd": None, "residual_pct": None,
                "price_chg_pct": None}
    return {
        "expected_mcap": expected,
        "residual_usd": m1 - expected,
        "residual_pct": (m1 / expected - 1.0) * 100.0,
        "price_chg_pct": (p1 / p0 - 1.0) * 100.0,
    }


def impulse_label(residual_pct, price_chg_pct) -> str:
    """Read the implied supply change. A description of supply, never of price.

    NEUTRAL is a finding, not a shrug: it says market cap moved as much as price did, so
    the day was repricing and the implied unit count did not move. UNREADABLE is the
    separate case where the move is inside the quantisation of the published inputs and
    the arithmetic cannot distinguish it from rounding.
    """
    r = _num(residual_pct)
    if r is None:
        return IMPULSE_UNREADABLE
    if abs(r) < IMPULSE_MIN_PCT:
        return IMPULSE_NEUTRAL
    if r <= -IMPULSE_MIN_PCT:
        return IMPULSE_REDEMPTION
    pc = _num(price_chg_pct)
    if pc is not None and abs(pc) < IMPULSE_FLAT_PRICE_PCT:
        # Implied supply grew and price did not explain it. This is the reading the
        # brief calls strong adoption, and the only one where the price leg is
        # load-bearing.
        return IMPULSE_STRONG
    return IMPULSE_MINTING


# ---------------------------------------------------------------------------
# 2 — wrapper quality, integrity and the dislocation tape
# ---------------------------------------------------------------------------
def _clamp01(x) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else float(x))


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def wrapper_liveness(w: dict, now: datetime | None = None) -> dict:
    """Is this wrapper a market, or an entry on a dead shelf?

    Two gates, both measured rather than assumed. Dinari's inventory is real, listed and
    priced, and its rows were last updated three weeks ago against $8.59 of daily volume
    — those tokens belong in the graph because a dead shelf is a finding about an issuer,
    and they must never price a basis.

    An unknown age fails the freshness gate. A feed that stopped stamping its rows is
    exactly the feed whose price should not be trusted, so absence is treated as stale
    rather than as fresh.
    """
    vol = _num(w.get("volume_24h"))
    age = _age_hours(w.get("last_updated"), now)
    traded = vol is not None and vol >= WRAPPER_LIVE_VOL_USD
    fresh = age is not None and age <= WRAPPER_STALE_HOURS
    reasons = []
    if not traded:
        reasons.append("below the volume floor" if vol is not None else "no volume published")
    if not fresh:
        reasons.append(f"{age:.0f}h stale" if age is not None else "no timestamp")
    return {"live": bool(traded and fresh), "traded": traded, "fresh": fresh,
            "age_hours": age, "volume_24h": vol,
            "reason": "live" if (traded and fresh) else "; ".join(reasons)}


def comparable_set(live: list) -> dict:
    """The live wrappers that denominate the same quantity, anchored on the deepest.

    Returns ``{legs, reference, other_denominations}``. The anchor is the highest-volume
    live wrapper rather than the median, because volume is the only evidence available
    about which denomination the market actually trades — and a median over a mixed set
    is a number describing no instrument at all. Gold's median across five live wrappers
    was $142.95, a gram, while 99.6% of the dollar volume was in ounces.

    Everything outside the band is REPORTED, not dropped silently: a market quoted in
    both ounces and grams is a real fact about that market, and an engine that simply
    showed fewer rows would be hiding it.
    """
    ranked = sorted(live, key=lambda w: -(_num(w.get("volume_24h")) or 0.0))
    ref = next((w for w in ranked if (_num(w.get("price")) or 0) > 0), None)
    if ref is None:
        return {"legs": [], "reference": None, "other_denominations": []}
    rp = _num(ref["price"])
    same, other = [], []
    for w in ranked:
        px = _num(w.get("price"))
        if not px or px <= 0:
            continue
        ratio = px / rp
        if abs(ratio - 1.0) <= DENOMINATION_TOLERANCE:
            same.append(w)
        else:
            unit = unit_explanation(ratio)
            other.append({
                "token_id": w.get("token_id"), "symbol": w.get("symbol"),
                "price": px, "ratio_to_reference": round(ratio, 4),
                "volume_24h": _num(w.get("volume_24h")),
                "kind": "denomination" if unit else "unexplained",
                "unit": unit,
                "reason": ((f"prices at {ratio:.4g}x the deepest wrapper, which is {unit} "
                            f"— a unit convention, not a basis. CoinGecko publishes no "
                            f"unit metadata for RWA tokens, so this is reported rather "
                            f"than converted.") if unit else
                           (f"prices at {ratio:.4g}x the deepest wrapper and NO unit "
                            f"convention explains it. This is either a fractional-share "
                            f"denomination nobody documented or a wrapper that has come "
                            f"off its peg, and price alone cannot separate them — so it "
                            f"is neither compared nor dismissed."))})
    return {"legs": same, "reference": ref, "other_denominations": other}


def dislocations(priced: list, now: datetime | None = None) -> dict:
    """WRAPPER PRICE DIVERGENCE on one underlying, within one denomination.

    Pre-execution by construction. The name of this function is historical; what it
    returns is a divergence observation, never an executable dislocation.

    Priced against the median of the LIVE wrappers that denominate the same quantity,
    and never against ``tokenized_market_data.current_price``. That aggregate is a blend
    of these same wrappers — for 18 underlyings it equalled a wrapper price to the cent,
    and Netflix's published aggregate of $68.72 sat between a $810 one-share token and an
    $80 tenth-share token, describing neither — so a basis measured against it is partly
    a basis against itself and partly a basis against a unit conversion.

    What this therefore is: the last-price gap between two tokens redeemable for the same
    quantity of the same thing. What it is NOT, and every row says so: an executable
    dislocation, an executable basis, or an opportunity after friction. Executable means
    after bid/ask, depth, cost-to-move and a freshness/trust check, and all of those live
    behind ``/rwas/{id}/tickers``, which answers 401 on this plan. Each leg therefore
    carries ``stage: PRE_EXECUTION`` and ``execution_evidence: UNAVAILABLE``.

    Informational. Nothing here auto-executes, and nothing here is sized.
    """
    # A contradicted edge is excluded HERE, before the median is taken, and not merely
    # from the published legs. build_graph keeps such an edge and withholds the reading;
    # a wrapper that might be a different company setting the median — and therefore the
    # dispersion that reaches score_integrity and the flow ledger — is that reading by
    # another route. Measured: one conflicted leg at $160 beside two correct ones at
    # $180 took dispersion from 5.6bp to 1256bp and integrity from 20/20 to 5/20, while
    # the tape showed no dislocation at all.
    usable = [w for w in priced if w.get("join_rule") != JOIN_CONFLICT]
    live = [w for w in usable if w.get("liveness", {}).get("live") and _num(w.get("price"))]
    contradicted = sum(1 for w in priced if w.get("join_rule") == JOIN_CONFLICT)
    comp = comparable_set(live)
    legs_in = comp["legs"]
    base = {"kind": "wrapper_price_divergence", "stage": DIVERGENCE_STAGE,
            "execution_evidence": EXECUTION_UNAVAILABLE,
            "median_price": None, "dispersion_bps": None, "legs": [],
            "live_n": len(live), "comparable_n": len(legs_in),
            "contradicted_n": contradicted,
            "other_denominations": comp["other_denominations"]}
    if len(legs_in) < DISLOCATION_MIN_LIVE:
        detail = (f"{len(live)} live wrapper(s), {len(legs_in)} in the deepest "
                  f"denomination; a cross-section needs {DISLOCATION_MIN_LIVE} — one "
                  f"wrapper is its own median")
        if comp["other_denominations"]:
            detail += (f". {len(comp['other_denominations'])} wrapper(s) price at a "
                       f"different denomination and are reported rather than compared")
        return {**base, "status": "insufficient", "detail": detail}

    prices = [_num(w["price"]) for w in legs_in]
    med = _median(prices)
    if not med or med <= 0:
        return {**base, "status": "insufficient", "detail": "no usable median price"}

    legs = []
    for w in legs_in:
        basis_bps = (_num(w["price"]) / med - 1.0) * 10_000.0
        if abs(basis_bps) < DISLOCATION_MIN_BPS:
            continue
        breadth = _clamp01((len(legs_in) - DISLOCATION_MIN_LIVE + 1) / 4.0)
        age = w["liveness"].get("age_hours")
        freshness = _clamp01(1.0 - (age or WRAPPER_STALE_HOURS) / WRAPPER_STALE_HOURS)
        vol = _num(w.get("volume_24h")) or 0.0
        depth = _clamp01(math.log10(max(vol, 1.0) / WRAPPER_LIVE_VOL_USD + 1.0) / 2.0)
        # NOT a confidence in a trade, and deliberately not called one. It scores how well
        # EVIDENCED the divergence reading is — how many comparable wrappers formed the
        # median, how fresh this leg is, how much volume stands behind it — and says
        # nothing about whether the gap survives a spread. An earlier version published
        # this as `confidence` next to an `executable: false`, which reads as "we are
        # confident this is executable"; it reached 100 on rows where no execution
        # evidence existed at all.
        evidence = round(100.0 * (0.40 * breadth + 0.30 * freshness + 0.30 * depth), 1)
        legs.append({
            "token_id": w["token_id"], "symbol": w.get("symbol"),
            "issuer_id": w.get("issuer_id"), "price": _num(w["price"]),
            "basis_bps": round(basis_bps, 1), "volume_24h": vol,
            "age_hours": None if age is None else round(age, 1),
            "stage": DIVERGENCE_STAGE,
            "observation_evidence": evidence,
            "execution_evidence": EXECUTION_UNAVAILABLE,
            "executable_after_friction": None,
            "join_rule": w.get("join_rule"),
        })
    # Belt and braces. `usable` already removed these before the median was taken, so
    # this can only ever be a no-op — and it stays because the two filters guard
    # different things: that one keeps a contradicted price out of the arithmetic, this
    # one keeps it off the published tape.
    legs = [l for l in legs if l["join_rule"] != JOIN_CONFLICT]
    legs.sort(key=lambda l: -abs(l["basis_bps"]))
    spread = (max(prices) / min(prices) - 1.0) * 10_000.0 if min(prices) > 0 else None
    detail = (f"{len(legs_in)} of {len(live)} live wrapper(s) share the deepest "
              f"denomination, around a ${med:,.2f} median")
    if comp["other_denominations"]:
        detail += (f"; {len(comp['other_denominations'])} price at a different unit and "
                   f"are reported rather than compared")
    return {**base, "status": "live", "detail": detail,
            "median_price": med,
            "reference": (comp["reference"] or {}).get("symbol"),
            "dispersion_bps": None if spread is None else round(spread, 1),
            "legs": legs}


def wrapper_score(w: dict, peer: dict, issuer: dict | None = None) -> dict:
    """Which implementation of this underlying is structurally best.

    Answers "the best way to own this exposure", not "is NVDA going up". Four scored
    components and one declared absent:

        liquidity     35   24h volume and its share of the underlying's tokenized volume
        integrity     30   distance from the live peer median, and freshness
        distribution  20   chains it exists on, and whether it is the concentrated one
        issuer        15   the issuer's own float and turnover behind it
        execution     20   bid/ask, depth and cost-to-move — DECLARED and UNAVAILABLE

    The score is rescaled over the four that can be priced and carries `coverage`, which
    on this plan is 83.3% and cannot be higher. It is not a complete score with one
    component omitted; it is an incomplete score that says so.

    A wrapper that fails the liveness gate is not scored low, it is not scored. Ranking
    a three-week-stale token at 8/100 invites the reading that it is a worse version of
    a live thing, when the truthful statement is that there is no market there to rank.
    """
    liveness = w.get("liveness") or {}
    if not liveness.get("live"):
        return {"score": None, "label": RWA_UNRATED, "components": {},
                "reason": f"not scored — {liveness.get('reason', 'not live')}"}
    if w.get("join_rule") == JOIN_CONFLICT:
        # The ticker and the id disagree about what this token wraps. Scoring it would
        # rank a wrapper of an unknown company against wrappers of a known one — and the
        # denomination escape hatch below would hand it middle marks on integrity for
        # being far from a median it has no business being near.
        return {"score": None, "label": RWA_UNRATED, "components": {},
                "reason": ("not scored — the ticker and the id disagree about which "
                           "underlying this wraps, so every comparison is between two "
                           "different things")}

    vol = _num(w.get("volume_24h")) or 0.0
    peer_vol = _num(peer.get("total_volume")) or 0.0
    c_liq = W_W_LIQ * (0.6 * _clamp01(
        math.log10(max(vol, LIQ_FLOOR_USD) / LIQ_FLOOR_USD)
        / math.log10(LIQ_CEIL_USD / LIQ_FLOOR_USD))
        + 0.4 * _clamp01(vol / peer_vol if peer_vol > 0 else 0.0))

    med = _num(peer.get("median_price"))
    price = _num(w.get("price"))
    other_denom = w.get("token_id") in (peer.get("other_denominations") or set())
    if other_denom:
        # This wrapper denominates a different quantity from the one the median
        # describes — a gram against an ounce, a tenth of a share against a share. Its
        # distance from that median is a unit conversion and measuring it as disagreement
        # would score a perfectly coherent token at zero on the component named integrity.
        # Middle marks, same as having no peer at all, because that is the situation:
        # there is no comparable wrapper to agree or disagree with.
        tight = 0.5
    elif med and med > 0 and price:
        off_bps = abs(price / med - 1.0) * 10_000.0
        tight = _clamp01(1.0 - (off_bps - INTEGRITY_TIGHT_BPS)
                         / (INTEGRITY_BROKEN_BPS - INTEGRITY_TIGHT_BPS))
    else:
        # A single-wrapper underlying has no peer to disagree with. That is not
        # integrity, it is an absence of evidence, so it scores the middle rather than
        # full marks — otherwise the least distributed wrappers rank highest on the
        # component that is supposed to measure agreement.
        tight = 0.5
    age = liveness.get("age_hours")
    fresh = _clamp01(1.0 - (age or WRAPPER_STALE_HOURS) / WRAPPER_STALE_HOURS)
    c_int = W_W_INT * (0.7 * tight + 0.3 * fresh)

    chains = len(w.get("chains") or [])
    c_dist = W_W_DIST * _clamp01(chains / float(DIST_CHAINS_FULL))

    iss_mcap = _num((issuer or {}).get("market_cap")) or 0.0
    iss_vol = _num((issuer or {}).get("volume_24h")) or 0.0
    iss_turn = (iss_vol / iss_mcap) if iss_mcap > 0 else None
    c_iss = W_W_ISS * (0.6 * _clamp01(math.log10(max(iss_mcap, 1e6) / 1e6) / 4.0)
                    + 0.4 * (_clamp01((iss_turn or 0.0) / TURNOVER_HEALTHY_HI)))

    # Same rule as rwa_conviction: execution is declared, unpriceable, and IN the
    # denominator. Scoring four components out of a declared five and calling the result
    # a complete wrapper score is the redistribution this guards against — the score is
    # rescaled over what was priced and stamped with how much that was.
    priced = c_liq + c_int + c_dist + c_iss
    priced_weight = W_W_LIQ + W_W_INT + W_W_DIST + W_W_ISS
    declared_weight = priced_weight + W_W_EXEC
    total = 100.0 * priced / priced_weight
    coverage = 100.0 * priced_weight / declared_weight
    return {
        "score": round(total, 1),
        "label": rwa_label(total),
        "score_basis": SCORE_BASIS,
        "coverage": round(coverage, 1),
        "effective": round(total * coverage / 100.0, 1),
        "absent": ["execution"],
        "components": {"liquidity": round(c_liq, 1), "integrity": round(c_int, 1),
                       "distribution": round(c_dist, 1), "issuer": round(c_iss, 1),
                       "execution": None},
        "execution_evidence": EXECUTION_UNAVAILABLE,
        "reason": (f"${vol:,.0f} 24h on {chains or 'no listed'} chain(s), "
                   + ("a different denomination from the deepest wrapper, so no basis"
                      if other_denom else
                      "no peer to compare" if not (med and price) else
                      f"{abs((price / med - 1) * 10000):.0f}bp from the peer median")),
    }


# ---------------------------------------------------------------------------
# RWA CONVICTION — the underlying-level model
# ---------------------------------------------------------------------------
def score_liquidity(total_volume, market_cap) -> float | None:
    """Is there a market here at all, and does it turn over like one?

    Log-scaled because tokenized volume spans six orders of magnitude in this universe —
    eight dollars of Dinari inventory and twelve million of wrapped NVDA are both rows —
    and a linear axis would put six hundred of them in its bottom percent.

    Turnover rewards the middle rather than the maximum. A tokenized share turning over
    its entire float every day is not deep; it is a small float being passed around.
    """
    vol = _num(total_volume)
    if vol is None:
        return None
    depth = _clamp01(math.log10(max(vol, LIQ_FLOOR_USD) / LIQ_FLOOR_USD)
                     / math.log10(LIQ_CEIL_USD / LIQ_FLOOR_USD))
    mc = _num(market_cap)
    if mc and mc > 0:
        turn = vol / mc
        if turn < TURNOVER_HEALTHY_LO:
            health = _clamp01(turn / TURNOVER_HEALTHY_LO)
        elif turn <= TURNOVER_HEALTHY_HI:
            health = 1.0
        else:
            health = _clamp01(1.0 - (turn - TURNOVER_HEALTHY_HI) / TURNOVER_HEALTHY_HI)
    else:
        health = depth
    return W_LIQUIDITY * (0.65 * depth + 0.35 * health)


def score_distribution(wrappers_live: int, issuers_n: int, chains_n: int, hhi) -> float | None:
    """How many independent ways there are to hold this, and whether that is real.

    Issuers and chains count for more than wrapper count, because five wrappers from one
    issuer on one chain is a single point of failure wearing five hats. The Herfindahl
    over wrapper volume is what catches that arithmetically: at or above
    ``DIST_CONCENTRATED_HHI`` one wrapper IS the market, whatever the count says.
    """
    if wrappers_live is None:
        return None
    w = _clamp01(wrappers_live / float(DIST_WRAPPERS_FULL))
    i = _clamp01((issuers_n or 0) / float(DIST_ISSUERS_FULL))
    c = _clamp01((chains_n or 0) / float(DIST_CHAINS_FULL))
    breadth = 0.30 * w + 0.40 * i + 0.30 * c
    h = _num(hhi)
    if h is None:
        conc = 0.5
    else:
        conc = _clamp01((DIST_CONCENTRATED_HHI - h) / DIST_CONCENTRATED_HHI)
    return W_DISTRIBUTION * (0.70 * breadth + 0.30 * conc)


def score_impulse(residual_pct_trail: list) -> float | None:
    """Adoption, measured as implied supply change — never as price momentum.

    Takes the recorded daily impulse series and scores its CUMULATIVE growth, not its
    latest print. Supply is a stock and its change is a flow; a single day says almost
    nothing, and the same 1% arriving on twelve consecutive days is the finding.

    ``None`` when there is no recorded history yet, which on the first night is every
    row. That is what the coverage mechanism is for: the component is absent rather than
    zero, every row is affected identically, and the ranking stays internally comparable
    while the series accumulates.
    """
    vals = [_num(v) for v in (residual_pct_trail or [])]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    # Compound rather than sum: these are percentage changes in implied units, and
    # adding them overstates a long run of growth exactly as adding daily returns does.
    growth = 1.0
    for v in vals:
        growth *= (1.0 + v / 100.0)
    cum_pct = (growth - 1.0) * 100.0
    # A neutral market scores the middle. Redemption is a real negative reading and must
    # be able to pull the score down, not merely fail to raise it.
    return W_IMPULSE * _clamp01(0.5 + cum_pct / 40.0)


def score_integrity(dispersion_bps, live_share, conflicts: int = 0) -> float | None:
    """Do the wrappers agree with each other, and is the feed still speaking?

    Two tokens redeemable for the same share should not disagree. When they do, either
    one is stale or one is not really redeemable, and both readings belong in a score
    called integrity. A join CoinGecko's own issuer mapping contradicts is subtracted
    outright — an edge that might be wrong is not evidence of agreement.
    """
    if dispersion_bps is None and live_share is None:
        return None
    d = _num(dispersion_bps)
    if d is None:
        agree = 0.5   # nothing to disagree with; see wrapper_score for the same rule
    else:
        agree = _clamp01(1.0 - (d - INTEGRITY_TIGHT_BPS)
                         / (INTEGRITY_BROKEN_BPS - INTEGRITY_TIGHT_BPS))
    share = _clamp01(_num(live_share) if live_share is not None else 0.0)
    base = W_INTEGRITY * (0.65 * agree + 0.35 * share)
    return max(0.0, base - float(conflicts or 0) * 2.0)


def rwa_conviction(components: dict) -> dict:
    """Weighted mean over the components that produced a value, rescaled to 0-100.

    The rescale is what makes the board honest before its history exists. On night one
    the impulse component cannot be computed for any row, so 25 points of weight are
    absent everywhere; rescaling over the 75 that remain leaves every row comparable to
    every other and states the coverage rather than hiding it. The alternative — scoring
    an absent component as zero — would publish a board on which every asset looked
    equally unadopted, which is a claim nobody measured.

    Below ``RWA_MIN_COVERAGE`` the model refuses. UNRATED is not a grade of zero.
    """
    got = {k: _num(v) for k, v in (components or {}).items()}
    got = {k: v for k, v in got.items() if v is not None and k in COMPONENT_WEIGHTS}
    priced = sum(COMPONENT_WEIGHTS[k] for k in got)
    # Against the DECLARED total, which includes execution. A score computed over four of
    # five components is not a complete score just because the fifth was excluded from the
    # arithmetic, and reporting 100% there is the specific dishonesty this guards.
    total = sum(DECLARED_WEIGHTS.values())
    coverage = 100.0 * priced / total if total else 0.0
    absent = sorted(set(DECLARED_WEIGHTS) - set(got))
    if coverage < RWA_MIN_COVERAGE or priced <= 0:
        return {"score": None, "label": RWA_UNRATED, "coverage": round(coverage, 1),
                "absent": absent,
                "reason": (f"{coverage:.0f}% of the model's weight could be priced, "
                           f"below the {RWA_MIN_COVERAGE:.0f}% floor — "
                           f"absent: {', '.join(absent) or 'none'}")}
    score = 100.0 * sum(got.values()) / priced
    # THE CONTRACT, in the return value rather than in a comment, so the artifact carries
    # it and the UI cannot render a bare number without the denominator beside it:
    #
    #   score        AVAILABLE-EVIDENCE NORMALIZED. The weighted mean of the components
    #                that produced a value, rescaled to 0-100 over the weight of those
    #                components only. 94 means "94 out of 100 on the evidence that was
    #                priced" — it does NOT mean 94 of a fully evidenced 100.
    #   coverage     Priced declared weight / total declared weight. Execution is in the
    #                denominator and is never priced on this plan.
    #   effective    score x coverage / 100. A plain product — the coverage-adjusted
    #                reading for anyone who wants the absent evidence to count against
    #                the number. Not a new formula; not what the board ranks by.
    #   label        The RWA SIGNAL band of `score`, the normalized reading. One concept.
    #
    # The board ranks by `score` because a ranking across rows with identical coverage
    # (every row, on any given night) is the same under either presentation, and the
    # normalized reading is the one whose components a reader can inspect.
    return {"score": round(score, 1), "label": rwa_label(score),
            "score_basis": SCORE_BASIS,
            "coverage": round(coverage, 1), "absent": absent,
            "evidence_weight_priced": priced,
            "evidence_weight_declared": total,
            "effective": round(score * coverage / 100.0, 1),
            "max_coverage_on_this_plan": round(
                100.0 * sum(COMPONENT_WEIGHTS.values()) / sum(DECLARED_WEIGHTS.values()), 1),
            "reason": (f"{coverage:.0f}% of declared model weight priced"
                       + (f"; absent: {', '.join(absent)}" if absent else ""))}


def rwa_label(score) -> str:
    """The market-structure vocabulary. Never the crypto action ladder."""
    s = _num(score)
    if s is None:
        return RWA_UNRATED
    if s >= RWA_T_DEEP:
        return RWA_DEEP
    if s >= RWA_T_SOUND:
        return RWA_SOUND
    if s >= RWA_T_THIN:
        return RWA_THIN
    if s >= RWA_T_FRAGILE:
        return RWA_FRAGILE
    return RWA_DORMANT


# ---------------------------------------------------------------------------
# 3 — 24/7 tokenized price discovery (weekend gap nowcast)
# ---------------------------------------------------------------------------
# A hand-maintained NYSE calendar, stdlib only, with an explicit expiry.
#
# There is no free holiday API that does not want a key, and a wrong calendar is worse
# than no calendar: it would silently measure a "weekend gap" across a Tuesday. So the
# list is written down, its last covered year is declared, and `session_calendar_status`
# refuses to answer past it rather than assuming every weekday is a session. The refusal
# is the maintenance trigger — this needs an owner and a review each December.
# MERGE PREREQUISITE, not a preference: this hand-maintained table must be replaced by a
# pinned, tested exchange-calendar implementation BEFORE 2027-12-31. It is not the
# permanent architecture and should not become it by default.
#
# Why it is still here: the nightly runs on the standard library with no install step, and
# every maintained exchange-calendar package (exchange_calendars, pandas_market_calendars)
# brings pandas with it. Adding that to this workflow is a bigger change than this branch
# should make, and it is a decision with an owner rather than a detail.
#
# What makes it safe in the meantime is the refusal below: past the horizon every reading
# is withheld rather than measured against an assumed session. The refusal IS the
# maintenance trigger, and it fires loudly rather than degrading quietly.
#
# What is never acceptable, with or without a library: inferring that a weekday is a
# trading day.
CALENDAR_FIRST_YEAR = 2026
CALENDAR_LAST_YEAR = 2027
CALENDAR_REPLACEMENT_DUE = "2027-12-31"
NYSE_HOLIDAYS = frozenset({
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
})
# Early closes end at 13:00 ET rather than 16:00. An unlisted one costs three hours at
# the edge of the window; the window itself is published on every reading so the error
# is visible rather than absorbed.
NYSE_EARLY_CLOSES = frozenset({"2026-11-27", "2026-12-24", "2027-11-26"})
SESSION_OPEN_ET = (9, 30)
SESSION_CLOSE_ET = (16, 0)
SESSION_EARLY_CLOSE_ET = (13, 0)
# Below this the off-hours window is an overnight, not a weekend. Both are readable; the
# label distinguishes them because a 65-hour weekend and a 17-hour overnight accumulate
# very different amounts of information.
WEEKEND_MIN_HOURS = 40.0

# ---------------------------------------------------------------------------
# the equity leg, and why it is pending rather than sourced
# ---------------------------------------------------------------------------
# Audited before writing any of this: there is no cash-equity data in this repository.
# The "sibling equity project" that nightly.py and the ledger validator mention appears
# only in their prose — it is a different repository, and nothing here imports, reads or
# is configured to reach it. No ledger carries a session close, a session date or an
# official opening print, and no equity provider secret is configured in any workflow.
#
# So the Monday-gap half of this reading is PENDING, and no provider is being added for
# it. That is a deliberate scope refusal: a new vendor and a new secret to complete one
# panel is a larger commitment than the panel is worth today, and the tokenized side
# accumulates perfectly well without it.
#
# What a future equity source must satisfy, so the coupling stays clean when it arrives:
# a persisted artifact this module READS, never a runtime call into another repository.
# The shape below is the whole interface — one row per symbol per session — and nothing
# in this file will consume anything wider.
EQUITY_ARTIFACT = "ledger/equity_sessions.csv"
EQUITY_REQUIRED_FIELDS = ("symbol", "session_date", "prior_close", "official_open")
EQUITY_PENDING = "PENDING EQUITY PRINT HISTORY"


def equity_prints(ledger_dir: Path | None = None) -> dict:
    """Whatever cash-equity session prints have been persisted for this module to read.

    Read-only and artifact-shaped on purpose. A direct call into another project's code
    would couple two nightlies at runtime and make each one's failure the other's; a file
    that either exists or does not is a boundary that survives either side changing.

    Returns a report rather than a value, so the absence is a state the artifact carries
    rather than an exception the caller has to know about.
    """
    path = (ledger_dir or LEDGER_DIR) / Path(EQUITY_ARTIFACT).name
    if not path.exists():
        return {"status": "pending", "detail": (
            f"no {EQUITY_ARTIFACT} in this repository. Audited: no ledger carries a "
            f"session close, session date or official opening print, and no equity "
            f"provider is configured in any workflow. The implied gap stays "
            f"{EQUITY_PENDING} and the tokenized inputs accumulate meanwhile."),
            "rows": {}}
    rows = {}
    for r in read_rows(path, list(EQUITY_REQUIRED_FIELDS)):
        sym = (r.get("symbol") or "").lower()
        if sym:
            rows.setdefault(sym, []).append(r)
    return {"status": "live" if rows else "empty",
            "detail": f"{len(rows)} symbol(s) of cash-session prints", "rows": rows}
# Sparkline arrays carry no timestamps, so the hour of each point is INFERRED from
# last_updated. Every reading built on that inference says so.
#
# The length is a band rather than a number, and that is a measurement: the live feed
# returned 169 points where the first probe of it returned 168. Seven days of hourly
# samples lands on either side of 168 depending on where in the hour the request fell,
# and a hard equality check would fire every other night on a feed that is behaving.
# What must NOT vary is the spacing — one hour between points, ending at last_updated —
# so the band is tight enough that a switch to a different cadence still trips it.
SPARKLINE_MIN_POINTS = 160
SPARKLINE_MAX_POINTS = 176


def _et(dt: datetime) -> datetime:
    """UTC -> America/New_York, falling back to a fixed offset if tzdata is absent.

    zoneinfo is stdlib and the runner has tzdata, but a container without it raises
    rather than returning a wrong time, and this module must not take a dependency to
    stay importable. The fallback is EST/EDT by US rule, which is right except in the
    hour either side of a transition; ``session_calendar_status`` reports which path ran.
    """
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        # Second Sunday in March to first Sunday in November, approximated at the day
        # boundary. Named as an approximation rather than presented as the rule.
        y = dt.year
        mar = datetime(y, 3, 8, tzinfo=timezone.utc)
        mar += timedelta(days=(6 - mar.weekday()) % 7)
        nov = datetime(y, 11, 1, tzinfo=timezone.utc)
        nov += timedelta(days=(6 - nov.weekday()) % 7)
        offset = -4 if mar <= dt < nov else -5
        return dt.astimezone(timezone(timedelta(hours=offset)))


def session_calendar_status(now: datetime) -> dict:
    """Whether the hand-maintained calendar still covers the date being asked about."""
    tz_ok = True
    try:
        from zoneinfo import ZoneInfo  # noqa: F401
    except Exception:  # noqa: BLE001
        tz_ok = False
    # Bounded at BOTH ends. A missing upper bound would measure 2029 against an assumed
    # session; a missing lower bound does the same thing to a backfill or a replayed
    # date, where every 2025 holiday is absent from the table and reads as a trading day.
    if not CALENDAR_FIRST_YEAR <= now.year <= CALENDAR_LAST_YEAR:
        return {"ok": False, "tz": "zoneinfo" if tz_ok else "fixed-offset approximation",
                "detail": (f"the NYSE calendar in rwa.py covers {CALENDAR_FIRST_YEAR}-"
                           f"{CALENDAR_LAST_YEAR} and this reading is dated {now.year}; "
                           f"every off-hours reading is withheld rather than measured "
                           f"against an assumed session")}
    return {"ok": True, "tz": "zoneinfo" if tz_ok else "fixed-offset approximation",
            "detail": (f"NYSE calendar covers {CALENDAR_FIRST_YEAR}-{CALENDAR_LAST_YEAR}")}


def _session_bounds(day_et: datetime):
    """Open and close for one calendar day in ET, or None when it is not a session."""
    key = day_et.strftime("%Y-%m-%d")
    if day_et.weekday() >= 5 or key in NYSE_HOLIDAYS:
        return None
    close_h, close_m = (SESSION_EARLY_CLOSE_ET if key in NYSE_EARLY_CLOSES
                        else SESSION_CLOSE_ET)
    return (day_et.replace(hour=SESSION_OPEN_ET[0], minute=SESSION_OPEN_ET[1],
                           second=0, microsecond=0),
            day_et.replace(hour=close_h, minute=close_m, second=0, microsecond=0))


def last_close(now: datetime, lookback_days: int = 10):
    """The most recent cash-session close at or before ``now``, as UTC.

    Walks backwards a day at a time rather than computing, because holidays make the
    "previous session" a lookup and not an arithmetic. Returns None past the calendar's
    expiry or if ten days of walking find no session, which would itself be a finding.
    """
    et = _et(now)
    for back in range(lookback_days):
        day = et - timedelta(days=back)
        bounds = _session_bounds(day)
        if not bounds:
            continue
        _, close = bounds
        if close <= et:
            return close.astimezone(timezone.utc)
    return None


def market_open_now(now: datetime) -> bool:
    et = _et(now)
    bounds = _session_bounds(et)
    return bool(bounds and bounds[0] <= et <= bounds[1])


# The inference quality of a sparkline, as a DISCRETE field rather than a sentence the
# consumer has to pattern-match. The terminal marks a degraded window differently from a
# merely inferred one; before this it could only have done that by string-matching the
# prose in `detail`, which is a classification living in two places and eventually
# disagreeing with itself. rwa.py owns the classification; the prose explains it.
SPARK_HOURLY = "hourly_inferred"            # anchored, and the point count is in band
SPARK_CADENCE_UNVERIFIED = "cadence_unverified"   # anchored, but the cadence may not be hourly
SPARK_UNANCHORED = "unanchored"             # no last_updated to hang the series on
SPARK_ABSENT = "absent"                     # no usable sparkline at all
SPARK_DEGRADED = (SPARK_CADENCE_UNVERIFIED, SPARK_UNANCHORED, SPARK_ABSENT)


def sparkline_hours(prices: list, last_updated, now: datetime | None = None) -> dict:
    """Attach an inferred UTC hour to each sparkline point.

    CoinGecko publishes ``sparkline_in_7d.price`` as a bare array with no timestamps, so
    the only available mapping is: the last point is ``last_updated``, and each earlier
    point is one hour before the next. That is an INFERENCE, it is stated as one on every
    reading built from it, and the array length is reported rather than assumed —
    168 points is what was measured, and a feed that returns 169 must not silently shift
    every hour in the series by one.
    """
    vals = [_num(p) for p in (prices or [])]
    if not vals or any(v is None for v in vals):
        return {"points": [], "inferred": True, "n": len(vals),
                "quality": SPARK_ABSENT,
                "detail": "sparkline absent or contained an unparseable value"}
    end = _iso(last_updated) or now
    if end is None:
        return {"points": [], "inferred": True, "n": len(vals),
                "quality": SPARK_UNANCHORED,
                "detail": "no last_updated to anchor the sparkline to"}
    n = len(vals)
    pts = [{"t": end - timedelta(hours=(n - 1 - i)), "price": v} for i, v in enumerate(vals)]
    detail = f"{n} hourly point(s) inferred backwards from last_updated"
    quality = SPARK_HOURLY
    if not SPARKLINE_MIN_POINTS <= n <= SPARKLINE_MAX_POINTS:
        quality = SPARK_CADENCE_UNVERIFIED
        detail += (f" — outside the {SPARKLINE_MIN_POINTS}-{SPARKLINE_MAX_POINTS} band a "
                   f"7-day hourly series falls in, so the cadence may not be hourly and "
                   f"every hour in this window may be misplaced")
    # The flag and the prose are composed in the SAME branch, on the same condition, so
    # they cannot come to disagree. The terminal reads `quality` and shows `detail`; a
    # reader of either is reading the same decision.
    return {"points": pts, "inferred": True, "n": n, "quality": quality, "detail": detail}


def _price_at(points: list, when: datetime):
    """The sparkline point nearest ``when``, and how far off it was."""
    if not points or when is None:
        return None, None
    best = min(points, key=lambda p: abs((p["t"] - when).total_seconds()))
    return best["price"], abs((best["t"] - when).total_seconds()) / 3600.0


def offhours_reading(row: dict, wrappers_live: list, dispersion_bps,
                     now: datetime, volume_ratio=None) -> dict:
    """What the tokenized tape did while the cash equity market was shut.

    The tokenized tape runs 24/7 and the underlying's cash market does not, so between
    Friday's close and Monday's open the only price discovery that happened anywhere is
    the one recorded here. That is the whole reason this reading exists.

    What it publishes: the off-hours return, how long the window has been open, how many
    wrappers agree on its direction, how far apart they are, and how the tokenized volume
    compares to its own recorded baseline.

    What it deliberately does NOT publish: an implied Monday gap or a confidence in one.
    Converting a tokenized drift into an expected cash-open gap needs the cash prints as
    the other half of the pair. This repository has none — see EQUITY_ARTIFACT above for
    the audit — and CoinGecko cannot supply them, because its own "underlying" price is a
    blend of these same wrappers. Publishing a gap from one side of that relationship
    would be inventing the side nobody measured, so the state is PENDING and the inputs
    accumulate.
    """
    cal = session_calendar_status(now)
    if not cal["ok"]:
        return {"status": "unavailable", "detail": cal["detail"], "window": None}
    if row.get("asset_type") not in ("stock", "etf"):
        return {"status": "n/a",
                "detail": f"{row.get('asset_type') or 'unknown'} has no cash session to be shut",
                "window": None}
    if market_open_now(now):
        return {"status": "session_open",
                "detail": "the cash market is trading; there is no off-hours window to read",
                "window": None}
    close = last_close(now)
    if close is None:
        return {"status": "unavailable",
                "detail": "no cash-session close found in the last ten days", "window": None}

    tmd = row.get("tokenized_market_data") or {}
    spark = sparkline_hours((tmd.get("sparkline_in_7d") or {}).get("price"),
                            tmd.get("last_updated"), now)
    price_now = _num(tmd.get("current_price"))
    at_close, drift_h = _price_at(spark["points"], close)
    hours_closed = (now - close).total_seconds() / 3600.0
    if at_close is None or not price_now or at_close <= 0:
        return {"status": "insufficient",
                "detail": f"no sparkline price at the {close:%Y-%m-%d %H:%M} UTC close "
                          f"({spark['detail']})",
                "window": {"closed_at": close.isoformat(), "hours_closed": round(hours_closed, 1)}}

    ret_pct = (price_now / at_close - 1.0) * 100.0
    # Only wrappers that actually published a 24h change are counted, on both sides of
    # the ratio. Including the silent ones in the denominator reports "0 of 5 agree" from
    # a market where five wrappers said nothing, which reads as disagreement rather than
    # as absence.
    moves = [v for v in (_num(w.get("price_change_pct_24h")) for w in wrappers_live)
             if v is not None and v != 0]
    direction = 1 if ret_pct > 0 else (-1 if ret_pct < 0 else 0)
    agree = sum(1 for v in moves if (1 if v > 0 else -1) == direction)
    return {
        "status": "live",
        "detail": (f"{hours_closed:.0f}h since the cash close; "
                   f"{'weekend' if hours_closed >= WEEKEND_MIN_HOURS else 'overnight'} window"),
        "window": {"closed_at": close.isoformat(),
                   "hours_closed": round(hours_closed, 1),
                   "kind": "weekend" if hours_closed >= WEEKEND_MIN_HOURS else "overnight",
                   "close_price_from": round(drift_h, 2) if drift_h is not None else None,
                   "sparkline": spark["detail"], "inferred_hours": spark["inferred"],
                   # The discrete form of the sentence beside it. Every live window on
                   # this plan is inferred, so `inferred_hours` alone cannot separate the
                   # ordinary case from the one where the cadence itself is in doubt.
                   "inference_quality": spark.get("quality"),
                   "inference_degraded": spark.get("quality") in SPARK_DEGRADED},
        "offhours_return_pct": round(ret_pct, 3),
        "price_at_close": at_close,
        "price_now": price_now,
        "wrappers_live": len(wrappers_live),
        "wrappers_voting": len(moves),
        "wrappers_agree": agree,
        "agreement": (round(agree / len(moves), 3) if moves else None),
        "dispersion_bps": dispersion_bps,
        "volume_ratio": volume_ratio,
        "implied_gap_pct": None,
        "implied_gap_state": EQUITY_PENDING,
        "implied_gap_confidence": None,
        "implied_gap_blocked_by": (
            "an implied cash-open gap needs the underlying's own equity prints as the "
            "other half of the pair, and this repository has none: audited, no ledger "
            "carries a session close or an official opening print and no equity provider "
            "is configured. CoinGecko cannot supply it — its 'underlying' price is a "
            "blend of these same wrappers. The gap is therefore withheld, no vendor is "
            "being added for it, and the tokenized inputs above are recorded nightly so "
            f"the study runs the day {EQUITY_ARTIFACT} exists."),
    }


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------
RWA_FLOW_CSV = LEDGER_DIR / "rwa_flow.csv"
RWA_ISSUERS_CSV = LEDGER_DIR / "rwa_issuers.csv"
RWA_WRAPPERS_CSV = LEDGER_DIR / "rwa_wrappers.csv"
RWA_JSON = LEDGER_DIR / "rwa.json"

# The flow ledger is the irreplaceable one. /rwas/{id}/market_chart answers 401 below
# the Basic plan, so there is no way to backfill a night that was not recorded — not
# later, not with a key, not at any price. Every other artifact here can be rebuilt from
# a fresh fetch; this one cannot.
RWA_FLOW_FIELDS = [
    "date", "underlying_id", "symbol", "name", "asset_type",
    "price", "market_cap", "total_volume",
    "expected_mcap", "residual_usd", "residual_pct", "residual_pct_daily",
    "price_chg_pct", "impulse", "span_days", "supply_index",
    "wrappers_n", "wrappers_live", "issuers_n", "chains_n",
    "dispersion_bps", "conviction", "label", "coverage", "spec_hash",
    "degraded", "peer_set_complete",
]
RWA_ISSUER_FIELDS = [
    "date", "issuer_id", "name", "market_cap", "market_cap_change_24h",
    "volume_24h", "tokens_n", "chains_n", "underlyings_n", "live_tokens_n",
]
RWA_WRAPPER_FIELDS = [
    "date", "token_id", "symbol", "name", "underlying_id", "issuer_id", "join_rule",
    "price", "market_cap", "volume_24h", "chains", "live", "age_hours",
    "basis_bps", "wrapper_score",
]


# ---------------------------------------------------------------------------
# the evidence contract for the historical record
# ---------------------------------------------------------------------------
# The dataset is the asset. Everything else in this module can be rebuilt from a fresh
# fetch; the recorded series cannot, because /rwas/{id}/market_chart answers 401. So the
# write path carries an explicit contract rather than a convention, and the invariant at
# the top of it is the one that matters:
#
#     A DEGRADED OR PARTIAL FETCH MAY NEVER REPLACE A COMPLETE CANONICAL OBSERVATION.
#
# Three real failures motivated each clause. A 429 lost an issuer and the run wrote a
# thinner graph over a fuller one. A 414 lost 250 wrappers and the run published medians
# computed from whichever peers survived — an incomplete peer set does not merely hide
# signal, it manufactures it. And a same-day re-run read the row it had just written and
# recorded a 0.0% impulse over a real one.
#
# The clauses:
#   * observations are persisted BEFORE any derivation, in their own file
#   * every run writes a manifest row: what was asked for, what came back, and the
#     vendor's own timestamp — not merely ours
#   * publication is atomic, so a killed process cannot leave a half-written ledger
#   * a re-run at equal or better completeness replaces; a worse one is quarantined
#   * quarantined runs are KEPT, as diagnostic evidence, and never promoted
#   * a derived row computed from an incomplete peer set is marked degraded on the row
RUN_COMPLETE = "complete"
RUN_DEGRADED = "degraded"
RUN_FAILED = "failed"
RUN_RANK = {RUN_FAILED: 0, RUN_DEGRADED: 1, RUN_COMPLETE: 2}

RWA_OBSERVED_FIELDS = [
    "date", "run_ts", "underlying_id", "symbol", "asset_type",
    "price", "market_cap", "total_volume", "source_last_updated",
]
RWA_RUN_FIELDS = [
    "date", "run_ts", "run_status", "spec_hash", "plan",
    "underlyings_listed", "underlyings_observed", "issuers_expected", "issuers_received",
    "wrappers_in_graph", "wrappers_priced", "wrappers_unresolved",
    "feed_list", "feed_markets", "feed_issuers", "feed_wrappers",
    "coverage_pct", "promoted", "note",
]


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file and one rename.

    A process killed midway through a direct write leaves a truncated file that still
    parses as CSV — a shorter ledger that looks like a real one. os.replace is atomic on
    the same filesystem, so a reader sees either the old file or the new one.
    """
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_completeness(feeds: dict, graph: dict, observed_n: int, listed_n: int,
                     issuers_received: int = 0, issuers_listed: int = 0) -> dict:
    """What was asked for against what came back, and the resulting run status.

    COMPLETE requires every feed live AND every wrapper in the graph priced. Anything
    less is DEGRADED — including the case where all four feeds report "live" but the
    wrapper set came back short, because that is exactly how the 414 presented.

    ``coverage_pct`` is CONTINUOUS, and that is a correction. It began as three booleans,
    which meant a run that fetched 31 of 34 issuers and one that fetched 33 both scored
    66.7 — so the promotion rule, whose whole job is to prefer the fuller observation,
    could not tell them apart and let the thinner one publish over the fuller. Measured
    on a live pair, which is how it was found. Fractions, not flags.
    """
    named = ("list", "markets", "issuers", "wrappers")
    statuses = {k: (feeds.get(k) or {}).get("status") for k in named}
    wrappers_n = len(graph.get("wrappers") or [])
    priced = int(graph.get("wrappers_priced") or 0)
    all_live = all(statuses.get(k) == "live" for k in named)
    peers_whole = wrappers_n > 0 and priced >= wrappers_n
    observed_whole = listed_n > 0 and observed_n >= listed_n - LIST_MARKETS_GAP_EXPECTED

    if statuses.get("list") not in ("live", "partial") or observed_n == 0:
        status = RUN_FAILED
    elif all_live and peers_whole and observed_whole:
        status = RUN_COMPLETE
    else:
        status = RUN_DEGRADED

    def share(got, want):
        return 1.0 if want <= 0 else _clamp01(got / float(want))

    shares = {
        "feeds": sum(1 for k in named if statuses.get(k) == "live") / float(len(named)),
        "wrappers": share(priced, wrappers_n),
        "issuers": share(issuers_received, issuers_listed or issuers_received),
        "universe": share(observed_n, max(1, listed_n - LIST_MARKETS_GAP_EXPECTED)),
    }
    return {
        "status": status,
        "feeds": statuses,
        "all_feeds_live": all_live,
        "peer_set_complete": peers_whole,
        "universe_complete": observed_whole,
        "shares": {k: round(v, 4) for k, v in shares.items()},
        "coverage_pct": round(100.0 * sum(shares.values()) / len(shares), 2),
        "wrappers_priced": priced, "wrappers_in_graph": wrappers_n,
        "issuers_received": issuers_received, "issuers_listed": issuers_listed,
        "observed_n": observed_n, "listed_n": listed_n,
        "note": ("every feed live, every wrapper priced, universe whole"
                 if status == RUN_COMPLETE else
                 "; ".join(filter(None, [
                     None if all_live else "a feed did not come back live",
                     None if peers_whole else
                     f"{wrappers_n - priced} wrapper(s) unpriced — medians and integrity "
                     f"are computed from an incomplete peer set",
                     None if observed_whole else
                     f"{listed_n - observed_n} underlying(s) not observed"]))),
    }


def prior_run_quality(path: Path, today: str):
    """The quality of the best run already promoted for ``today``: (rank, coverage).

    None when nothing has been promoted yet.
    """
    best = None
    for r in read_rows(path, RWA_RUN_FIELDS):
        if r.get("date") != today or str(r.get("promoted") or "") != "1":
            continue
        q = (RUN_RANK.get((r.get("run_status") or "").strip(), -1),
             _num(r.get("coverage_pct")) or 0.0)
        if best is None or q > best:
            best = q
    return best


def prior_run_status(path: Path, today: str) -> str | None:
    """The status word of the best run already promoted for ``today``, for reporting."""
    q = prior_run_quality(path, today)
    if q is None:
        return None
    return next((k for k, v in RUN_RANK.items() if v == q[0]), None)


def may_promote(new_status: str, prior_status: str | None,
                new_coverage: float | None = None,
                prior_quality=None) -> bool:
    """The invariant, in one function so it can be tested in one place.

    A run may publish over today's canonical rows only when it is at least as good as
    whatever already stands there, compared on (status rank, coverage) in that order.

    Status alone was not enough, and the live run is what showed it: a night where one
    issuer 429s is DEGRADED, and a retry that lost a whole wrapper batch is also
    DEGRADED — equal rank, and under a rank-only rule the thinner run would have replaced
    the fuller one. Coverage is the tie-break, so "may never replace a more complete
    observation" holds inside a status as well as across statuses.

    Equal on both replaces, which is what keeps an identical re-run idempotent.
    """
    if new_status == RUN_FAILED:
        return False
    if prior_status is None and prior_quality is None:
        return True
    prior_q = prior_quality if prior_quality is not None else (
        RUN_RANK.get(prior_status, -1), 0.0)
    return (RUN_RANK.get(new_status, -1), float(new_coverage or 0.0)) >= prior_q


def read_rows(path: Path, fields: list) -> list:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [{k: r.get(k) for k in fields} for r in csv.DictReader(f)]


def append_daily_rows(path: Path, fields: list, today: str, rows: list) -> int:
    """Replace today's rows and rewrite. Same rule as ``nightly._append_context_rows``.

    Restated here rather than imported because ``nightly.py`` imports this module, so
    borrowing its helper would be a cycle. The rule itself must not diverge, so
    ``tests/test_rwa.py`` asserts the two implementations agree on the same input — the
    duplication is a checked invariant rather than a second source of truth.

    Replace, not append: a second run on the same day otherwise records that day twice,
    and a supply chain that reads the file as a daily series then compounds one day's
    issuance nine times.
    """
    import io
    kept = [r for r in read_rows(path, fields) if r.get("date") != today]
    fresh = [{k: r.get(k) for k in fields} for r in rows]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n")
    w.writeheader()
    w.writerows(kept + fresh)
    _atomic_write(path, buf.getvalue())
    return len(fresh)


def _append_manifest(path: Path, row: dict) -> None:
    """Append one run row. Never replaces, so the record of every attempt survives."""
    import io
    existing = read_rows(path, RWA_RUN_FIELDS)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=RWA_RUN_FIELDS, lineterminator="\r\n")
    w.writeheader()
    w.writerows(existing + [{k: row.get(k) for k in RWA_RUN_FIELDS}])
    _atomic_write(path, buf.getvalue())


def _prior_flow(path: Path = None, today: str | None = None) -> dict:
    """The most recent recorded row per underlying STRICTLY BEFORE ``today``.

    Read before tonight's row is written, for the reason nightly.py states three times
    about its own trailing reads: a series that already contains its own outcome is not
    a series a decision could have been made against.

    The ``today`` exclusion is what makes that true across PROCESSES and not merely
    within one. ``append_daily_rows`` replaces today's rows, and the nightly carries
    ``workflow_dispatch`` — so a re-run is an expected mode, not a mishap. Without the
    filter the second run reads the row the first run just wrote, compares tonight's
    price and cap against themselves, and records a residual of exactly 0.0 labelled
    NEUTRAL over the top of a real minting event. In a ledger that
    ``/rwas/{id}/market_chart`` cannot backfill, that is the single most destructive
    thing this module could do. nightly.py already applies the same filter to its own
    trailing reads (``_compute_market_intel`` drops today from the macro and DEX
    history); these readers were the outliers.
    """
    rows = read_rows(path or RWA_FLOW_CSV, RWA_FLOW_FIELDS)
    latest = {}
    for r in rows:
        uid, date = r.get("underlying_id"), r.get("date")
        if not uid or not date or (today is not None and date >= today):
            continue
        if uid not in latest or date > latest[uid]["date"]:
            latest[uid] = r
    return latest


def _span_days(prev_date: str, today: str) -> int:
    try:
        a = datetime.strptime(prev_date, "%Y-%m-%d")
        b = datetime.strptime(today, "%Y-%m-%d")
    except (TypeError, ValueError):
        return 1
    return max(1, (b - a).days)


def _daily_rate(residual_pct, span_days: int):
    """Geometric daily rate from a residual measured across ``span_days``.

    A gap in the chain does not invalidate the residual — implied units still changed by
    that much between the two observations — but it does change what the number means. 3%
    over seven nights is 0.42% a day and reads NEUTRAL; labelling it MINTING because the
    raw figure crossed a daily threshold would turn an outage into a signal. The raw
    residual is what the supply index compounds; the daily rate is what the label reads.
    """
    r = _num(residual_pct)
    if r is None or span_days < 1:
        return None
    if r <= -100.0:
        return None
    return ((1.0 + r / 100.0) ** (1.0 / span_days) - 1.0) * 100.0


# ---------------------------------------------------------------------------
# the nightly snapshot
# ---------------------------------------------------------------------------
def _hhi(values: list) -> float | None:
    """Herfindahl over shares. None when there is nothing to concentrate."""
    vals = [v for v in (_num(x) or 0.0 for x in values) if v > 0]
    total = sum(vals)
    if not vals or total <= 0:
        return None
    return sum((v / total) ** 2 for v in vals)


def assemble(underlying_rows: list, graph: dict, wrapper_prices: dict,
             prior: dict, today: str, now: datetime,
             volume_baseline: dict | None = None,
             impulse_trail: dict | None = None,
             issuers_by_id: dict | None = None,
             degraded: bool = False) -> dict:
    """Join every feed into the board, the wrapper tape and the flow rows.

    Pure: no network and no clock of its own. Everything it needs arrives as an
    argument, which is what makes the whole model testable against fixtures rather than
    against whatever the API happened to return on the day.
    """
    by_underlying = graph.get("by_underlying") or {}
    volume_baseline = volume_baseline or {}
    impulse_trail = impulse_trail or {}
    issuers_by_id = issuers_by_id or {}
    # A run-level degradation marks EVERY row it produced. The 414 that lost 250 wrappers
    # published medians computed from whichever peers survived, and those rows were
    # indistinguishable from rows computed over a whole peer set — an incomplete
    # cross-section does not merely hide signal, it manufactures it. Per-row peer
    # completeness is recorded alongside, because an underlying whose own wrappers all
    # priced is still clean on a night when another underlying's did not.
    board, flow_rows, wrapper_rows, tape = [], [], [], []
    sh = spec_hash()

    for row in underlying_rows:
        uid = row.get("id")
        tmd = row.get("tokenized_market_data") or {}
        price, mcap = _num(tmd.get("current_price")), _num(tmd.get("market_cap"))
        vol = _num(tmd.get("total_volume"))

        priced = []
        for w in by_underlying.get(uid, []):
            px = wrapper_prices.get(w["token_id"])
            merged = dict(w)
            merged.update(px or {})
            merged["liveness"] = wrapper_liveness(merged, now)
            priced.append(merged)
        live = [w for w in priced if w["liveness"]["live"]]

        disloc = dislocations(priced, now)
        peer = {"total_volume": vol, "median_price": disloc.get("median_price"),
                "other_denominations": {o["token_id"] for o
                                        in (disloc.get("other_denominations") or [])}}
        # LIVE wrappers on all three counts. Counting issuers and chains over every
        # wrapper while counting only live ones for the wrapper term scored a dead shelf
        # as broad distribution: Dinari lists 132 tokens across five chains and one of
        # them trades, and the component read as though all five chains were markets.
        issuers_here = {w["issuer_id"] for w in live if w.get("issuer_id")}
        chains_here = {c for w in live for c in (w.get("chains") or [])}
        issuers_listed = {w["issuer_id"] for w in priced if w.get("issuer_id")}
        chains_listed = {c for w in priced for c in (w.get("chains") or [])}
        conflicts = sum(1 for w in priced if w.get("join_rule") == JOIN_CONFLICT)

        for w in priced:
            ws = wrapper_score(w, peer, issuers_by_id.get(w.get("issuer_id")))
            w["score"] = ws
            leg = next((l for l in disloc.get("legs") or []
                        if l["token_id"] == w["token_id"]), None)
            # Carried onto the wrapper itself, not only into the CSV row. The artifact's
            # wrapper table renders w.basis_bps, and without this the column was
            # permanently a dash on every row of every underlying.
            w["basis_bps"] = (leg or {}).get("basis_bps")
            wrapper_rows.append({
                "date": today, "token_id": w["token_id"], "symbol": w.get("symbol"),
                "name": w.get("name"), "underlying_id": uid,
                "issuer_id": w.get("issuer_id"), "join_rule": w.get("join_rule"),
                "price": w.get("price"), "market_cap": w.get("market_cap"),
                "volume_24h": w.get("volume_24h"),
                "chains": "|".join(w.get("chains") or []),
                "live": int(bool(w["liveness"]["live"])),
                "age_hours": (None if w["liveness"]["age_hours"] is None
                              else round(w["liveness"]["age_hours"], 1)),
                "basis_bps": (leg or {}).get("basis_bps"),
                "wrapper_score": ws.get("score"),
            })

        prev = prior.get(uid)
        span = _span_days(prev.get("date"), today) if prev else 1
        res = flow_residual(_num((prev or {}).get("price")),
                            _num((prev or {}).get("market_cap")), price, mcap)
        daily = _daily_rate(res["residual_pct"], span)
        # BOTH legs per-day. The residual was rebased for the span and the price change
        # was not, so across a seven-night gap a 6% cumulative move read as 6% "today"
        # and pushed every quiet supply build out of STRONG_ADOPTION into MINTING — a
        # label change caused by an outage rather than by a market.
        impulse = impulse_label(daily, _daily_rate(res["price_chg_pct"], span))
        prev_index = _num((prev or {}).get("supply_index"))
        if res["residual_pct"] is None:
            # No chain yet, or an unusable pair. The index restarts at 100 rather than
            # inheriting a number it cannot justify; `span_days` on the row is what says
            # a restart happened.
            supply_index = prev_index if prev_index else 100.0
        else:
            supply_index = (prev_index or 100.0) * (1.0 + res["residual_pct"] / 100.0)

        # The recorded series, NOT tonight's single print. Supply is a stock and
        # issuance is its flow: one night's mint says almost nothing, and the same 1%
        # arriving on twelve consecutive nights is the finding. Tonight's reading is
        # appended here rather than in the ledger read, so the component sees the same
        # series the row publishes.
        trail = list(impulse_trail.get(uid) or [])
        if daily is not None:
            trail.append(daily)
        comps = {
            "liquidity": score_liquidity(vol, mcap),
            "distribution": score_distribution(len(live), len(issuers_here),
                                               len(chains_here),
                                               _hhi([w.get("volume_24h") for w in live])),
            "integrity": score_integrity(disloc.get("dispersion_bps"),
                                         (len(live) / len(priced)) if priced else None,
                                         conflicts),
            "impulse": score_impulse(trail),
        }
        conv = rwa_conviction(comps)

        base = volume_baseline.get(uid)
        vol_ratio = (round(vol / base, 2) if (vol and base and base > 0) else None)
        oh = offhours_reading(row, live, disloc.get("dispersion_bps"), now, vol_ratio)

        # This underlying's own peer set: did every wrapper the graph knows about get a
        # price tonight?
        row_peers_whole = all(wrapper_prices.get(w["token_id"]) for w in by_underlying.get(uid, []))
        row_degraded = bool(degraded) or not row_peers_whole
        rec = {
            "id": uid, "symbol": row.get("symbol"), "name": row.get("name"),
            "degraded": row_degraded, "peer_set_complete": bool(row_peers_whole),
            "asset_type": row.get("asset_type"), "image": row.get("image"),
            "price": price, "market_cap": mcap, "total_volume": vol,
            "price_chg_pct_24h": _num(tmd.get("price_change_percentage_24h")),
            "mcap_chg_pct_24h": _num(tmd.get("market_cap_change_percentage_24h")),
            "last_updated": tmd.get("last_updated"),
            "horizons": {h: _num(tmd.get(f"price_change_percentage_{h}_in_currency"))
                         for h in ("1h", "24h", "7d", "14d", "30d", "200d", "1y")},
            "wrappers_n": len(priced), "wrappers_live": len(live),
            "issuers_n": len(issuers_here), "chains_n": len(chains_here),
            "issuers_listed_n": len(issuers_listed), "chains_listed_n": len(chains_listed),
            "issuers": sorted(issuers_listed), "chains": sorted(chains_listed),
            "conflicts_n": conflicts,
            "flow": {**res, "residual_pct_daily": daily, "impulse": impulse,
                     "span_days": span, "supply_index": supply_index,
                     "chain_days": len(trail), "trail": trail[-30:]},
            "dislocation": disloc,
            "components": {k: (None if v is None else round(v, 2)) for k, v in comps.items()},
            "conviction": conv["score"], "label": conv["label"],
            "conviction_basis": conv.get("score_basis"),
            "conviction_effective": conv.get("effective"),
            "coverage": conv["coverage"], "absent": conv["absent"],
            "evidence_weight_priced": conv.get("evidence_weight_priced"),
            "evidence_weight_declared": conv.get("evidence_weight_declared"),
            "score_reason": conv["reason"],
            "offhours": oh,
            "wrappers": [{"token_id": w["token_id"], "symbol": w.get("symbol"),
                          "name": w.get("name"), "issuer_id": w.get("issuer_id"),
                          "issuer_name": w.get("issuer_name"),
                          "join_rule": w.get("join_rule"), "chains": w.get("chains"),
                          "price": w.get("price"), "volume_24h": w.get("volume_24h"),
                          "market_cap": w.get("market_cap"),
                          "live": w["liveness"]["live"], "liveness": w["liveness"]["reason"],
                          "age_hours": w["liveness"]["age_hours"],
                          "basis_bps": w.get("basis_bps"),
                          "score": w["score"].get("score"),
                          "score_components": w["score"].get("components"),
                          "score_reason": w["score"].get("reason")}
                         for w in sorted(priced, key=lambda x: -(_num(x.get("volume_24h")) or 0))],
        }
        # The board ranks underlyings anyone can actually hold. Without the gate the top
        # of a 642-row ranking is decided by tokens with three digits of daily volume,
        # which is a token dump with a sort applied rather than a board.
        if len(live) >= BOARD_MIN_LIVE_WRAPPERS:
            board.append(rec)
        for leg in disloc.get("legs") or []:
            tape.append({**leg, "underlying_id": uid, "underlying_symbol": row.get("symbol"),
                         "median_price": disloc.get("median_price")})

        flow_rows.append({
            "date": today, "underlying_id": uid, "symbol": row.get("symbol"),
            "name": row.get("name"), "asset_type": row.get("asset_type"),
            "price": price, "market_cap": mcap, "total_volume": vol,
            "expected_mcap": res["expected_mcap"], "residual_usd": res["residual_usd"],
            "residual_pct": res["residual_pct"], "residual_pct_daily": daily,
            "price_chg_pct": res["price_chg_pct"], "impulse": impulse,
            "span_days": span, "supply_index": supply_index,
            "wrappers_n": len(priced), "wrappers_live": len(live),
            "issuers_n": len(issuers_here), "chains_n": len(chains_here),
            "dispersion_bps": disloc.get("dispersion_bps"),
            "conviction": conv["score"], "label": conv["label"],
            "coverage": conv["coverage"], "spec_hash": sh,
            "degraded": int(row_degraded), "peer_set_complete": int(bool(row_peers_whole)),
        })

    board.sort(key=lambda r: (r["conviction"] is None, -(r["conviction"] or 0)))
    tape.sort(key=lambda l: -abs(l["basis_bps"]))
    return {"board": board, "flow_rows": flow_rows, "wrapper_rows": wrapper_rows,
            "tape": tape}


def flow_series(path: Path = None, window: int = 30, today: str | None = None) -> dict:
    """Trailing tokenization-impulse series per underlying, oldest first.

    This is what ``score_impulse`` consumes, and it is read from the recorded ledger
    rather than recomputed, because the recorded series is the one that actually existed
    on the nights it describes.
    """
    rows = read_rows(path or RWA_FLOW_CSV, RWA_FLOW_FIELDS)
    out = {}
    for r in sorted(rows, key=lambda x: x.get("date") or ""):
        # Today excluded for the same reason as _prior_flow: assemble() appends tonight's
        # reading to this trail, so leaving today's recorded row in it makes a same-day
        # re-run count one calendar day twice in the impulse component.
        if today is not None and (r.get("date") or "") >= today:
            continue
        v = _num(r.get("residual_pct_daily"))
        if r.get("underlying_id") and v is not None:
            out.setdefault(r["underlying_id"], []).append(v)
    return {k: v[-window:] for k, v in out.items()}


def volume_baseline(path: Path = None, window: int = 14, today: str | None = None) -> dict:
    """Median recorded tokenized 24h volume per underlying, for the off-hours ratio.

    From our own ledger because there is nowhere else: ``total_volume`` is a rolling
    24-hour window on every response, so a baseline cannot be fetched, only accumulated.
    """
    rows = read_rows(path or RWA_FLOW_CSV, RWA_FLOW_FIELDS)
    hist = {}
    for r in sorted(rows, key=lambda x: x.get("date") or ""):
        if today is not None and (r.get("date") or "") >= today:
            continue
        v = _num(r.get("total_volume"))
        if r.get("underlying_id") and v is not None:
            hist.setdefault(r["underlying_id"], []).append(v)
    return {k: _median(v[-window:]) for k, v in hist.items() if v}


def snapshot(session: dict | None = None, getter=None, sleep=None,
             now: datetime | None = None, ledger_dir: Path | None = None,
             write: bool = True) -> dict:
    """One night of the RWA workspace: fetch, join, score, and record.

    Returns the artifact and the three ledger row sets. Never raises on a feed failure —
    every fetch returns a report, a failed report degrades the artifact rather than the
    run, and the caller (``nightly.main``) is a straight-line function with no exception
    handler to catch anything thrown from here. A night where CoinGecko is down must
    produce an RWA artifact that says CoinGecko was down, not a traceback that also
    prevents the crypto ledger from committing.
    """
    import time as _time
    sleep = sleep or _time.sleep
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    ledger_dir = ledger_dir or LEDGER_DIR
    flow_csv = ledger_dir / "rwa_flow.csv"
    session = session if session is not None else cg.open_session()
    delay = fetch_delay(session)

    feeds = {}
    list_rep = fetch_list(session, getter)
    feeds["list"] = {k: list_rep[k] for k in ("status", "detail", "http_status")}
    if list_rep["status"] not in ("live", "partial"):
        return {"status": "unavailable", "date": today, "generated_at": now.isoformat(),
                "session": {"plan": session.get("plan"), "status": session.get("status")},
                "feeds": feeds, "spec_hash": spec_hash(), "board": [], "tape": [],
                "issuers": [], "graph": None,
                "detail": f"the underlying universe is unavailable: {list_rep['detail']}"}
    underlyings = list_rep["data"]

    sleep(delay)
    mkt_rep = fetch_markets(session, getter, sleep)
    feeds["markets"] = {k: mkt_rep[k] for k in ("status", "detail", "http_status")}
    market_rows = mkt_rep["data"] if isinstance(mkt_rep.get("data"), list) else []

    sleep(delay)
    iss_rep = fetch_issuers(session, getter, sleep)
    feeds["issuers"] = {k: iss_rep[k] for k in ("status", "detail", "http_status")}
    issuers = iss_rep["data"] if isinstance(iss_rep.get("data"), list) else []

    graph = build_graph(underlyings, issuers)
    token_ids = [w["token_id"] for w in graph["wrappers"]]
    sleep(delay)
    px_rep = fetch_wrapper_coins(session, token_ids, getter, sleep)
    feeds["wrappers"] = {k: px_rep[k] for k in ("status", "detail", "http_status")}
    wrapper_prices = px_rep["data"] if isinstance(px_rep.get("data"), dict) else {}

    # The two paid endpoints, recorded as declared absences rather than omitted. A reader
    # of the artifact should be able to see what this model could not ask for.
    feeds["tickers"] = {"status": "unavailable", "http_status": 401,
                        "detail": ("/rwas/{id}/tickers is Basic plan or above — bid/ask, "
                                   "cost-to-move, venue depth and the stale/anomaly/trust "
                                   "fields are therefore not computed. Execution is a "
                                   "declared component of both models, is UNAVAILABLE, and "
                                   "its weight is NOT redistributed: it sits in the "
                                   "denominator so coverage can never read complete.")}
    feeds["market_chart"] = {"status": "unavailable", "http_status": 401,
                             "detail": ("/rwas/{id}/market_chart is Basic plan or above — "
                                        "the net-issuance series cannot be backfilled and "
                                        "exists only from the first night recorded here")}

    # ---- OBSERVATION, captured before anything is derived from it ---------------
    # The vendor's own fields and its own timestamp, in their own rows. Everything below
    # is a calculation over these, and a calculation whose inputs were never written down
    # is not auditable later — which matters most for the one series that cannot be
    # re-fetched at any price.
    observed_rows = [{
        "date": today, "run_ts": now.isoformat(),
        "underlying_id": row.get("id"), "symbol": row.get("symbol"),
        "asset_type": row.get("asset_type"),
        "price": _num((row.get("tokenized_market_data") or {}).get("current_price")),
        "market_cap": _num((row.get("tokenized_market_data") or {}).get("market_cap")),
        "total_volume": _num((row.get("tokenized_market_data") or {}).get("total_volume")),
        "source_last_updated": (row.get("tokenized_market_data") or {}).get("last_updated"),
    } for row in market_rows]

    graph_stats = dict(graph)
    graph_stats["wrappers_priced"] = len(wrapper_prices)
    completeness = run_completeness(feeds, graph_stats, len(observed_rows), len(underlyings),
                                    len(issuers), iss_rep.get("listed_n") or len(issuers))
    # How far the freshest source timestamp leads the run's own clock. A few minutes is
    # the run's own duration; more than an hour is a clock worth investigating.
    completeness["source_clock_lead_hours"] = _clock_skew_hours(
        [r["source_last_updated"] for r in observed_rows]
        + [w.get("last_updated") for w in wrapper_prices.values()], now)
    runs_csv = ledger_dir / "rwa_runs.csv"
    prior_quality = prior_run_quality(runs_csv, today)
    prior_status = prior_run_status(runs_csv, today)
    promote = may_promote(completeness["status"], prior_status,
                          completeness["coverage_pct"], prior_quality)

    prior = _prior_flow(flow_csv, today)
    trail = flow_series(flow_csv, today=today)
    baseline = volume_baseline(flow_csv, today=today)
    built = assemble(market_rows, graph, wrapper_prices, prior, today, now,
                     baseline, trail, {i["id"]: i for i in issuers},
                     degraded=not completeness["peer_set_complete"])

    live_by_issuer = {}
    for w in built["wrapper_rows"]:
        if w["live"]:
            live_by_issuer[w["issuer_id"]] = live_by_issuer.get(w["issuer_id"], 0) + 1
    issuer_rows = []
    for i in issuers:
        chains = {c for t in i["tokens"] for c in (t.get("platforms") or {})}
        unders = {w["underlying_id"] for w in graph["wrappers"] if w["issuer_id"] == i["id"]}
        issuer_rows.append({
            "date": today, "issuer_id": i["id"], "name": i.get("name"),
            "market_cap": i.get("market_cap"),
            "market_cap_change_24h": i.get("market_cap_change_24h"),
            "volume_24h": i.get("volume_24h"), "tokens_n": len(i["tokens"]),
            "chains_n": len(chains), "underlyings_n": len(unders),
            "live_tokens_n": live_by_issuer.get(i["id"], 0),
        })

    written, skipped, quarantined = {}, {}, {}
    if write:
        run_row = {
            "date": today, "run_ts": now.isoformat(),
            "run_status": completeness["status"], "spec_hash": spec_hash(),
            "plan": session.get("plan"),
            "underlyings_listed": len(underlyings),
            "underlyings_observed": len(observed_rows),
            "issuers_expected": completeness.get("issuers_listed"),
            "issuers_received": len(issuers),
            "wrappers_in_graph": len(graph["wrappers"]),
            "wrappers_priced": len(wrapper_prices),
            "wrappers_unresolved": len(graph["unresolved"]),
            "feed_list": completeness["feeds"].get("list"),
            "feed_markets": completeness["feeds"].get("markets"),
            "feed_issuers": completeness["feeds"].get("issuers"),
            "feed_wrappers": completeness["feeds"].get("wrappers"),
            "coverage_pct": completeness["coverage_pct"],
            "promoted": int(bool(promote)), "note": completeness["note"],
        }
        # The manifest is APPENDED, never replaced — every run of every night is kept,
        # promoted or not. A rejected run IS the evidence that a rejection happened, and a
        # ledger that silently drops its failures cannot be audited for the one thing this
        # contract exists to guarantee.
        _append_manifest(ledger_dir / "rwa_runs.csv", run_row)
        written["rwa_runs.csv"] = 1

        if promote:
            # An EMPTY row set is still never written: append_daily_rows replaces today's
            # rows, which is right for a re-run with data and exactly wrong without it.
            for name, fields, rows in (
                    ("rwa_observed.csv", RWA_OBSERVED_FIELDS, observed_rows),
                    ("rwa_flow.csv", RWA_FLOW_FIELDS, built["flow_rows"]),
                    ("rwa_issuers.csv", RWA_ISSUER_FIELDS, issuer_rows),
                    ("rwa_wrappers.csv", RWA_WRAPPER_FIELDS, built["wrapper_rows"])):
                if rows:
                    written[name] = append_daily_rows(ledger_dir / name, fields, today, rows)
                else:
                    skipped[name] = ("nothing to record tonight; the existing file is "
                                     "left untouched rather than emptied of today")
        else:
            # The invariant doing its job. A 429 that lost an issuer or a 414 that lost a
            # quarter of the wrapper set must not overwrite the night that got them all.
            # The run is retained where nothing derives from it.
            _append_manifest(ledger_dir / "rwa_quarantine.csv", run_row)
            quarantined = {
                "reason": (
                    f"this run is {completeness['status']} at "
                    f"{completeness['coverage_pct']}% coverage; {prior_status} rows at "
                    f"{(prior_quality or (0, 0.0))[1]}% already stand for {today}. A fetch "
                    f"that saw less may not replace a more complete canonical observation."),
                "run_status": completeness["status"],
                "run_coverage_pct": completeness["coverage_pct"],
                "prior_status": prior_status,
                "prior_coverage_pct": (prior_quality or (0, None))[1],
                "note": completeness["note"], "retained_in": "rwa_quarantine.csv",
            }

    board = built["board"]
    graded = [r for r in board if r["conviction"] is not None]
    artifact = {
        "status": "live" if graded else "degraded",
        "date": today, "generated_at": now.isoformat(),
        "spec_hash": spec_hash(),
        "session": {"plan": session.get("plan"), "status": session.get("status"),
                    "detail": session.get("detail")},
        "feeds": feeds,
        "calendar": session_calendar_status(now),
        "equity_leg": {**{k: v for k, v in equity_prints(ledger_dir).items() if k != "rows"},
                       "required_fields": list(EQUITY_REQUIRED_FIELDS),
                       "artifact": EQUITY_ARTIFACT,
                       "gap_state": EQUITY_PENDING},
        "impulse_provenance": {
            "kind": "DERIVED",
            "observed": ["tokenized_market_data.current_price",
                         "tokenized_market_data.market_cap"],
            "identity": "MC_t / (MC_{t-1} * P_t / P_{t-1}) = implied_units_t / implied_units_{t-1}",
            "claim": "implied change in tokenized supply",
            "not_a_claim": ("net issuance, mint minus redemption, or any verified "
                            "on-chain supply fact"),
            "unverified_assumption": (
                "that CoinGecko's historical market cap equals contemporaneous "
                "circulating units times the same published price, with no supply "
                "revision, reclassification or backfill. This is not documented and has "
                "not been checked against an issuance source, because no free one exists "
                "for these tokens. A vendor restating supply would move this series with "
                "no token minted, and nothing here could tell the difference."),
            "would_promote_it": "a token-supply or mint/burn feed to corroborate against",
        },
        "model": {
            "score_definition": dict(SCORE_DEFINITION),
            "score_basis": SCORE_BASIS,
            # Per-FIELD, not per-column: this file has no opinion about the terminal's
            # layout, and a column map published from here would go stale the first time
            # a column moved.
            "field_evidence": dict(FIELD_EVIDENCE),
            "evidence_vocabulary": {
                EVIDENCE_OBSERVED: "the vendor published this number",
                EVIDENCE_DERIVED: "computed here from observed inputs",
                EVIDENCE_NORMALIZED: "rescaled or banded by the model",
            },
            "declared_weights": dict(DECLARED_WEIGHTS),
            "priceable_weights": dict(COMPONENT_WEIGHTS),
            "execution_weight": W_EXECUTION,
            "execution_status": EXECUTION_UNAVAILABLE,
            "max_coverage_on_this_plan": round(
                100.0 * sum(COMPONENT_WEIGHTS.values()) / sum(DECLARED_WEIGHTS.values()), 1),
            "min_coverage": RWA_MIN_COVERAGE,
            "labels": {"DEEP": RWA_T_DEEP, "SOUND": RWA_T_SOUND, "THIN": RWA_T_THIN,
                       "FRAGILE": RWA_T_FRAGILE, "DORMANT": 0.0},
            "note": ("An independent model. Nothing here is computed by, compared to, or "
                     "convertible into the crypto conviction score, and the labels are "
                     "deliberately a different vocabulary so the two cannot be read as "
                     "one scale."),
        },
        "graph": {
            "underlyings_listed": len(underlyings),
            "underlyings_ranked": len(market_rows),
            "list_only_n": max(0, len(underlyings) - len(market_rows)),
            "list_only_note": ("/rwas/list publishes more underlyings than /rwas/markets "
                               "paginates. Recorded as a named absence rather than "
                               "reconciled away."),
            "underlyings_with_wrappers": graph["underlyings_with_wrappers"],
            "wrappers_n": len(graph["wrappers"]),
            "wrappers_priced": len(wrapper_prices),
            "unresolved_n": len(graph["unresolved"]),
            "unresolved": graph["unresolved"][:20],
            "join_rule_counts": graph["join_rule_counts"],
            "issuers_n": graph["issuers_n"],
        },
        "board": board,
        "board_gate": {
            "min_live_wrappers": BOARD_MIN_LIVE_WRAPPERS,
            "live_volume_floor_usd": WRAPPER_LIVE_VOL_USD,
            "stale_after_hours": WRAPPER_STALE_HOURS,
            "ranked": len(board), "graded": len(graded),
            "excluded": max(0, len(market_rows) - len(board)),
            "note": (f"{max(0, len(market_rows) - len(board))} underlying(s) were fetched "
                     f"and are not on the board: no wrapper cleared "
                     f"${WRAPPER_LIVE_VOL_USD:,.0f} of 24h volume within "
                     f"{WRAPPER_STALE_HOURS:.0f}h. They are in the ledger, not the ranking."),
        },
        "tape": built["tape"][:TAPE_CAP],
        # A truncation the consumer cannot see is the same violation whether it happens
        # in the browser or here. The terminal declares the wrapper cap it applies itself;
        # it can only declare this one if the artifact says the cap exists and how many
        # legs it dropped.
        "tape_total_n": len(built["tape"]),
        "tape_cap": TAPE_CAP,
        "tape_kind": "wrapper_price_divergence",
        "tape_stage": DIVERGENCE_STAGE,
        "tape_note": ("WRAPPER PRICE DIVERGENCE — pre-execution. Wrapper against wrapper, "
                      "on the median of the live wrappers sharing the deepest "
                      "denomination. NOT against tokenized_market_data.current_price, "
                      "which is a blend of these same wrappers. NOT an executable "
                      "dislocation, an executable basis, or an after-friction "
                      "opportunity: bid/ask, depth, cost-to-move and the trust fields all "
                      "need /rwas/{id}/tickers. The per-leg score is OBSERVATION evidence "
                      "and is not a confidence in a trade."),
        "execution": {"status": "UNAVAILABLE",
                      "detail": EXECUTION_UNAVAILABLE,
                      "requires": "/rwas/{id}/tickers (Basic plan or above)",
                      "missing_fields": ["bid/ask", "cost-to-move-up", "cost-to-move-down",
                                         "venue depth", "stale", "anomaly", "trust score"],
                      "promotion_rule": ("a divergence becomes an EXECUTABLE dislocation "
                                         "only after a ticker feed exists AND the spread, "
                                         "depth and friction gates pass. No code path in "
                                         "this module can set that state.")},
        "issuers": sorted(issuer_rows, key=lambda r: -(_num(r["market_cap"]) or 0)),
        "run": {**completeness, "promoted": bool(promote),
                "prior_status": prior_status, "prior_quality": prior_quality,
                "run_ts": now.isoformat()},
        "written": written,
        "not_written": skipped,
        "quarantined": quarantined,
    }
    # The artifact follows the same promotion rule as the ledgers: a degraded run must not
    # overwrite the board a complete one published. Written atomically either way — to
    # rwa.json when promoted, to rwa.degraded.json when not — so the degraded view stays
    # inspectable without ever being canonical.
    if write:
        _atomic_write(ledger_dir / ("rwa.json" if promote else "rwa.degraded.json"),
                      json.dumps(artifact, indent=2))
    return artifact


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------
def _smoke() -> int:
    """Prove the shapes against the live API, the way coingecko.py does.

    Every field name in this module came from a response this repository actually
    received on 2026-09-01, not from documentation — ``/rwas/{id}`` omitting ``tokens[]``
    below the Basic plan, and ``sparkline_in_7d`` sitting INSIDE ``tokenized_market_data``
    rather than beside it, are both things the documentation does not say. This
    entrypoint is what keeps those facts true after a CoinGecko change.

    Exit code is non-zero only when a feed that should be FREE fails. The two paid
    endpoints are expected to refuse and their refusal is not an error; a run without a
    key reports and returns success.
    """
    now = datetime.now(timezone.utc)
    sess = cg.open_session()
    delay = fetch_delay(sess)
    print(f"[rwa] session: {sess['plan']} / {sess['status']} — {sess['detail']}")
    print(f"[rwa] spec {spec_hash()} · calendar {session_calendar_status(now)['detail']}")
    rc = 0

    lst = fetch_list(sess)
    print(f"[rwa] list: {lst['status']} — {lst['detail']}")
    if lst["status"] != "live":
        return 1
    types = {}
    for u in lst["data"]:
        types[u["asset_type"]] = types.get(u["asset_type"], 0) + 1
    print(f"[rwa] universe: " + ", ".join(f"{k} {v}" for k, v in sorted(types.items())))

    import time as _time
    _time.sleep(delay)
    # One page on purpose: the smoke is proving SHAPES, and three pages would spend two
    # more minutes of rate limit to re-prove the same one. The truncation notice in the
    # detail below is therefore expected here and is a finding only in the nightly.
    mkt = fetch_markets(sess, max_pages=1)
    print(f"[rwa] markets: {mkt['status']} — {mkt['detail']} (one page sampled on purpose)")
    if mkt["status"] not in ("live", "partial"):
        rc = 1
    else:
        top = mkt["data"][0]
        tmd = top.get("tokenized_market_data") or {}
        spark = (tmd.get("sparkline_in_7d") or {}).get("price") or []
        print(f"[rwa] tape: {top['id']} ${_num(tmd.get('current_price'))} "
              f"cap ${(_num(tmd.get('market_cap')) or 0)/1e9:.2f}B "
              f"· {len(spark)} sparkline point(s) · stamped {tmd.get('last_updated')}")
        if not spark:
            print("[rwa] WARN sparkline absent — the off-hours reading has no input")
            rc = 1

    _time.sleep(delay)
    for path in ("/rwas/gold/tickers", "/rwas/gold/market_chart"):
        rep = cg.get(sess, path, {"vs_currency": "usd", "days": 7}, retries=1)
        # A 401 here is the documented shape of the free tier and is not a failure. A 200
        # is news: it means the plan changed and two components of this model can start
        # being computed.
        note = ("expected on this plan" if rep["http_status"] == 401
                else "PLAN CHANGED — execution and market_chart are now available")
        print(f"[rwa] {path}: {rep['status']} [{rep['http_status']}] — {note}")
        _time.sleep(delay)

    iss = fetch_issuers(sess)
    print(f"[rwa] issuers: {iss['status']} — {iss['detail']}")
    if iss["status"] not in ("live", "partial"):
        return 1

    graph = build_graph(lst["data"], iss["data"])
    total = len(graph["wrappers"]) + len(graph["unresolved"])
    pct = 100.0 * len(graph["wrappers"]) / total if total else 0.0
    print(f"[rwa] join: {len(graph['wrappers'])}/{total} wrapper(s) resolved ({pct:.1f}%) "
          f"over {graph['underlyings_with_wrappers']} underlying(s)")
    print(f"[rwa] rules: " + ", ".join(f"{k}={v}" for k, v in
                                       sorted(graph["join_rule_counts"].items()) if v))
    for u in graph["unresolved"][:5]:
        print(f"[rwa]   unresolved: {u['symbol']} ({u['token_id']}) from {u['issuer_id']}")
    # The join is the load-bearing inference in this module. 99.3% was measured over the
    # whole live graph, and the eight tokens that do not resolve wrap underlyings
    # CoinGecko does not carry. A floor of 97% therefore leaves room for a few new
    # unlisted names and still catches a lost convention — which is a failure that raises
    # nothing on its own: the graph builds, and the missing wrappers are simply not in it.
    if pct < JOIN_MIN_RESOLUTION_PCT and total:
        print(f"[rwa] FAIL join resolution fell to {pct:.1f}%, below the "
              f"{JOIN_MIN_RESOLUTION_PCT:.0f}% floor — a naming convention changed")
        rc = 1
    return rc


def summarize(art: dict) -> list:
    """The run, in the lines the nightly prints for it. One place, two callers."""
    lines = []
    g = art.get("graph") or {}
    bg = art.get("board_gate") or {}
    if art.get("status") == "unavailable":
        lines.append(f"[rwa] unavailable — {art.get('detail')}")
    else:
        lines.append(f"[rwa] {art['status']} · {bg.get('ranked', 0)} ranked / "
                     f"{bg.get('graded', 0)} graded of {g.get('underlyings_ranked', 0)} "
                     f"underlying(s) · {g.get('wrappers_priced', 0)}/{g.get('wrappers_n', 0)} "
                     f"wrapper(s) priced · {g.get('unresolved_n', 0)} unresolved "
                     f"· spec {art.get('spec_hash')}")
    for name, rep in (art.get("feeds") or {}).items():
        if rep["status"] not in ("live", "unavailable"):
            lines.append(f"[rwa] {name}: {rep['status']} — {rep['detail']}")
    run = art.get("run") or {}
    if run:
        lines.append(f"[rwa] run {run.get('status')} · coverage {run.get('coverage_pct')}% · "
                     f"promoted={run.get('promoted')} · {run.get('note')}")
        lines.append(f"[rwa] issuers {run.get('issuers_received')}/{run.get('issuers_listed')} "
                     f"· wrappers {run.get('wrappers_priced')}/{run.get('wrappers_in_graph')} "
                     f"· universe {run.get('observed_n')}/{run.get('listed_n')}")
    if art.get("quarantined"):
        q = art["quarantined"]
        lines.append(f"[rwa] QUARANTINED — {q.get('reason')}")
        lines.append(f"[rwa] tonight's canonical rows are unchanged; the attempt is kept in "
                     f"{q.get('retained_in')}")
    if art.get("written"):
        lines.append("[rwa] ledger: " + ", ".join(f"{k} {v} row(s)"
                                                  for k, v in sorted(art["written"].items())))
    top = [r for r in (art.get("board") or []) if r.get("conviction") is not None][:5]
    if top:
        lines.append("[rwa] " + " | ".join(
            f"{(r['symbol'] or '').upper()} {r['conviction']:.0f} {r['label']}" for r in top))
    return lines


def _release(snap=None) -> int:
    """One RWA snapshot on its own, for the dispatchable release workflow.

    The same call the nightly makes, writing the same files under the same promotion
    invariant, and nothing else. It exists because the nightly's commit step sits behind
    the crypto gates, and the canonical ATR eligibility gate refuses a same-day re-run of
    the crypto pipeline — measured on 2026-09-01: the scheduled run committed at 11:43
    UTC, a 19:21 UTC dispatch failed that gate (22 bars recorded against 21 rebuilt), and
    the RWA snapshot that had come back COMPLETE in the same job was lost with the
    runner. An independent model with an independent ledger needs an independent way to
    publish it, and one that can never touch a crypto file.

    Non-zero only when nothing was written: the universe was unavailable, or the run
    FAILED. A DEGRADED run that was quarantined exits 0 — the quarantine row and the
    manifest row ARE the evidence, and they are what the workflow commits.
    """
    art = (snap or snapshot)()
    for line in summarize(art):
        print(line)
    if art.get("status") == "unavailable":
        return 1
    if (art.get("run") or {}).get("status") == RUN_FAILED:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_release() if "--snapshot" in sys.argv[1:] else _smoke())
