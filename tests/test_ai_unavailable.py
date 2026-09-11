"""Tests for R4 (an AI outage never fails the run) and R6 (progress reporting).

The behaviour under test:

* ``verify_database`` reports ``ai_unavailable`` — a *success* — when no route
  can serve a model, whether the router halted mid-run or the model had no
  healthy route before the first batch;
* ``python -m agent.cli verify-db`` exits 0 for that status (exit 3/4 stay
  reserved for rate limiting / the work window);
* ``run`` keeps going after an ``ai_unavailable`` phase, still builds the mock
  catalogue, and exits 0 with the status written to ``checkpoint.json``,
  ``manifest.json``, ``state/progress.json`` and ``database/_meta/PROGRESS.md``;
* the progress roll-up carries, per phase, total / done / % / this run /
  remaining / ETA and the per ``(provider, model)`` route health.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from agent import checkpoint, cli, config, llm, paths, tracking, verify
from agent import router as router_mod
from agent.util import Log

QUIET = Log("test", quiet=True)


def make_settings(**policy_overrides: Any) -> config.Settings:
    from dataclasses import replace

    base = config.load_settings()
    if policy_overrides:
        base = replace(base, rate_limit=replace(base.rate_limit, **policy_overrides))
    return base


def records_for(subject: str, count: int) -> List[Dict[str, Any]]:
    return [
        {
            "qid": f"{subject}-{i}",
            "subject": subject,
            "exam": "CGL",
            "year": 2024,
            "ordinal": i,
            "concept": "Grammar",
            "chapter": "Grammar",
            "topic": None,
        }
        for i in range(1, count + 1)
    ]


class VerifyUnavailableTests(unittest.TestCase):
    """``verify_database`` turns a total outage into ``ai_unavailable``."""

    def setUp(self) -> None:
        router_mod.reset_router()
        self.addCleanup(router_mod.reset_router)
        self.enterContext(mock.patch.object(llm, "provider_keys", return_value=None))

    def _run(self, *, chat_effect, records, window=None, batch_size=None):
        with mock.patch.object(llm, "provider_keys", return_value={
            "agentrouter": ["k"],
            "ar-worker": ["t"],
            "jw-worker": ["t"],
            "justwoker": ["k"],
        }), mock.patch.object(llm, "chat_completion", side_effect=chat_effect), mock.patch.object(
            verify.indexer, "read_index", return_value=records
        ), mock.patch.object(verify, "load_state", return_value={"phases": {}}), mock.patch.object(
            verify, "save_state"
        ), mock.patch.object(verify.ckpt, "load_checkpoint", return_value={}), mock.patch.object(
            verify.ckpt, "save_checkpoint"
        ) as save_checkpoint, mock.patch.object(verify.tracking, "record_ai") as record_ai, (
            mock.patch.object(verify.tracking, "record_routes")
        ) as record_routes, mock.patch.object(verify.tracking, "write_progress_md"), mock.patch.object(
            verify.debate_mod, "append_jsonl"
        ):
            result = verify.verify_database(
                make_settings(provider_cooldown_seconds=300),
                phase="phase1",
                batch_size=batch_size,
                window=window,
                log=QUIET,
            )
        return result, save_checkpoint, record_ai, record_routes

    def test_every_route_rate_limited_mid_run_is_ai_unavailable(self) -> None:
        def chat(**kwargs: Any):
            raise llm.LlmRateLimited(f"HTTP 503 from {kwargs['base_url']}: all_keys_exhausted")

        result, save_checkpoint, record_ai, record_routes = self._run(
            chat_effect=chat, records=records_for("ENG", 3)
        )

        self.assertEqual(result.status, verify.STATUS_AI_UNAVAILABLE)
        self.assertNotEqual(result.status, verify.STATUS_RATE_LIMITED)
        self.assertEqual(result.ai_status, verify.STATUS_AI_UNAVAILABLE)
        self.assertTrue(any("python extraction results were kept unchanged" in n for n in result.notes))
        # the outage is check-pointed so the next run resumes with the AI step
        self.assertTrue(save_checkpoint.called)
        self.assertEqual(save_checkpoint.call_args[0][0].status, checkpoint.STATUS_AI_UNAVAILABLE)
        self.assertTrue(record_ai.called)
        self.assertTrue(record_routes.called)

    def test_no_healthy_route_at_startup_is_ai_unavailable(self) -> None:
        called: List[str] = []

        def chat(**kwargs: Any):  # pragma: no cover - must not be reached
            called.append(kwargs["base_url"])
            raise AssertionError("no request may be sent when no route is healthy")

        router = router_mod.get_router(make_settings())
        router.halt("all routes exhausted", model="gpt-5.6-sol")

        result, _save, _ai, _routes = self._run(chat_effect=chat, records=records_for("ENG", 3))

        self.assertEqual(result.status, verify.STATUS_AI_UNAVAILABLE)
        self.assertEqual(called, [])
        self.assertTrue(any("no healthy route" in note for note in result.notes))

    def test_no_keys_is_still_skipped_not_unavailable(self) -> None:
        with mock.patch.object(llm, "provider_keys", return_value={}), mock.patch.object(
            verify.indexer, "read_index", return_value=records_for("ENG", 1)
        ):
            result = verify.verify_database(make_settings(), phase="phase1", log=QUIET)
        self.assertEqual(result.status, verify.STATUS_SKIPPED)

    def test_index_work_is_resumable_and_reports_progress(self) -> None:
        def chat(**kwargs: Any):
            return llm.ChatResult(
                text=json.dumps({"items": [{"qid": "ENG-1", "ok": True}]}),
                model=kwargs.get("model", ""),
                provider="",
                key_ref="...abcd",
                latency_ms=1,
            )

        records = records_for("ENG", 4)
        result, _save, record_ai, _routes = self._run(
            chat_effect=chat,
            records=records,
            batch_size=2,
            window=checkpoint.WorkWindow(max_seconds=600, interval_seconds=0),
        )
        self.assertEqual(result.status, verify.STATUS_OK)
        self.assertEqual(result.counters["items_total"], 4)
        self.assertEqual(result.counters["items_done"], 4)
        self.assertEqual(result.counters["items_this_run"], 4)
        self.assertEqual(result.counters["items_remaining"], 0)
        self.assertEqual(result.counters["batches_verified"], 2)
        # the AI progress block carries what PROGRESS.md renders
        payload = record_ai.call_args[0][1]
        self.assertEqual(payload["items_total"], 4)
        self.assertEqual(payload["items_done"], 4)
        self.assertIn("eta_seconds", payload)


class VerifyExitCodeTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(phase="phase1", limit=0, batch_size=None, report_only=True)

    def test_ai_unavailable_is_a_success(self) -> None:
        result = verify.VerifyResult(verify.STATUS_AI_UNAVAILABLE, counters={}, notes=[])
        with mock.patch.object(verify, "verify_database", return_value=result):
            code = cli.cmd_verify_db(self._args(), make_settings(), config.exams(), QUIET, "run-test")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(code, 0)

    def test_status_mapping(self) -> None:
        self.assertEqual(cli.exit_code_for_status("ai_unavailable"), 0)
        self.assertEqual(cli.exit_code_for_status("ok"), 0)
        self.assertEqual(cli.exit_code_for_status("skipped_no_keys"), 0)
        self.assertEqual(cli.exit_code_for_status("rate_limited"), 3)
        self.assertEqual(cli.exit_code_for_status("time_limit"), 4)
        self.assertEqual(cli.exit_code_for_status("error"), 1)
        self.assertEqual(cli.exit_code_for_status("something-else"), 1)


class RunContinuesOnAiOutageTests(unittest.TestCase):
    """``run`` completes the deterministic pipeline and exits 0 (R4)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        # redirect every generated artefact into a scratch tree
        for name in (
            "STATE_DIR",
            "DATABASE_DIR",
            "REPORT_DIR",
            "META_DB_DIR",
            "MOCKS_DIR",
            "PROGRESS_JSON",
            "CHECKPOINT_JSON",
            "MANIFEST_JSON",
            "JOURNAL_JSONL",
        ):
            self.enterContext(mock.patch.object(paths, name, self.tmp / name.lower()))
        for target in (self.tmp / "state_dir", self.tmp / "database_dir", self.tmp / "mocks_dir"):
            target.mkdir(parents=True, exist_ok=True)

    def test_run_finishes_with_ai_unavailable_and_publishes_the_status(self) -> None:
        phases_run: List[str] = []

        def fake_phase(number: int, settings, exams, log, run_id, window=None, *, no_ai=False) -> int:
            name = f"phase{number}"
            phases_run.append(name)
            status = "ai_unavailable" if name == "phase2" else "ok"
            tracking.record_phase(
                name,
                status,
                {"questions": 1},
                notes=["no healthy route for gpt-5.6-sol at startup"] if status != "ok" else None,
            )
            return cli.EXIT_OK

        args = argparse.Namespace(phase=None, fresh=True, no_ai=False)
        with mock.patch.object(cli, "cmd_phase", side_effect=fake_phase), mock.patch(
            "agent.mockdata.write_mock_catalogue", return_value={"packs": 0}
        ):
            code = cli.cmd_run(args, make_settings(), config.exams(), QUIET)

        self.assertEqual(code, 0)
        self.assertEqual(
            phases_run,
            ["phase0", "phase1", "phase2", "phase3", "phase4", "phase5"],
            "every phase must run (and build its database slice) even after an AI outage",
        )

        progress = json.loads(paths.PROGRESS_JSON.read_text(encoding="utf-8"))
        self.assertEqual(progress["status"], "ai_unavailable")
        self.assertIn("deterministic pipeline", progress["status_note"])

        manifest = json.loads(paths.MANIFEST_JSON.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "ai_unavailable")

        stored = json.loads(paths.CHECKPOINT_JSON.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "ai_unavailable")
        self.assertEqual(stored["cursor"]["next"], None)
        self.assertEqual(stored["window"]["interval_seconds"], make_settings().checkpoint_interval_seconds)

        progress_md = (paths.META_DB_DIR / "PROGRESS.md").read_text(encoding="utf-8")
        self.assertIn("status: **`ai_unavailable`**", progress_md)
        self.assertIn("phase2", progress_md)

    def test_run_still_stops_on_a_hard_error(self) -> None:
        phases_run: List[str] = []

        def fake_phase(number: int, *a, **kw) -> int:
            phases_run.append(f"phase{number}")
            return cli.EXIT_OK if number < 2 else cli.EXIT_ERROR

        args = argparse.Namespace(phase=None, fresh=True, no_ai=False)
        with mock.patch.object(cli, "cmd_phase", side_effect=fake_phase):
            code = cli.cmd_run(args, make_settings(), config.exams(), QUIET)

        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual(phases_run, ["phase0", "phase1", "phase2"])
        stored = json.loads(paths.CHECKPOINT_JSON.read_text(encoding="utf-8"))
        self.assertEqual(stored["status"], "error")


class ProgressRollupTests(unittest.TestCase):
    """PROGRESS.md / the CI summary render the R6 fields."""

    def test_progress_md_shows_work_items_and_route_health(self) -> None:
        progress = {
            "status": "ai_unavailable",
            "status_at": "2026-01-01T00:00:00Z",
            "phases": {
                "phase1": {
                    "status": "ai_unavailable",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "ai": {
                        "status": "ai_unavailable",
                        "items_total": 1000,
                        "items_done": 250,
                        "items_this_run": 250,
                        "items_remaining": 750,
                        "eta_seconds": 900,
                    },
                }
            },
            "routes": {
                "updated_at": "2026-01-01T00:00:00Z",
                "report": {
                    "ar-worker/deepseek-v4-flash": {
                        "provider": "ar-worker",
                        "model": "deepseek-v4-flash",
                        "state": "open",
                        "ok": 3,
                        "fail": 4,
                        "rate_limited": 2,
                        "trips": 1,
                        "cooldown_seconds": 300.0,
                        "retry_in_seconds": 120.0,
                        "last_reason": "HTTP 503 all_keys_exhausted",
                        "configured": True,
                    },
                    "jw-worker/gpt-5.6-sol": {
                        "provider": "jw-worker",
                        "model": "gpt-5.6-sol",
                        "state": "closed",
                        "ok": 7,
                        "fail": 0,
                        "configured": True,
                    },
                },
            },
        }
        checkpoint_data = {
            "run_id": "run-1",
            "phase": "phase2",
            "status": "ai_unavailable",
            "updated_at": "2026-01-01T00:05:00Z",
            "window": {"max_seconds": 19800, "interval_seconds": 900, "elapsed_seconds": 300.0},
            "cursor": {"completed": ["phase0", "phase1"]},
        }
        text = tracking.render_progress_md(progress, {"phases": {}}, checkpoint_data)

        self.assertIn("**`ai_unavailable`**", text)
        self.assertIn("| Phase | Status | Items | Done | % | This run | Remaining | ETA | Updated |", text)
        self.assertIn("| `phase1` | ai_unavailable | 1,000 | 250 | 25.0% | 250 | 750 | 15m |", text)
        self.assertIn("checkpoint every 900s", text)
        self.assertIn("## Provider health (per provider + model", text)
        self.assertIn("| `ar-worker/deepseek-v4-flash` | open | 3 | 4 | 2 | 1 |", text)
        self.assertIn("| `jw-worker/gpt-5.6-sol` | closed | 7 | 0 | 0 | 0 |", text)

    def test_ci_summary_renders_status_and_phases(self) -> None:
        from tools import ci_summary

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        (state / "checkpoint.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "phase": "phase3",
                    "status": "ai_unavailable",
                    "updated_at": "2026-01-01T00:10:00Z",
                    "window": {"max_seconds": 19800, "interval_seconds": 900, "elapsed_seconds": 60.0},
                }
            ),
            encoding="utf-8",
        )
        (state / "progress.json").write_text(
            json.dumps(
                {
                    "status": "ai_unavailable",
                    "phases": {
                        "phase1": {
                            "status": "ok",
                            "ai_status": "ok",
                            "ai": {
                                "items_total": 500,
                                "items_done": 500,
                                "items_this_run": 10,
                                "items_remaining": 0,
                                "eta_seconds": 0,
                            },
                        }
                    },
                    "routes": {
                        "updated_at": "2026-01-01T00:10:00Z",
                        "report": {
                            "ar-worker/deepseek-v4-flash": {
                                "state": "open",
                                "healthy": False,
                                "ok": 1,
                                "fail": 2,
                                "rate_limited": 2,
                                "cooldown_seconds": 300.0,
                                "configured": True,
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        text = ci_summary.render(state)
        self.assertIn("- status: **`ai_unavailable`**", text)
        self.assertIn("checkpoint every 900s", text)
        self.assertIn("| `phase1` | ok | ok | 500 | 500 | 100.0% |", text)
        self.assertIn("| `ar-worker/deepseek-v4-flash` | open | 1 | 2 | 2 | 5m |", text)


if __name__ == "__main__":
    unittest.main()
