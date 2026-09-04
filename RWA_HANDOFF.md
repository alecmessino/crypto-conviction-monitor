# Claude Code handoff — RWA workspace hardening

> **Committed to the repo on 2026-09-04 so a fresh session can read it.**
> The brief below is the original, verbatim and unedited. Parts of it were found to be
> wrong once the file was open; **read `## Amendments` at the end before acting on any
> work item.** The amendments are the authority where they and the brief disagree.

Paste everything below the line into Claude Code from the repo root.

---

## Context

This repo publishes a single static terminal to GitHub Pages at
`https://alecmessino.github.io/crypto-conviction-monitor/`. `index.html` is the whole
front end: ~5,660 lines, inline CSS and inline JS, no build step, no dependencies, no
framework. A nightly Python job writes committed artifacts under `ledger/`, and the page
renders them. `tests/test_terminal.py` asserts against the markup.

I want the **RWA workspace** (`[data-ws="rwa"]`, reachable at `#rwa`) brought to
institutional standard. The crypto workspace is out of scope except where a shared
function has to change; if a change touches shared code, say so explicitly and keep the
crypto board's rendered output byte-identical.

### Conventions this codebase already holds, which you must hold too

1. **Nothing is estimated in place of missing data.** Every absence is named on screen
   with its cause. `ledger/rwa.json` is authoritative; the JS renders it and never
   recomputes a score, a threshold, or a weight. A second copy of a threshold in
   JavaScript is a second set of thresholds.
2. **Comments explain why, and usually name the failure that motivated the code.** Match
   that register. Do not add comments that restate what the line does.
3. **Tooltips are `data-tip`, never `title`.** `initTips()` upgrades them through a
   MutationObserver, so anything you render dynamically is picked up. A native `title`
   renders on no phone.
4. **Element ids are load-bearing.** Renderers and tests select on them. Do not rename
   `tbl-rwa`, `tbl-rwa-wrap`, `tbl-rwa-iss`, `tbl-rwa-flow`, `tbl-rwa-disloc`,
   `rw-inspector`, `rw-offhours`, `rw-filter`, `rw-gate`, `rwa-venues`, `rwa-issuance`,
   `rwa-snap-date`, `rwa-run`, `rw-under`, `rw-wrap`, `rw-iss`, `rw-cov`, `rw-exec`,
   `rw-model`, `rw-absent`, `rw-join`, `rw-spec`, `rw-insp-sym`.
5. `tests/test_terminal.py` slices the markup between the status ribbon and the board
   heading and fails on any class in that slice beginning `ribbon`. Read the test file
   before moving markup around.
6. Tables use a **roving tabindex** (one row at 0, the rest at -1, arrow keys move
   focus). Keep that pattern for anything you add.
7. There is a `prefers-reduced-motion` block near the top of the CSS. Anything animated
   must degrade there.
8. `--tert` is `#7C8AA3` deliberately, for contrast. Do not darken it.

### Ground truth about the data, verified today

- `ledger/rwa.json` is **2.0 MB** raw, ~164 KB gzipped, `last-modified` once per night.
- `ledger/signals.json` is **3.26 MB** raw.
- `rwa.json` top-level keys: `status, date, generated_at, spec_hash, session, feeds,
  calendar, equity_leg, impulse_provenance, model, graph, board, board_gate, tape,
  tape_kind, tape_stage, tape_note, execution, issuers, run, written, not_written,
  quarantined`.
- `board` has 305 rows. Row keys include `id, symbol, name, degraded,
  peer_set_complete, asset_type, price, market_cap, total_volume, price_chg_pct_24h,
  wrappers_n, wrappers_live, issuers, chains, conflicts_n, flow, dislocation,
  components, conviction, label, conviction_effective, coverage, absent, score_reason,
  offhours, wrappers`.
- `flow` keys: `expected_mcap, residual_usd, residual_pct, price_chg_pct,
  residual_pct_daily, impulse, span_days, supply_index, chain_days, trail`.
- `offhours` keys: `status, detail, window, offhours_return_pct, price_at_close,
  price_now, wrappers_live, wrappers_voting, wrappers_agree, agreement, dispersion_bps,
  volume_ratio, implied_gap_pct, implied_gap_state, implied_gap_confidence,
  implied_gap_blocked_by`. `window` carries `closed_at, hours_closed, kind,
  close_price_from, sparkline, inferred_hours`.
- `tape` has 130 legs. Keys: `token_id, symbol, issuer_id, price, basis_bps,
  volume_24h, age_hours, stage, observation_evidence, execution_evidence,
  executable_after_friction, join_rule, underlying_id, underlying_symbol, median_price`.
- 1,077 wrappers in graph. `renderRwaWrappers()` renders `all.slice(0,300)`.

Fetch the live artifact and read the real values before you change any renderer that
consumes them. Do not infer a field's shape from my summary.

---

## Work items

Deliver in the order given. After each phase, stop, show me the diff summary and the
verification output, and wait. Do not batch phases.

---

### PHASE 1 — the two panels that discredit the page

Both of these currently open on rows that a professional reads as broken data, which
costs the page its credibility before the reasoning is read.

#### 1.1 Flows tab: rank by materiality, not by percentage

`renderRwaFlows()` sorts by `Math.abs(flow.residual_pct_daily)`. On tonight's artifact
that puts TMO at +1,724.8% on a $244K tokenized cap at the top, followed by four more
sub-$100K names. The percentage is arithmetically correct and completely uninformative
at that size.

- Default sort becomes `Math.abs(flow.residual_usd)` descending.
- Add a small control above the table with three orderings: `residual $` (default),
  `residual %`, `supply index`. Persist the choice for the session only.
- Add a `Span` column reading `flow.span_days`, and render `span_days === 1` rows with a
  visible one-night marker. A 1,724% daily residual computed across a single night is a
  different object from one computed across a chain, and nothing on screen currently
  distinguishes them.
- Keep every row. Do not filter microcaps out. Reorder and label.

#### 1.2 Off-Hours Tape: separate real overnight discovery from pricing artifacts

Two structural problems in `renderRwaOffhours()`:

- Every live window in tonight's artifact has `window.inferred_hours === true` and
  `window.sparkline` reading `"169 hourly point(s) inferred backwards from
  last_updated"`. The close price is therefore **derived from an inferred series**, and
  the panel presents the resulting return in the same typeface as an observed one.
- Most rows have `wrappers_voting === 1`, which renders as "1/1 wrappers agree". An
  agreement statistic over a single voter is not an agreement statistic. LULU currently
  prints -19.28% on exactly that basis.

Required behaviour:

- Split the panel into two labelled groups: **corroborated** (`wrappers_voting >= 2`)
  and **single-wrapper** (`wrappers_voting < 2`). Corroborated first. Do not hide the
  single-wrapper group; demote it and name why it is demoted.
- Where `window.inferred_hours` is true, attach `<span class="ev ev-derived">inferred
  close</span>` to the row and a `data-tip` carrying `window.sparkline` verbatim.
- Suppress the "N/N wrappers agree" string entirely when `wrappers_voting < 2`. Replace
  it with `single wrapper — no corroboration`.
- Keep the existing IMPLIED MONDAY GAP block exactly as it is. It is correct.

**Acceptance:** load `#rwa` against the live artifact. The Flows tab's first five rows
are the five largest dollar residuals. The Off-Hours panel's first rows all have two or
more voting wrappers. No row anywhere asserts agreement over a single voter.

---

### PHASE 2 — fetch and render discipline

Currently `loadRwa()` fetches 2.0 MB with `cache:"no-store"` on every page load, for
every visitor including those who never open the RWA workspace, and re-fetches on a
120-second `setInterval`. `load()` does the same for `signals.json` at 3.26 MB. Both
artifacts change once per night. A tab left open for a working day pulls hundreds of
megabytes of a file that did not change.

Implement all four:

1. **Manifest gate.** Add a nightly-written `ledger/manifest.json` carrying at minimum
   `{ "rwa": {"date": ..., "spec_hash": ...}, "signals": {"date": ...}, "generated_at":
   ... }`. Write it from the same Python job that writes the other artifacts, in the
   same commit, so it can never describe a night that was not published. The 120s timer
   fetches the manifest only. A full artifact refetch happens only when a hash or date
   changes.
2. **Lazy RWA load.** Remove the unconditional `loadRwa().then(renderRwa)` at module
   scope. Load on first reveal of the RWA workspace, driven by `switchWorkspace()`,
   which already calls `renderRwa()` on reveal and already handles the `#rwa` deep link.
   A reader who never opens RWA never fetches the 2 MB.
3. **Drop `cache:"no-store"`** on the artifact fetches. GitHub Pages serves an ETag; a
   conditional request answers 304 at a few hundred bytes. Keep `no-store` only on the
   manifest if you find caching makes it stale.
4. **Visibility guard.** Suspend both timers on `document.hidden` and do one immediate
   manifest check on `visibilitychange` back to visible.

Then: **only re-render when the data changed.** Today's full `innerHTML` rebuild every
120 seconds destroys scroll position inside `.scroll-pane`, kills an in-progress text
selection, and drops focus off a focused row. With the manifest gate this stops being a
problem for free, but add an explicit guard so a future caller cannot reintroduce it: if
`RWA.spec_hash` and `RWA.date` are unchanged, `renderRwa()` returns without touching the
DOM. Add a `force` parameter for the filter and sort paths that must re-render.

**Acceptance:** open DevTools Network, load `#crypto`, wait five minutes. Zero requests
for `rwa.json`. Switch to `#rwa`: one request. Wait five more minutes: manifest requests
only, each under 1 KB. Scroll the underlyings board halfway and leave it five minutes:
scroll position is unchanged.

---

### PHASE 3 — sorting, and the undeclared truncation

#### 3.1 Sortable columns on all four RWA tables

305 underlyings across 12 columns with a single fixed order is the largest practical gap
on the screen. The model's own ranking is exactly the ordering an analyst wants to argue
with.

- Click-to-sort on every column of `tbl-rwa`, `tbl-rwa-wrap`, `tbl-rwa-flow` and
  `tbl-rwa-disloc`. Ascending and descending, with an explicit third click returning to
  the model's own order on `tbl-rwa`.
- Sort indicator in the header. Set `aria-sort` on the active `th`.
- Headers must remain keyboard-reachable and activate on Enter and Space.
- Nulls sort last in both directions, never as zero. A missing dispersion is not a
  dispersion of zero, and this codebase has already had to remove one substitution of
  that kind.
- **The RWA Conv column needs a stated rule.** `conviction` is normalized over available
  evidence, so sorting it ranks a 94-on-83%-evidence above an 88-on-100%. Sort on raw
  `conviction`, and put a `data-tip` on that header saying so and naming
  `conviction_effective` as the alternative available in the inspector. State the choice
  rather than leaving it implicit.

#### 3.2 Declare the 300-row wrapper cap

`renderRwaWrappers()` truncates 1,077 wrappers to 300 with no note anywhere. Every other
gap on this page is named: 345 gated underlyings, 8 unresolved joins, execution at 401,
the calendar refusal. This is the one absence the page does not declare, and it is the
inconsistency a skeptical reader would find and hold against everything else.

Either render the count in the panel header — `showing 300 of 1,077, ranked by 24h
volume` — with a control to lift the cap, or virtualize the table and remove the cap.
Prefer the first; it is smaller and it is honest, which is the house standard.

**Acceptance:** grep the RWA renderers for every `.slice(` and confirm each one is
either declared on screen or documented in a comment as bounded by the data itself.

---

### PHASE 4 — visual

Read `/mnt/skills/public/frontend-design/SKILL.md` equivalent guidance if available in
your environment, but the constraints below are specific to this page and take
precedence.

#### 4.1 Give the board its height back

`.scroll-pane{max-height:34vh}` is global. On a 1440px display the flagship 305-row
board shows about eleven rows, and the reading experience becomes a scroll pane inside a
page inside a workspace.

- On the RWA workspace only, the underlyings board gets roughly `min(62vh, 720px)`.
- The tabbed plate below it becomes collapsible, defaulting open, remembering state for
  the session.
- Do not change `.scroll-pane` globally; scope it. The crypto workspace's stacked panels
  depend on the current value.

#### 4.2 Stop the two colour systems colliding

`.rwa-badge` runs green through red for DEEP → SOUND → THIN → FRAGILE. `.pos/.neg` runs
the same green and red for price direction, one column away. A DEEP badge beside a red
-3% reads as internally contradictory at a glance, and this is the single biggest cost
to scan speed on the board.

- Reserve green and red **exclusively** for signed numbers.
- Re-express the signal band as a non-hue ramp: filled, then tinted, then outlined, then
  dashed for DORMANT and UNRATED. Weight and border, not hue. Keep the text labels
  exactly as they are.
- Verify the result stays legible under `prefers-contrast: more` and in grayscale.

#### 4.3 Mark evidence at the column, not the cell

The five-state evidence vocabulary is the strongest idea on the page and the board
barely uses it. Price, Cap, Vol and Disp all print identically whether observed or
derived; the only place a reader learns that Impulse is derived is inside a header
tooltip.

Add a small evidence marker to the column **headers** of `tbl-rwa` — observed, derived,
normalized — driven by `model.score_definition` and the artifact rather than hardcoded.
Per-cell marking would be noise; per-column it survives a screenshot.

#### 4.4 Raise the type floor

`.ev` is 7px and the coverage sub-line is 8px. Those annotations are the reasoning, and
they are currently the least legible thing on the page. 10px floor across the RWA
workspace, bought back from vertical padding rather than from density.

#### 4.5 Fix the cold-load state

A first-time reader currently sees: nav, strip, three folded cards, a truncated board,
and an empty right rail instructing them to click something.

- Default `RWA_SEL` to `board[0].id` on first render so the inspector ships populated.
  Keep click-to-toggle behaviour; only the initial null changes.
- Open the RWA MODEL card by default. A reader needs the weights to interpret the score
  in front of them before they need the caveats.
- Fold WHAT THIS MODEL CANNOT SEE by default, and surface a persistent count chip in the
  status strip — `2 feeds unavailable` — that expands it. The absences stay one click
  away and stay visible as a count.

#### 4.6 Promote the Off-Hours Tape

Once Phase 1.2 lands, it is the most distinctive panel on the workspace and it sits at
the bottom of the right rail below a formerly empty inspector. Move it above the
inspector, or give it a summary line in the status strip.

#### 4.7 Recolour the execution gap

`rw-exec` renders `UNAVAILABLE` in `.neg` red as the fifth item in the status strip. Red
reads as a system that fell over. This is a scoped plan limitation, not a fault.

- Amber, not red.
- Label it `NOT ON THIS PLAN` with the existing `data-tip` naming
  `/rwas/{id}/tickers` and the HTTP 401.
- Same treatment on the Divergence banner and the Venues panel.

---

### PHASE 5 — practical, second tier

- **CSV export per pane.** A copy-to-clipboard control that emits the currently
  filtered, currently sorted rows with headers. A clipboard helper already exists in the
  file; reuse it rather than adding a second one. Emit the same precision shown on
  screen, and include a header comment line carrying `RWA.date` and `RWA.spec_hash` so
  an exported file can never be mistaken for a different night.
- **Filter chips.** The free-text box is substring-only over symbol, name, asset type,
  label and issuers. Add chips for asset type, degraded peer set only, multi-denomination
  only, and coverage below the grading floor. Keep the text box.
- **URL state.** `RWA_SEL`, the active tab, the filter and the sort never reach the URL,
  so a view cannot be sent to a colleague. Serialize to the hash —
  `#rwa/<underlying-id>?tab=disloc&q=gold&sort=disp` — and restore on load. Keep
  `history.replaceState` so the 120s timer never writes history, consistent with the
  existing comment on `switchWorkspace()`.
- **Snapshot age on the strip.** The crypto workspace shows `2026-09-03 · 16h`. RWA shows
  the date only. Compute age from `generated_at` and render it beside `rwa-snap-date`,
  with a threshold colour once it exceeds the nightly cadence by a stated margin.
- **Null guard in `renderRwaDisloc()`.** It calls `l.basis_bps.toFixed(0)` and
  `l.basis_bps>0` with no null check, unlike every sibling renderer, all of which route
  through `rwNum`. One null in `RWA.tape` throws inside the `.map()` and takes the whole
  tbody with it. Route it through `rwNum` and render `—` on null.
- **Complete the ARIA contract on the tabs.** `role="tablist"` and `role="tab"` are
  present, but the panes carry no `role="tabpanel"` and no `aria-labelledby`, there is no
  arrow-key roving between tabs, and the `aria-disabled` deferred tab still activates its
  pane. Half a declared contract is worse than none. Either complete it or remove the
  roles and let them be buttons.
- **`aria-live` on the board.** Announce the row count when the filter or sort changes.
  The crypto side already has a `role="status"` / `aria-live="polite"` pattern; reuse it.

---

## Non-negotiables

- No new runtime dependencies. No build step. `index.html` stays a single file served
  statically.
- No number appears on screen that was not read from an artifact or derived by
  arithmetic that is stated on screen.
- Nothing is filtered out silently. Demote, label, and name the reason.
- No client-side recomputation of a score, weight, band or threshold that `rwa.py`
  already computed.
- Preserve every element id listed above.
- `tests/test_terminal.py` passes. If a change requires a test change, show me the test
  diff separately and justify it.

## Verification before you hand each phase back

1. `python -m pytest tests/ -q`
2. Load `#rwa` in a browser against the live artifact and confirm the phase's stated
   acceptance criteria.
3. Load `#crypto` and confirm the crypto board renders identically to `main` — diff a
   screenshot or dump the rendered `tbl-conviction` tbody before and after.
4. Keyboard-only pass over anything you added: reachable, operable, visible focus ring.
5. Report the byte delta on `index.html` and the number of network bytes a five-minute
   idle session now costs, before and after.

## Deliverable per phase

A diff summary in prose, the verification output, and one paragraph on anything you
found that this brief got wrong. If a work item is a bad idea once you have the file
open, say so and do not implement it. I would rather have the objection than the
compliance.


---

## Amendments

Recorded as each phase was accepted. **Where these and the brief above disagree, these
win.** The brief is kept verbatim so the reasoning behind each correction stays legible.

### Status

| Phase | State |
|---|---|
| 1 — flows materiality, off-hours corroboration | accepted |
| 2 — fetch and render discipline | accepted |
| 3 — sorting and the wrapper cap | in progress |
| 4 — visual | not started |
| 5 — practical, second tier | not started |
| 6 — backlog below | not started |

### A1 · `span_days` — the brief's marker is inverted (§1.1)

The brief asks for a marker on `span_days === 1`. That is backwards. `_span_days()` in
`rwa.py` is the gap between the previous observation and tonight, so `1` is the *clean*
case where `residual_pct_daily` **is** the observed one-night residual. Above 1 the chain
has a gap and Per Day is `_daily_rate()`'s geometric de-compounding — the derived one.
On the 2026-09-04 artifact every one of the 305 rows is `span_days === 1`, so the marker
as briefed would have fired on every row and said nothing.

**Shipped:** the Span column marks `span_days > 1` with `ev-derived`. Zero rows tonight,
which is the marker working. TMO's +1,724.8% is not a span artifact — it is a $244K
tokenized cap, which is what the dollar sort addresses.

### A2 · Sort presets are derived, never stored (§3.1 vs §1.1)

Phase 3's click-to-sort on `tbl-rwa-flow` collides with Phase 1's ordering control. They
are reconciled into **one** sort state `{col, dir}` with one comparator and one
serialized `?sort=` value. The three buttons are named entry points that *set* that
state; the active preset is **derived** from `{col, dir}` on render and never stored
beside it. Clicking a column header clears the preset highlight without any separate
bookkeeping. The control stays because it is what makes the table's sortability and its
default ordering discoverable at all.

### A3 · All view state lives in the hash — no browser storage

Superseded: the brief's "persist the choice for the session only" (§1.1). An ordering in
`sessionStorage` cannot be sent to anyone — a colleague opening the link gets the default
and a different top row, and neither party can tell. Grammar:

    #<workspace>[/<id>][?k=v&k=v]

`hashParse()` / `hashWrite()` own it; `replaceState` only, so neither the timer nor any
control writes history. Defaults are absent from the URL rather than spelled out in every
copied link. `localStorage` remains in use for the sizing inputs only — those are a
reader's own numbers, not a view of the board.

### A4 · The manifest gate reports its own failure modes (§2)

The gate added in Phase 2 fails **open** in both directions, and says so on the strip via
`rw-feed`:

- **UNGATED** — `manifest.json` has not answered for `GATE_MISS_LIMIT` consecutive
  checks. Artifacts fall back to `cache:"no-cache"` conditional revalidation, so a 304
  proves the file did not change and a body replaces it. The cost is bandwidth, never
  accuracy.
- **BEHIND** — the manifest describes a published night this browser could not fetch. The
  board and the Snapshot date are both the *older* night, because `rwa-snap-date` is read
  from `RWA.date` and never from the manifest. A date from one night over a board from
  another is the one arrangement this strip must not show.

The state is derived from the miss counter, the manifest and the artifact in memory —
never from a flag set at the failure site, which would be one more thing able to disagree
with the board.

### A5 · Both publishing paths share one concurrency group (§2)

`nightly.yml` and `rwa_release.yml` both write `ledger/manifest.json`. Without
serialisation the crypto job can push between the RWA job's checkout and its push, and
the RWA job then publishes a manifest whose signals hash describes the file it checked
out — every client reads that as "signals unchanged" and never refetches again.

Both carry `concurrency: {group: ledger-publish, cancel-in-progress: false}` —
`false` because cancelling a nightly mid-run loses `rwa_flow.csv`, which
`/rwas/{id}/market_chart` cannot backfill at any price. Each also rebases before push and
**unconditionally regenerates** the manifest from the rebased tree before amending.
Simulating the race is what showed why the regeneration cannot be a conflict handler: git
merges the two manifests **cleanly**, because the two jobs touch different lines of the
same JSON, and produces a file describing neither tree — no conflict, no exit code, no
way to notice. The clean merge is the dangerous case.

---

## PHASE 6 — backlog, not scheduled

### 6.1 Split the signals ledger so cold load drops under 1 MB

After Phase 2 a cold load on `#crypto` is ~3,971 KB, and `signals.json` is 3,279 KB of
it — the single largest remaining cost, and mostly history nobody reads on arrival. It
carries 1,740 rows spanning every recorded night; the board needs the latest night, and
the rewind scrubber needs the rest.

Split it into `signals_latest.json` and `signals_history.json`, load the history only
when the rewind scrubber is first touched, and apply the same lazy principle that took
`rwa.json` off the cold path. Estimated cold load after: **under 1 MB**.

Both new files go in the manifest, keyed like everything else. `LEDGER_BY_DATE` and
`REWIND_DATES` are built from the full row set today, so the rewind path is the one that
has to change; `ALL_ROWS` consumers must be audited for anything that silently depends on
history being present at first paint — `latest[]`, `STRAT`, `ATR14` and `TURN_Z` all read
`ALL_ROWS` and all want only the newest row per symbol.

Raised while measuring Phase 2. Not scheduled.
