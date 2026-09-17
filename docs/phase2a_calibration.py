#!/usr/bin/env python3
"""Phase 2A calibration evidence. Read-only: nothing here writes to the repo.

Multipliers are RECOMPUTED FROM THE RECORDED INPUTS (market_cap, turnover_pct, fdv_usd)
rather than recovered from the display scales, and then reconciled against the recorded
c_depth / c_liquidity / emission_mult. The display columns are x20 and x30 scales rounded
to one decimal — 0.005 and 0.0033 of a multiplier — which is coarser than the percentiles
being fitted. The inputs are exact except turnover_pct, which is rounded to 0.01 of a
percent; at the observed turnovers that is about 0.0002 of a LIQUIDITY multiplier.
"""
import csv, json, math, os, statistics, collections, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import importlib.util
_s = importlib.util.spec_from_file_location("n", ROOT + "/nightly.py")
n = importlib.util.module_from_spec(_s); _s.loader.exec_module(n)

def num(v):
    try:
        if v in (None, "", "None"): return None
        return float(v)
    except (TypeError, ValueError): return None

def liqfit(t):
    if t <= 0: return 0.0
    if t <= 0.30: return 10 + (t/0.30)*20
    if t <= 0.60: return 30 - abs(t-0.45)/0.15*6
    if t <= 1.20: return 20 - (t-0.60)/0.60*12
    return max(2, 8-(t-1.20)*4)

def q(xs, p):
    xs = sorted(xs)
    if not xs: return float("nan")
    k = (len(xs)-1)*p; f, c = math.floor(k), math.ceil(k)
    return xs[int(k)] if f == c else xs[f]*(c-k)+xs[c]*(k-f)

PCTS = [("min",0.0),("p1",.01),("p5",.05),("p25",.25),("med",.50),
        ("p75",.75),("p95",.95),("p99",.99),("max",1.0)]

def row_line(label, xs, fmt="{:.4f}"):
    return (f"| {label} | " + " | ".join(fmt.format(q(xs,p)) for _,p in PCTS)
            + f" | {len(xs)} |")

HDR = "| " + " | ".join(["series"] + [n_ for n_,_ in PCTS] + ["n"]) + " |"
SEP = "|" + "---|"*(len(PCTS)+2)

# ---------------------------------------------------------------- load + recompute
rows = [r for r in csv.DictReader(open(ROOT + "/ledger/xsec/2026-09.csv"))
        if r.get("src") == "live"]
recon = collections.Counter()
for r in rows:
    mc = num(r["market_cap"]); turn = (num(r["turnover_pct"]) or 0)/100.0
    fdv = num(r["fdv_usd"])
    r["_depth"] = max(0.0, min(1.0, (math.log10(mc)-6)/4.0)) if mc else 0.0
    r["_a_raw"] = liqfit(turn)/30.0                 # the curve alone, no floor, no bypass
    r["_a_nofloor_bypass"] = 1.0 if r["_depth"] >= 0.90 else r["_a_raw"]
    r["_a_frac"] = 1.0 if r["_depth"] >= 0.90 else max(0.40, r["_a_raw"])
    r["_em"] = n.emission_mult(fdv, mc)
    r["_turn"] = turn
    r["_perp"] = num(r["perp_mult"]) or 1.0
    r["_cm"] = (num(r["c_momentum"]) or 0)/20.0     # display scale; CONFIRM is out of scope
    # reconciliation against what the nightly recorded
    for key, col, scale, tol, rnd in (("_depth","c_depth",20.0,0.06,1),
                                      ("_a_frac","c_liquidity",30.0,0.06,1),
                                      ("_em","emission_mult",1.0,1e-6,None)):
        got = round(r[key]*scale, rnd) if rnd is not None else r[key]*scale
        want = num(r[col])
        recon["ok" if want is not None and abs(got-want) <= tol else "MISMATCH " + col] += 1

print("## 0 — the sample, and that the recomputation is faithful\n")
dates = sorted({r["date"] for r in rows})
print(f"wide ledger `ledger/xsec/`: {len(dates)} nights ({dates[0]} … {dates[-1]}), "
      f"{len(rows)} rows, {len(rows)//len(dates)}±1 per night — the WHOLE scored "
      f"cross-section, not a truncation.\n")
print("Recomputation from recorded inputs vs the recorded display components "
      f"(tolerance = one rounding step): `{dict(recon)}`\n")

by_night = collections.defaultdict(list)
for r in rows: by_night[r["date"]].append(r)

print("\n## 1 — distributions, pooled and by night\n")
for label, key in (("DEPTH","_depth"), ("LIQUIDITY","_a_frac"), ("SUPPLY","_em")):
    print(f"**{label}**\n")
    print(HDR); print(SEP)
    print(row_line("pooled (703)", [r[key] for r in rows]))
    for d in dates:
        print(row_line(d, [r[key] for r in by_night[d]]))
    # the widest gap between nights at each percentile: is a bound being fitted to one night?
    drift = max(abs(q([r[key] for r in by_night[a]], p) - q([r[key] for r in by_night[b]], p))
                for _, p in PCTS for a in dates for b in dates)
    print(f"\nlargest between-night gap at any reported percentile: **{drift:.4f}**\n")

print("\n### Where each factor piles up\n")
print("| factor | at its floor | at exactly 1.000 | strictly inside | floor value |")
print("|---|---|---|---|---|")
for label, key, floor in (("DEPTH","_depth",0.0), ("LIQUIDITY","_a_frac",0.40),
                          ("SUPPLY","_em",0.90)):
    xs = [r[key] for r in rows]
    at_f = sum(1 for v in xs if abs(v-floor) < 1e-9)
    at_1 = sum(1 for v in xs if abs(v-1.0) < 1e-9)
    print(f"| {label} | {at_f} ({100*at_f/len(xs):.1f}%) | {at_1} ({100*at_1/len(xs):.1f}%) "
          f"| {len(xs)-at_f-at_1} | {floor} |")

print("\n### Maximum influence each factor can exert on a chain\n")
print("The quantity a bound is FOR: how far one factor can move the product, in nats of")
print("log. Computed from the observed range, and from the constructional range where")
print("they differ.\n")
print("| factor | observed range | max \\|log\\| observed | constructional range | max \\|log\\| possible |")
print("|---|---|---|---|---|")
spec = [("DEPTH","_depth",(0.0,1.0)), ("CONFIRM","_cm",(0.10,1.00)),
        ("LIQUIDITY","_a_frac",(0.40,1.0)), ("SUPPLY","_em",(0.90,1.0)),
        ("FUNDING","_perp",(0.85,1.15))]
for label, key, (clo, chi) in spec:
    xs = [r[key] for r in rows if r[key] > 0]
    lo, hi = min(xs), max(xs)
    obs = max(abs(math.log(lo)), abs(math.log(hi)))
    con = max(abs(math.log(clo)) if clo > 0 else float("inf"), abs(math.log(chi)))
    star = " *(out of Phase 2A scope)*" if label in ("CONFIRM","FUNDING") else ""
    print(f"| {label}{star} | {lo:.4f} – {hi:.4f} | {obs:.3f} | {clo} – {chi} | "
          + ("∞" if con == float("inf") else f"{con:.3f}") + " |")

# ============================================================ 2. LIQUIDITY anatomy
print("\n\n## 2 — LIQUIDITY: calibration problem, or misspecified curve?\n")
print("### 2.1 The multiplier, decomposed\n")
print(HDR); print(SEP)
print(row_line("as scored (floor + bypass)", [r["_a_frac"] for r in rows]))
print(row_line("bypass only, no 0.40 floor", [r["_a_nofloor_bypass"] for r in rows]))
print(row_line("curve alone (no floor, no bypass)", [r["_a_raw"] for r in rows]))
nb = [r for r in rows if r["_depth"] < 0.90]
bp = [r for r in rows if r["_depth"] >= 0.90]
print(row_line(f"curve alone, non-bypass rows only", [r["_a_raw"] for r in nb]))
print()
print(f"rows taking the DEPTH>=0.90 bypass: **{len(bp)}/{len(rows)} "
      f"({100*len(bp)/len(rows):.1f}%)** — every one is set to exactly 1.000")
reach1 = [r for r in nb if r["_a_raw"] >= 1.0 - 1e-9]
print(f"non-bypass rows reaching 1.000 through turnover: **{len(reach1)}**")
print(f"highest LIQUIDITY a non-bypass row achieved: **{max(r['_a_raw'] for r in nb):.4f}** "
      f"({max(nb, key=lambda r: r['_a_raw'])['symbol']})")
floored = [r for r in nb if r["_a_raw"] < 0.40]
print(f"non-bypass rows the 0.40 floor RESCUED from a lower curve value: "
      f"**{len(floored)}/{len(nb)} ({100*len(floored)/len(nb):.1f}%)**; "
      f"without the floor their median would be "
      f"{statistics.median([r['_a_raw'] for r in floored]):.4f} "
      f"and their minimum {min(r['_a_raw'] for r in floored):.4f}")

print("\n### 2.2 Turnover — the input the curve is a function of\n")
turns = [r["_turn"] for r in rows]
print(HDR); print(SEP)
print(row_line("turnover (vol/mcap, fraction)", turns, "{:.4f}"))
for d in dates:
    print(row_line(d, [r["_turn"] for r in by_night[d]], "{:.4f}"))
print()
bands = [("0", lambda t: t <= 0), ("0 – 3% (floored)", lambda t: 0 < t <= 0.03),
         ("3 – 10%", lambda t: 0.03 < t <= 0.10), ("10 – 30%", lambda t: 0.10 < t <= 0.30),
         ("30 – 60% (the design peak)", lambda t: 0.30 < t <= 0.60),
         ("60 – 120%", lambda t: 0.60 < t <= 1.20), (">120% (wash band)", lambda t: t > 1.20)]
print("| turnover band | rows | share | what the curve does there |")
print("|---|---|---|---|")
notes = {"0":"a_frac 0 -> floored to 0.40", "0 – 3% (floored)":"curve < 0.40, floor binds",
         "3 – 10%":"curve rising, 0.40 – 0.55", "10 – 30%":"curve rising, 0.55 – 1.00",
         "30 – 60% (the design peak)":"curve at or near its 1.00 peak",
         "60 – 120%":"curve falling, 0.67 – 0.27", ">120% (wash band)":"curve collapsing"}
for name, f in bands:
    k = sum(1 for t in turns if f(t))
    print(f"| {name} | {k} | {100*k/len(turns):.1f}% | {notes[name]} |")
print(f"\nThe curve peaks at 45% turnover. The universe's median is "
      f"**{statistics.median(turns)*100:.2f}%** and its 95th percentile is "
      f"**{q(turns,.95)*100:.1f}%**. "
      f"{sum(1 for t in turns if 0.30 < t <= 0.60)} of {len(turns)} rows "
      f"({100*sum(1 for t in turns if 0.30 < t <= 0.60)/len(turns):.1f}%) sit in the band "
      f"the curve was shaped around.")

# ---------------------------------------------------- 2.3 turnover vs forward return
def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0]*len(v); i = 0
        while i < len(order):
            j = i
            while j+1 < len(order) and v[order[j+1]] == v[order[i]]: j += 1
            avg = (i+j)/2.0 + 1
            for k in range(i, j+1): rk[order[k]] = avg
            i = j+1
        return rk
    a, b = rank(xs), rank(ys)
    n_ = len(a); ma, mb = sum(a)/n_, sum(b)/n_
    num_ = sum((x-ma)*(y-mb) for x, y in zip(a, b))
    den = (sum((x-ma)**2 for x in a) * sum((y-mb)**2 for y in b)) ** 0.5
    return num_/den if den else None

def legs_from(by_date_sym, keyfn, label):
    ds = sorted(by_date_sym)
    out = []
    for a, b in zip(ds, ds[1:]):
        prev, curr = by_date_sym[a], by_date_sym[b]
        xs, ys = [], []
        for sym, r in prev.items():
            c = curr.get(sym)
            p0, p1 = num(r.get("price")), (num(c.get("price")) if c else None)
            k = keyfn(r)
            if p0 and p1 and k is not None:
                xs.append(k); ys.append(p1/p0 - 1.0)
        if len(xs) >= 20:
            out.append({"from": a, "to": b, "n": len(xs), "ic": spearman(xs, ys)})
    return out

print("\n### 2.3 Turnover against forward return — what the history can actually support\n")
wide = collections.defaultdict(dict)
for r in rows: wide[r["date"]][r["symbol"].upper()] = r
narrow = collections.defaultdict(dict)
for r in csv.DictReader(open(ROOT + "/ledger/signals.csv")):
    if r.get("date"): narrow[r["date"]][(r.get("symbol") or "").upper()] = r

def turn_of(r):
    t = num(r.get("turnover_pct"))
    return None if t is None else t/100.0
def liq_of(r):
    t = turn_of(r)
    if t is None: return None
    mc = num(r.get("market_cap"))
    d = max(0.0, min(1.0, (math.log10(mc)-6)/4.0)) if mc else 0.0
    return 1.0 if d >= 0.90 else max(0.40, liqfit(t)/30.0)

print("| sample | legs | mean IC | SE | 95% CI | legs positive | verdict |")
print("|---|---|---|---|---|---|---|")
for src_label, src, keyfn, kname in (
        ("wide (whole cross-section)", wide, turn_of, "turnover"),
        ("wide (whole cross-section)", wide, liq_of, "LIQUIDITY multiplier"),
        ("narrow (top-50 by conviction)", narrow, turn_of, "turnover"),
        ("narrow (top-50 by conviction)", narrow, liq_of, "LIQUIDITY multiplier")):
    L = [l for l in legs_from(src, keyfn, kname) if l["ic"] is not None]
    if not L:
        print(f"| {kname} — {src_label} | 0 | — | — | — | — | nothing to measure |"); continue
    ics = [l["ic"] for l in L]; m = sum(ics)/len(ics)
    if len(ics) > 1:
        var = sum((i-m)**2 for i in ics)/(len(ics)-1); se = (var/len(ics))**0.5
        ci = f"[{m-1.96*se:+.4f}, {m+1.96*se:+.4f}]"
    else:
        se = float("nan"); ci = "— (one leg)"
    good = len(ics) >= 40 and (m-1.96*se > 0 or m+1.96*se < 0)
    verdict = ("measurable" if good else
               "**not evidence** — too few legs" if len(ics) < 40 else "spans zero")
    print(f"| {kname} — {src_label} | {len(ics)} | {m:+.4f} | "
          + (f"{se:.4f}" if se == se else "—") + f" | {ci} | {sum(1 for i in ics if i>0)} "
          f"| {verdict} |")
print()
print("The wide ledger is three nights old, so it yields **two** forward legs. Two legs")
print("cannot support a statement about a curve. The narrow ledger has forty-seven, and")
print("is the wrong sample for this question in a specific way: `signals.csv` keeps the")
print("top fifty BY CONVICTION, and turnover is an input to conviction — so selecting on")
print("conviction selects on turnover. Its LIQUIDITY column spans a restricted range by")
print("construction, and an IC computed over it is an IC over that restriction.")

# ============================================================ 3. dominance framework
print("\n\n## 3 — the dominance statistic\n")
FACTORS = (("DEPTH","_depth"), ("CONFIRM","_cm"), ("LIQUIDITY","_a_frac"),
           ("SUPPLY","_em"), ("FUNDING","_perp"))
for r in rows:
    contrib = {}
    for label, key in FACTORS:
        v = r[key]
        contrib[label] = math.log(v) if v > 0 else None
    r["_contrib"] = contrib
    mags = {k: abs(v) for k, v in contrib.items() if v is not None}
    tot = sum(mags.values())
    r["_total"] = tot
    if tot > 1e-12:
        top = max(mags, key=mags.get)
        r["_dom"] = top; r["_share"] = mags[top]/tot; r["_mag"] = mags[top]
        r["_signed"] = contrib[top]
    else:
        r["_dom"] = ""; r["_share"] = 0.0; r["_mag"] = 0.0; r["_signed"] = 0.0

print(HDR); print(SEP)
print(row_line("concentration share of the largest factor", [r["_share"] for r in rows], "{:.3f}"))
print(row_line("absolute magnitude |log| of that factor", [r["_mag"] for r in rows], "{:.3f}"))
print(row_line("total |log| across the whole chain", [r["_total"] for r in rows], "{:.3f}"))
print()
cnt = collections.Counter(r["_dom"] for r in rows)
print("which factor is largest, pooled:",
      " · ".join(f"{k} {v} ({100*v/len(rows):.1f}%)" for k, v in cnt.most_common()))
print()
print("Per night, to show this is not one cross-section:\n")
print("| night | median share | p90 share | median \\|log\\| | p90 \\|log\\| | largest factor, modal |")
print("|---|---|---|---|---|---|")
for d in dates:
    g = by_night[d]
    print(f"| {d} | {q([r['_share'] for r in g],.5):.3f} | {q([r['_share'] for r in g],.90):.3f} "
          f"| {q([r['_mag'] for r in g],.5):.3f} | {q([r['_mag'] for r in g],.90):.3f} "
          f"| {collections.Counter(r['_dom'] for r in g).most_common(1)[0][0]} |")

print("\n### 3.1 Why share alone is the wrong flag\n")
share_only = [r for r in rows if r["_share"] > 0.60]
share_only_tiny = [r for r in share_only if r["_mag"] < 0.10]
print(f"rows with share > 0.60: **{len(share_only)}** ({100*len(share_only)/len(rows):.1f}%)")
print(f"of those, rows whose 'dominant' factor moves the score by less than 10% "
      f"(|log| < 0.10): **{len(share_only_tiny)}**")
if share_only_tiny:
    ex = sorted(share_only_tiny, key=lambda r: -r["_share"])[:5]
    print("\nthe five most 'dominated' of them, by share alone:\n")
    print("| date | sym | share | \\|log\\| | total \\|log\\| | dominant | conviction |")
    print("|---|---|---|---|---|---|---|")
    for r in ex:
        print(f"| {r['date']} | {r['symbol']} | {r['_share']:.3f} | {r['_mag']:.4f} "
              f"| {r['_total']:.4f} | {r['_dom']} | {r['conviction']} |")
    print("\nEvery one of these is a chain in which nothing much happened. A flag that")
    print("fires on them is a flag nobody will read by the second week.")

print("\n### 3.2 Calibrating the two-part threshold\n")
print("| share > | and \\|log\\| > | rows flagged | share of universe | modal factor |")
print("|---|---|---|---|---|")
for S in (0.50, 0.55, 0.60, 0.65, 0.70):
    for M in (0.15, 0.25, 0.35, 0.50):
        f = [r for r in rows if r["_share"] > S and r["_mag"] > M]
        mf = collections.Counter(x["_dom"] for x in f).most_common(1)
        print(f"| {S:.2f} | {M:.2f} | {len(f)} | {100*len(f)/len(rows):.1f}% | "
              f"{mf[0][0] + ' ' + str(mf[0][1]) if mf else '—'} |")

# ======================================================= 4. bound impact, verified
print("\n\n## 4 — impact of the proposed bounds, computed rather than asserted\n")
def chain(r, depth=None, liq=None, em=None):
    d = r["_depth"] if depth is None else depth
    a = r["_a_frac"] if liq is None else liq
    e = r["_em"] if em is None else em
    return 100.0 * d * r["_cm"] * a * e * r["_perp"]

CANDIDATES = [
    ("DEPTH floor at pooled p1 (0.506)", lambda r: dict(depth=max(0.506, r["_depth"]))),
    ("DEPTH cap at pooled p99 (1.000)",  lambda r: dict(depth=min(1.000, r["_depth"]))),
    ("SUPPLY floor at pooled p1 (0.906)", lambda r: dict(em=max(0.9059, r["_em"]))),
    ("LIQUIDITY cap at p95 of the non-bypass curve (0.824)",
     lambda r: dict(liq=min(0.8242, r["_a_frac"]))),
    ("LIQUIDITY floor raised 0.40 -> 0.55", lambda r: dict(liq=max(0.55, r["_a_frac"]))),
    ("LIQUIDITY floor raised 0.40 -> 0.50", lambda r: dict(liq=max(0.50, r["_a_frac"]))),
]
print("| candidate bound | rows it touches | scores changed | tiers changed | "
      "clamp count | max rank move | top-10 change |")
print("|---|---|---|---|---|---|---|")
tonight = [r for r in rows if r["date"] == dates[-1]]
def tier(v):
    return "T1" if v >= 80 else "T2" if v >= 70 else "T3" if v >= 55 else "T4" if v >= 40 else "T5"
base = sorted(((r, chain(r)) for r in tonight), key=lambda kv: -kv[1])
b_idx = {r["symbol"]: i+1 for i, (r, _) in enumerate(base)}
b_sc = {r["symbol"]: max(0, min(100, round(v))) for r, v in base}
b_cl = sum(1 for _, v in base if round(v) > 100)
b_top = [r["symbol"] for r, _ in base[:10]]
for label, f in CANDIDATES:
    new = sorted(((r, chain(r, **f(r))) for r in tonight), key=lambda kv: -kv[1])
    n_idx = {r["symbol"]: i+1 for i, (r, _) in enumerate(new)}
    n_sc = {r["symbol"]: max(0, min(100, round(v))) for r, v in new}
    touched = sum(1 for r in tonight
                  if any(abs(v - r[{"depth":"_depth","liq":"_a_frac","em":"_em"}[k]]) > 1e-9
                         for k, v in f(r).items()))
    sc = [s for s in b_sc if b_sc[s] != n_sc[s]]
    ti = [s for s in b_sc if tier(b_sc[s]) != tier(n_sc[s])]
    mv = max(abs(b_idx[s]-n_idx[s]) for s in b_idx)
    n_top = [r["symbol"] for r, _ in new[:10]]
    n_cl = sum(1 for _, v in new if round(v) > 100)
    chg = ",".join(sorted(set(b_top) ^ set(n_top))) or "none"
    print(f"| {label} | {touched} | {len(sc)} | {len(ti)} | {b_cl} → {n_cl} | {mv} | {chg} |")

print("\n*(computed on the 2026-09-17 cross-section, 235 rows, through the recorded chain)*")

# ============================================================== 5. the ZEC case
print("\n\n## 5 — ZEC, as a diagnostic\n")
z = [r for r in rows if r["symbol"] == "ZEC"]
print("| night | DEPTH | CONFIRM | LIQUIDITY | SUPPLY | FUNDING | pre-clamp | published |")
print("|---|---|---|---|---|---|---|---|")
for r in sorted(z, key=lambda r: r["date"]):
    print(f"| {r['date']} | {r['_depth']:.4f} | {r['_cm']:.4f} | {r['_a_frac']:.4f} | "
          f"{r['_em']:.4f} | {r['_perp']:.4f} | {chain(r):.1f} | {r['conviction']} |")
zt = [r for r in z if r["date"] == dates[-1]][0]
print()
print("Which factors create the overshoot, in order of what they contribute:\n")
print("| factor | multiplier | signed log | share of chain |log| |")
print("|---|---|---|---|")
for label, key in FACTORS:
    v = zt[key]; lg = math.log(v) if v > 0 else float("nan")
    sh = abs(lg)/zt["_total"] if zt["_total"] > 1e-12 else 0.0
    print(f"| {label} | {v:.4f} | {lg:+.4f} | {sh:.3f} |")
print(f"\ntotal chain |log| = {zt['_total']:.4f}; rs_blend = {zt['rs_blend']}")

print("\n### 5.1 The invariant the clamp now has\n")
print("Only ONE factor in the chain can exceed 1.0. DEPTH, CONFIRM, LIQUIDITY and SUPPLY")
print("are each bounded above at exactly 1.0 by construction, so a chain of four")
print("saturated factors is exactly 100.0 and cannot overshoot. FUNDING's envelope is")
print("[0.85, 1.15]. Therefore:\n")
print(f"    max possible pre-clamp = 100 x 1 x 1 x 1 x 1 x {n.PERP_ENVELOPE_HI} "
      f"= **{100*n.PERP_ENVELOPE_HI:.1f}**\n")
print("and every clamp overshoot is, necessarily, funding. Before Phase 1.5 this was not")
print("true — the overlay could serve any number the ledger happened to hold, and did")
print("(HBAR, 242.9). The bound that fixed the clamp was the funding envelope, and it has")
print("already landed.\n")
mx = max(chain(r) for r in rows)
print(f"largest pre-clamp anywhere in the wide ledger: **{mx:.1f}**, "
      f"against the {100*n.PERP_ENVELOPE_HI:.0f} ceiling.")

print("\n\n## 6 — the dominance threshold, proposed\n")
S, M = 0.70, 0.22
print(f"Proposed: **share > {S:.2f} AND |log| > {M:.2f}**.\n")
print(f"- `share > {S:.2f}` is the pooled 90th percentile of the concentration")
print(f"  distribution ({q([r['_share'] for r in rows], .90):.3f}), and it is stable per")
print("  night (0.694 / 0.705 / 0.711). The rule is therefore 'the decile of chains most")
print("  concentrated in one factor', not a number chosen to produce a pleasing count.")
print(f"- `|log| > {M:.2f}` is log(1.25): the dominant factor, on its own, moves the score")
print("  by at least a quarter. That is the 'economically meaningful' leg, and it is")
print("  stated as a multiplier rather than a percentile because a percentile of")
print("  magnitude would move with the regime while the meaning of 'a quarter' does not.\n")
flag = [r for r in rows if r["_share"] > S and r["_mag"] > M]
rej_share = [r for r in rows if r["_share"] > S and r["_mag"] <= M]
print(f"flags **{len(flag)} of {len(rows)} rows ({100*len(flag)/len(rows):.1f}%)** pooled; "
      f"per night " + " / ".join(
          str(sum(1 for r in by_night[d] if r["_share"] > S and r["_mag"] > M)) for d in dates))
print(f"\nrows the magnitude leg REJECTS that share alone would have flagged: "
      f"**{len(rej_share)}** — " + ", ".join(f"{r['symbol']} ({r['date']}, share "
      f"{r['_share']:.2f}, |log| {r['_mag']:.3f})" for r in rej_share))
print("\nwhich factor the flag names, at the proposed threshold:\n")
print("| dominant factor | rows flagged | share of all flags | in Phase 2A scope? |")
print("|---|---|---|---|")
fc = collections.Counter(r["_dom"] for r in flag)
for k, v in fc.most_common():
    scope = "yes" if k in ("DEPTH","LIQUIDITY","SUPPLY") else "**no — deferred**"
    print(f"| {k} | {v} | {100*v/len(flag):.1f}% | {scope} |")
print("\n| direction | rows | reading |")
print("|---|---|---|")
dn = sum(1 for r in flag if r["_signed"] < 0)
print(f"| the dominant factor is CUTTING the score | {dn} | a single haircut is most of the chain |")
print(f"| the dominant factor is LIFTING it | {len(flag)-dn} | a single boost is most of the chain |")
