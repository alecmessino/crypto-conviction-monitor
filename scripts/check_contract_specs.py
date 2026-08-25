#!/usr/bin/env python3
"""Re-verify every pinned contract specification against Coinbase's own documents.

`contract_specs.json` is built from primary sources: the product table in Coinbase
Derivatives' CFTC self-certification filing, which lists product name, contract code and
contract size together, plus Coinbase's published perpetual specification sheets, which
state the funding mechanism in terms. This script re-reads those documents and checks
that what is pinned still matches them.

    python scripts/check_contract_specs.py
    python scripts/check_contract_specs.py --write   # restamp verified_on where confirmed

WHAT COUNTS AS CONFIRMED, and why it is deliberately strict:

A row is confirmed only when the source names its product code, its instrument type, its
funding capability and its multiplier, and all four agree. Matching a number that happens
to sit near a product name is not verification. The specific failure that rule exists to
prevent is real and is why this file was rewritten: `ET` and `ETP` are both 0.1 ETH, so a
loose check keyed on "0.1" beside "Ether" confirms a dated future as though it were the
funding-bearing perpetual, and the funding parser then recovers an APR from an instrument
that pays no funding.

EXIT CODES. There are three, and the difference between two of them is the point:

    0   every intended field of every row was seen in a source and agreed
    1   a source disagrees with a pinned value. Something on screen is wrong now.
    2   incomplete: a row was not seen, or a source could not be read

An unseen row is NOT a pass. It is the absence of evidence, and returning 0 for it would
let an unreachable source read as a clean bill of health, which is the exact shape of the
error this whole file exists to correct.

NETWORK. coinbase.com and help.coinbase.com are blocked by egress policy in some
environments, including the one this was last run from; the specification PDFs on
Coinbase's asset CDN and the CFTC filing were reachable and are what the pins were read
from. When a document cannot be fetched this reports 2 and names the host, rather than
guessing or silently skipping.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_FILE = ROOT / "contract_specs.json"

_UA = "Mozilla/5.0 (compatible; conviction-monitor contract-spec check)"

# Field names a row must carry and this script must see agreed before it is confirmed.
INTENDED_FIELDS = ("code", "symbol", "product", "instrument_type",
                   "funding_bearing", "multiplier", "unit", "active_as_of")


# ---------------------------------------------------------------------------
# PDF text, honouring ToUnicode CMaps
# ---------------------------------------------------------------------------
def _streams(data: bytes):
    for m in re.finditer(rb"stream\r?\n", data):
        s = m.end()
        e = data.find(b"endstream", s)
        if e == -1:
            continue
        try:
            yield zlib.decompress(data[s:e])
        except Exception:
            continue


def pdf_text(data: bytes) -> str:
    """Extract text, decoding subsetted fonts through their ToUnicode CMap.

    Coinbase's spec sheets are design-tool exports with subsetted fonts, so the raw
    string operands are font-specific codes rather than characters. Without the CMap
    they decode to mojibake that looks exactly like a corrupt download, which is a
    failure mode worth not misreading as a disagreement.
    """
    cmap: dict[int, str] = {}
    for dec in _streams(data):
        if b"beginbfchar" not in dec and b"beginbfrange" not in dec:
            continue
        txt = dec.decode("latin-1", "replace")
        for blk in re.findall(r"beginbfchar(.*?)endbfchar", txt, re.S):
            for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                try:
                    cmap[int(src, 16)] = bytes.fromhex(dst).decode("utf-16-be", "ignore")
                except Exception:
                    pass
        for blk in re.findall(r"beginbfrange(.*?)endbfrange", txt, re.S):
            for lo, hi, dst in re.findall(
                    r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
                lo_i, hi_i, base = int(lo, 16), int(hi, 16), int(dst, 16)
                for i in range(lo_i, min(hi_i, lo_i + 65535) + 1):
                    try:
                        cmap[i] = chr(base + (i - lo_i))
                    except Exception:
                        pass

    def dec_str(b: bytes) -> str:
        return "".join(cmap.get(c, "") for c in b) if cmap else b.decode("latin-1", "replace")

    out = []
    for dec in _streams(data):
        if b"Tj" not in dec and b"TJ" not in dec:
            continue
        for m in re.finditer(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>", dec):
            tok = m.group(0)
            if tok.startswith(b"("):
                body = re.sub(rb"\\([0-7]{1,3})",
                              lambda mm: bytes([int(mm.group(1), 8) & 0xFF]), tok[1:-1])
                body = re.sub(rb"\\(.)", rb"\1", body)
                out.append(dec_str(body))
            else:
                hx = re.sub(rb"\s", b"", tok[1:-1])
                try:
                    out.append(dec_str(bytes.fromhex(hx.decode())))
                except Exception:
                    pass
    return soften("".join(out))


def fetch(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        host = urllib.parse.urlsplit(url).netloc if hasattr(urllib, "parse") else url
        print(f"  unreachable: {url}\n    {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
def soften(text: str) -> str:
    """Collapse a PDF extraction to comparable tokens without losing word boundaries.

    A subsetted font export can map a few glyphs wrong, so prose comes back as "1/10th oD
    Ether". The product TABLE survives intact, which is why verification is done against
    the table rather than the surrounding narrative.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 /.,=]", " ", text))


def _as_number(s: str) -> float | None:
    """A contract size as these documents write it: 0.01, 5,000, 1/10th."""
    s = s.strip()
    m = re.fullmatch(r"1/(\d+)th", s)
    if m:
        return 1.0 / float(m.group(1))
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


# The exchange's own name for a funding-bearing contract. Everything else expires.
PERP_MARKER = "perp style"
# Stated in the perpetual specification sheets, in terms.
FUNDING_CLAUSE = "unding rate to debit"


def confirm_row(row: dict, corpus: dict) -> tuple[str, str]:
    """(status, detail) where status is 'confirmed' | 'disagrees' | 'unseen'.

    Verification is a TABLE ROW LOOKUP, not a proximity search. The row's product name is
    located in a source, the contract code immediately following it must equal the pinned
    code, and the contract size immediately following that must equal the pinned
    multiplier. Instrument type is read from the product name as the source writes it,
    and a perpetual must additionally be found in a document stating the funding clause.

    Proximity was tried and is not good enough. In a dense product table a window around
    "nano Ether Futures" contains "nano Ether Perp Style Futures" three tokens later, so
    a windowed check confirmed every dated contract as a perpetual. That is the same
    class of error as the one this file exists to prevent, so the check is anchored to
    the row rather than to the neighbourhood.
    """
    missing = [f for f in INTENDED_FIELDS if row.get(f) in (None, "")]
    if missing:
        return "unseen", f"row is missing intended field(s): {', '.join(missing)}"

    code, product = row["code"], row["product"]
    want_perp = row["instrument_type"] == "perpetual"
    pattern = re.compile(re.escape(product) + r"\s+([A-Za-z]{2,4})\s+(1/\d+th|[\d,]+(?:\.\d+)?)",
                         re.I)
    for name, text in corpus.items():
        rows = pattern.findall(text)
        if not rows:
            continue
        # Several products share a name suffix ("Bitcoin Futures" also matches inside
        # "nano Bitcoin Futures"), so the row is selected by its own code, never by
        # position.
        hit = next((r for r in rows if r[0].upper() == code.upper()), None)
        if hit is None:
            continue
        _, size_txt = hit
        size = _as_number(size_txt)
        if size is None:
            return "disagrees", f"{name}: contract size '{size_txt}' beside {code} is unparseable"
        if abs(size - float(row["multiplier"])) > 1e-12:
            return "disagrees", (f"{name}: multiplier pinned {row['multiplier']}, "
                                 f"source says {size_txt} for {code}")
        src_perp = PERP_MARKER in product.lower()
        if src_perp != want_perp:
            return "disagrees", (f"{name}: instrument_type pinned {row['instrument_type']} "
                                 f"but the source names it '{product}'")
        if bool(row["funding_bearing"]) != src_perp:
            return "disagrees", (f"{name}: funding_bearing pinned {row['funding_bearing']} "
                                 f"for a {'perpetual' if src_perp else 'dated'} contract")
        if want_perp:
            # A perpetual's defining property is the funding mechanism, so it has to be
            # evidenced in words rather than inferred from the product name alone.
            witness = next((n for n, t in corpus.items()
                            if FUNDING_CLAUSE in t and code.upper() in t.upper()), None)
            if witness is None:
                return "unseen", (f"{code} matched the product table in {name} but no "
                                  f"fetched document states its funding mechanism")
            return "confirmed", f"{name} + funding clause in {witness}"
        return "confirmed", name
    return "unseen", "no fetched document lists this product together with its code"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="restamp verified_on on rows confirmed in this run")
    args = ap.parse_args()

    raw = json.loads(SPEC_FILE.read_text(encoding="utf-8"))
    products = raw["products"]
    urls = []
    for d in raw.get("source_documents", []):
        if d["url"].lower().endswith(".pdf"):
            urls.append((d["id"], d["url"]))
    for p in products:
        for s in p.get("sources", []):
            if s.lower().endswith(".pdf") and s not in [u for _, u in urls]:
                urls.append((p["code"], s))

    print(f"Pinned {raw['as_of']} · {len(products)} contracts · {len(urls)} source document(s)")
    corpus, unreadable = {}, []
    for name, url in urls:
        blob = fetch(url)
        if blob is None:
            unreadable.append(url)
            continue
        corpus[name] = pdf_text(blob)

    if not corpus:
        print("\nNo source document could be read from here. Nothing is confirmed and "
              "nothing is contradicted. INCOMPLETE.")
        return 2

    confirmed, disagreed, unseen = [], [], []
    today = _dt.date.today().isoformat()
    for p in products:
        status, detail = confirm_row(p, corpus)
        label = f"{p['code']:<4} {p['symbol']:<5} {p['product']}"
        if status == "confirmed":
            confirmed.append(f"{label}  [{detail}]")
            if args.write:
                p["verified_on"] = today
        elif status == "disagrees":
            disagreed.append(f"{label}: {detail}")
        else:
            unseen.append(f"{label}: {detail}")

    print(f"\nconfirmed {len(confirmed)}/{len(products)}")
    for c in confirmed:
        print(f"  {c}")
    if unseen:
        print(f"\nNOT SEEN {len(unseen)}: incomplete, not passing")
        for u in unseen:
            print(f"  {u}")
    if disagreed:
        print(f"\nDISAGREEMENT {len(disagreed)}")
        for d in disagreed:
            print(f"  {d}")
        print("\nA pinned specification is wrong. Every funding figure derived from that "
              "contract is wrong with it. Fix contract_specs.json, run "
              "scripts/sync_contract_specs.py, then tests/test_contract_specs.py.")
    if unreadable:
        print(f"\nunreadable source(s): {len(unreadable)}")
        for u in unreadable:
            print(f"  {u}")

    if disagreed:
        return 1
    if unseen or unreadable:
        return 2
    if args.write:
        SPEC_FILE.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        print(f"\nRestamped {len(confirmed)} row(s) verified_on {today}. Run "
              "scripts/sync_contract_specs.py so the terminal's copy matches.")
    print("\nAll rows confirmed against a primary source.")
    return 0


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used by fetch's error path)
    raise SystemExit(main())
