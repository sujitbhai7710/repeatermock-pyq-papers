"""Test package for the SSC PYQ agent.

Running the suite must never touch the real, append-only AI error ledger: the
router writes a row for every failed route attempt, so a failover test that
simulates 300 outages would otherwise append 300 rows to ``state/errors.jsonl``
and overwrite the record of what actually happened in production.

The redirect itself lives in :mod:`tests._isolation`, which sets
``PYQ_STATE_DIR`` + ``PYQ_ERRORS_LEDGER`` to a temporary directory.  Importing
this package triggers it, but **that is not enough**::

    python -m unittest discover -s tests        # -t . omitted

makes ``tests/`` the top-level directory, so unittest imports the test modules
as top-level modules and never imports this package at all.  Every test module
therefore imports ``tests._isolation`` explicitly as its first import, and
``tests/test_zz_state_isolation.py`` (which sorts last) asserts that the real
``state/`` was left untouched.
"""

from __future__ import annotations

from . import _isolation  # noqa: F401  (importing it performs the redirect)

__all__ = ["_isolation"]
