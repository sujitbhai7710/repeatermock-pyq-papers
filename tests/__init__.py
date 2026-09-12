"""Test package for the SSC PYQ agent.

Running the suite must never touch the real, append-only AI error ledger: the
router writes a row for every failed route attempt, so a failover test that
simulates 300 outages would otherwise append 300 rows to ``state/errors.jsonl``
and overwrite the record of what actually happened in production.  Importing this
package therefore redirects the ledger to a temporary file (the tests that assert
on the ledger's *shape* override this per test).  ``PYQ_ERRORS_LEDGER`` set by the
caller always wins.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

if not os.environ.get("PYQ_ERRORS_LEDGER"):
    _LEDGER_DIR = Path(tempfile.mkdtemp(prefix="pyq-test-ledger-"))
    os.environ["PYQ_ERRORS_LEDGER"] = str(_LEDGER_DIR / "state" / "errors.jsonl")
