"""Outbound Telegram alerts (Brief 4). Outbound only: sendMessage is the one
Bot API method called; nothing is read back, no polling, no webhook.

send(text) posts "[forgetrader] " + text as plain text (no parse_mode) to
TELEGRAM_CHAT_ID through the bot TELEGRAM_BOT_TOKEN. Both are read from the
repo-root .env through the same path credentials.py loads (load_dotenv on
credentials._ENV_PATH); an already-set environment variable wins, as there.

An alert never changes a run: send() never raises. A missing token or chat
id, a transport exception or a non-200 reply each print ONE stderr WARNING
line and return False. The WARNING names the exception TYPE only, never its
message and never the URL: the URL carries the token, and urllib error text
can echo it. The token is placed in the URL only; it is never printed,
journalled or put in the request body.

Stdlib transport (urllib) only; no new dependency. transport, when given, is
called as transport(url, body_bytes, timeout_s) and returns the HTTP status;
tests pass a fake so the network is never touched.
"""
import os
import sys
import urllib.parse
import urllib.request

from dotenv import load_dotenv

import credentials

ENV_PATH = credentials._ENV_PATH
API_BASE = "https://api.telegram.org"
PREFIX = "[forgetrader] "
TIMEOUT_S = 10


def _config():
    """(token, chat_id) from .env / the environment; either may be None."""
    load_dotenv(dotenv_path=ENV_PATH)
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def _urllib_transport(url, body, timeout):
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def send(text, transport=None):
    """Post one alert. True if Telegram answered 200, else False. Never raises."""
    try:
        token, chat_id = _config()
        if not token or not chat_id:
            print("WARNING: alert skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID "
                  "not set", file=sys.stderr)
            return False
        body = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": PREFIX + str(text)}).encode("utf-8")
        status = (transport or _urllib_transport)(
            f"{API_BASE}/bot{token}/sendMessage", body, TIMEOUT_S)
        if status != 200:
            print("WARNING: alert not sent: non-200 reply", file=sys.stderr)
            return False
        return True
    except Exception as e:  # an alert must never change the run
        print(f"WARNING: alert not sent: {type(e).__name__}", file=sys.stderr)
        return False
