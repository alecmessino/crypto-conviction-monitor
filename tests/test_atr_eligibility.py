"""The ATR transition verifies itself.

Today every row on the board refuses a stop, because the recorded bar series is one
night short of the ATR window: quant.atr needs fifteen bars and the deepest symbol has
fourteen. That refusal is correct, and this file says so rather than treating it as a
failure.

What it must not become is a manual follow-up. The night the first symbol reaches the
window, the production path has to prove itself without anybody remembering to look. So
the invariant is written over ELIGIBILITY rather than over a symbol or a date:

  * no row eligible          -> every atr14 null is VALID, and the gate says "accumulating"
  * some row eligible        -> EVERY eligible row must carry a finite positive atr14,
                                unless that row carries an explicit structured reason
                                saying why the computation was impossible
  * a row that is not eligible must NEVER carry an atr14
  * every atr14 that exists must reach the serialized output with its observation date
    and its bar count, and the terminal must read all three

The invariant is over every eligible row, not over the cohort. "At least one eligible
row produced a value" was the earlier rule and it is satisfied by one working row while
every other eligible row silently fails — which is the shape of the bug such a gate
exists to catch, so the gate would have been quietest exactly when it mattered.

Bar count alone is not proof of computability. `adx_bars` is `len(bars)`, so it settles
the WINDOW; a true range also needs a high, a low and the previous close on every bar,
and quant.atr returns None when any of the three is absent however long the series is.
An eligible row therefore has two distinguishable ways to hold a null, and only one of
them is acceptable. `atr_status` — `accumulating`, `computed`, `input_missing`, written
by the same function that produces the value — is what separates them. A null on an
eligible row is a FAILURE unless the row says `input_missing`; a blank reason is not an
excuse, because a new failure mode must not be able to arrive wearing one.

Nothing here names HBAR, or ZEC, or 2026-08-26. A gate keyed to a symbol passes forever
once that symbol is dropped from the universe, which is the failure mode of a test
written the day before the data arrives.

Runs under pytest, and standalone for the nightly.
"""
import csv
import importlib.util
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_q = importlib.util.spec_from_file_location("quant", os.path.join(_ROOT, "quant.py"))
quant = importlib.util.module_from_spec(_q)
_q.loader.exec_module(quant)

LEDGER = os.path.join(_ROOT, "ledger", "signals.json")
TERMINAL = os.path.join(_ROOT, "index.html")

# The bar count recorded beside every row. It is len(bars) over the same accumulated
# series quant.atr consumes, so it is the eligibility input, read rather than restated.
BAR_FIELD = "adx_bars"


def _num(row, key):
    v = row.get(key)
    if v is None or str(v).strip() in ("", "None"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def latest_rows() -> list:
    with open(LEDGER, encoding="utf-8") as fh:
        rows = json.load(fh)["rows"]
    latest = max(r["date"] for r in rows)
    return [r for r in rows if r["date"] == latest]


def check_the_window_is_derived_not_assumed():
    """Fifteen is `period + 1`, and the gate asks quant for it.

    Hardcoding the number here would let the two drift: raise ATR_PERIOD and this file
    would go on testing a window the code no longer uses, and would go on passing.
    """
    need = quant.atr_min_bars()
    assert need == quant.ATR_PERIOD + 1, "atr_min_bars no longer matches its period"

    def series(n):
        return [{"high": 10 + i, "low": 9 + i, "close": 9.5 + i} for i in range(n)]

    assert quant.atr(series(need - 1)) is None, \
        f"an ATR was produced from {need - 1} bars, one short of the window"
    val = quant.atr(series(need))
    assert val is not None and val > 0, \
        f"no ATR was produced from {need} bars, which is exactly the window"


STATUS_FIELD = "atr_status"


def _status(row):
    v = row.get(STATUS_FIELD)
    v = "" if v is None else str(v).strip()
    return v or None


def _finite_positive(v):
    return v is not None and v == v and v > 0 and v not in (float("inf"), float("-inf"))


def atr_invariant(rows, need, reason_for=_status, value_for=None):
    """The invariant itself, over any list of rows. Raises AssertionError on violation.

    ``reason_for(row)`` supplies the structured reason an ATR is absent. Against the
    board it is DERIVED from the same bar series production fed to quant.atr, so the
    reason cannot be forged by a row that simply omits it; the constructed cohorts below
    read it off the row instead. One rule, two sources for the reason.

    ``value_for(row)``, where the inputs are available, supplies the ATR those inputs
    produce, so a recorded value is checked against what it should be rather than merely
    for being non-null.

    Pure and parameterised so it can be run against a constructed cohort as well as
    against the board. A rule that can only be exercised by the data that happens to be
    on disk is untested on every shape the data has not taken yet — and the shape that
    matters here (some eligible rows working, others not) is precisely the one the real
    ledger will never show on demand.

    Returns a note when nothing is eligible yet, None otherwise.
    """
    assert rows, "no rows to check"
    eligible, ineligible, unknown_bars = [], [], []
    for r in rows:
        bars = _num(r, BAR_FIELD)
        if bars is None:
            unknown_bars.append(r)
        elif bars >= need:
            eligible.append(r)
        else:
            ineligible.append(r)

    # A status the vocabulary does not contain is a value nothing downstream can read.
    for r in rows:
        st = reason_for(r)
        assert st is None or st in quant.ATR_STATUSES, (
            f"{r['symbol']}: {STATUS_FIELD} is {st!r}, which is not one of "
            f"{list(quant.ATR_STATUSES)}")

    # An ATR on a row that cannot have one is manufactured, whatever produced it.
    manufactured = [r["symbol"] for r in ineligible if _num(r, "atr14") is not None]
    assert not manufactured, (
        f"{len(manufactured)} row(s) carry an ATR14 with fewer than {need} recorded "
        f"bars: {manufactured[:5]}. An ATR is never produced for an ineligible row.")
    blind = [r["symbol"] for r in unknown_bars if _num(r, "atr14") is not None]
    assert not blind, (
        f"{len(blind)} row(s) carry an ATR14 with no recorded bar count at all: "
        f"{blind[:5]}. An ATR whose eligibility cannot be checked is not usable.")
    # Short series can only be accumulating. Anything else on an ineligible row means
    # the status and the bar count disagree about the same series.
    for r in ineligible:
        st = reason_for(r)
        assert st in (None, quant.ATR_ACCUMULATING), (
            f"{r['symbol']}: {int(_num(r, BAR_FIELD))} bars against a window of {need}, "
            f"but the status says {st!r} rather than {quant.ATR_ACCUMULATING!r}")

    if not eligible:
        deepest = max([b for b in (_num(r, BAR_FIELD) for r in rows) if b is not None],
                      default=0)
        return (f"accumulating: no row has {need} bars yet (deepest {int(deepest)}), "
                f"so every ATR14 is null and that is the correct reading")

    # ---- the row-level rule ----
    # Every eligible row, not one of them. A cohort rule is satisfied by a single
    # working row while every other eligible row fails silently beside it.
    failures = []
    for r in eligible:
        v = _num(r, "atr14")
        st = reason_for(r)
        if v is not None:
            if not _finite_positive(v):
                failures.append(f"{r['symbol']}: atr14 is {r.get('atr14')!r}, "
                                f"not a finite positive number")
            elif st is not None and st != quant.ATR_COMPUTED:
                failures.append(f"{r['symbol']}: carries an atr14 of {v} but reports "
                                f"status {st!r} rather than {quant.ATR_COMPUTED!r}")
            elif value_for is not None:
                want = value_for(r)
                if want is not None and abs(v - want) > max(1e-9, abs(want) * 1e-9):
                    failures.append(
                        f"{r['symbol']}: recorded atr14 {v} but its own bar series "
                        f"produces {want}. A value that does not follow from the inputs "
                        f"beside it is worse than no value")
            continue
        # No value on a row whose window is satisfied. Excused only by an explicit
        # reason, and only by the one reason that is compatible with a long-enough
        # series. A blank reason is not an excuse.
        if st is None:
            failures.append(
                f"{r['symbol']}: {int(_num(r, BAR_FIELD))} bars satisfy the {need}-bar "
                f"window, no atr14 was produced, and no {STATUS_FIELD} says why. An "
                f"unexplained null on an eligible row is the production path failing")
        elif st == quant.ATR_ACCUMULATING:
            failures.append(
                f"{r['symbol']}: status {quant.ATR_ACCUMULATING!r} contradicts "
                f"{int(_num(r, BAR_FIELD))} recorded bars against a {need}-bar window")
        elif st == quant.ATR_COMPUTED:
            failures.append(
                f"{r['symbol']}: status {quant.ATR_COMPUTED!r} but no atr14 was serialized")
        # st == input_missing: the one legitimate absence on an eligible row.
    assert not failures, (
        f"{len(failures)} of {len(eligible)} eligible row(s) failed the ATR invariant:\n  "
        + "\n  ".join(failures[:8]))
    return None


def _nightly():
    spec = importlib.util.spec_from_file_location(
        "nightly_for_atr_gate", os.path.join(_ROOT, "nightly.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def derived_expectations():
    """Per symbol, the (value, status) the night's OWN inputs produce.

    Eligibility is taken from the required valid inputs rather than from the bar count
    beside the row. `adx_bars` settles the window and nothing else, and the two are not
    the same question: a true range needs a high, a low and the previous close on every
    bar, so a long series can still be uncomputable. Reading the inputs answers both at
    once, and the reason it returns cannot be forged by a row that omits it.

    The series is rebuilt from the rows STRICTLY BEFORE the night in question, because
    that is what the build for that night saw — its own rows are appended after the
    indicators are computed. Rebuilding from everything on disk would compare a row
    written last night against inputs that arrived since, and every symbol whose series
    has crossed the window in the meantime would read as a production failure.
    """
    nightly = _nightly()
    with open(LEDGER, encoding="utf-8") as fh:
        rows = json.load(fh)["rows"]
    latest = max(r["date"] for r in rows)
    prior = [r for r in rows if r["date"] < latest]
    bars = (nightly._series_from_ledger(prior) or {}).get("bars") or {}
    return {sym: (quant.atr_with_reason(seq), len(seq)) for sym, seq in bars.items()}


def check_eligibility_decides_whether_null_is_correct():
    """The invariant, over the recorded board, against the inputs that produced it.

    This is the check that flips itself on. While nothing is computable it asserts the
    refusal; the night something becomes computable it starts asserting the value on
    every one of them, with no edit here and no symbol named.
    """
    rows = latest_rows()
    assert rows, "the ledger's latest night has no rows"
    need = quant.atr_min_bars()
    derived = derived_expectations()

    # The row's own bar count must agree with the series it was computed from. If these
    # disagree, the row and its inputs are describing different series and nothing below
    # means anything.
    drift = []
    for r in rows:
        rec = _num(r, BAR_FIELD)
        exp = derived.get(r["symbol"])
        if rec is None or exp is None:
            continue
        if int(rec) != exp[1]:
            drift.append(f"{r['symbol']}: row records {int(rec)} bars, its own series has {exp[1]}")
    assert not drift, (
        f"{len(drift)} row(s) disagree with their inputs about the length of the series:"
        f"\n  " + "\n  ".join(drift[:6]))

    def reason(row):
        exp = derived.get(row["symbol"])
        return exp[0][1] if exp else None

    def value(row):
        exp = derived.get(row["symbol"])
        return exp[0][0] if exp else None

    note = atr_invariant(rows, need, reason_for=reason, value_for=value)
    if note:
        counts = {}
        for (_, st), _n in derived.values():
            counts[st] = counts.get(st, 0) + 1
        return note + f" · derived from inputs: {counts}"
    return None


def check_a_mixed_cohort_fails():
    """The regression that the cohort rule could not express.

    Two eligible rows, one finite and one null. Under "at least one eligible row must
    carry a finite ATR14" this cohort PASSES, and the row that produced nothing is
    invisible. It must fail.
    """
    need = quant.atr_min_bars()
    mixed = [
        {"symbol": "AAA", "date": "2026-01-02", BAR_FIELD: need + 3, "atr14": 1.25},
        {"symbol": "BBB", "date": "2026-01-02", BAR_FIELD: need + 3, "atr14": None},
    ]
    try:
        atr_invariant(mixed, need)
    except AssertionError as exc:
        assert "BBB" in str(exc), \
            f"the mixed cohort failed, but not for the row that produced nothing: {exc}"
        return None
    raise AssertionError(
        "a cohort of two eligible rows, one with a finite ATR14 and one with none, "
        "PASSED the invariant. One working row is masking the other, which is exactly "
        "the failure a cohort-level rule cannot see.")


def check_an_ineligible_or_uncheckable_row_may_not_carry_an_atr():
    """The two boundary rules the real board cannot exercise.

    Neither shape exists in the ledger — which is why both assertions need a
    constructed row. An assertion that only ever runs against data that cannot
    violate it has never been executed against a violation, and is indistinguishable
    from a comment.
    """
    need = quant.atr_min_bars()

    short = [{"symbol": "SHORT", "date": "2026-01-02", BAR_FIELD: need - 1, "atr14": 0.9}]
    try:
        atr_invariant(short, need)
    except AssertionError as exc:
        assert "SHORT" in str(exc)
    else:
        raise AssertionError(
            f"a row with {need - 1} bars carrying an ATR14 passed. An ATR is never "
            f"produced below the window, so this value came from somewhere else.")

    blind = [{"symbol": "BLIND", "date": "2026-01-02", BAR_FIELD: None, "atr14": 0.9}]
    try:
        atr_invariant(blind, need)
    except AssertionError as exc:
        assert "BLIND" in str(exc)
    else:
        raise AssertionError(
            "a row carrying an ATR14 with no bar count at all passed. Its eligibility "
            "cannot be checked, so the value cannot be trusted.")

    # A short row is allowed to hold a null, and must not be dragged in by the above.
    assert atr_invariant(
        [{"symbol": "OK", "date": "2026-01-02", BAR_FIELD: need - 1, "atr14": None}],
        need) is not None, "a wholly accumulating cohort no longer reports accumulating"


def check_an_explicit_reason_is_the_only_excuse():
    """A null on an eligible row is excused by `input_missing` and by nothing else.

    The three rejected shapes are each a different way of claiming the absence is fine:
    saying nothing, claiming the series is still short when it is not, and claiming a
    value was computed when none was serialized.
    """
    need = quant.atr_min_bars()

    def row(**kw):
        base = {"symbol": "ZZZ", "date": "2026-01-02", BAR_FIELD: need + 5, "atr14": None}
        base.update(kw)
        return [base]

    # The one legitimate absence.
    assert atr_invariant(row(atr_status=quant.ATR_INPUT_MISSING), need) is None, \
        "an eligible row explicitly reporting input_missing was rejected"

    for label, kw in (
            ("no reason at all", {}),
            ("a reason that contradicts the bar count", {"atr_status": quant.ATR_ACCUMULATING}),
            ("a claim that it was computed", {"atr_status": quant.ATR_COMPUTED})):
        try:
            atr_invariant(row(**kw), need)
        except AssertionError:
            continue
        raise AssertionError(
            f"an eligible row with a null ATR14 and {label} passed the invariant")

    # And the mirror: a value present must not claim it was not computed.
    try:
        atr_invariant(row(atr14=2.0, atr_status=quant.ATR_INPUT_MISSING), need)
    except AssertionError:
        pass
    else:
        raise AssertionError(
            "a row carrying an ATR14 while reporting input_missing passed the invariant")


def check_bar_count_alone_is_not_treated_as_computability():
    """The gap the status closes, demonstrated against quant rather than asserted.

    A series far longer than the window still yields no ATR when one bar is missing a
    leg of its true range. Length was the only eligibility input the gate had, so this
    row used to be indistinguishable from a production failure.
    """
    need = quant.atr_min_bars()
    long_series = [{"high": 10 + i, "low": 9 + i, "close": 9.5 + i} for i in range(need + 10)]
    long_series[4]["low"] = None
    val, st = quant.atr_with_reason(long_series)
    assert len(long_series) > need, "the fixture is not longer than the window"
    assert val is None, "a bar with no low still produced an ATR"
    assert st == quant.ATR_INPUT_MISSING, \
        f"a missing input reported {st!r} rather than {quant.ATR_INPUT_MISSING!r}"
    # Same length, all inputs present: the distinction is the content, not the count.
    whole = [{"high": 10 + i, "low": 9 + i, "close": 9.5 + i} for i in range(need + 10)]
    val2, st2 = quant.atr_with_reason(whole)
    assert _finite_positive(val2) and st2 == quant.ATR_COMPUTED, \
        "an intact series of the same length did not compute"


def check_the_value_and_its_reason_come_from_one_function():
    """`atr` delegates to `atr_with_reason`, so the number and the reason cannot disagree.

    Two implementations would drift, and the drift would be silent in exactly the
    direction that matters: a value present with a status saying it is absent.
    """
    src = open(os.path.join(_ROOT, "quant.py"), encoding="utf-8").read()
    body = src[src.index("def atr(bars"):]
    body = body[:body.index("\n\n\n")]
    assert "atr_with_reason(bars, period)[0]" in body, \
        "atr no longer delegates to atr_with_reason — the two can now disagree"
    for n in (quant.atr_min_bars() - 1, quant.atr_min_bars(), quant.atr_min_bars() + 6):
        series = [{"high": 10 + i, "low": 9 + i, "close": 9.5 + i} for i in range(n)]
        assert quant.atr(series) == quant.atr_with_reason(series)[0], \
            f"atr and atr_with_reason disagree at {n} bars"


def check_the_reason_is_derived_from_production_inputs():
    """The gate reads production's own series builder, not a restatement of it.

    A gate that rebuilds bars by its own rules is a second implementation, and the two
    would agree right up until the builder changed. It also has to take the same
    as-at-that-night view of the ledger the build took, which is the subtlety this
    check pins: `< latest`, not `<= latest`.
    """
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    body = src[src.index("def derived_expectations("):]
    body = body[:body.index("\n\n\ndef ")]
    assert "_series_from_ledger" in body, \
        "the gate no longer builds its bars with production's own series builder"
    assert 'r["date"] < latest' in body, \
        "the gate compares a written row against inputs that arrived after it was written"

    # And it genuinely reproduces what the row recorded, which is the claim the whole
    # derivation rests on.
    derived = derived_expectations()
    rows = latest_rows()
    checked = 0
    for r in rows:
        rec, exp = _num(r, BAR_FIELD), derived.get(r["symbol"])
        if rec is None or exp is None:
            continue
        assert int(rec) == exp[1], (
            f"{r['symbol']}: the rebuilt series has {exp[1]} bars against {int(rec)} "
            f"recorded. The gate is not seeing what the build saw.")
        checked += 1
    assert checked, (
        "not one row could be matched to a rebuilt series, so the derivation is "
        "asserting nothing at all")


def check_provenance_reaches_the_serialized_output():
    """A value is only usable if its date and depth travel with it.

    Checked on whatever exists: while nothing is eligible there is nothing to carry, so
    this asserts the FIELDS are serialized; once values appear it asserts they are
    accompanied.
    """
    with open(LEDGER, encoding="utf-8") as fh:
        rows = json.load(fh)["rows"]
    assert rows, "no ledger rows"
    for field in ("date", BAR_FIELD, "atr14"):
        assert field in rows[0], f"{field} is not serialized on a ledger row"

    csv_path = os.path.join(_ROOT, "ledger", "signals.csv")
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        for field in ("date", BAR_FIELD, "atr14"):
            assert field in header, f"{field} is missing from signals.csv"

    for r in rows:
        if _num(r, "atr14") is None:
            continue
        assert str(r.get("date", "")).strip(), \
            f"{r['symbol']}: an ATR14 with no observation date"
        assert _num(r, BAR_FIELD) is not None, \
            f"{r['symbol']} on {r['date']}: an ATR14 with no bar count behind it"


def check_the_terminal_reads_value_date_and_depth():
    """The rendered product must carry the provenance, not just the number."""
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    ingest = re.search(r'ATR14\[sy\]\s*=\s*\{([^}]*)\}', html)
    assert ingest, "the terminal no longer stores ATR14 as a record"
    body = ingest.group(1)
    for key in ("v:", "date:", "bars:"):
        assert key in body, f"the terminal drops {key!r} when ingesting ATR14"
    # The value is read into a local a line or two above the record literal, so the
    # window is the surrounding statement rather than the braces alone.
    window = html[max(0, ingest.start() - 400): ingest.end()]
    assert '"atr14"' in window, "the value is not read from the ledger row"
    assert '"adx_bars"' in body or '"adx_bars"' in window, \
        "the bar count is not read from the ledger row"
    # And it is rendered, not merely stored.
    assert "ATR recorded" in html, "the stop does not print the night its ATR came from"
    assert "obs.bars" in html, "the stop does not print the bar count behind its ATR"


def check_nothing_substitutes_for_a_missing_atr():
    """dailySigma may fall back to the 24h range; the stop may not.

    An impact estimate degrades gracefully. A stop does not: one derived from a single
    day of range is systematically tighter in a quiet week and wider after one gap, and
    it would render in the same typeface as the real thing.
    """
    with open(TERMINAL, encoding="utf-8") as fh:
        html = fh.read()
    fn = html[html.index("function atrStop("):]
    fn = fn[:fn.index("\n}")]
    assert "high24" not in fn and "low24" not in fn, \
        "atrStop reaches for the 24h range when ATR14 is absent"
    assert "no ATR14 recorded" in fn, "atrStop no longer says why it has no stop"


_CHECKS = [
    check_the_window_is_derived_not_assumed,
    check_eligibility_decides_whether_null_is_correct,
    check_a_mixed_cohort_fails,
    check_an_ineligible_or_uncheckable_row_may_not_carry_an_atr,
    check_an_explicit_reason_is_the_only_excuse,
    check_bar_count_alone_is_not_treated_as_computability,
    check_the_value_and_its_reason_come_from_one_function,
    check_the_reason_is_derived_from_production_inputs,
    check_provenance_reaches_the_serialized_output,
    check_the_terminal_reads_value_date_and_depth,
    check_nothing_substitutes_for_a_missing_atr,
]


def _run_all():
    failures, notes = [], []
    for fn in _CHECKS:
        try:
            note = fn()
            if note:
                notes.append(note)
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
    return failures, notes


if __name__ == "__main__":
    failures, notes = _run_all()
    for n in notes:
        print(n)
    for f in failures:
        print("FAIL " + f)
    print(f"atr eligibility: {len(_CHECKS) - len(failures)}/{len(_CHECKS)} passed")
    sys.exit(1 if failures else 0)
else:
    def test_window_is_derived():
        check_the_window_is_derived_not_assumed()

    def test_eligibility_invariant():
        check_eligibility_decides_whether_null_is_correct()

    def test_a_mixed_cohort_fails():
        check_a_mixed_cohort_fails()

    def test_ineligible_or_uncheckable_row_may_not_carry_an_atr():
        check_an_ineligible_or_uncheckable_row_may_not_carry_an_atr()

    def test_an_explicit_reason_is_the_only_excuse():
        check_an_explicit_reason_is_the_only_excuse()

    def test_bar_count_alone_is_not_computability():
        check_bar_count_alone_is_not_treated_as_computability()

    def test_value_and_reason_share_one_function():
        check_the_value_and_its_reason_come_from_one_function()

    def test_the_reason_is_derived_from_production_inputs():
        check_the_reason_is_derived_from_production_inputs()

    def test_provenance_serialized():
        check_provenance_reaches_the_serialized_output()

    def test_terminal_reads_provenance():
        check_the_terminal_reads_value_date_and_depth()

    def test_no_substitute_for_missing_atr():
        check_nothing_substitutes_for_a_missing_atr()
