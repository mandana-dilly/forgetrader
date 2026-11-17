"""notify.py TEXT - one plain-text Telegram message via crash_alert's .env parser and
sender. Run as forge under /usr/bin/python3 -I. Prints an error's CLASS only, never its
text (the URL carries the token). Architect-authored."""
import sys
sys.path.insert(0, "/home/forge/forgetrader/deploy")
import crash_alert as ca

ENV = "/home/forge/forgetrader/.env"


def main(argv):
    if len(argv) != 1:
        print("usage: notify.py TEXT", file=sys.stderr)
        return 2
    try:
        cfg = ca.load_env(ENV)
        ca.send(cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"], argv[0])
    except BaseException as e:
        print(f"notify: NOT SENT {type(e).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
