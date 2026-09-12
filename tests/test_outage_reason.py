"""``ai_unavailable`` must say *why* — the machine-readable reason code.

``status=ai_unavailable`` on its own is not actionable: it does not distinguish
"every provider's key pool is exhausted" from "Cloudflare blocked the runner"
from "no candidate model had a route the router was willing to try", and those
three need three different fixes.  ``errors.outage_reason`` derives one code from
the append-only error ledger and ``tracking.record_status`` stores it as
``status_reason`` in ``progress.json`` and ``manifest.json`` (and PROGRESS.md
renders it).

This module also pins ``cli.os_environ_keys``: the preflight must know every
credential variable the shipped routes declare, so a newly added provider cannot
silently look "unconfigured" (the original defect listed six of the eight).
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

import json
import os
import shutil
import pathlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import cli, config, errors, llm, paths, tracking


class ReasonTests(unittest.TestCase):
    """``errors.outage_reason`` — the code and the evidence behind it."""

    def setUp(self) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="pyq-test-")
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        tmp = pathlib.Path(tmp_dir)
        self.ledger = Path(tmp.name) / "errors.jsonl"

    def _record(self, kind: str, **kwargs: object) -> None:
        errors.record(kind=kind, message=str(kwargs.pop("message", "")), path=self.ledger, **kwargs)

    def test_the_codes_are_a_closed_vocabulary(self) -> None:
        self.assertEqual(len(set(errors.REASONS)), len(errors.REASONS))
        for code in errors.REASONS:
            self.assertTrue(errors.REASON_MEANINGS.get(code), f"{code} has no meaning")
            self.assertIn(code, errors.format_reason(code))

    def test_a_waf_block_is_not_reported_as_a_bad_key(self) -> None:
        """403 with an HTML challenge body: infrastructure, not credentials.

        The ledger records the same event as ``auth`` when the status survives
        (403) and as ``http`` when only the HTML body does — both must read as a
        WAF block, never as "your key is wrong".
        """

        self._record(
            errors.KIND_AUTH,
            provider="groq",
            model="openai/gpt-oss-120b",
            http_status=403,
            message="HTTP 403 from https://api.groq.com/openai/v1: browser integrity check",
        )
        self._record(
            errors.KIND_HTTP,
            provider="ar-worker",
            model="openai/gpt-oss-120b",
            message="HTTP 403 from https://ar-rotator.example/v1: <!doctype html> aliyun_waf_aa",
        )
        code, evidence = errors.outage_reason(self.ledger)
        self.assertEqual(code, errors.REASON_ALL_PROVIDERS_403_WAF)
        self.assertEqual(evidence["kinds"], {"auth": 1, "http": 1})
        self.assertEqual(evidence["providers"], ["ar-worker", "groq"])

    def test_a_rejected_key_is_reported_as_auth(self) -> None:
        self._record(
            errors.KIND_AUTH,
            provider="jw-worker",
            http_status=401,
            message="HTTP 401 from https://jw-rotator.example/v1: invalid api key",
        )
        self.assertEqual(errors.outage_reason(self.ledger)[0], errors.REASON_ALL_PROVIDERS_AUTH)

    def test_an_exhausted_pool_is_reported_as_all_keys_exhausted(self) -> None:
        for provider in ("groq", "jw-worker", "ar-worker"):
            self._record(
                errors.KIND_RATE_LIMIT,
                provider=provider,
                http_status=503,
                message=f'HTTP 503 from https://{provider}.example/v1: {{"error":'
                '{"type":"all_keys_exhausted","message":"All upstream keys failed."}}',
            )
        self.assertEqual(errors.outage_reason(self.ledger)[0], errors.REASON_ALL_KEYS_EXHAUSTED)

    def test_a_balance_answer_is_reported_as_rate_limited(self) -> None:
        self._record(
            errors.KIND_RATE_LIMIT,
            provider="groq",
            http_status=402,
            message="HTTP 402 from https://api.groq.com/openai/v1: balance is at $0",
        )
        self.assertEqual(errors.outage_reason(self.ledger)[0], errors.REASON_ALL_PROVIDERS_RATE_LIMITED)

    def test_a_transport_failure_is_reported_as_transport(self) -> None:
        self._record(
            errors.KIND_TRANSPORT,
            provider="groq",
            message="transport error to https://api.groq.com/openai/v1: <urlopen error timed out>",
        )
        self.assertEqual(errors.outage_reason(self.ledger)[0], errors.REASON_ALL_PROVIDERS_TRANSPORT)

    def test_a_skipped_router_is_reported_as_no_route_for_the_model(self) -> None:
        """No route attempt at all — only the router's own verdict."""

        errors.record(
            kind=errors.KIND_RATE_LIMIT,
            provider="groq",
            model="openai/gpt-oss-120b",
            message="no healthy route for openai/gpt-oss-120b after 5 attempt(s) "
            "(tried groq)",
            scope=errors.SCOPE_BATCH,
            path=self.ledger,
        )
        self.assertEqual(errors.outage_reason(self.ledger)[0], errors.REASON_NO_ROUTE_FOR_MODEL)

    def test_a_missing_key_is_reported_as_no_keys_configured(self) -> None:
        code, _evidence = errors.outage_reason(self.ledger, keys_configured=False)
        self.assertEqual(code, errors.REASON_NO_KEYS)

    def test_an_empty_ledger_is_unclassified_and_never_raises(self) -> None:
        code, evidence = errors.outage_reason(self.ledger)
        self.assertEqual(code, errors.REASON_UNKNOWN)
        self.assertEqual(evidence["rows"], 0)

    def test_rows_older_than_the_window_are_ignored(self) -> None:
        """An old outage must not explain today's: only the tail is read."""

        self._record(errors.KIND_AUTH, provider="jw-worker", message="HTTP 403: invalid api key")
        stale = json.loads(self.ledger.read_text(encoding="utf-8").strip())
        stale["ts"] = "2020-01-01T00:00:00Z"
        self.ledger.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        self._record(
            errors.KIND_RATE_LIMIT,
            provider="groq",
            http_status=503,
            message="HTTP 503: all_keys_exhausted",
        )
        code, evidence = errors.outage_reason(self.ledger)
        self.assertEqual(code, errors.REASON_ALL_KEYS_EXHAUSTED)
        self.assertEqual(evidence["rows"], 1, "the stale row must not be part of the verdict")

    def test_the_evidence_never_carries_a_credential(self) -> None:
        self._record(
            errors.KIND_AUTH,
            provider="groq",
            http_status=403,
            message="HTTP 403 from https://api.groq.com/openai/v1 ?api_key=sk-abcdef1234567890",
        )
        _code, evidence = errors.outage_reason(self.ledger)
        blob = json.dumps(evidence)
        self.assertNotIn("sk-abcdef1234567890", blob)
        self.assertIn("<redacted", blob)


class StatusRecordingTests(unittest.TestCase):
    """The code must reach progress.json, manifest.json and PROGRESS.md."""

    def setUp(self) -> None:
        tmp_dir = tempfile.mkdtemp(prefix="pyq-test-")
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        tmp = pathlib.Path(tmp_dir)
        self.root = Path(tmp.name)
        self.progress = self.root / "progress.json"
        self.manifest = self.root / "manifest.json"
        self.ledger = self.root / "errors.jsonl"
        errors.record(
            kind=errors.KIND_HTTP,
            provider="groq",
            http_status=403,
            message="HTTP 403 from https://api.groq.com/openai/v1: browser integrity check",
            path=self.ledger,
        )

    def test_record_status_stores_the_reason_in_both_files(self) -> None:
        reason, evidence = errors.outage_reason(self.ledger)
        with mock.patch.object(paths, "PROGRESS_JSON", self.progress), mock.patch.object(
            paths, "MANIFEST_JSON", self.manifest
        ):
            tracking.record_status(
                "ai_unavailable",
                note="no AI route available",
                reason=reason,
                reason_evidence=evidence,
            )
        progress = json.loads(self.progress.read_text(encoding="utf-8"))
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(progress["status"], "ai_unavailable")
        self.assertEqual(progress["status_reason"], errors.REASON_ALL_PROVIDERS_403_WAF)
        self.assertEqual(manifest["status_reason"], errors.REASON_ALL_PROVIDERS_403_WAF)
        self.assertEqual(progress["status_reason_evidence"]["kinds"], {"http": 1})

    def test_progress_md_renders_the_reason(self) -> None:
        progress = tracking.load_progress(self.progress)
        progress["status"] = "ai_unavailable"
        progress["status_reason"] = errors.REASON_ALL_KEYS_EXHAUSTED
        progress["status_reason_evidence"] = {"rows": 3, "kinds": {"rate_limit": 3}}
        text = tracking.render_progress_md(progress, tracking.load_manifest(self.manifest), None)
        self.assertIn("`all_keys_exhausted", text)
        self.assertIn("every provider answered that its key pool is exhausted", text)
        self.assertIn("kinds={'rate_limit': 3}", text)

    def test_a_run_without_a_reason_has_no_status_reason_field(self) -> None:
        with mock.patch.object(paths, "PROGRESS_JSON", self.progress), mock.patch.object(
            paths, "MANIFEST_JSON", self.manifest
        ):
            tracking.record_status("ok", note="run complete")
        progress = json.loads(self.progress.read_text(encoding="utf-8"))
        self.assertNotIn("status_reason", progress)
        self.assertNotIn("status_reason", json.loads(self.manifest.read_text(encoding="utf-8")))

    def test_cli_derives_the_reason_from_the_ledger(self) -> None:
        """``record_run_status`` wires the code into the two state files."""

        with mock.patch.object(paths, "PROGRESS_JSON", self.progress), mock.patch.object(
            paths, "MANIFEST_JSON", self.manifest
        ), mock.patch.dict(os.environ, {"PYQ_ERRORS_LEDGER": str(self.ledger)}, clear=False), mock.patch.object(
            cli, "os_environ_keys", return_value=["GROQ_API_KEY"]
        ):
            code = cli.record_run_status("ai_unavailable", note="no route", log=None)
        self.assertEqual(code, errors.REASON_ALL_PROVIDERS_403_WAF)
        progress = json.loads(self.progress.read_text(encoding="utf-8"))
        self.assertEqual(progress["status_reason"], errors.REASON_ALL_PROVIDERS_403_WAF)
        self.assertIn(f"[reason: {errors.REASON_ALL_PROVIDERS_403_WAF}]", progress["status_note"])

    def test_record_run_status_ignores_a_healthy_run(self) -> None:
        with mock.patch.object(paths, "PROGRESS_JSON", self.progress), mock.patch.object(
            paths, "MANIFEST_JSON", self.manifest
        ):
            code = cli.record_run_status("ok", note="run complete", log=None)
        self.assertEqual(code, "")
        self.assertNotIn("status_reason", json.loads(self.progress.read_text(encoding="utf-8")))

    def test_a_broken_ledger_never_fails_the_run(self) -> None:
        """Deriving the reason is best-effort: it must never raise out of a run."""

        with mock.patch.object(errors, "iter_errors", side_effect=OSError("disk gone")):
            self.assertEqual(cli.outage_reason(log=None, path=self.ledger), "")


class PreflightKeyTests(unittest.TestCase):
    """``cli.os_environ_keys`` must know every credential variable we support."""

    #: every credential variable the shipped routes + tools declare
    SUPPORTED = (
        "AGENTROUTER_KEYS",
        "OPENAI_KEYS",
        "JUSTWOKER_KEYS",
        "DEEPSEEK_KEYS",
        "AR_PROXY_TOKEN",
        "JW_PROXY_TOKEN",
        "ZEN_PROXY_TOKEN",
        "GROQ_API_KEY",
        "MONID_API_KEY",
    )

    def test_every_supported_variable_is_detected(self) -> None:
        env = {name: "x" for name in self.SUPPORTED}
        with mock.patch.dict(os.environ, env, clear=True):
            found = cli.os_environ_keys()
        self.assertEqual(sorted(found), sorted(self.SUPPORTED))

    def test_the_shipped_routes_are_all_covered(self) -> None:
        """Derived from ``providers.routes`` — a new route cannot be forgotten."""

        settings = config.load_settings()
        declared = set(llm.keys_env_names(llm.resolve_providers(settings)))
        self.assertIn("GROQ_API_KEY", declared, "the groq route declares GROQ_API_KEY")
        for name in declared:
            with mock.patch.dict(os.environ, {name: "x"}, clear=True):
                self.assertEqual(cli.os_environ_keys(settings), [name])

    def test_an_empty_environment_reports_nothing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.os_environ_keys(), [])

    def test_blank_values_do_not_count_as_configured(self) -> None:
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "   "}, clear=True):
            self.assertEqual(cli.os_environ_keys(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
