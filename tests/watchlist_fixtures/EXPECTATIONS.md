# Fixture expectations

All fixtures are evaluated against the real system now_et (America/New_York),
which is 2026-08-24 at the time these fixtures were written. Dates below are
chosen relative to that fixed point so results are stable.

- `fixture_clean.json` - every field valid, earnings_date and review_by both
  in the future. Expect: APPROVED AAPL, exit 0.
- `fixture_missing_earnings.json` - earnings_date is null. Expect: REJECTED
  NOK missing_field:earnings_date, exit 1.
- `fixture_bad_ticker.json` - ticker "aapl" is lowercase, fails the
  `^[A-Z]{1,5}$` pattern. Expect: REJECTED aapl bad_ticker_format, exit 1.
- `fixture_bad_date.json` - `added` is "08-24-2026" (wrong shape, not
  YYYY-MM-DD). Expect: REJECTED MSFT bad_date_format:added, exit 1.
- `fixture_earnings_past.json` - earnings_date "2026-08-01" is before
  now_et.date() (2026-08-24). Expect: REJECTED TSLA earnings_date_in_past,
  exit 1.
- `fixture_empty_note.json` - own_100_note is whitespace-only. Expect:
  REJECTED AMD empty_own_100_note, exit 1.
- `fixture_duplicate.json` - two otherwise-valid entries both ticker MSFT.
  Expect: REJECTED MSFT duplicate_ticker (twice, once per entry), exit 1.
- `fixture_review_overdue.json` - review_by "2026-06-01" is before now_et
  (2026-08-24), everything else valid. Expect: APPROVED NVDA (WARNING:
  review_overdue), exit 0 -- this is advisory only, the entry still trades.
