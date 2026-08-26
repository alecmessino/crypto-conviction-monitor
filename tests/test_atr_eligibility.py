"""The ATR transition verifies itself.

Today every row on the board refuses a stop, because the recorded bar series is one
night short of the ATR window: quant.atr needs fifteen bars and the deepest symbol has
fourteen. That refusal is correct, and this file says so rather than treating it as a
failure.

What it must not become is a manual follow-up. The night the first symbol reaches the
window, the production path has to prove itself without anybody remembering to look. So
the invariant is written over ELIGIBILITY rather than over a symbol or a date:

  * no row eligible          -> every atr14 null is VALID, and the gate says "accumulating"
  * some row eligible        -> at least one eligible row must carry a finite atr14
  * a row that is not eligible must NEVER carry an atr14
  * every atr14 that exists must reach the serialized output with its observation date
    and its bar count, and the terminal must read all three

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


def check_eligibility_decides_whether_null_is_correct():
    """The invariant, over the recorded board.

    This is the check that flips itself on. While nothing is eligible it asserts the
    refusal; the night something becomes eligible it starts asserting the value, with no
    edit here and no symbol named.
    """
    rows = latest_rows()
    assert rows, "the ledger's latest night has no rows"
    need = quant.atr_min_bars()

    eligible, ineligible, unknown_bars = [], [], []
    for r in rows:
        bars = _num(r, BAR_FIELD)
        if bars is None:
            unknown_bars.append(r["symbol"])
        elif bars >= need:
            eligible.append(r)
        else:
            ineligible.append(r)

    # An ATR on a row that cannot have one is manufactured, whatever produced it.
    manufactured = [r["symbol"] for r in ineligible if _num(r, "atr14") is not None]
    assert not manufactured, (
        f"{len(manufactured)} row(s) carry an ATR14 with fewer than {need} recorded "
        f"bars: {manufactured[:5]}. An ATR is never produced for an ineligible row.")
    blind = [s for s in unknown_bars
             if _num(next(r for r in rows if r["symbol"] == s), "atr14") is not None]
    assert not blind, (
        f"{len(blind)} row(s) carry an ATR14 with no recorded bar count at all: "
        f"{blind[:5]}. An ATR whose eligibility cannot be checked is not usable.")

    if not eligible:
        # Correct, and reported rather than silently passing.
        deepest = max([b for b in (_num(r, BAR_FIELD) for r in rows) if b is not None],
                      default=0)
        return (f"accumulating: no row has {need} bars yet (deepest {int(deepest)}), "
                f"so every ATR14 is null and that is the correct reading")

    with_value = [r for r in eligible if _num(r, "atr14") is not None]
    assert with_value, (
        f"{len(eligible)} row(s) have reached {need} bars and NOT ONE produced an ATR14. "
        f"The window is satisfied, so this is the production path failing rather than "
        f"the series being short. Symbols: {[r['symbol'] for r in eligible][:5]}")
    for r in with_value:
        v = _num(r, "atr14")
        assert v is not None and v > 0 and v == v and v not in (float("inf"), float("-inf")), \
            f"{r['symbol']}: atr14 is {r.get('atr14')!r}, not a finite positive number"
    return None


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

    def test_provenance_serialized():
        check_provenance_reaches_the_serialized_output()

    def test_terminal_reads_provenance():
        check_the_terminal_reads_value_date_and_depth()

    def test_no_substitute_for_missing_atr():
        check_nothing_substitutes_for_a_missing_atr()
