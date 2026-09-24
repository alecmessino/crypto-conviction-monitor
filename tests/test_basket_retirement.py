"""The experimental hysteresis basket is retired from the product (2026-09-24).

docs/DECISION-2026-09-24-model-layer.md §B. The canonical Index — the Top-10
conviction-weighted, score-proportional, ungated book the nightly chains from
signals.csv — is the only portfolio the product publishes. What these pin:

  * the terminal reads exactly one thing from index.json, its `canonical` block, and
    nothing of the basket's record (basket.json, index.csv, basket_*, latest_holdings,
    macro_regime …) — so no basket field is quietly redirected into an Index surface;
  * no visible terminal copy presents the basket as a live or canonical portfolio;
  * the Method page states one construction rule for a production portfolio, and the
    basket's specification survives only inside an archived block;
  * the basket's recorded history is kept unedited: every index.csv row through the
    retirement night is byte-identical to what was published.

The nightly writer is NOT changed by the retirement. Stopping it has downstream
consequences (the decision memo enumerates them) and is a separate, reviewed change.
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "index.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", HTML, re.S).group(1)
METHOD = (ROOT / "methodology.html").read_text(encoding="utf-8")

RETIRED_ON = "2026-09-24"
# sha256 of ledger/index.csv — header plus every row through RETIRED_ON — as published by
# the 2026-09-24 nightly (commit 207ea9e). The nightly may append after it; it may never
# change a byte of it.
INDEX_CSV_THROUGH_RETIREMENT_SHA256 = (
    "be69877946e0751252daf063cee0b9940512e496c060568df7e00a3f62061525")
INDEX_CSV_ROWS_THROUGH_RETIREMENT = 45

# The one visible string that says "basket" and is not about build_basket(): quant.py's
# ADX × Choppiness strategy label, recorded per row in the ledger. It names a style of
# book, not a published portfolio, and it is recorded vocabulary — not renamed here.
STRATEGY_LABEL = "TREND / ALPHA BASKET"


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # A line comment, but not the // inside a URL literal.
    return re.sub(r"(?<![:\"'`\\])//[^\n]*", "", src)


CODE = _strip_js_comments(SCRIPT)


def _fn(name: str) -> str:
    body = SCRIPT[SCRIPT.index(f"function {name}("):]
    return body[:body.index("\n}\n")]


# ---------------------------------------------------------------------------
# the terminal: one portfolio, and the basket's record never reaches it
# ---------------------------------------------------------------------------
def test_the_terminal_reads_only_the_canonical_block_of_index_json():
    """INDEX is index.json. It carries the canonical block AND the retired basket's
    fields side by side, so the only defensible reader is one that names the canonical
    block and nothing else. The macro-regime pill read `INDEX.macro_regime` — a field the
    basket's writer computes — onto the canonical Index card; that is the kind of silent
    redirection this pins out."""
    props = set(re.findall(r"\bINDEX\s*(?:&&\s*INDEX\s*)?(?:\?\.|\.)\s*([A-Za-z_$][\w$]*)", CODE))
    assert props == {"canonical"}, f"the terminal reads index.json fields {sorted(props)}"
    assert not re.search(r"\bINDEX\s*\[", CODE), "index.json is read by computed key"
    # Every other use of INDEX hands the whole document to a reader that is itself pinned:
    # canonIndex() and the freshness table.
    for m in re.finditer(r"\bINDEX\b", CODE):
        ctx = CODE[max(0, m.start() - 80):m.end() + 40]
        assert ("INDEX.canonical" in ctx or "INDEX = await" in ctx or "let INDEX" in ctx
                or '"index.json": INDEX' in ctx), f"unpinned use of INDEX: …{ctx.strip()}…"


def test_index_json_is_dated_by_the_block_the_terminal_reads():
    """The freshness chip on the Index card used to date index.json by `latest.date` —
    the basket's row. It must date what the card shows."""
    spec = re.search(r'"index\.json":\s*\[("[^"]+"),\s*(d\s*=>[^\]]+)\]', CODE)
    assert spec, "index.json has no freshness spec"
    assert spec.group(1) == '"canonical.to"', spec.group(1)
    assert "canonical" in spec.group(2) and "latest" not in spec.group(2), spec.group(2)
    assert "generated_at" not in spec.group(2), (
        "a file-level timestamp would call the canonical block current when only the "
        "basket's writer ran")


def test_no_terminal_code_touches_the_retired_basket_record():
    """No fetch of the basket's files, and no read of any of its fields under any object
    name. (Prose may NAME the files — the provenance line says where the record is kept —
    so this matches reads, not words.)"""
    fetched = re.findall(r"fetch\(\s*[\"'`]([^\"'`]+)[\"'`]", CODE)
    for f in fetched:
        assert not re.search(r"basket\.json|index\.csv", f), f"the terminal fetches {f}"
    fields = ("latest_holdings", "current_holdings", r"basket_\w+", "macro_regime",
              "macro_riskoff_days", r"eject_\w+", "entry_global_mcap",
              "exec_adjusted_total_return", "benchmark_total_return", "sharpe_convention")
    for name in fields:
        dotted = re.search(r"(?:\?\.|\.)\s*(" + name + r")\b", CODE)
        keyed = re.search(r"\[\s*[\"'`](" + name + r")[\"'`]\s*\]", CODE)
        hit = dotted or keyed
        assert not hit, f"the terminal reads the retired basket's field {hit.group(1)}"


def test_the_index_surfaces_carry_no_basket_overlay():
    """The macro pill was the basket's overlay D. It never touched the canonical book."""
    for el in ('id="idx-macro"', 'id="is-macro"'):
        assert el not in HTML, f"{el} is still on an Index surface"
    for fn in ("renderIndex", "renderIndexStudy", "canonIndex"):
        assert "macro" not in _fn(fn).lower(), f"{fn}() still renders a macro overlay"


def _visible_strings() -> list[str]:
    """Everything a visitor can read: text nodes, the attributes that are read aloud or
    shown on hover, and every JS string or template literal (which is where rendered
    copy lives in this file)."""
    page = re.sub(r"<script>.*?</script>", " ", HTML, flags=re.S)
    page = re.sub(r"<style>.*?</style>", " ", page, flags=re.S)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    out = [re.sub(r"<[^>]+>", " ", page)]
    out += re.findall(r'(?:aria-label|title|data-tip|placeholder|alt)="([^"]*)"', page)
    out += re.findall(r"`(?:[^`\\]|\\.)*`", CODE, re.S)
    out += re.findall(r'"(?:[^"\\\n]|\\.)*"', CODE)
    out += re.findall(r"'(?:[^'\\\n]|\\.)*'", CODE)
    return out


def test_no_visible_terminal_copy_presents_the_basket_as_live():
    """A visible mention of the basket is allowed only in a sentence that says it was
    retired. Before this change the rail card's placeholder read "Cumulative basket vs
    market" and the conviction × 7D map was announced to screen readers as "Basket alpha
    against benchmark"."""
    offenders = []
    for seg in _visible_strings():
        text = seg.replace(STRATEGY_LABEL, "")
        for m in re.finditer(r"basket|hysteresis", text, re.I):
            # the sentence the word sits in
            bounds = [b.end() for b in re.finditer(r"[.!?]\s|\n", text[:m.start()])]
            start = bounds[-1] if bounds else 0
            nxt = re.search(r"[.!?](\s|$)|\n", text[m.end():])
            sentence = text[start:(m.end() + nxt.start()) if nxt else len(text)]
            if "retired" not in sentence.lower():
                offenders.append(sentence.strip()[:140])
    assert not offenders, "visible copy presents the basket as current:\n  " + "\n  ".join(offenders)


def test_the_index_card_names_the_canonical_book():
    card = HTML[HTML.index('id="idx-card"'):]
    card = card[:card.index("</details>")]
    assert "No canonical book yet" in card and "Canonical book vs BTC" in card
    assert "basket" not in card.lower()


# ---------------------------------------------------------------------------
# the Method page: one construction rule; the basket survives only as an archive
# ---------------------------------------------------------------------------
def _method_without_archive() -> str:
    return re.sub(r'<details class="archive".*?</details>', " ", METHOD, flags=re.S)


def _section(src: str, heading: str) -> str:
    s = src[src.index(heading):]
    return s[:s.index("<h2>", 1)]


def test_the_method_page_states_one_production_portfolio_rule():
    live = _method_without_archive()
    constr = _section(live, "<h2>4 · Portfolio Construction</h2>")
    # exactly one construction formula for a portfolio, and it is the canonical book's
    assert constr.count('<div class="formula">') == 1, (
        "Portfolio Construction states more than one construction rule outside the archive")
    formula = re.search(r'<div class="formula">(.*?)</div>', constr, re.S).group(1)
    assert "top 10 by conviction on night t−1" in formula and "no gate" in formula
    assert "conviction_i / Σ conviction_j" in formula
    # none of the basket's machinery is described as live outside the archive
    for word in ("Rebalance Hysteresis", "Ejection Alpha Delta", "Execution-Adjusted Counterfactual",
                 "Macro Liquidity Gate", "rank ≥ 13", "Calendar rebalance"):
        assert word not in live, f"the Method page still specifies the basket outside the archive: {word}"


def test_the_method_page_archives_the_basket_and_says_it_is_retired():
    arch = re.findall(r'<details class="archive"[^>]*>(.*?)</details>', METHOD, re.S)
    assert len(arch) == 1, "the retired basket should be archived in exactly one place"
    summary = re.search(r"<summary>(.*?)</summary>", arch[0], re.S).group(1)
    assert "retired" in summary.lower() and "not a production portfolio" in summary
    assert "Rebalance Hysteresis" in arch[0], "the archived specification was dropped, not kept"
    assert 'id="basket-retired"' in METHOD
    callout = METHOD[METHOD.index('id="basket-retired"'):]
    callout = callout[:callout.index("</div>")]
    assert f"RETIRED {RETIRED_ON}" in callout and "only production portfolio" in callout
    assert "kept unedited" in callout, "the callout does not say the record is preserved"


def test_every_live_method_mention_of_the_basket_says_it_is_retired():
    """Sections 4 and 5 describe the product as it stands. The dated changelog in §8
    keeps its historical wording — it is a record of what was true on its date."""
    live = _method_without_archive()
    for heading in ("<h2>4 · Portfolio Construction</h2>", "<h2>5 · Analytics</h2>"):
        sec = _section(live, heading)
        blocks = re.findall(r"<(p|div|li)[^>]*>(.*?)</\1>", sec, re.S)
        for _, block in blocks:
            text = re.sub(r"<[^>]+>", "", block).replace(STRATEGY_LABEL, "")
            # Text the page quotes as superseded ("Original text follows for the record")
            # is a quotation, not a description of the product.
            text = text.split("Original text follows for the record")[0]
            if re.search(r"basket|build_basket|hysteresis", text, re.I):
                assert re.search(r"retired", text, re.I), (
                    f"{heading}: a live paragraph mentions the basket without retiring it: "
                    f"{text.strip()[:140]}")


# ---------------------------------------------------------------------------
# the record: immutable history
# ---------------------------------------------------------------------------
def test_the_retired_basket_history_in_index_csv_is_unedited():
    """Retirement preserves; it does not rewrite. Every byte through the retirement night
    is what the ledger published. The nightly may append after it."""
    raw = (ROOT / "ledger" / "index.csv").read_bytes()
    lines = raw.splitlines(keepends=True)
    assert lines[0].startswith(b"date,"), "index.csv lost its header"
    cut = None
    for i, line in enumerate(lines[1:], start=1):
        if line.startswith(RETIRED_ON.encode() + b","):
            cut = i
            break
    assert cut is not None, f"index.csv has no row for the retirement night {RETIRED_ON}"
    assert cut == INDEX_CSV_ROWS_THROUGH_RETIREMENT, (
        f"{cut} rows through {RETIRED_ON}, expected {INDEX_CSV_ROWS_THROUGH_RETIREMENT}")
    prefix = b"".join(lines[:cut + 1])
    assert hashlib.sha256(prefix).hexdigest() == INDEX_CSV_THROUGH_RETIREMENT_SHA256, (
        "the retired basket's published history in index.csv was edited")


def test_the_basket_record_stays_labelled_non_canonical():
    import json
    j = json.loads((ROOT / "ledger" / "index.json").read_text(encoding="utf-8"))
    assert "NON-CANONICAL" in j.get("basket_note", ""), "index.json no longer labels the basket"
    assert j["canonical"]["definition"]["gated"] is False
    assert j["canonical"]["definition"]["top_n"] == 10
    assert (ROOT / "ledger" / "basket.json").exists(), "the basket's record was deleted"
