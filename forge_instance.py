"""Per-account instance config: which policy file and which alert prefix this
checkout uses. One fixed table, no default.

current() reads FORGE_INSTANCE from the environment after loading the
repo-root .env through the same path credentials.py loads (load_dotenv on
credentials._ENV_PATH); an already-set environment variable wins, as there.
Resolved at call time, never at import.

Exact match only: a missing or empty FORGE_INSTANCE, "A", " a" or any value
not in the table raises InstanceError. The error names the FORGE_INSTANCE
value and no other key.

    "a": policy.toml,   alert prefix "[forgetrader] "
    "b": policy-b.toml, alert prefix "[forgetrader-b] "

Imports NO alpaca-py, so it is import-safe everywhere including
forgetrader/run.py.
"""
import os
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

import credentials

ROOT = Path(__file__).resolve().parent  # policy files live at the repo root
ENV_VAR = "FORGE_INSTANCE"
UNKNOWN_PREFIX = "[forgetrader-?] "  # alert prefix when no instance resolves


class InstanceError(RuntimeError):
    """Raised when FORGE_INSTANCE is missing or not exactly a table key."""


class Instance(NamedTuple):
    name: str
    policy_file: str
    prefix: str

    @property
    def policy_path(self):
        return ROOT / self.policy_file


_TABLE = {
    "a": Instance("a", "policy.toml", "[forgetrader] "),
    "b": Instance("b", "policy-b.toml", "[forgetrader-b] "),
}


def current():
    """The Instance row for FORGE_INSTANCE. Raises InstanceError; no default."""
    load_dotenv(dotenv_path=credentials._ENV_PATH)
    value = os.environ.get(ENV_VAR)
    if value is None:
        raise InstanceError(f"{ENV_VAR} is not set")
    row = _TABLE.get(value)
    if row is None:
        raise InstanceError(f"{ENV_VAR}={value!r} is not one of "
                            f"{', '.join(sorted(_TABLE))}")
    return row
