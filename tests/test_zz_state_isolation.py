"""The suite must leave the repository's own ``state/`` untouched (LESSONS.md L25).

This module is named ``test_zz_…`` on purpose: unittest runs discovered modules
in sorted order, so it executes **after** every other test file and therefore
observes the whole suite, not just its own writes.

Background — the defect this guards::

    python -m unittest discover -s tests          # -t . omitted (2 failures)
    python -m unittest discover -s tests -t .     # the documented form

Without ``-t .`` unittest treats ``tests/`` as the top-level directory and
imports the modules as top-level ``test_*`` modules, so the ``tests`` package
(and with it the redirect in ``tests/__init__.py``) was never imported.  The
router-outage tests then appended their simulated failures to the real,
append-only ``state/errors.jsonl``: 840 rows in one 2026-09-12 run, which is
also why ``agent.cli errors`` reported fixture noise as production evidence.

``tests/_isolation.py`` now performs the redirect, and every test module imports
it first.  The checks below assert the redirect is in place and that the real
state tree — ledger included — is byte-identical to how the suite found it.
"""

from __future__ import annotations

# The suite must never write the repository's own ``state/`` (LESSONS.md L25):
# imported before any ``agent`` module so ``PYQ_STATE_DIR``/``PYQ_ERRORS_LEDGER``
# are set first, and importable in both discovery modes (``tests.test_x`` with
# ``-t .``, the top-level ``test_x`` without).
try:  # pragma: no cover - the import name depends on the discovery mode
    from tests import _isolation  # noqa: F401
except ImportError:  # pragma: no cover
    import _isolation  # type: ignore[no-redef]  # noqa: F401

import hashlib
import os
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from agent import config, errors, llm, paths
from agent import router as router_mod

MODEL = "deepseek-v4-flash"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


class StateIsolationTests(unittest.TestCase):
    """Proof that a suite run cannot write the repository's state tree."""

    def test_the_state_dir_is_redirected_out_of_the_repository(self) -> None:
        """``PYQ_STATE_DIR`` must point at a temp tree, in either discovery mode."""

        override = os.environ.get("PYQ_STATE_DIR", "")
        self.assertTrue(override, "tests/_isolation.py must set PYQ_STATE_DIR")
        state = Path(override)
        self.assertNotEqual(state, _isolation.REAL_STATE_DIR)
        self.assertFalse(
            state.is_relative_to(_isolation.REPO_ROOT),
            f"the suite's state dir {state} is inside the repository",
        )
        self.assertEqual(paths.STATE_DIR, state)
        self.assertTrue(state.is_dir())

    def test_every_generated_artefact_lands_in_the_temp_state_dir(self) -> None:
        """Not just the ledger: the redirect covers progress/journal/manifest too."""

        for path in (
            paths.PROGRESS_JSON,
            paths.MANIFEST_JSON,
            paths.JOURNAL_JSONL,
            paths.CHECKPOINT_JSON,
            paths.TAXONOMY_JSON,
            paths.DISTRIBUTION_JSON,
            paths.INDEX_DIR,
            paths.ERRORS_JSONL,
        ):
            self.assertTrue(
                Path(path).is_relative_to(paths.STATE_DIR),
                f"{path} escaped the suite's state dir",
            )
            self.assertFalse(
                Path(path).is_relative_to(_isolation.REAL_STATE_DIR),
                f"{path} still points at the repository's state/",
            )

    def test_the_real_errors_ledger_is_byte_identical(self) -> None:
        """The acceptance check: ``state/errors.jsonl`` unchanged by the suite."""

        real = _isolation.REAL_ERRORS_LEDGER
        self.assertEqual(
            _isolation.BASELINE_LEDGER_SHA256,
            _sha256(real),
            "the suite appended to the real state/errors.jsonl",
        )

    def test_the_real_state_tree_is_unchanged(self) -> None:
        """Nothing else under ``state/`` was written either (size/mtime/sha256)."""

        baseline = _isolation.BASELINE
        current = _isolation.fingerprint(_isolation.REAL_STATE_DIR)
        added = sorted(set(current) - set(baseline))
        removed = sorted(set(baseline) - set(current))
        changed = sorted(
            name for name in set(baseline) & set(current) if baseline[name] != current[name]
        )
        self.assertEqual(
            (added, removed, changed),
            ([], [], []),
            "the suite modified the repository's state/ tree "
            f"(added={added[:5]} removed={removed[:5]} changed={changed[:5]})",
        )

    def test_a_simulated_router_outage_writes_only_the_temp_ledger(self) -> None:
        """The defect itself: an all-routes-down test must not touch production.

        This is the shape of the failover tests that polluted the ledger — a
        router whose every route fails, exercised with the real settings.
        """

        base = config.load_settings()
        order = tuple(name for name in ("agentrouter", "jw-worker") if name in {p.name for p in base.providers})
        settings = replace(
            base,
            provider_order=order,
            providers=tuple(p for name in order for p in base.providers if p.name == name),
            debate=replace(
                base.debate,
                proposer_model=MODEL,
                critic_model=MODEL,
                proposer_models=(MODEL,),
                critic_models=(MODEL,),
                provider_order=order,
                proposer_provider_order=order,
                critic_provider_order=order,
                model_provider_orders={},
            ),
        )

        def always_forbidden(**kwargs: Any) -> llm.ChatResult:
            raise llm.LlmError(
                f"HTTP 403 from {kwargs['base_url']}: browser integrity check", status=403
            )

        keys = {name: [f"key-{name}"] for name in order}
        before = _sha256(_isolation.REAL_ERRORS_LEDGER)
        router_mod.reset_router()
        self.addCleanup(router_mod.reset_router)
        with mock.patch.object(llm, "provider_keys", return_value=keys), mock.patch.object(
            llm, "chat_completion", side_effect=always_forbidden
        ):
            router = router_mod.Router(settings)
            with errors.ai_context(phase="phase1", task="verify", batch_id="fixture", item_ids=["q1"]):
                with self.assertRaises((router_mod.GlobalHalt, llm.LlmError)):
                    router.chat(model=MODEL, messages=[llm.ChatMessage("user", "hi")])

        self.assertEqual(
            before,
            _sha256(_isolation.REAL_ERRORS_LEDGER),
            "a simulated route outage appended to the real ledger",
        )
        # ... while the ledger the router does write is the temporary one
        rows = list(errors.iter_errors())
        self.assertTrue(rows, "the outage must still be recorded — in the temp ledger")
        self.assertTrue(str(errors.ledger_path()).startswith(str(paths.STATE_DIR)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
