#!/usr/bin/env python3
"""Journal-vs-broker order and activity report (funding criterion 3). Read-only.

  python -m forgetrader.journal_report [--since YYYY-MM-DD] DIR

DIR is an account directory (state.json, journal.db, .env); label = basename
of DIR. Run it from that account's checkout: credentials.load_credentials()
reads the .env at the checkout root of the code that runs, so DIR must
resolve to that root or the report refuses (ERROR account_dir_mismatch) -
never one account's journal against another account's orders.

Journal side: DIR/journal.db opened read-only (sqlite URI mode=ro; a missing
journal is refused, never created). Per client_order_id the orders rows give
symbol, side, qty, limit (the first row, the write-ahead intent) and the
result status (the row with a non-NULL status). The journal status is the
pending_order_status of the decisions row that resolved the order, when
there is one: the first decision after the order's first row, and before the
next order's first row (one pending order at a time), whose stage_from is
CSP_PENDING or CC_PENDING and whose stage_to differs (a transition, not a
noop or HALT). Else the result row status; else "intent_only".

Broker side: the trading client is built as run.py builds it
(load_credentials, refuse unless paper is True, TradingClient). It only ever
lists orders: status all, ascending by submission, PAGE_LIMIT per page (the
installed alpaca-py documents a max of 500), each next page from the last
page's latest submitted_at minus PAGE_OVERLAP, deduplicated by order id, until
a page comes back short. A full page with no new order is an error.

Activities: alpaca-py 0.43.5 TradingClient has no activities method (only
BrokerClient does, on the Broker API), so the report makes one read through
the client's own REST get on ACTIVITIES_PATH with the documented params
direction asc, page_size ACTIVITY_PAGE_SIZE (documented max 100), page_token
= the last activity id of the previous page, and with --since, after = D
minus ACTIVITY_LAG as YYYY-MM-DD. No activity_types filter is sent (not
documented for this path in the installed SDK): every activity is listed and
the types in ACTIVITY_TYPES (alpaca.trading.enums.ActivityType OPASN, OPEXP,
OPEXC, OPTRD) are kept. Pages are deduplicated by id until an empty page; a
non-empty page with no new id is an error.

Journal transitions: every decisions row whose (stage_from, stage_to) is a
legal transition out of CSP_OPEN or CC_OPEN (forgetrader_state's
LEGAL_TRANSITIONS; the four keys of EXPECTED_ACTIVITY) is one transition. Its
contract is the symbol of the journal order whose resolving decision moved
the wheel into that OPEN stage, after the previous exit from it; none found
is contract "-" (it cannot match). Expected broker type:
  CSP_OPEN to HOLDING (assigned)            OPASN
  CSP_OPEN to FLAT (expired_worthless)      OPEXP
  CC_OPEN to FLAT (called_away)             OPASN
  CC_OPEN to HOLDING (cc_expired_worthless) OPEXP
Each transition, in ts order, pairs with the first unpaired OPASN / OPEXP /
OPEXC activity (date, id order) carrying the same contract symbol. An OPTRD
(symbol = the underlying, the paired share trade) attaches to the OPASN or
OPEXC of the same date whose contract root is its symbol, and is shown on
that line as share_qty / share_price.

--since D keeps journal orders whose first row ts, broker orders whose
submitted_at, transitions whose decision ts are at or after D 00:00 UTC, and
activities whose date is on or after D. An order journalled just before
midnight UTC and submitted just after it (or the reverse) reports one-sided;
so does a transition journalled a day after its activity date (paper NTAs
lag about a day) when D falls between them.

Per order one ORDER line: coid, symbol, side, qty, limit, journal status,
broker status, filled qty, filled avg price, flags. Journal values are shown
when the order is journalled, else broker values; a mismatched field also
shows broker_<field>. Flags:
  JOURNAL_ONLY       journalled, absent at the broker (a journal row with no
                     client_order_id is one JOURNAL_ONLY line per row)
  BROKER_ONLY        at the broker, absent from the journal (an order with no
                     client_order_id is always BROKER_ONLY)
  MISMATCH:symbol / side / qty / limit   values differ (qty and limit compared
                     as exact decimals, never rounded)
  MISMATCH:status    the journal status contradicts the broker: the journal
                     holds only the intent row, or its status is not a known
                     OrderStatus, or it is terminal (TERMINAL) and differs
                     from the broker status. A non-terminal journal status
                     ("new", "accepted", ...) is an earlier observation, not
                     a contradiction.

Per transition or unpaired activity one ACTIVITY line: symbol, from, to,
trigger, journal ts, expected type, broker type, broker date, broker qty,
share qty, share price, flags. Flags:
  TRANSITION_ONLY    journalled transition, no activity for its contract
  ACTIVITY_ONLY      an OPASN / OPEXP / OPEXC with no transition, or an OPTRD
                     with no OPASN / OPEXC to pair with
  MISMATCH:type      paired, but the broker type is not the expected one

Last line: REPORT <label> orders=N matched=M activities=A
           activities_matched=P discrepancies=K
  N = ORDER lines, M = coids present on both sides, A = ACTIVITY lines,
  P = transitions paired with an activity, K = lines (both kinds) with any flag.
Exit 0 only if K == 0; 3 if K > 0; 1 on any error or refusal (argparse
errors included). Never prints a key, token or URL: an exception prints its
type name only.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

from alpaca.trading.enums import ActivityType

import forgetrader_state as fs

PAGE_LIMIT = 500  # documented max for GetOrdersRequest.limit (alpaca-py 0.43.5)
PAGE_OVERLAP = timedelta(seconds=1)
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})
PENDING_STAGES = (fs.CSP_PENDING, fs.CC_PENDING)
FIELDS = ("symbol", "side", "qty", "limit")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

ACTIVITIES_PATH = "/account/activities"
ACTIVITY_PAGE_SIZE = 100  # documented max page_size (ACCOUNT_ACTIVITIES_DEFAULT_PAGE_SIZE)
ACTIVITY_LAG = timedelta(days=1)
OPTION_EVENTS = (ActivityType.OPASN, ActivityType.OPEXP, ActivityType.OPEXC)
SHARE_TRADE = ActivityType.OPTRD
ACTIVITY_TYPES = frozenset(t.value for t in (*OPTION_EVENTS, SHARE_TRADE))
OPEN_STAGES = (fs.CSP_OPEN, fs.CC_OPEN)
EXPECTED_ACTIVITY = {
    (fs.CSP_OPEN, fs.HOLDING): ActivityType.OPASN.value,  # put assigned
    (fs.CSP_OPEN, fs.FLAT): ActivityType.OPEXP.value,     # put expired
    (fs.CC_OPEN, fs.FLAT): ActivityType.OPASN.value,      # called away
    (fs.CC_OPEN, fs.HOLDING): ActivityType.OPEXP.value,   # call expired
}
TRIGGERS = {(f, t): trig for (f, trig), t in fs.LEGAL_TRANSITIONS.items()
            if f in OPEN_STAGES}
if set(TRIGGERS) != set(EXPECTED_ACTIVITY):
    raise RuntimeError("LEGAL_TRANSITIONS out of an OPEN stage changed: "
                       "EXPECTED_ACTIVITY needs review")
_NO_REDIRECT = str.maketrans({"<": "_", ">": "_"})


class ReportError(RuntimeError):
    """A refusal or unreadable input: printed as ERROR <msg>, exit 1."""


def _parse_since(text):
    if not _DATE_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(f"--since not YYYY-MM-DD: {text!r}")
    try:
        d = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"--since not a date: {text!r}")
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _val(x):
    return x.value if isinstance(x, Enum) else x


def _fmt(x):
    return "-" if x is None else str(_val(x)).translate(_NO_REDIRECT)


def _dec(x):
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except InvalidOperation:
        return None


def _ts(text):
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        raise ReportError(f"journal ts not tz-aware: {_fmt(text)}")
    return t


# --------------------------------------------------------------------------- #
# Journal side (read-only)                                                    #
# --------------------------------------------------------------------------- #

def _read_journal(path):
    """(orders rows, decisions rows) from journal.db opened mode=ro. The
    connection is local and released on return; nothing is ever written."""
    if not path.is_file():
        raise ReportError(f"journal missing: {path.name}")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        orders = conn.execute(
            "SELECT rowid, ts, symbol, side, qty, limit_price, status, "
            "client_order_id, broker_order_id FROM orders ORDER BY rowid"
        ).fetchall()
        decisions = conn.execute(
            "SELECT rowid, ts, stage_from, stage_to, action, detail "
            "FROM decisions ORDER BY rowid"
        ).fetchall()
    except sqlite3.Error as e:
        raise ReportError(f"journal unreadable: {type(e).__name__}")
    return orders, decisions


def _resolve_orders(orders, decisions):
    """journal_orders before the --since filter. A decision-resolved entry
    also carries resolved_to / resolved_ts (the stage and ts it moved to)."""
    by_coid, no_coid = {}, []
    for r in orders:
        if r["client_order_id"] is None:
            no_coid.append({"coid": None, "symbol": r["symbol"], "side": r["side"],
                            "qty": r["qty"], "limit": r["limit_price"],
                            "status": r["status"], "source": "no_coid",
                            "ts": _ts(r["ts"])})
            continue
        e = by_coid.get(r["client_order_id"])
        if e is None:
            e = by_coid[r["client_order_id"]] = {
                "coid": r["client_order_id"], "symbol": r["symbol"],
                "side": r["side"], "qty": r["qty"], "limit": r["limit_price"],
                "status": None, "source": "intent_only", "ts": _ts(r["ts"])}
        if r["status"] is not None:
            e["status"], e["source"] = r["status"], "result"

    # Resolve each order through the decisions row that closed its pending
    # stage, inside the window up to the next order's first row.
    seq = sorted(by_coid.values(), key=lambda e: e["ts"])
    dec = [(_ts(d["ts"]), d) for d in decisions]
    for i, e in enumerate(seq):
        end = seq[i + 1]["ts"] if i + 1 < len(seq) else None
        for t, d in dec:
            if t < e["ts"] or (end is not None and t >= end):
                continue
            if d["stage_from"] in PENDING_STAGES and d["stage_to"] != d["stage_from"]:
                try:
                    snap = json.loads(d["detail"])
                except (TypeError, ValueError):
                    raise ReportError(f"decision detail unparseable at rowid {d['rowid']}")
                st = snap.get("pending_order_status") if isinstance(snap, dict) else None
                if isinstance(st, str):
                    e["status"], e["source"] = st, "decision"
                e["resolved_to"], e["resolved_ts"] = d["stage_to"], t
                break
    return by_coid, no_coid


def journal_orders(orders, decisions, since=None):
    """{coid: entry} for journalled orders, plus a list of entries for rows
    without a client_order_id. Entry: symbol, side, qty, limit, status,
    source, ts."""
    by_coid, no_coid = _resolve_orders(orders, decisions)
    if since is not None:
        by_coid = {k: e for k, e in by_coid.items() if e["ts"] >= since}
        no_coid = [e for e in no_coid if e["ts"] >= since]
    return by_coid, no_coid


def journal_transitions(orders, decisions, since=None):
    """Transitions out of CSP_OPEN / CC_OPEN, in ts order: dicts rowid, ts,
    stage_from, stage_to, trigger, expected, contract (None if no opening
    order is journalled since the previous exit from that stage)."""
    by_coid, _ = _resolve_orders(orders, decisions)
    opens = sorted((e["resolved_ts"], e["resolved_to"], e["symbol"])
                   for e in by_coid.values() if e.get("resolved_to") in OPEN_STAGES)
    out, last_exit = [], {}
    for t, d in sorted(((_ts(d["ts"]), d) for d in decisions),
                       key=lambda x: (x[0], x[1]["rowid"])):
        key = (d["stage_from"], d["stage_to"])
        if key not in EXPECTED_ACTIVITY:
            continue
        floor = last_exit.get(d["stage_from"])
        contract = None
        for ot, stage, sym in opens:
            if stage == d["stage_from"] and ot <= t and (floor is None or ot > floor):
                contract = sym
        last_exit[d["stage_from"]] = t
        out.append({"rowid": d["rowid"], "ts": t, "stage_from": key[0],
                    "stage_to": key[1], "trigger": TRIGGERS[key],
                    "expected": EXPECTED_ACTIVITY[key], "contract": contract})
    if since is not None:
        out = [x for x in out if x["ts"] >= since]
    return out


# --------------------------------------------------------------------------- #
# Broker side (list orders and activities only)                               #
# --------------------------------------------------------------------------- #

def build_trading_client():
    """As run.py: credentials, the paper check BEFORE any client exists, then
    TradingClient(key, secret, paper=paper)."""
    from credentials import load_credentials
    api_key, api_secret, paper = load_credentials()
    if paper is not True:
        raise ReportError("refused:not_paper")
    from alpaca.trading.client import TradingClient
    return TradingClient(api_key, api_secret, paper=paper)


def list_broker_orders(client, since=None):
    """Every order (all statuses) submitted at or after `since`, paginated to
    completion. Returns a list in first-seen order."""
    from alpaca.common.enums import Sort
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    seen = {}
    after = None if since is None else since - PAGE_OVERLAP
    while True:
        page = client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.ALL, limit=PAGE_LIMIT, after=after,
            direction=Sort.ASC))
        new = [o for o in page if str(o.id) not in seen]
        for o in new:
            seen[str(o.id)] = o
        if len(page) < PAGE_LIMIT:
            break
        if not new:
            raise ReportError("pagination stalled: a full page with no new order")
        after = max(o.submitted_at for o in page) - PAGE_OVERLAP
    out = list(seen.values())
    if since is not None:
        out = [o for o in out if o.submitted_at >= since]
    return out


def _activity(raw):
    """A kept activity as a dict: id, type, symbol, date, qty, price."""
    try:
        a = {"id": raw["id"], "type": ActivityType(raw["activity_type"]).value,
             "symbol": raw["symbol"], "date": date.fromisoformat(raw["date"]),
             "qty": raw.get("qty"), "price": raw.get("price")}
    except (KeyError, TypeError, ValueError):
        raise ReportError(f"activity malformed: {_fmt(raw.get('id'))}")
    if not isinstance(a["symbol"], str) or not a["symbol"]:
        raise ReportError(f"activity malformed: {_fmt(a['id'])}")
    return a


def list_broker_activities(client, since=None):
    """Every OPASN / OPEXP / OPEXC / OPTRD activity dated on or after
    `since`, paginated to completion. Returns a list in first-seen order."""
    from alpaca.common.enums import Sort

    params = {"direction": Sort.ASC.value, "page_size": ACTIVITY_PAGE_SIZE}
    if since is not None:
        params["after"] = (since - ACTIVITY_LAG).date().isoformat()
    seen = {}
    while True:
        page = client.get(ACTIVITIES_PATH, dict(params))
        if not isinstance(page, list):
            raise ReportError("activities response not a list")
        if not page:
            break
        if not all(isinstance(a, dict) and isinstance(a.get("id"), str) for a in page):
            raise ReportError("activity without an id")
        new = [a for a in page if a["id"] not in seen]
        if not new:
            raise ReportError("pagination stalled: a page with no new activity")
        for a in new:
            seen[a["id"]] = a
        params["page_token"] = page[-1]["id"]
    out = [_activity(a) for a in seen.values() if a.get("activity_type") in ACTIVITY_TYPES]
    if since is not None:
        out = [a for a in out if a["date"] >= since.date()]
    return out


# --------------------------------------------------------------------------- #
# Match                                                                       #
# --------------------------------------------------------------------------- #

def status_contradicts(journal_status, broker_status):
    if journal_status is None or journal_status not in fs.ORDER_STATUS_CLASS:
        return True
    return journal_status in TERMINAL and journal_status != broker_status


def _broker_fields(o):
    return {"symbol": o.symbol, "side": _val(o.side), "qty": o.qty,
            "limit": o.limit_price}


def _field_differs(field, j, b):
    if field in ("qty", "limit"):
        jd, bd = _dec(j), _dec(b)
        return jd is None or bd is None or jd != bd
    return j != b


def match(jmap, jnone, border):
    """Rows: dict(coid, shown fields, journal_status, broker_status, filled_qty,
    filled_avg_price, flags, broker_diff, matched). Raises on a duplicate
    broker coid."""
    bmap, bnone = {}, []
    for o in border:
        if not o.client_order_id:
            bnone.append(o)
        elif o.client_order_id in bmap:
            raise ReportError(f"duplicate client_order_id at broker: {_fmt(o.client_order_id)}")
        else:
            bmap[o.client_order_id] = o

    rows = []
    for coid in sorted(set(jmap) | set(bmap)):
        j, o = jmap.get(coid), bmap.get(coid)
        row = {"coid": coid, "flags": [], "broker_diff": {},
               "matched": j is not None and o is not None}
        if j is not None:
            row.update({f: j[f] for f in FIELDS})
            row["journal_status"] = j["status"] if j["status"] is not None else "intent_only"
        else:
            row.update(_broker_fields(o))
            row["journal_status"] = None
        if o is not None:
            row.update(broker_status=_val(o.status), filled_qty=o.filled_qty,
                       filled_avg_price=o.filled_avg_price)
        else:
            row.update(broker_status=None, filled_qty=None, filled_avg_price=None)
        if o is None:
            row["flags"].append("JOURNAL_ONLY")
        elif j is None:
            row["flags"].append("BROKER_ONLY")
        else:
            bf = _broker_fields(o)
            for f in FIELDS:
                if _field_differs(f, j[f], bf[f]):
                    row["flags"].append(f"MISMATCH:{f}")
                    row["broker_diff"][f] = bf[f]
            if status_contradicts(j["status"], row["broker_status"]):
                row["flags"].append("MISMATCH:status")
        rows.append(row)

    for e in jnone:
        rows.append({"coid": None, **{f: e[f] for f in FIELDS},
                     "journal_status": e["status"] or "intent_only",
                     "broker_status": None, "filled_qty": None,
                     "filled_avg_price": None, "flags": ["JOURNAL_ONLY"],
                     "broker_diff": {}, "matched": False})
    for o in sorted(bnone, key=lambda o: str(o.id)):
        rows.append({"coid": None, **_broker_fields(o), "journal_status": None,
                     "broker_status": _val(o.status), "filled_qty": o.filled_qty,
                     "filled_avg_price": o.filled_avg_price,
                     "flags": ["BROKER_ONLY"], "broker_diff": {},
                     "matched": False})
    return rows


def _underlying(contract):
    """OCC root of a contract symbol (ROOT YYMMDD C/P 8 digits), else None."""
    if len(contract) > 15 and contract[-8:].isdigit() and contract[-9] in "CP":
        return contract[:-15]
    return None


def match_activities(transitions, activities):
    """ACTIVITY rows: dict(symbol, stage_from, stage_to, trigger, journal_ts,
    expected, broker_type, broker_date, broker_qty, share_qty, share_price,
    flags, matched)."""
    key = lambda a: (a["date"], a["id"])  # noqa: E731
    events = sorted((a for a in activities if a["type"] != SHARE_TRADE.value), key=key)
    trades = sorted((a for a in activities if a["type"] == SHARE_TRADE.value), key=key)
    share, paired_trades = {}, set()  # event id -> its OPTRD
    for ev in events:
        if ev["type"] == ActivityType.OPEXP.value:
            continue
        for t in trades:
            if (t["id"] not in paired_trades and t["date"] == ev["date"]
                    and t["symbol"] == _underlying(ev["symbol"])):
                share[ev["id"]] = t
                paired_trades.add(t["id"])
                break

    def row(symbol, tr=None, a=None):
        s = share.get(a["id"]) if a else None
        return {"symbol": symbol,
                "stage_from": tr and tr["stage_from"], "stage_to": tr and tr["stage_to"],
                "trigger": tr and tr["trigger"], "journal_ts": tr and tr["ts"].isoformat(),
                "expected": tr and tr["expected"],
                "broker_type": a and a["type"], "broker_date": a and a["date"],
                "broker_qty": a and a["qty"],
                "share_qty": s and s["qty"], "share_price": s and s["price"],
                "flags": [], "matched": tr is not None and a is not None}

    rows, used = [], set()
    for tr in sorted(transitions, key=lambda x: (x["ts"], x["rowid"])):
        a = None
        if tr["contract"] is not None:
            a = next((ev for ev in events if ev["id"] not in used
                      and ev["symbol"] == tr["contract"]), None)
        r = row(tr["contract"], tr, a)
        if a is None:
            r["flags"].append("TRANSITION_ONLY")
        else:
            used.add(a["id"])
            if a["type"] != tr["expected"]:
                r["flags"].append("MISMATCH:type")
        rows.append(r)
    for ev in events:
        if ev["id"] not in used:
            r = row(ev["symbol"], None, ev)
            r["flags"].append("ACTIVITY_ONLY")
            rows.append(r)
    for t in trades:
        if t["id"] not in paired_trades:
            r = row(t["symbol"], None, t)
            r["broker_qty"], r["share_qty"], r["share_price"] = None, t["qty"], t["price"]
            r["flags"].append("ACTIVITY_ONLY")
            rows.append(r)
    return rows


def render_activity(r):
    return (f"ACTIVITY symbol={_fmt(r['symbol'])} from={_fmt(r['stage_from'])} "
            f"to={_fmt(r['stage_to'])} trigger={_fmt(r['trigger'])} "
            f"journal_ts={_fmt(r['journal_ts'])} expected={_fmt(r['expected'])} "
            f"broker_type={_fmt(r['broker_type'])} broker_date={_fmt(r['broker_date'])} "
            f"broker_qty={_fmt(r['broker_qty'])} share_qty={_fmt(r['share_qty'])} "
            f"share_price={_fmt(r['share_price'])} "
            f"flags={','.join(r['flags']) or 'OK'}")


def render_row(r):
    text = (f"ORDER coid={_fmt(r['coid'])} symbol={_fmt(r['symbol'])} "
            f"side={_fmt(r['side'])} qty={_fmt(r['qty'])} limit={_fmt(r['limit'])} "
            f"journal_status={_fmt(r['journal_status'])} "
            f"broker_status={_fmt(r['broker_status'])} "
            f"filled_qty={_fmt(r['filled_qty'])} "
            f"filled_avg_price={_fmt(r['filled_avg_price'])} "
            f"flags={','.join(r['flags']) or 'OK'}")
    for f, v in r["broker_diff"].items():
        text += f" broker_{f}={_fmt(v)}"
    return text


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(
        prog="forgetrader.journal_report",
        description="Journal vs broker order history, matched by "
                    "client_order_id, and journal transitions out of an OPEN "
                    "stage vs broker option activities, matched by contract. "
                    "Read-only; lists orders and activities, never sends one.")
    p.add_argument("--since", type=_parse_since, default=None,
                   help="YYYY-MM-DD (UTC): only orders, transitions and "
                        "activities at or after this date")
    p.add_argument("dir", metavar="DIR",
                   help="account directory (state.json, journal.db, .env), "
                        "the checkout this runs from; label = basename")
    return p


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    d = Path(args.dir).resolve()
    label = _fmt(d.name)
    try:
        import credentials
        if d != Path(credentials._ENV_PATH).resolve().parent:
            raise ReportError("account_dir_mismatch: DIR is not the checkout "
                              "this code runs from")
        orders, decisions = _read_journal(d / "journal.db")
        jmap, jnone = journal_orders(orders, decisions, args.since)
        transitions = journal_transitions(orders, decisions, args.since)
        client = build_trading_client()
        border = list_broker_orders(client, args.since)
        activities = list_broker_activities(client, args.since)
        rows = match(jmap, jnone, border)
        arows = match_activities(transitions, activities)
    except ReportError as e:
        print(f"ERROR {label} {_fmt(e)}", file=sys.stderr)
        return 1
    except Exception as e:  # type name only: broker text can carry a URL
        print(f"ERROR {label} {type(e).__name__}", file=sys.stderr)
        return 1

    for r in rows:
        print(render_row(r))
    for r in arows:
        print(render_activity(r))
    matched = sum(1 for r in rows if r["matched"])
    amatched = sum(1 for r in arows if r["matched"])
    discrepancies = sum(1 for r in rows + arows if r["flags"])
    print(f"REPORT {label} orders={len(rows)} matched={matched} "
          f"activities={len(arows)} activities_matched={amatched} "
          f"discrepancies={discrepancies}")
    return 3 if discrepancies else 0


if __name__ == "__main__":
    sys.exit(main())
