#!/usr/bin/env python3
"""Journal-vs-broker order report (funding criterion 3). Read-only.

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

--since D keeps journal orders whose first row ts and broker orders whose
submitted_at are at or after D 00:00 UTC. An order journalled just before
midnight UTC and submitted just after it (or the reverse) reports one-sided.

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

Last line: REPORT <label> orders=N matched=M discrepancies=K
  N = ORDER lines, M = coids present on both sides, K = lines with any flag.
Exit 0 only if K == 0; 3 if K > 0; 1 on any error or refusal (argparse
errors included). Never prints a key, token or URL: an exception prints its
type name only.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

import forgetrader_state as fs

PAGE_LIMIT = 500  # documented max for GetOrdersRequest.limit (alpaca-py 0.43.5)
PAGE_OVERLAP = timedelta(seconds=1)
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "replaced"})
PENDING_STAGES = (fs.CSP_PENDING, fs.CC_PENDING)
FIELDS = ("symbol", "side", "qty", "limit")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
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


def journal_orders(orders, decisions, since=None):
    """{coid: entry} for journalled orders, plus a list of entries for rows
    without a client_order_id. Entry: symbol, side, qty, limit, status,
    source, ts."""
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
                break

    if since is not None:
        by_coid = {k: e for k, e in by_coid.items() if e["ts"] >= since}
        no_coid = [e for e in no_coid if e["ts"] >= since]
    return by_coid, no_coid


# --------------------------------------------------------------------------- #
# Broker side (list orders only)                                              #
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
                    "client_order_id. Read-only; lists orders, never sends one.")
    p.add_argument("--since", type=_parse_since, default=None,
                   help="YYYY-MM-DD (UTC): only orders at or after this date")
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
        client = build_trading_client()
        border = list_broker_orders(client, args.since)
        rows = match(jmap, jnone, border)
    except ReportError as e:
        print(f"ERROR {label} {_fmt(e)}", file=sys.stderr)
        return 1
    except Exception as e:  # type name only: broker text can carry a URL
        print(f"ERROR {label} {type(e).__name__}", file=sys.stderr)
        return 1

    for r in rows:
        print(render_row(r))
    matched = sum(1 for r in rows if r["matched"])
    discrepancies = sum(1 for r in rows if r["flags"])
    print(f"REPORT {label} orders={len(rows)} matched={matched} "
          f"discrepancies={discrepancies}")
    return 3 if discrepancies else 0


if __name__ == "__main__":
    sys.exit(main())
