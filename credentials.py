"""Single source of truth for Alpaca credential resolution.
Imports NO alpaca-py, so it is import-safe everywhere including forgetrader/run.py."""
import os
from pathlib import Path
from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"  # anchors to repo root, caller-location-independent


class CredentialsError(RuntimeError):
    """Raised when required Alpaca env vars are absent."""


def load_credentials() -> tuple[str, str, bool]:
    """Return (api_key, api_secret, paper). Raises CredentialsError if key/secret missing.
    Canonical var names ONLY: ALPACA_API_KEY / ALPACA_API_SECRET / ALPACA_PAPER."""
    load_dotenv(dotenv_path=_ENV_PATH)
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    if not api_key or not api_secret:
        raise CredentialsError("ALPACA_API_KEY or ALPACA_API_SECRET missing from .env")
    return api_key, api_secret, paper
