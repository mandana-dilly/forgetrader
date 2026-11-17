"""Offline scorer for picking the cheap scout model (D42). Stdlib only, no network.

  python scouts/eval/score.py ANSWER_KEY.json CANDIDATE.json

Both files share one shape:
  {"earnings": {"TICKER": "YYYY-MM-DD" or null, ...},
   "filings":  {"TICKER": [{"form": "8-K", "filed": "YYYY-MM-DD"}, ...], ...}}
earnings_date: per answer-key ticker, exact string match (a missing ticker is a miss).
filings: per answer-key filing, matched if the candidate lists the same (form, filed)
for that ticker; candidate filings not in the key are listed as extra misses but do
not enter the denominator. Prints accuracy per field, then every miss. Exit 0 on a
scored run, 2 on unreadable input.
"""
import json
import sys
from collections import Counter


def load(path):
    with open(path) as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: not an object")
    return {"earnings": doc.get("earnings") or {}, "filings": doc.get("filings") or {}}


def score(key, cand):
    """(results, misses); results maps field -> (hits, total, extra)."""
    misses = []
    hits = 0
    for t in sorted(key["earnings"]):
        want = key["earnings"][t]
        got = cand["earnings"].get(t, "absent")
        if got == want:
            hits += 1
        else:
            misses.append(f"MISS earnings_date {t} expected {want} got {got}")
    results = {"earnings_date": (hits, len(key["earnings"]), 0)}

    f_hits = f_total = f_extra = 0
    for t in sorted(set(key["filings"]) | set(cand["filings"])):
        want = Counter((f["form"], f["filed"]) for f in key["filings"].get(t, []))
        got = Counter((f["form"], f["filed"]) for f in cand["filings"].get(t, []))
        f_total += sum(want.values())
        f_hits += sum((want & got).values())
        for form, filed in sorted((want - got).elements()):
            misses.append(f"MISS filings {t} expected {form} {filed} not found")
        for form, filed in sorted((got - want).elements()):
            f_extra += 1
            misses.append(f"MISS filings {t} extra {form} {filed} not in key")
    results["filings"] = (f_hits, f_total, f_extra)
    return results, misses


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: score.py ANSWER_KEY.json CANDIDATE.json", file=sys.stderr)
        return 2
    try:
        key, cand = load(argv[0]), load(argv[1])
        results, misses = score(key, cand)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"score: unreadable input: {e}", file=sys.stderr)
        return 2
    for field, (h, tot, extra) in results.items():
        pct = f"{100.0 * h / tot:.1f}%" if tot else "n/a"
        print(f"{field}: {h}/{tot} {pct}" + (f" extra={extra}" if field == "filings" else ""))
    for m in misses:
        print(m)
    print(f"misses={len(misses)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
