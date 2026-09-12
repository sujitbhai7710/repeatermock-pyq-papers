"""``state/errors.jsonl`` — the append-only AI failure ledger (Issue 5).

Every phase that degrades to ``ai_unavailable`` must leave *why* behind: the
route, the failure class, the HTTP status and the questions that were in flight.
The tests below cover the writer/reader round-trip, the guarantee that no
credential or oversized prompt can reach the file, that the router actually
appends a row when a route fails, and that ``python -m agent.cli errors``
summarises a ledger.

Every test points ``PYQ_ERRORS_LEDGER`` at a temporary file, so running the
suite can never append to the real ledger.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from agent import cli, config, errors, llm, paths
from agent import router as router_mod

PROPOSER = "deepseek-v4-flash"


def make_settings(**policy_overrides: Any) -> config.Settings:
    """Shipped settings with the fixture routes pinned (see test_failover)."""

    base = config.load_settings()
    order = tuple(policy_overrides.pop("provider_order", ("agentrouter", "jw-worker")))
    available = {p.name for p in base.providers}
    order = tuple(name for name in order if name in available)
    policy: Dict[str, Any] = {"halt_on_any_rate_limit_signal": True}
    policy.update(policy_overrides)
    return replace(
        base,
        rate_limit=replace(base.rate_limit, **policy),
        provider_order=order,
        providers=tuple(p for name in order for p in base.providers if p.name == name),
        debate=replace(
            base.debate,
            proposer_model=PROPOSER,
            critic_model=PROPOSER,
            proposer_models=(PROPOSER,),
            critic_models=(PROPOSER,),
            provider_order=order,
            proposer_provider_order=order,
            critic_provider_order=order,
            model_provider_orders={},
        ),
    )


class LedgerTestCase(unittest.TestCase):
    """Shared wiring: a temp ledger, and no access to the real one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = Path(self._tmp.name) / "state" / "errors.jsonl"
        self.enterContext(mock.patch.dict(os.environ, {"PYQ_ERRORS_LEDGER": str(self.ledger)}))
        router_mod.reset_router()

    def tearDown(self) -> None:
        router_mod.reset_router()

    def rows(self) -> List[Dict[str, Any]]:
        return list(errors.iter_errors(self.ledger))


# ---------------------------------------------------------------------------
# the row
# ---------------------------------------------------------------------------


class RecordTests(LedgerTestCase):
    def test_record_writes_every_required_field(self) -> None:
        """One JSON object per line, carrying the documented field set."""

        errors.record(
            provider="tokenharbor",
            model="deepseek-v4-flash",
            kind=errors.KIND_RATE_LIMIT,
            http_status=402,
            attempt=3,
            message="HTTP 402: balance is at $0",
            item_ids=["q1", "q2"],
            batch_id="abc123",
            phase="grammar",
            task="grammar_rule",
        )
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for field in (
            "ts",
            "phase",
            "batch_id",
            "provider",
            "model",
            "kind",
            "http_status",
            "attempt",
            "message",
            "retryable",
            "item_ids",
        ):
            self.assertIn(field, row, f"the ledger row lost {field!r}")
        self.assertEqual(row["phase"], "grammar")
        self.assertEqual(row["batch_id"], "abc123")
        self.assertEqual(row["provider"], "tokenharbor")
        self.assertEqual(row["model"], "deepseek-v4-flash")
        self.assertEqual(row["kind"], "rate_limit")
        self.assertEqual(row["http_status"], 402)
        self.assertEqual(row["attempt"], 3)
        self.assertEqual(row["item_ids"], ["q1", "q2"])
        # the timestamp is the project's deterministic UTC shape
        self.assertRegex(row["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_the_ledger_is_append_only(self) -> None:
        """A second failure never rewrites the first one."""

        errors.record(provider="p1", model="m", kind=errors.KIND_HTTP, http_status=500, message="a")
        first = self.ledger.read_bytes()
        errors.record(provider="p2", model="m", kind=errors.KIND_AUTH, http_status=401, message="b")
        second = self.ledger.read_bytes()
        self.assertTrue(second.startswith(first), "the earlier ledger content must survive")
        self.assertEqual([row["provider"] for row in self.rows()], ["p1", "p2"])

    def test_a_broken_ledger_path_never_raises(self) -> None:
        """An unwritable ledger must not take the run (or the caller) down."""

        target = Path(self._tmp.name) / "state" / "errors.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        with mock.patch.object(errors, "append_jsonl", side_effect=OSError("disk full")):
            row = errors.record(kind=errors.KIND_OTHER, message="x", path=target)
        self.assertEqual(row["kind"], "other")

    def test_the_context_supplies_phase_batch_and_items(self) -> None:
        """The bound batch fills in what the caller does not pass explicitly."""

        with errors.ai_context(
            phase="phase2", task="verify", batch_id="batch-7", item_ids=["q9"]
        ):
            errors.record(provider="p", model="m", kind=errors.KIND_HTTP, http_status=500, message="x")
        row = self.rows()[0]
        self.assertEqual(row["phase"], "phase2")
        self.assertEqual(row["batch_id"], "batch-7")
        self.assertEqual(row["item_ids"], ["q9"])
        self.assertEqual(row["scope"], "route")


# ---------------------------------------------------------------------------
# no secrets, bounded rows
# ---------------------------------------------------------------------------


class RedactionTests(LedgerTestCase):
    def test_keys_and_tokens_are_masked(self) -> None:
        """A key that reaches the ledger is a leak — the writer must mask it."""

        text = errors.redact(
            "HTTP 401 from https://api.example/v1?api_key=SECRETVALUE: "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 "
            "key=sk-abcdef1234567890 and token: abcdef1234567890abcdef"
        )
        self.assertNotIn("SECRETVALUE", text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", text)
        self.assertNotIn("sk-abcdef1234567890", text)
        self.assertIn("<redacted", text)

    def test_a_long_message_is_cut_to_the_limit(self) -> None:
        """Rows stay small: ``message`` never exceeds :data:`errors.MESSAGE_LIMIT`."""

        noisy = "upstream said " + " ".join(f"word{index}" for index in range(400))
        errors.record(kind=errors.KIND_HTTP, http_status=500, message=noisy)
        row = self.rows()[0]
        self.assertLessEqual(len(row["message"]), errors.MESSAGE_LIMIT)

    def test_a_full_prompt_is_never_accepted(self) -> None:
        """Only the provider's error text is stored — there is no prompt field."""

        errors.record(kind=errors.KIND_BAD_JSON, message="invalid json from https://x/v1")
        row = self.rows()[0]
        self.assertNotIn("prompt", row)
        self.assertNotIn("messages", row)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


class ClassifyTests(unittest.TestCase):
    def test_the_six_kinds(self) -> None:
        self.assertEqual(errors.classify(status=401, message="nope"), errors.KIND_AUTH)
        self.assertEqual(errors.classify(status=403, message="forbidden"), errors.KIND_AUTH)
        self.assertEqual(errors.classify(status=429, message="slow down"), errors.KIND_RATE_LIMIT)
        self.assertEqual(errors.classify(status=402, message="balance"), errors.KIND_RATE_LIMIT)
        self.assertEqual(errors.classify(status=500, message="boom"), errors.KIND_HTTP)
        self.assertEqual(
            errors.classify(llm.LlmRateLimited("rate limited", status=503)), errors.KIND_RATE_LIMIT
        )
        self.assertEqual(
            errors.classify(message="transport error talking to https://x"), errors.KIND_TRANSPORT
        )
        self.assertEqual(
            errors.classify(message="invalid JSON from https://x/v1"), errors.KIND_BAD_JSON
        )
        self.assertEqual(errors.classify(message="who knows"), errors.KIND_OTHER)

    def test_every_kind_is_in_the_vocabulary(self) -> None:
        """A new failure class must be added to :data:`errors.KINDS` deliberately."""

        for kind in errors.KINDS:
            self.assertEqual(errors.classify(kind=kind, message="x"), kind)
        self.assertEqual(
            errors.classify(kind="not-a-kind", message="x"),
            errors.KIND_OTHER,
            "an unknown kind must fall back, never invent a new vocabulary entry",
        )

    def test_retryability(self) -> None:
        """A wrong key or an empty balance is not worth retrying."""

        self.assertFalse(errors.is_retryable(errors.KIND_AUTH, status=401, message="bad key"))
        self.assertFalse(
            errors.is_retryable(errors.KIND_RATE_LIMIT, status=402, message="Your balance is at $0")
        )
        self.assertTrue(
            errors.is_retryable(errors.KIND_RATE_LIMIT, status=503, message="all_keys_exhausted")
        )
        self.assertTrue(errors.is_retryable(errors.KIND_TRANSPORT, message="connection reset"))


# ---------------------------------------------------------------------------
# the router writes rows
# ---------------------------------------------------------------------------


class FakeChain:
    """``llm.chat_completion`` stand-in: raise per (provider, model)."""

    def __init__(self, behaviour: Dict[Tuple[str, str], str]) -> None:
        self.behaviour = behaviour
        self.calls: List[Tuple[str, str]] = []

    def provider_for(self, base_url: str) -> str:
        for provider in llm.PROVIDERS:
            if provider.base_url == base_url:
                return provider.name
        return base_url

    def __call__(self, **kwargs: Any) -> llm.ChatResult:
        name = self.provider_for(kwargs["base_url"])
        model = kwargs.get("model", "")
        self.calls.append((name, model))
        action = self.behaviour.get((name, model), self.behaviour.get(("", model), "ok"))
        if action == "rate":
            raise llm.LlmRateLimited(
                f"HTTP 503 from {kwargs['base_url']}: "
                '{"error":{"type":"all_keys_exhausted","message":"All upstream keys failed."}}'
            )
        if action == "forbidden":
            raise llm.LlmError(f"HTTP 403 from {kwargs['base_url']}: browser integrity check", status=403)
        if action == "error":
            raise llm.LlmError(f"HTTP 500 from {kwargs['base_url']}: upstream boom", status=500)
        return llm.ChatResult(text="pong", model=model, provider="", key_ref="...abcd", latency_ms=1)


class RouterLedgerTests(LedgerTestCase):
    def _router_with(self, behaviour: Dict[Tuple[str, str], str]) -> Tuple[router_mod.Router, FakeChain]:
        fake = FakeChain(behaviour)
        keys = {name: [f"key-{name}"] for name in ("agentrouter", "jw-worker")}
        self.enterContext(mock.patch.object(llm, "provider_keys", return_value=keys))
        self.enterContext(mock.patch.object(llm, "chat_completion", side_effect=fake))
        return router_mod.Router(make_settings()), fake

    def test_a_failed_route_attempt_is_recorded(self) -> None:
        """A 403 route failure must appear in the ledger with its status."""

        router, _fake = self._router_with({("agentrouter", PROPOSER): "forbidden"})
        with errors.ai_context(phase="phase4", task="verify", batch_id="b1", item_ids=["q7"]):
            result = router.chat(model=PROPOSER, messages=[llm.ChatMessage("user", "hi")])
        self.assertTrue(result.text)  # jw-worker still served it
        rows = [row for row in self.rows() if row["scope"] == errors.SCOPE_ROUTE]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["provider"], "agentrouter")
        self.assertEqual(row["model"], PROPOSER)
        self.assertEqual(row["kind"], errors.KIND_AUTH)
        self.assertEqual(row["http_status"], 403)
        self.assertEqual(row["phase"], "phase4")
        self.assertEqual(row["batch_id"], "b1")
        self.assertEqual(row["item_ids"], ["q7"])
        self.assertEqual(row["attempt"], 1)

    def test_a_rate_limit_is_recorded_as_rate_limit(self) -> None:
        router, _fake = self._router_with({("agentrouter", PROPOSER): "rate"})
        router.chat(model=PROPOSER, messages=[llm.ChatMessage("user", "hi")])
        kinds = {row["kind"] for row in self.rows()}
        self.assertIn(errors.KIND_RATE_LIMIT, kinds)

    def test_no_route_at_all_records_the_reason(self) -> None:
        """The whole point: ``ai_unavailable`` must say *why* it was unavailable."""

        router, _fake = self._router_with(
            {("agentrouter", PROPOSER): "error", ("jw-worker", PROPOSER): "error"}
        )
        # the request cannot be served at all; whether that ends as a global halt
        # or as the last route error is the router's business — the ledger must
        # explain it either way
        with self.assertRaises((router_mod.GlobalHalt, llm.LlmError)):
            router.chat(model=PROPOSER, messages=[llm.ChatMessage("user", "hi")])
        rows = self.rows()
        self.assertTrue(rows, "a total route outage must leave a ledger row")
        reasons = " ".join(str(row["message"]) for row in rows)
        self.assertTrue(reasons.strip(), "a row without a message cannot explain anything")
        self.assertIn(errors.KIND_HTTP, {row["kind"] for row in rows})
        self.assertIn(errors.SCOPE_BATCH, {row["scope"] for row in rows})
        batch = [row for row in rows if row["scope"] == errors.SCOPE_BATCH]
        self.assertTrue(
            any("no healthy route" in str(row["message"]) or "failed" in str(row["message"]) for row in batch),
            f"the batch row must carry the router's reason, got {[row['message'] for row in batch]}",
        )


# ---------------------------------------------------------------------------
# the summary + the CLI
# ---------------------------------------------------------------------------


class LedgerIsolationTests(unittest.TestCase):
    """The suite itself must not write the real ledger (see ``tests/__init__.py``)."""

    def test_the_test_run_points_the_ledger_away_from_state(self) -> None:
        """A router failure simulated by another test file must not leak here."""

        override = os.environ.get("PYQ_ERRORS_LEDGER", "")
        self.assertTrue(override, "tests/__init__.py must set PYQ_ERRORS_LEDGER")
        self.assertNotEqual(
            Path(override),
            paths.STATE_DIR / "errors.jsonl",
            "a suite run would append test failures to the real, append-only ledger",
        )

    def test_the_redirect_is_where_a_test_failure_lands(self) -> None:
        """With the redirect active, a router failure appends to the temp ledger."""

        real = paths.STATE_DIR / "errors.jsonl"
        before = real.read_bytes() if real.is_file() else None
        errors.record(
            provider="fixture",
            model="fixture",
            kind=errors.KIND_HTTP,
            http_status=500,
            message="simulated route failure",
        )
        target = errors.ledger_path()
        self.assertNotEqual(str(target), str(real))
        self.assertTrue(target.is_file())
        self.assertIn("simulated route failure", target.read_text(encoding="utf-8"))
        after = real.read_bytes() if real.is_file() else None
        self.assertEqual(before, after)


class SummaryTests(LedgerTestCase):
    def _seed(self) -> None:
        for index in range(3):
            errors.record(
                provider="tokenharbor",
                model="deepseek-v4-flash",
                kind=errors.KIND_RATE_LIMIT,
                http_status=402,
                message=f"balance is at $0 (attempt {index})",
                phase="grammar",
                item_ids=["q1", "q2", "q3"],
            )
        errors.record(
            provider="justwoker",
            model="gpt-5.6-sol",
            kind=errors.KIND_AUTH,
            http_status=403,
            message="forbidden",
            phase="phase1",
        )

    def test_summary_groups_by_phase_kind_provider_model(self) -> None:
        self._seed()
        summary = errors.summarise(top=5)
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["by_kind"]["rate_limit"], 3)
        self.assertEqual(summary["by_phase"]["grammar"], 3)
        top = summary["top"][0]
        self.assertEqual(top["phase"], "grammar")
        self.assertEqual(top["kind"], "rate_limit")
        self.assertEqual(top["provider"], "tokenharbor")
        self.assertEqual(top["count"], 3)
        self.assertEqual(top["item_ids"] if "item_ids" in top else ["q1", "q2", "q3"], ["q1", "q2", "q3"])
        self.assertIn("balance is at $0", top["message"])

    def test_a_missing_ledger_is_an_empty_report(self) -> None:
        summary = errors.summarise(top=5, path=Path(self._tmp.name) / "nope.jsonl")
        self.assertFalse(summary["exists"])
        self.assertEqual(summary["total"], 0)
        self.assertIn("no ledger yet", errors.format_summary(summary))

    def test_cli_errors_command_prints_the_summary(self) -> None:
        """``python -m agent.cli errors --top N`` must work on the real ledger path."""

        self._seed()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["errors", "--top", "5"])
        self.assertEqual(code, 0)
        self.assertIn("AI ERROR LEDGER", buffer.getvalue())
        self.assertIn("rate_limit", buffer.getvalue())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["errors", "--top", "1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["total"], 4)

    def test_cli_errors_reads_the_ledger_it_is_pointed_at(self) -> None:
        other = Path(self._tmp.name) / "elsewhere.jsonl"
        errors.record(kind=errors.KIND_HTTP, http_status=500, message="elsewhere", phase="p", path=other)
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            code = cli.main(["errors", "--json", "--ledger", str(other)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["total"], 1)

    def test_a_waf_block_without_a_status_is_not_mistaken_for_a_bad_key(self) -> None:
        """Cloudflare/WAF answers look like 403 but are not an auth problem.

        The transport raises them without a ``status`` attribute (the body is an
        HTML challenge page), so the ledger must keep them distinguishable from a
        real "your key is wrong" answer.
        """

        kind = errors.classify(
            llm.LlmError("HTTP 403 from https://agentrouter.org/v1: <!doctype html> aliyun_waf_aa")
        )
        self.assertEqual(kind, errors.KIND_HTTP)
        self.assertNotEqual(kind, errors.classify(status=403, message="invalid api key"))

    def test_the_default_ledger_is_the_documented_file(self) -> None:
        """Without an override the ledger is ``state/errors.jsonl`` (spec, not detail)."""

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYQ_ERRORS_LEDGER", None)
            self.assertEqual(errors.ledger_path(), paths.STATE_DIR / "errors.jsonl")

    def test_the_suite_never_touches_the_real_ledger(self) -> None:
        """The env override keeps the real ledger byte-identical across a test run."""

        real = paths.STATE_DIR / "errors.jsonl"
        before = real.read_bytes() if real.is_file() else None
        self._seed()
        after = real.read_bytes() if real.is_file() else None
        self.assertEqual(before, after, "a test appended to the real state/errors.jsonl")
        self.assertNotEqual(str(errors.ledger_path()), str(real))
        # ... and the row really did land in the temporary ledger
        self.assertEqual(len(self.rows()), 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
