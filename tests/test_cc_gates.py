"""Brief B5 - Stage 2 producer: cost_basis on assignment, holding_basis_invalid,
gates.evaluate_call, forgetrader.cc_scan.scan_calls and its dry CLI.

Every reject and HALT token has a fixture asserting WHICH check fired (the
exact rejected_by string or action string). Fixtures pin their dates: the
run clock is 2026-01-15 15:00 UTC (10:00 ET, ET date 2026-01-15) and no
fixture reads the host clock. No broker, no keys, no state.json / journal.db:
the CLI runs against fake clients in a temp cwd and must write nothing.

Gate baseline (every gate passes against the pinned policy below):
    cost_basis 25.00, otm_above_basis_pct 10 -> strike floor 27.50
    strike 28.00, expiry 2026-02-12 -> dte 28 (14..35)
    bid 0.50 / ask 0.55 -> spread 0.05 (<= 0.10), spread/bid 0.10 (<= 0.5)
    open_interest "150" (>= 100), price_ts 10 min before the run clock (<= 60)
    yield (0.50*100)/(25.00*100) * 365/28 * 100 = 26.0714 (>= 7)
    earnings 2026-03-01, expiry strictly before it

Style follows tests/test_state_machine.py: plain checks, a counter, last line
PASS: ... or FAIL: N failed of M assertions.
"""
import ast
import contextlib
import copy
import io
import json
import math
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import forgetrader_state as fs  # noqa: E402
import gates  # noqa: E402
import screener  # noqa: E402
import watchlist  # noqa: E402
from forgetrader import cc_scan  # noqa: E402

PASSED = 0
FAILURES = []


def check(cond, msg):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILURES.append(msg)


# --------------------------------------------------------------------------- #
# Part S - cost_basis on assignment + holding_basis_invalid                    #
# --------------------------------------------------------------------------- #

BASIS_PUT = "NOK261218P00025000"
AS_OF = date(2026, 12, 18)


def _wheel_state(stage, contract=None, basis=None):
    s = fs.fresh_flat_state()
    s["wheel"].update({"stage": stage, "ticker": "NOK",
                       "contract_symbol": contract, "cost_basis": basis,
                       "intent": "wheel", "client_order_id": "coid-1"})
    return s


def part_s():
    # S1 basis fixture: assigned on ...P00025000 gives cost_basis 25.
    st = _wheel_state(fs.CSP_OPEN, BASIS_PUT)
    ns, a = fs.reconcile(st, {"shares": 100, "put_open": False}, AS_OF)
    check(a == "CSP_OPEN->HOLDING (assigned)", f"S1 action {a!r}")
    check(ns["wheel"]["cost_basis"] == 25 and
          type(ns["wheel"]["cost_basis"]) is float,
          f"S1 cost_basis {ns['wheel']['cost_basis']!r}")
    check(ns["wheel"] == dict(fs.fresh_flat_state()["wheel"], stage="HOLDING",
                              ticker="NOK", cost_basis=25.0),
          f"S1 wheel after allowlist reset {ns['wheel']!r}")
    check(st["wheel"]["cost_basis"] is None and st["wheel"]["stage"] == "CSP_OPEN",
          "S1 input mutated")
    # direct apply_trigger, fractional strike, Decimal parse
    ns, a = fs.apply_trigger(_wheel_state(fs.CSP_OPEN, "NOK261218P00007500"),
                             "assigned")
    check(a == "CSP_OPEN->HOLDING (assigned)" and
          ns["wheel"]["cost_basis"] == 7.5, f"S1b {a!r} {ns['wheel']!r}")
    # the assignment overwrites a stale seeded basis with the put's strike
    ns, a = fs.apply_trigger(_wheel_state(fs.CSP_OPEN, BASIS_PUT, 99.0),
                             "assigned")
    check(ns["wheel"]["cost_basis"] == 25.0, f"S1c {ns['wheel']!r}")

    # S2 assignment HALTs: missing / unparseable symbol, right not P.
    for label, contract, exp in (
        ("none", None,
         "HALT:CSP_OPEN assigned cannot parse strike from contract_symbol None"),
        ("short", "NOK2611P0",
         "HALT:CSP_OPEN assigned cannot parse strike from contract_symbol 'NOK2611P0'"),
        ("non-digit", "NOK261218P0002500X",
         "HALT:CSP_OPEN assigned cannot parse strike from contract_symbol "
         "'NOK261218P0002500X'"),
        ("call", "NOK261218C00025000",
         "HALT:CSP_OPEN assigned contract_symbol 'NOK261218C00025000' right is not P"),
    ):
        st = _wheel_state(fs.CSP_OPEN, contract)
        ns, a = fs.reconcile(st, {"shares": 100, "put_open": False}, AS_OF)
        check(a == exp, f"S2-{label} reconcile action {a!r}")
        check(ns is st and st["wheel"]["stage"] == "CSP_OPEN",
              f"S2-{label} reconcile must return the input at CSP_OPEN")
        ns, a = fs.apply_trigger(st, "assigned")
        check(a == exp and ns is st, f"S2-{label} apply_trigger {a!r}")

    # S3 HOLDING stable needs a numeric basis > 0.
    for label, basis, exp in (
        ("none", None, "HALT:holding_basis_invalid cost_basis=None"),
        ("str", "25", "HALT:holding_basis_invalid cost_basis='25'"),
        ("bool", True, "HALT:holding_basis_invalid cost_basis=True"),
        ("zero", 0, "HALT:holding_basis_invalid cost_basis=0"),
        ("neg", -1.0, "HALT:holding_basis_invalid cost_basis=-1.0"),
        ("nan", float("nan"), "HALT:holding_basis_invalid cost_basis=nan"),
        ("inf", float("inf"), "HALT:holding_basis_invalid cost_basis=inf"),
        ("ok", 25.0, "noop:HOLDING stable"),
        ("ok-int", 25, "noop:HOLDING stable"),
    ):
        st = _wheel_state(fs.HOLDING, None, basis)
        ns, a = fs.reconcile(st, {"shares": 100, "put_open": False,
                                  "call_open": False}, AS_OF)
        check(a == exp, f"S3-{label} action {a!r}")
        check(ns is st and st["wheel"]["stage"] == "HOLDING",
              f"S3-{label} must return the input at HOLDING")
    # broker mismatch at HOLDING keeps its own HALT, ahead of the basis check
    st = _wheel_state(fs.HOLDING, None, None)
    ns, a = fs.reconcile(st, {"shares": 0}, AS_OF)
    check(a == "HALT:HOLDING but broker shows shares=0 put_open=False "
               "call_open=False", f"S3-mismatch {a!r}")

    # S4 every transition INTO HOLDING checks the basis.
    CALL = "NOK261218C00030000"
    into = (
        ("cc_pending-canceled", fs.CC_PENDING, None,
         {"shares": 100, "call_open": False, "pending_order_status": "canceled"},
         "CC_PENDING->HOLDING (cancel_or_expire)"),
        ("cc_open-expired", fs.CC_OPEN, CALL,
         {"shares": 100, "call_open": False},
         "CC_OPEN->HOLDING (cc_expired_worthless)"),
    )
    for label, stage, contract, snap, ok_action in into:
        st = _wheel_state(stage, contract, None)
        ns, a = fs.reconcile(st, snap, AS_OF)
        check(a == "HALT:holding_basis_invalid cost_basis=None",
              f"S4-{label}-none action {a!r}")
        check(ns is st and st["wheel"]["stage"] == stage,
              f"S4-{label}-none must return the input at {stage}")
        st = _wheel_state(stage, contract, 25.0)
        ns, a = fs.reconcile(st, snap, AS_OF)
        check(a == ok_action and ns["wheel"]["cost_basis"] == 25.0 and
              ns["wheel"]["stage"] == "HOLDING", f"S4-{label}-ok {a!r}")
    # assignment on a zero strike parses, then fails the HOLDING basis check
    st = _wheel_state(fs.CSP_OPEN, "NOK261218P00000000")
    ns, a = fs.reconcile(st, {"shares": 100, "put_open": False}, AS_OF)
    check(a == "HALT:holding_basis_invalid cost_basis=0.0",
          f"S4-zero-strike action {a!r}")
    check(ns is st and st["wheel"]["stage"] == "CSP_OPEN",
          "S4-zero-strike must return the input at CSP_OPEN")


# --------------------------------------------------------------------------- #
# Part G - gates.evaluate_call                                                #
# --------------------------------------------------------------------------- #

UTC = timezone.utc
RUN_TS = datetime(2026, 1, 15, 15, 0, tzinfo=UTC)   # 10:00 ET
TODAY = date(2026, 1, 15)
EARN = date(2026, 3, 1)

POLICY = {
    "entry": {"dte_min": 14, "dte_max": 35, "min_bid": 0.10,
              "min_open_interest": 100, "max_spread_abs": 0.10,
              "max_spread_to_bid_ratio": 0.5, "min_annualized_yield_pct": 7,
              "otm_target_pct": 10},
    "universe": {"min_underlying_price": 5.0, "max_price_staleness_min": 60},
    "calls": {"otm_above_basis_pct": 10},
    "runtime": {"rate_limit_per_min": 100000, "snapshot_batch_size": 100,
                "feed": "iex"},
}


class FakeContract:
    def __init__(self, symbol="NOK260212C00028000", strike=28.0,
                 expiry=date(2026, 2, 12), oi="150", root="NOK",
                 underlying="NOK"):
        self.symbol = symbol
        self.strike_price = strike
        self.expiration_date = expiry
        self.open_interest = oi
        self.root_symbol = root
        self.underlying_symbol = underlying


class FakeQuote:
    def __init__(self, bid, ask):
        self.bid_price = bid
        self.ask_price = ask


class FakeSnap:
    def __init__(self, bid=0.50, ask=0.55, quote=True):
        self.latest_quote = FakeQuote(bid, ask) if quote else None


def _eval(contract=None, snap="base", basis=25.0, price=30.0,
          price_ts=RUN_TS - timedelta(minutes=10), run_ts=RUN_TS,
          earnings=EARN):
    contract = contract or FakeContract()
    if snap == "base":
        snap = FakeSnap()
    e = POLICY["entry"]
    return gates.evaluate_call(
        contract, snap, basis, price, price_ts, run_ts, TODAY, earnings,
        e["dte_min"], e["dte_max"], POLICY["calls"]["otm_above_basis_pct"],
        e["min_bid"], e["min_open_interest"], e["max_spread_abs"],
        e["max_spread_to_bid_ratio"], e["min_annualized_yield_pct"],
        POLICY["universe"]["max_price_staleness_min"])


def _c(**kw):
    return FakeContract(**kw)


UNDEF_SPREAD_YIELD = "undefined_metric_spread;undefined_metric_yield"

# (label, kwargs for _eval, expected rejected_by)
GATE_FIXTURES = [
    ("baseline", {}, ""),
    ("adjusted", {"contract": _c(root="NOK1")}, "adjusted_contract"),
    ("right-put", {"contract": _c(symbol="NOK260212P00028000")}, "right_not_call"),
    ("right-malformed", {"contract": _c(symbol="NOK")}, "right_not_call"),
    ("stale-61", {"price_ts": RUN_TS - timedelta(minutes=61)},
     "stale_underlying_price"),
    ("stale-boundary-60", {"price_ts": RUN_TS - timedelta(minutes=60)}, ""),
    ("stale-future-price", {"price_ts": RUN_TS + timedelta(minutes=2)}, ""),
    ("stale-price-ts-none", {"price_ts": None}, "stale_underlying_price"),
    ("stale-price-ts-naive", {"price_ts": datetime(2026, 1, 15, 14, 50)},
     "stale_underlying_price"),
    ("stale-run-ts-naive", {"run_ts": datetime(2026, 1, 15, 15, 0)},
     "stale_underlying_price"),
    ("dte-13", {"contract": _c(expiry=date(2026, 1, 28))}, "dte"),
    ("dte-14", {"contract": _c(expiry=date(2026, 1, 29))}, ""),
    ("dte-35", {"contract": _c(expiry=date(2026, 2, 19))}, ""),
    ("dte-36", {"contract": _c(expiry=date(2026, 2, 20))}, "dte"),
    ("dte-0", {"contract": _c(expiry=TODAY)}, "dte;undefined_metric_yield"),
    ("strike-at-floor", {"contract": _c(strike=27.5)}, ""),
    ("strike-below-floor", {"contract": _c(strike=27.49)},
     "strike_below_basis_floor"),
    ("basis-none", {"basis": None}, "undefined_metric_basis;undefined_metric_yield"),
    ("basis-zero", {"basis": 0}, "undefined_metric_basis;undefined_metric_yield"),
    ("basis-neg", {"basis": -25.0}, "undefined_metric_basis;undefined_metric_yield"),
    ("basis-bool", {"basis": True}, "undefined_metric_basis;undefined_metric_yield"),
    ("basis-nan", {"basis": float("nan")},
     "undefined_metric_basis;undefined_metric_yield"),
    ("no-snapshot", {"snap": None}, f"no_snapshot;{UNDEF_SPREAD_YIELD}"),
    ("null-quote", {"snap": FakeSnap(quote=False)}, f"null_quote;{UNDEF_SPREAD_YIELD}"),
    ("null-bid", {"snap": FakeSnap(bid=None)}, f"null_bid;{UNDEF_SPREAD_YIELD}"),
    ("null-ask", {"snap": FakeSnap(ask=None)}, "undefined_metric_spread"),
    ("zero-bid", {"snap": FakeSnap(bid=0.0, ask=0.05)},
     "zero_bid;spread_ratio_undefined_no_bid;yield_too_low"),
    ("bid-too-low", {"snap": FakeSnap(bid=0.05, ask=0.07)},
     "bid_too_low;yield_too_low"),
    ("oi-null", {"contract": _c(oi=None)}, "null_oi"),
    ("oi-99", {"contract": _c(oi="99")}, "oi_too_low"),
    ("oi-100", {"contract": _c(oi="100")}, ""),
    ("spread-too-wide", {"snap": FakeSnap(bid=0.50, ask=0.65)}, "spread_too_wide"),
    ("spread-wide-vs-bid", {"snap": FakeSnap(bid=0.15, ask=0.24)},
     "spread_wide_vs_bid"),
    ("spread-both", {"snap": FakeSnap(bid=0.15, ask=0.40)},
     "spread_too_wide;spread_wide_vs_bid"),
    ("yield-too-low", {"snap": FakeSnap(bid=0.12, ask=0.15)}, "yield_too_low"),
    # yield passes on the basis denominator (7.82%), would fail on strike (4.89%)
    ("yield-denominator-basis", {"contract": _c(strike=40.0),
                                 "snap": FakeSnap(bid=0.15, ask=0.18)}, ""),
    ("earnings-equal-expiry", {"earnings": date(2026, 2, 12)},
     "earnings_inside_option"),
    ("earnings-day-after", {"earnings": date(2026, 2, 13)}, ""),
    ("earnings-none", {"earnings": None}, "earnings_inside_option"),
    ("many-fails-counted", {"contract": _c(root="NOK1", oi="5", strike=20.0),
                            "earnings": date(2026, 2, 1)},
     "adjusted_contract;strike_below_basis_floor;oi_too_low;earnings_inside_option"),
]


def part_g():
    for label, kw, exp in GATE_FIXTURES:
        row = _eval(**kw)
        check(row["rejected_by"] == exp,
              f"G-{label}: rejected_by expected {exp!r} got {row['rejected_by']!r}")
        check(row["all_pass"] is (exp == ""),
              f"G-{label}: all_pass {row['all_pass']!r}")

    # exact values on the baseline row
    row = _eval()
    for key, exp in (("dte", 28), ("strike_floor", 27.5), ("bid", 0.50),
                     ("open_interest", 150), ("spread_abs", 0.05),
                     ("spread_to_bid", 0.1), ("underlying_staleness_min", 10.0),
                     ("annualized_yield_pct", 26.0714), ("right", "C"),
                     ("cost_basis", 25.0), ("earnings_date", "2026-03-01"),
                     ("contract_symbol", "NOK260212C00028000")):
        check(row[key] == exp, f"G-values: {key} expected {exp!r} got {row[key]!r}")
    # staleness is measured against the run clock, never the host clock: the
    # host is months past 2026-01-15, so a host-clock read would be stale.
    check(row["staleness_pass"] is True and row["underlying_staleness_min"] == 10.0,
          f"G-run-clock staleness {row['underlying_staleness_min']!r}")
    # future-stamped price renders a negative staleness and passes
    row = _eval(price_ts=RUN_TS + timedelta(minutes=2))
    check(row["underlying_staleness_min"] == -2.0,
          f"G-future staleness {row['underlying_staleness_min']!r}")
    # yield denominator is the basis: 0.15*100/(25*100)*365/28*100
    row = _eval(contract=_c(strike=40.0), snap=FakeSnap(bid=0.15, ask=0.18))
    check(row["annualized_yield_pct"] == round(15 / 2500 * 365 / 28 * 100, 4),
          f"G-yield-basis value {row['annualized_yield_pct']!r}")

    # missing values are "" never None
    for label, kw, keys in (
        ("no-snapshot", {"snap": None},
         ("bid", "ask", "spread_abs", "spread_to_bid", "annualized_yield_pct")),
        ("basis-none", {"basis": None}, ("cost_basis", "strike_floor",
                                         "annualized_yield_pct")),
        ("price-ts-none", {"price_ts": None, "price": None},
         ("underlying_staleness_min", "underlying_price_ts", "underlying_price")),
        ("oi-null", {"contract": _c(oi=None)}, ("open_interest",)),
        ("earnings-none", {"earnings": None}, ("earnings_date",)),
    ):
        row = _eval(**kw)
        check(all(row[k] == "" for k in keys),
              f"G-blank-{label}: {[(k, row[k]) for k in keys]!r}")
    check(all(v is not None for v in _eval(snap=None, basis=None,
                                           price_ts=None).values()),
          "G-blank: a None value leaked into the row")

    # evaluate_contract is unchanged in signature
    import inspect
    params = list(inspect.signature(gates.evaluate_contract).parameters)
    check(params == ["contract", "snap", "underlying_price", "underlying_price_ts",
                     "underlying_staleness_min", "today", "dte_min", "dte_max",
                     "otm_target_pct", "min_bid", "min_oi", "max_spread_abs",
                     "max_spread_to_bid_ratio", "min_yield_pct", "available",
                     "max_staleness_min"],
          f"G-evaluate_contract signature changed: {params!r}")


# --------------------------------------------------------------------------- #
# Part C - scan_calls with fake clients                                       #
# --------------------------------------------------------------------------- #

class FakeClock:
    def __init__(self, ts=RUN_TS, is_open=True):
        self.timestamp = ts
        self.is_open = is_open
        self.next_open = self.next_close = ts


class FakeTrade:
    def __init__(self, price, ts):
        self.price = price
        self.timestamp = ts


class FakeStockSnap:
    def __init__(self, price=30.0, ts=RUN_TS - timedelta(minutes=10)):
        self.latest_trade = FakeTrade(price, ts) if price is not None else None
        self.minute_bar = self.daily_bar = self.previous_daily_bar = None


class FakeResp:
    def __init__(self, contracts, token=None):
        self.option_contracts = contracts
        self.next_page_token = token


class FakeTrading:
    def __init__(self, pages, clock=None):
        self.pages = list(pages)
        self.requests = []
        self.clock_calls = 0
        self.clock = clock or FakeClock()
        self.order_calls = 0

    def get_clock(self):
        self.clock_calls += 1
        return self.clock

    def get_option_contracts(self, req):
        self.requests.append(req)
        return self.pages.pop(0)

    def submit_order(self, *a, **k):  # tripwire: the scan has no order path
        self.order_calls += 1
        raise AssertionError("submit_order called")


class FakeStock:
    def __init__(self, snaps):
        self.snaps = snaps
        self.requests = []

    def get_stock_snapshot(self, req):
        self.requests.append(req)
        return self.snaps


class FakeOption:
    def __init__(self, chain):
        self.chain = chain
        self.requests = []

    def get_option_chain(self, req):
        self.requests.append(req)
        return self.chain


def _wl_entry(ticker, earnings="2026-03-01", note="Willing to own 100 shares."):
    return {"ticker": ticker, "added": "2026-01-01", "earnings_date": earnings,
            "earnings_source": "test fixture", "earnings_stamped": "2026-01-02",
            "own_100_note": note, "review_by": "2026-02-01"}


def _write_wl(tmp, entries, name="wl.json"):
    p = Path(tmp) / name
    p.write_text(json.dumps({"version": 2, "entries": entries}))
    return p


def _chain_fixture():
    """Four calls: A OI 500, B OI 300 yield higher than C OI 300, D fails."""
    a = FakeContract(symbol="NOK260212C00028000", strike=28.0, oi="500")
    b = FakeContract(symbol="NOK260212C00029000", strike=29.0, oi="300")
    c = FakeContract(symbol="NOK260212C00030000", strike=30.0, oi="300")
    d = FakeContract(symbol="NOK260212C00027000", strike=27.0, oi="900")
    chain = {a.symbol: FakeSnap(0.50, 0.55), b.symbol: FakeSnap(0.40, 0.45),
             c.symbol: FakeSnap(0.30, 0.35), d.symbol: FakeSnap(0.60, 0.65)}
    return [a, b, c, d], chain


def _scan(tmp, entries, ticker="NOK", basis=25.0, pages=None, chain=None,
          snaps="base", clock=None):
    contracts, default_chain = _chain_fixture()
    tc = FakeTrading(pages if pages is not None else
                     [FakeResp(contracts[:2], "p2"), FakeResp(contracts[2:])])
    sc = FakeStock({ticker: FakeStockSnap()} if snaps == "base" else snaps)
    oc = FakeOption(default_chain if chain is None else chain)
    path = _write_wl(tmp, entries)
    with contextlib.redirect_stdout(io.StringIO()):
        res = cc_scan.scan_calls(ticker, basis, tc, sc, oc, POLICY,
                                 clock or FakeClock(), watchlist_path=path)
    return res, tc, sc, oc


def part_c():
    screener.init_throttle(POLICY["runtime"]["rate_limit_per_min"])
    from alpaca.trading.enums import ContractType
    with tempfile.TemporaryDirectory() as tmp:
        # C1 ranking: OI desc, yield tiebreak; D fails the strike floor
        res, tc, sc, oc = _scan(tmp, [_wl_entry("NOK")])
        order = [r["contract_symbol"] for r in res.candidates]
        check(order == ["NOK260212C00028000", "NOK260212C00029000",
                        "NOK260212C00030000"], f"C1 order {order!r}")
        check(res.rejection is None and len(res.all_rows) == 4,
              f"C1 rejection {res.rejection!r} rows {len(res.all_rows)}")
        check(dict(res.reject_tally) == {"strike_below_basis_floor": 1},
              f"C1 tally {dict(res.reject_tally)!r}")
        check(tc.clock_calls == 0, f"C1 scan read its own clock {tc.clock_calls}")
        check(tc.order_calls == 0, "C1 order call")
        check(len(tc.requests) == 2 and tc.requests[1].page_token == "p2",
              "C1 pagination not followed")
        r0 = tc.requests[0]
        check(r0.type == ContractType.CALL and r0.underlying_symbols == ["NOK"]
              and r0.expiration_date_gte == date(2026, 1, 29)
              and r0.expiration_date_lte == date(2026, 3, 1),
              f"C1 contracts request {r0!r}")
        check(len(oc.requests) == 1 and oc.requests[0].type == ContractType.CALL
              and oc.requests[0].underlying_symbol == "NOK",
              f"C1 chain request {oc.requests!r}")
        check(res.run_date == TODAY and res.earnings_date == EARN,
              f"C1 run_date {res.run_date} earnings {res.earnings_date}")

        # C2 not on the watchlist: zero candidates, named rejection, no data calls
        res, tc, sc, oc = _scan(tmp, [_wl_entry("BAC")])
        check(res.rejection == "not_on_watchlist" and res.candidates == []
              and dict(res.reject_tally) == {"name:not_on_watchlist": 1},
              f"C2 {res.rejection!r} {dict(res.reject_tally)!r}")
        check(not (tc.requests or sc.requests or oc.requests),
              "C2 data calls made for a non-watchlist ticker")
        # on the file but rejected for another reason is still not approved
        res, *_ = _scan(tmp, [_wl_entry("NOK", note=" ")])
        check(res.rejection == "not_on_watchlist",
              f"C2b empty note {res.rejection!r}")

        # C3 earnings lapsed (earnings_date on the run's ET date)
        res, tc, sc, oc = _scan(tmp, [_wl_entry("NOK", earnings="2026-01-15")])
        check(res.rejection == "earnings_lapsed" and res.candidates == []
              and dict(res.reject_tally) == {"name:earnings_lapsed": 1},
              f"C3 {res.rejection!r} {dict(res.reject_tally)!r}")
        check(not (tc.requests or sc.requests or oc.requests),
              "C3 data calls made for a lapsed ticker")
        # the day after the run date is not lapsed (ET date of the run clock)
        res, *_ = _scan(tmp, [_wl_entry("NOK", earnings="2026-01-16")])
        check(res.rejection is None, f"C3b {res.rejection!r}")

        # C4 structural watchlist failure raises WatchlistStructureError
        bad = Path(tmp) / "bad.json"
        bad.write_text("{not json")
        raised = None
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cc_scan.scan_calls("NOK", 25.0, FakeTrading([]), FakeStock({}),
                                   FakeOption({}), POLICY, FakeClock(),
                                   watchlist_path=bad)
        except watchlist.WatchlistStructureError as e:
            raised = e
        check(raised is not None, "C4 WatchlistStructureError not raised")

        # C5 clock invalid: naive or missing timestamp raises ClockInvalid
        for label, clk in (("naive", FakeClock(datetime(2026, 1, 15, 15, 0))),
                           ("none", FakeClock(None)), ("no-clock", None)):
            raised = None
            try:
                cc_scan.scan_calls("NOK", 25.0, FakeTrading([]), FakeStock({}),
                                   FakeOption({}), POLICY, clk,
                                   watchlist_path=_write_wl(tmp, [_wl_entry("NOK")]))
            except cc_scan.ClockInvalid as e:
                raised = e
            check(raised is not None, f"C5-{label} ClockInvalid not raised")

        # C6 name-level: no snapshot / null price
        res, tc, *_ = _scan(tmp, [_wl_entry("NOK")], snaps={})
        check(res.rejection == "no_snapshot" and res.candidates == []
              and dict(res.reject_tally) == {"name:no_snapshot": 1}
              and tc.requests == [], f"C6-no_snapshot {res.rejection!r}")
        res, tc, *_ = _scan(tmp, [_wl_entry("NOK")],
                            snaps={"NOK": FakeStockSnap(price=None)})
        check(res.rejection == "null_price" and res.candidates == []
              and dict(res.reject_tally) == {"name:null_price": 1}
              and tc.requests == [], f"C6-null_price {res.rejection!r}")

        # C7 staleness against the run clock: 30 min old passes, 61 fails
        res, *_ = _scan(tmp, [_wl_entry("NOK")], snaps={
            "NOK": FakeStockSnap(ts=RUN_TS - timedelta(minutes=30))})
        check(len(res.candidates) == 3, f"C7 fresh {len(res.candidates)}")
        res, *_ = _scan(tmp, [_wl_entry("NOK")], snaps={
            "NOK": FakeStockSnap(ts=RUN_TS - timedelta(minutes=61))})
        check(res.candidates == [] and
              res.reject_tally["stale_underlying_price"] == 4,
              f"C7 stale {dict(res.reject_tally)!r}")

        # C8 the watchlist earnings date reaches the gate (expiry == earnings)
        res, *_ = _scan(tmp, [_wl_entry("NOK", earnings="2026-02-12")])
        check(res.candidates == [] and
              res.reject_tally["earnings_inside_option"] == 4,
              f"C8 {dict(res.reject_tally)!r}")

        # C9 the basis reaches the gate: a 26.00 basis lifts the floor to 28.60
        res, *_ = _scan(tmp, [_wl_entry("NOK")], basis=26.0)
        order = [r["contract_symbol"] for r in res.candidates]
        check(order == ["NOK260212C00029000", "NOK260212C00030000"],
              f"C9 order {order!r}")


# --------------------------------------------------------------------------- #
# Part L - the dry CLI                                                        #
# --------------------------------------------------------------------------- #

def _snapshot_tree(*dirs):
    out = {}
    for d in dirs:
        d = Path(d)
        out[str(d)] = sorted(str(p.relative_to(d)) for p in d.rglob("*")
                             if ".venv" not in p.parts and ".git" not in p.parts
                             and "__pycache__" not in p.parts) if d.exists() else None
    return out


@contextlib.contextmanager
def _cli_env(tmp, entries=None, clock=None, wl_text=None):
    """Patch credentials, the three client classes, load_policy and the
    watchlist path. Yields a dict collecting what the CLI touched."""
    box = {"creds": 0, "tc": [], "sc": [], "oc": []}
    contracts, chain = _chain_fixture()
    wl = Path(tmp) / "cli_wl.json"
    wl.write_text(wl_text if wl_text is not None else
                  json.dumps({"version": 2,
                              "entries": entries or [_wl_entry("NOK")]}))

    def creds():
        box["creds"] += 1
        return "fake-key", "fake-secret", True

    def tc_factory(*a, **k):
        t = FakeTrading([FakeResp(contracts)], clock or FakeClock())
        box["tc"].append(t)
        return t

    def sc_factory(*a, **k):
        s = FakeStock({"NOK": FakeStockSnap()})
        box["sc"].append(s)
        return s

    def oc_factory(*a, **k):
        o = FakeOption(chain)
        box["oc"].append(o)
        return o

    saved = (cc_scan.credentials.load_credentials, cc_scan.TradingClient,
             cc_scan.StockHistoricalDataClient, cc_scan.OptionHistoricalDataClient,
             cc_scan.screener.load_policy, cc_scan.WATCHLIST_PATH)
    cwd = os.getcwd()
    try:
        cc_scan.credentials.load_credentials = creds
        cc_scan.TradingClient = tc_factory
        cc_scan.StockHistoricalDataClient = sc_factory
        cc_scan.OptionHistoricalDataClient = oc_factory
        cc_scan.screener.load_policy = lambda: copy.deepcopy(POLICY)
        cc_scan.WATCHLIST_PATH = wl
        os.chdir(tmp)
        yield box
    finally:
        os.chdir(cwd)
        (cc_scan.credentials.load_credentials, cc_scan.TradingClient,
         cc_scan.StockHistoricalDataClient, cc_scan.OptionHistoricalDataClient,
         cc_scan.screener.load_policy, cc_scan.WATCHLIST_PATH) = saved


def _run_cli(argv, **env):
    with tempfile.TemporaryDirectory() as tmp:
        out, err = io.StringIO(), io.StringIO()
        before = _snapshot_tree(REPO)
        with _cli_env(tmp, **env) as box:
            tmp_before = _snapshot_tree(tmp)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cc_scan.main(argv)
            tmp_after = _snapshot_tree(tmp)
        after = _snapshot_tree(REPO)
        return {"rc": rc, "out": out.getvalue(), "err": err.getvalue(),
                "box": box, "wrote_repo": before != after,
                "wrote_cwd": tmp_before != tmp_after}


def part_l():
    # L1 ok run: exit 0, ranked candidates + top_rejects, one clock, no writes
    o = _run_cli(["--ticker", "NOK", "--basis", "25.00"])
    check(o["rc"] == 0, f"L1 rc {o['rc']!r} err {o['err']!r}")
    lines = [ln.strip() for ln in o["out"].splitlines()
             if ln.strip().startswith("NOK2602")]
    check([ln.split()[0] for ln in lines] ==
          ["NOK260212C00028000", "NOK260212C00029000", "NOK260212C00030000"],
          f"L1 candidate lines {lines!r}")
    check("top_rejects=[strike_below_basis_floor=1]" in o["out"],
          f"L1 top_rejects missing: {o['out']!r}")
    check("cost_basis=25.0" in o["out"], "L1 basis not printed")
    tcs = o["box"]["tc"]
    check(len(tcs) == 1 and tcs[0].clock_calls == 1,
          f"L1 clock reads {[t.clock_calls for t in tcs]!r}")
    check(not o["wrote_repo"] and not o["wrote_cwd"],
          f"L1 wrote files repo={o['wrote_repo']} cwd={o['wrote_cwd']}")
    check(tcs[0].order_calls == 0, "L1 order call")

    # L2 bad args: exit 3, no credentials read, nothing built
    for label, argv in (
        ("no-basis", ["--ticker", "NOK"]),
        ("no-ticker", ["--basis", "25"]),
        ("basis-text", ["--ticker", "NOK", "--basis", "abc"]),
        ("basis-zero", ["--ticker", "NOK", "--basis", "0"]),
        ("basis-neg", ["--ticker", "NOK", "--basis", "-25"]),
        ("basis-nan", ["--ticker", "NOK", "--basis", "nan"]),
        ("basis-inf", ["--ticker", "NOK", "--basis", "Infinity"]),
        ("ticker-lower", ["--ticker", "nok", "--basis", "25"]),
        ("ticker-long", ["--ticker", "ABCDEF", "--basis", "25"]),
        ("unknown-flag", ["--ticker", "NOK", "--basis", "25", "--submit"]),
    ):
        o = _run_cli(argv)
        check(o["rc"] == 3 and "BAD ARGS" in o["err"],
              f"L2-{label} rc {o['rc']!r} err {o['err']!r}")
        check(o["box"]["creds"] == 0 and not o["box"]["tc"],
              f"L2-{label} credentials read or client built")

    # L3 clock invalid: exit 3 after the one clock read, no data calls
    for label, clk in (("naive", FakeClock(datetime(2026, 1, 15, 15, 0))),
                       ("none", FakeClock(None))):
        o = _run_cli(["--ticker", "NOK", "--basis", "25"], clock=clk)
        tcs = o["box"]["tc"]
        check(o["rc"] == 3 and "CLOCK INVALID" in o["err"],
              f"L3-{label} rc {o['rc']!r} err {o['err']!r}")
        check(len(tcs) == 1 and tcs[0].clock_calls == 1 and not tcs[0].requests
              and not o["box"]["sc"][0].requests,
              f"L3-{label} data calls after an invalid clock")

    # L4 structural watchlist failure: exit 4, nothing written
    o = _run_cli(["--ticker", "NOK", "--basis", "25"], wl_text="[]")
    check(o["rc"] == 4 and "HALT watchlist_structure" in o["err"],
          f"L4 rc {o['rc']!r} err {o['err']!r}")
    check(not o["wrote_repo"] and not o["wrote_cwd"], "L4 wrote files")

    # L5 not on the watchlist / lapsed: exit 0, named rejection printed
    o = _run_cli(["--ticker", "BAC", "--basis", "25"])
    check(o["rc"] == 0 and "REJECTED BAC: not_on_watchlist" in o["out"]
          and "CANDIDATES (none)" in o["out"],
          f"L5-not_on_watchlist {o['rc']!r} {o['out']!r}")
    o = _run_cli(["--ticker", "NOK", "--basis", "25"],
                 entries=[_wl_entry("NOK", earnings="2026-01-10")])
    check(o["rc"] == 0 and "REJECTED NOK: earnings_lapsed" in o["out"],
          f"L5-earnings_lapsed {o['rc']!r} {o['out']!r}")

    # L6 isolation: importing the CLI never imports submit; no write path
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; import forgetrader.cc_scan; "
         "print(sorted(m for m in sys.modules if m in "
         "('forgetrader.submit', 'forgetrader_state', 'forgetrader.run')))"],
        cwd=str(REPO), capture_output=True, text=True)
    check(proc.returncode == 0 and proc.stdout.strip() == "[]",
          f"L6 imports {proc.stdout!r} {proc.stderr[-300:]!r}")
    src = (REPO / "forgetrader" / "cc_scan.py").read_text()
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    banned = {"forgetrader.submit", "submit", "place_csp", "submit_order",
              "write_state", "journal_connect", "record_run", "record_decision",
              "record_event", "record_order", "dump_halt", "open", "write_text",
              "mkdir", "LimitOrderRequest"}
    check(not (names & banned), f"L6 banned names in cc_scan: {sorted(names & banned)}")


def main():
    part_s()
    part_g()
    part_c()
    part_l()
    total = PASSED + len(FAILURES)
    if FAILURES:
        for f in FAILURES:
            print("  " + f)
        print(f"FAIL: {len(FAILURES)} failed of {total} assertions")
        return 1
    print(f"PASS: test_cc_gates state + gates + scan + cli, {total} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
