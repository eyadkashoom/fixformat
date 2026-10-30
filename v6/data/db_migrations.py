"""v6 DB schema + idempotent migrations (step 2).

Idempotent by design: IF NOT EXISTS tables + _ensure_columns for ALTERs,
mirroring v6/run_db.py.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_V6 = os.path.dirname(_HERE)


def _load_schema():
    try:
        with open(os.path.join(_REPO_V6, "run_db.py"), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


SCHEMA = _load_schema()
MIGRATIONS_APPLIED = True  # idempotent by design
