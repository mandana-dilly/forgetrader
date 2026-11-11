"""Crash alert for a failed forgetrader unit (Brief B7). Run by systemd as the
OnFailure= handler forgetrader-crash-alert@.service, under /usr/bin/python3 -I.

Stdlib only. It never imports the forgetrader package, alerts.py, credentials.py
or python-dotenv and never uses .venv: a broken venv or package is one of the
failures it reports, so it must not share them.

    crash_alert.py ENV_PATH UNIT

UNIT is the failed unit's name (the template instance %i, set from %n by the
OnFailure= line). Result and exit status come from MONITOR_SERVICE_RESULT and
MONITOR_EXIT_STATUS (systemd.exec(5), v251+, passed to OnFailure= handlers);
absent, each is "unknown". The message is

    [forgetrader] CRASH unit=U result=R exit=E
    check: journalctl --user -u U --since today

ENV_PATH is read for TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID ONLY. The parser
follows python-dotenv's single-binding rules (parser.py): blank and # lines
skipped, optional "export ", optional '...' key, whitespace around =,
'...' and "..." values with their escapes, unquoted values cut at " #" and
right-stripped, a key with no = has no value, the last binding wins. Values of
any other key are skipped undecoded and never stored. Divergence: ${VAR}
interpolation is not done (it would read other keys). Unparseable text is
skipped silently: its content is never printed.

Sent as plain text (no parse_mode) to sendMessage, timeout 15 s. Exit 0 sent;
exit 1 on any failure, with one stderr line naming the error CLASS only plus
the message. The URL carries the token, so neither the URL nor exception text
is ever printed.
"""
import codecs
import os
import re
import sys
import urllib.parse
import urllib.request

API_BASE = "https://api.telegram.org"
TIMEOUT_S = 15
WANTED = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

# python-dotenv 1.2.2 parser.py regexes, verbatim.
_multiline_whitespace = re.compile(r"\s*", re.MULTILINE)
_whitespace = re.compile(r"[^\S\r\n]*")
_export = re.compile(r"(?:export[^\S\r\n]+)?")
_single_quoted_key = re.compile(r"'([^']+)'")
_unquoted_key = re.compile(r"([^=\#\s]+)")
_equal_sign = re.compile(r"(=[^\S\r\n]*)")
_single_quoted_value = re.compile(r"'((?:\\'|[^'])*)'")
_double_quoted_value = re.compile(r'"((?:\\"|[^"])*)"')
_unquoted_value = re.compile(r"([^\r\n]*)")
_comment = re.compile(r"(?:[^\S\r\n]*#[^\r\n]*)?")
_end_of_line = re.compile(r"[^\S\r\n]*(?:\r\n|\n|\r|$)")
_rest_of_line = re.compile(r"[^\r\n]*(?:\r|\n|\r\n)?")
_double_quote_escapes = re.compile(r"\\[\\'\"abfnrtv]")
_single_quote_escapes = re.compile(r"\\[\\']")


class EnvError(Exception):
    """A wanted key is missing or empty in the .env file."""


class SendError(Exception):
    """Telegram answered with a status other than 200."""


def build_message(unit, result, exit_status):
    """The alert text. Pure: no I/O, no environment."""
    return (f"[forgetrader] CRASH unit={unit} result={result} exit={exit_status}\n"
            f"check: journalctl --user -u {unit} --since today")


def _decode(regex, s):
    return regex.sub(lambda m: codecs.decode(m.group(0), "unicode-escape"), s)


def parse_env_text(text):
    """{TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID} from .env text, per python-dotenv's
    binding rules. Raises EnvError naming the key (never a value) if either is
    missing or empty. No other key's value is decoded or kept."""
    found = {}
    pos = 0

    def take(regex):
        nonlocal pos
        m = regex.match(text, pos)
        if m is None:
            raise ValueError
        pos = m.end()
        return m.groups()

    while pos < len(text):
        try:
            take(_multiline_whitespace)
            if pos >= len(text):
                break
            take(_export)
            c = text[pos:pos + 1]
            key = None
            if c != "#":
                (key,) = take(_single_quoted_key if c == "'" else _unquoted_key)
            take(_whitespace)
            value = None
            if text[pos:pos + 1] == "=":
                take(_equal_sign)
                c = text[pos:pos + 1]
                if c == "'":
                    (raw,) = take(_single_quoted_value)
                    if key in WANTED:
                        value = _decode(_single_quote_escapes, raw)
                elif c == '"':
                    (raw,) = take(_double_quoted_value)
                    if key in WANTED:
                        value = _decode(_double_quote_escapes, raw)
                elif c in ("", "\n", "\r"):
                    value = ""
                else:
                    (raw,) = take(_unquoted_value)
                    if key in WANTED:
                        value = re.sub(r"\s+#.*", "", raw).rstrip()
            take(_comment)
            take(_end_of_line)
            if key in WANTED:
                found[key] = value
        except ValueError:
            take(_rest_of_line)
    for k in WANTED:
        if not found.get(k):
            raise EnvError(f"{k} missing or empty")
    return {k: found[k] for k in WANTED}


def load_env(path):
    with open(path, encoding="utf-8") as f:
        return parse_env_text(f.read())


def send(token, chat_id, text):
    """POST text to sendMessage. Raises on any failure; never prints."""
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/bot{token}/sendMessage", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        if resp.status != 200:
            raise SendError()


def main(argv=None, environ=None):
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    unit = argv[1] if len(argv) > 1 else "unknown"
    msg = build_message(unit,
                        environ.get("MONITOR_SERVICE_RESULT") or "unknown",
                        environ.get("MONITOR_EXIT_STATUS") or "unknown")
    try:
        if len(argv) != 2:
            raise TypeError()
        cfg = load_env(argv[0])
        send(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"], msg)
    except BaseException as e:  # class name only: exception text can carry the URL
        print(f"crash_alert: NOT SENT {type(e).__name__}\n{msg}", file=sys.stderr)
        return 1
    print(f"crash_alert: sent\n{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
