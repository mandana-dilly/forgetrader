"""Schemas for scout role files RUN_DIR/<role>.json (D42). Stdlib only.

Every file is {"role", "run_date" YYYY-MM-DD, "items": [...]}. Every item carries
ticker (^[A-Z][A-Z.]{0,5}$) and fetched_at (ISO-8601, tz-aware). Unknown keys reject
at every level, so an agent cannot smuggle in a computed field (a "status", a
"confidence"): anything derived is recomputed by scouts/validate.py from raw facts.

check(doc, role) returns a list of error strings; empty means the shape is valid.
Clock-dependent rules (age, future, run_date versus RUN_DIR) live in validate.py.
"""
import re
from datetime import date, datetime
from urllib.parse import urlsplit

ROLES = ("earnings", "news", "filings", "redflags")
TICKER_RE = re.compile(r"^[A-Z][A-Z.]{0,5}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILING_FORMS = ("8-K", "4", "10-Q", "10-K")
FLAG_CODES = ("dilution", "litigation", "delisting", "guidance_cut", "going_concern", "other")

# role -> {key: kind}; every key is required, no other key is allowed.
ITEM_KEYS = {
    "earnings": {"ticker": "ticker", "fetched_at": "ts", "sources": "earnings_sources"},
    "news": {"ticker": "ticker", "fetched_at": "ts", "sentiment": "sentiment",
             "headlines": "str_list", "catalysts": "str_list", "sources": "url_list1"},
    "filings": {"ticker": "ticker", "fetched_at": "ts", "filings": "filings"},
    "redflags": {"ticker": "ticker", "fetched_at": "ts", "flags": "flags"},
}
TOP_KEYS = ("role", "run_date", "items")


def parse_date(s):
    """YYYY-MM-DD string to date, or None."""
    if not isinstance(s, str) or not DATE_RE.match(s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def parse_ts(s):
    """ISO-8601 string to a tz-aware datetime, or None (tz-naive is None too)."""
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def https_host(url):
    """Lower-case hostname of an https URL, or None if the URL is not https."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme != "https" or not host:
        return None
    return host


def _keys(obj, allowed, where, errs):
    if not isinstance(obj, dict):
        errs.append(f"{where}: not an object")
        return False
    for k in sorted(set(obj) - set(allowed)):
        errs.append(f"{where}: unknown key {k!r}")
    for k in allowed:
        if k not in obj:
            errs.append(f"{where}: missing key {k!r}")
    return set(obj) == set(allowed)


def _url(v, where, errs):
    if https_host(v) is None:
        errs.append(f"{where}: url not https: {v!r}")


def _str_list(v, where, errs):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        errs.append(f"{where}: not a list of strings")


def _field(kind, v, where, errs):
    if kind == "ticker":
        if not isinstance(v, str) or not TICKER_RE.match(v):
            errs.append(f"{where}: bad ticker {v!r}")
    elif kind == "ts":
        if parse_ts(v) is None:
            errs.append(f"{where}: fetched_at not ISO-8601 tz-aware: {v!r}")
    elif kind == "sentiment":
        if type(v) is not int or not -2 <= v <= 2:
            errs.append(f"{where}: sentiment not an int in -2..2: {v!r}")
    elif kind == "str_list":
        _str_list(v, where, errs)
    elif kind == "url_list1":
        if not isinstance(v, list) or not v:
            errs.append(f"{where}: needs a list of at least one url")
        else:
            for i, u in enumerate(v):
                _url(u, f"{where}[{i}]", errs)
    elif kind == "earnings_sources":
        if not isinstance(v, list) or not v:
            errs.append(f"{where}: needs a list of at least one source")
            return
        for i, s in enumerate(v):
            w = f"{where}[{i}]"
            if _keys(s, ("url", "date"), w, errs):
                _url(s["url"], f"{w}.url", errs)
                if parse_date(s["date"]) is None:
                    errs.append(f"{w}.date: not YYYY-MM-DD: {s['date']!r}")
    elif kind == "filings":
        if not isinstance(v, list):
            errs.append(f"{where}: not a list")
            return
        for i, f in enumerate(v):
            w = f"{where}[{i}]"
            if _keys(f, ("form", "filed", "url"), w, errs):
                if f["form"] not in FILING_FORMS:
                    errs.append(f"{w}.form: not one of {'|'.join(FILING_FORMS)}: {f['form']!r}")
                if parse_date(f["filed"]) is None:
                    errs.append(f"{w}.filed: not YYYY-MM-DD: {f['filed']!r}")
                _url(f["url"], f"{w}.url", errs)
    elif kind == "flags":
        if not isinstance(v, list):
            errs.append(f"{where}: not a list")
            return
        for i, f in enumerate(v):
            w = f"{where}[{i}]"
            if _keys(f, ("code", "note", "url"), w, errs):
                if f["code"] not in FLAG_CODES:
                    errs.append(f"{w}.code: not one of {'|'.join(FLAG_CODES)}: {f['code']!r}")
                if not isinstance(f["note"], str):
                    errs.append(f"{w}.note: not a string")
                _url(f["url"], f"{w}.url", errs)
    else:
        raise ValueError(f"unknown field kind {kind}")


def check(doc, role):
    """Shape errors for a parsed role file; [] when valid."""
    if role not in ROLES:
        return [f"unknown role {role!r}"]
    errs = []
    if not _keys(doc, TOP_KEYS, "file", errs):
        return errs
    if doc["role"] != role:
        errs.append(f"file: role {doc['role']!r} is not {role!r}")
    if parse_date(doc["run_date"]) is None:
        errs.append(f"file: run_date not YYYY-MM-DD: {doc['run_date']!r}")
    items = doc["items"]
    if not isinstance(items, list):
        errs.append("file: items not a list")
        return errs
    seen = set()
    spec = ITEM_KEYS[role]
    for i, it in enumerate(items):
        w = f"items[{i}]"
        if not _keys(it, tuple(spec), w, errs):
            continue
        for k, kind in spec.items():
            _field(kind, it[k], f"{w}.{k}", errs)
        t = it["ticker"]
        if isinstance(t, str):
            if t in seen:
                errs.append(f"{w}: duplicate ticker {t!r}")
            seen.add(t)
    return errs
