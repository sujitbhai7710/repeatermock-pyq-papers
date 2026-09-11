"""Tests for the soft-stop semantics (R2) and work-window pacing (R3).

* ``rate_limited`` -> exit 3, ``time_limit`` -> exit 4 (never 1), with the
  checkpoint + progress written before the process exits;
* ``verify_database`` stops cleanly between batches when the window is spent;
* the GitHub workflow treats 0 / 3 / 4 as success.
"""

from __future__ import annotations

import argparse
import unittest
from pathlib import Path
from unittest import mock

from agent import checkpoint, cli, config, llm, paths, verify
from agent.phases import PhaseResult
from agent.util import Log

QUIET = Log("test", quiet=True)


def settings(**policy_overrides):
    from dataclasses import replace

    base = config.load_settings()
    if policy_overrides:
        base = replace(base, rate_limit=replace(base.rate_limit, **policy_overrides))
    return base


def make_module(status: str):
    module = mock.Mock()
    module.run = mock.Mock(return_value=PhaseResult(phase="phase1", status=status, counters={}))
    return module


class PhaseExitCodeTests(unittest.TestCase):
    """cmd_phase maps a phase status to the documented exit code."""

    def _run(self, status: str) -> int:
        with mock.patch.object(cli, "load_classifier", return_value=object()), mock.patch(
            "agent.phases.phase_module", return_value=make_module(status)
        ), mock.patch.object(cli, "tracking"):
            return cli.cmd_phase(1, settings(), config.exams(), QUIET, "run-test")

    def test_ok_is_zero(self) -> None:
        self.assertEqual(self._run("ok"), cli.EXIT_OK)

    def test_rate_limited_is_three(self) -> None:
        self.assertEqual(self._run("rate_limited"), cli.EXIT_RATE_LIMITED)
        self.assertEqual(cli.EXIT_RATE_LIMITED, 3)

    def test_time_limit_is_four(self) -> None:
        self.assertEqual(self._run("time_limit"), cli.EXIT_TIME_LIMIT)
        self.assertEqual(cli.EXIT_TIME_LIMIT, 4)

    def test_error_is_one(self) -> None:
        self.assertEqual(self._run("error"), cli.EXIT_ERROR)


class VerifyExitCodeTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(phase="phase1", limit=0, batch_size=None, report_only=True)

    def _run(self, status: str) -> int:
        result = verify.VerifyResult(status, counters={}, notes=[])
        with mock.patch.object(verify, "verify_database", return_value=result):
            return cli.cmd_verify_db(self._args(), settings(), config.exams(), QUIET, "run-test")

    def test_rate_limited_is_three(self) -> None:
        self.assertEqual(self._run(verify.STATUS_RATE_LIMITED), cli.EXIT_RATE_LIMITED)

    def test_time_limit_is_four(self) -> None:
        self.assertEqual(self._run(verify.STATUS_TIME_LIMIT), cli.EXIT_TIME_LIMIT)

    def test_skipped_no_keys_is_success(self) -> None:
        self.assertEqual(self._run(verify.STATUS_SKIPPED), cli.EXIT_OK)


class VerifyWindowTests(unittest.TestCase):
    def test_window_is_checked_before_every_batch(self) -> None:
        records = [
            {
                "qid": "q1",
                "subject": "ENG",
                "exam": "CGL",
                "year": 2024,
                "ordinal": 1,
                "concept": "Grammar",
                "chapter": "Grammar",
            }
        ]
        window = checkpoint.WorkWindow(max_seconds=0, interval_seconds=1)  # already spent
        with mock.patch.object(llm, "keys_available", return_value=True), mock.patch.object(
            verify.indexer, "read_index", return_value=records
        ), mock.patch.object(verify, "load_state", return_value={"phases": {}}), mock.patch.object(
            verify, "save_state"
        ) as save_state, mock.patch.object(
            verify.ckpt, "load_checkpoint", return_value={}
        ), mock.patch.object(
            verify.ckpt, "save_checkpoint"
        ) as save_checkpoint, mock.patch.object(
            verify.tracking, "record_ai"
        ), mock.patch.object(
            verify.tracking, "record_routes"
        ), mock.patch.object(
            verify.tracking, "write_progress_md"
        ):
            result = verify.verify_database(
                settings(), phase="phase1", window=window, log=QUIET
            )

        self.assertEqual(result.status, verify.STATUS_TIME_LIMIT)
        self.assertEqual(result.counters.get("batches_not_attempted"), 1)
        self.assertTrue(any("work window expired" in note for note in result.notes))
        save_state.assert_called()
        # the soft stop is check-pointed so the next run resumes here
        self.assertEqual(save_checkpoint.call_args[0][0].status, checkpoint.STATUS_TIME_LIMIT)
        self.assertEqual(save_checkpoint.call_args[0][0].cursor["verify"]["items_total"], 1)

    def test_ai_unavailable_is_never_a_failure(self) -> None:
        """A total outage keeps the python extraction and exits 0 (R4)."""

        def chat(**kwargs):
            raise llm.LlmRateLimited("HTTP 503: all_keys_exhausted")

        records = [
            {
                "qid": "q1",
                "subject": "ENG",
                "exam": "CGL",
                "year": 2024,
                "ordinal": 1,
                "concept": "Grammar",
                "chapter": "Grammar",
            }
        ]
        keys = {"agentrouter": ["k"], "ar-worker": ["k"], "jw-worker": ["k"], "justwoker": ["k"]}
        with mock.patch.object(llm, "provider_keys", return_value=keys), mock.patch.object(
            llm, "chat_completion", side_effect=chat
        ), mock.patch.object(verify.indexer, "read_index", return_value=records), mock.patch.object(
            verify, "load_state", return_value={"phases": {}}
        ), mock.patch.object(verify, "save_state"), mock.patch.object(
            verify.ckpt, "load_checkpoint", return_value={}
        ), mock.patch.object(
            verify.ckpt, "save_checkpoint"
        ), mock.patch.object(
            verify.tracking, "record_ai"
        ), mock.patch.object(
            verify.tracking, "record_routes"
        ), mock.patch.object(
            verify.tracking, "write_progress_md"
        ):
            result = verify.verify_database(
                settings(provider_cooldown_seconds=300), phase="phase1", log=QUIET
            )

        self.assertEqual(result.status, verify.STATUS_AI_UNAVAILABLE)
        self.assertEqual(cli.exit_code_for_status(result.status), cli.EXIT_OK)


class RunSoftStopTests(unittest.TestCase):
    """``cmd_run`` propagates the soft exit codes and checkpoints first."""

    def _run(self, *, status_code: int, max_seconds: int):
        args = argparse.Namespace(phase=None, fresh=True, no_ai=True)
        window = checkpoint.WorkWindow(max_seconds=max_seconds, interval_seconds=10)
        with mock.patch.object(cli, "cmd_phase", return_value=status_code) as cmd_phase, mock.patch.object(
            cli.checkpoint, "load_checkpoint", return_value=None
        ), mock.patch.object(cli.checkpoint, "save_checkpoint") as save_checkpoint, mock.patch.object(
            cli.checkpoint, "make_window", return_value=window
        ), mock.patch.object(cli, "tracking"):
            code = cli.cmd_run(args, settings(), config.exams(), QUIET)
        return code, cmd_phase, save_checkpoint

    def test_rate_limited_run_exits_three_and_checkpoints(self) -> None:
        code, cmd_phase, save_checkpoint = self._run(status_code=cli.EXIT_RATE_LIMITED, max_seconds=600)
        self.assertEqual(code, cli.EXIT_RATE_LIMITED)
        self.assertTrue(cmd_phase.called)
        checkpoint_obj = save_checkpoint.call_args[0][0]
        self.assertEqual(checkpoint_obj.status, checkpoint.STATUS_RATE_LIMITED)

    def test_time_limit_run_exits_four_and_checkpoints(self) -> None:
        code, cmd_phase, save_checkpoint = self._run(status_code=cli.EXIT_TIME_LIMIT, max_seconds=600)
        self.assertEqual(code, cli.EXIT_TIME_LIMIT)
        checkpoint_obj = save_checkpoint.call_args[0][0]
        self.assertEqual(checkpoint_obj.status, checkpoint.STATUS_TIME_LIMIT)

    def test_spent_window_stops_before_the_phase_starts(self) -> None:
        code, cmd_phase, save_checkpoint = self._run(status_code=cli.EXIT_OK, max_seconds=0)
        self.assertEqual(code, cli.EXIT_TIME_LIMIT)
        self.assertFalse(cmd_phase.called)
        checkpoint_obj = save_checkpoint.call_args[0][0]
        self.assertEqual(checkpoint_obj.status, checkpoint.STATUS_TIME_LIMIT)
        self.assertEqual(checkpoint_obj.cursor.get("next"), "phase0")


class WorkflowSemanticsTests(unittest.TestCase):
    def test_workflow_treats_3_and_4_as_success(self) -> None:
        text = (Path(paths.PROJECT_ROOT) / ".github" / "workflows" / "pyq-agent.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('3) echo "status=rate_limited"', text)
        self.assertIn('4) echo "status=time_limit"', text)
        self.assertIn('-eq 3', text)
        self.assertIn('-eq 4', text)
        self.assertIn('exit 0', text)


if __name__ == "__main__":
    unittest.main()
