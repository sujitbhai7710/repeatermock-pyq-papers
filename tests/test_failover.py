"""Regression tests for provider-aware failover (R1) and reasoning truncation.

The live failure these cover: ``jw`` answered ``503 all_keys_exhausted`` while
``agentrouter`` was healthy, yet the router halted the whole run; and
``deepseek-v4-flash`` truncated its reasoning before writing ``content``, which
used to be reported as a provider failure.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Any, Dict, List, Optional
from unittest import mock

from agent import config, llm
from agent import router as router_mod
from agent.router import GlobalHalt


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_settings(**policy_overrides: Any) -> config.Settings:
    base = config.load_settings()
    policy = replace(base.rate_limit, **policy_overrides)
    return replace(base, rate_limit=policy)


def two_keys() -> Dict[str, List[str]]:
    return {"agentrouter": ["key-ar"], "jw": ["key-jw"]}


class FakeProvider:
    """Stand-in for :func:`agent.llm.chat_completion` keyed by base_url."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.behaviour: Dict[str, str] = {}

    def provider_name(self, base_url: str) -> str:
        return "agentrouter" if "ar-rotator" in base_url else "jw"

    def __call__(self, **kwargs: Any) -> llm.ChatResult:
        name = self.provider_name(kwargs["base_url"])
        self.calls.append(name)
        action = self.behaviour.get(name, "ok")
        if action == "rate":
            raise llm.LlmRateLimited(
                f"HTTP 503 from {kwargs['base_url']}: "
                '{"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown."}}'
            )
        if action == "error":
            raise llm.LlmError(f"HTTP 500 from {kwargs['base_url']}: upstream boom")
        return llm.ChatResult(
            text='{"ok": true}',
            model=kwargs.get("model", ""),
            provider="",
            key_ref="...abcd",
            latency_ms=1,
        )


def chat_once(router: router_mod.Router, *, model: str = "deepseek-v4-flash"):
    return router.chat(
        model=model,
        messages=[llm.ChatMessage("user", "hi")],
        provider_order=("agentrouter", "jw"),
    )


# ---------------------------------------------------------------------------
# R1 — provider-level outage falls over
# ---------------------------------------------------------------------------


class FailoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeProvider()
        self.enterContext(mock.patch.object(llm, "provider_keys", return_value=two_keys()))
        self.enterContext(mock.patch.object(llm, "chat_completion", side_effect=self.fake))

    def test_exhausted_provider_falls_over_to_the_next(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.fake.behaviour["agentrouter"] = "rate"

        result = chat_once(router)

        self.assertEqual(result.provider, "jw")
        self.assertEqual(self.fake.calls, ["agentrouter", "jw"])
        self.assertFalse(router.halted)
        self.assertEqual(router.stats.rate_limit_signals, 1)
        state = router.provider_state("agentrouter")
        self.assertEqual(state.trips, 1)
        self.assertTrue(state.cooling_down())
        # the success on jw reset the consecutive-failure counter
        self.assertEqual(router.stats.consecutive_failures, 0)

    def test_healthy_provider_never_touches_the_exhausted_one(self) -> None:
        router = router_mod.Router(make_settings())
        self.fake.behaviour["jw"] = "rate"

        result = chat_once(router)

        self.assertEqual(result.provider, "agentrouter")
        self.assertEqual(self.fake.calls, ["agentrouter"])
        self.assertFalse(router.halted)
        self.assertEqual(router.stats.rate_limit_signals, 0)

    def test_tripped_provider_is_not_retried_every_item(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.fake.behaviour["agentrouter"] = "rate"

        for _ in range(4):
            chat_once(router)

        self.assertEqual(self.fake.calls.count("agentrouter"), 1)
        self.assertEqual(self.fake.calls.count("jw"), 4)
        self.assertFalse(router.halted)

    def test_provider_is_probed_once_after_cooldown(self) -> None:
        clock = {"t": 1000.0}
        settings = make_settings(provider_cooldown_seconds=30, provider_cooldown_max_seconds=120)
        with mock.patch.object(router_mod, "monotonic", side_effect=lambda: clock["t"]):
            router = router_mod.Router(settings)
            self.fake.behaviour["agentrouter"] = "rate"
            chat_once(router)  # ar trips, jw serves

            clock["t"] += 29.0
            chat_once(router)  # still cooling down -> ar skipped
            self.assertEqual(self.fake.calls.count("agentrouter"), 1)

            self.fake.behaviour["agentrouter"] = "ok"
            clock["t"] += 2.0
            result = chat_once(router)  # cooldown elapsed -> single probe succeeds
            self.assertEqual(result.provider, "agentrouter")
            self.assertEqual(self.fake.calls.count("agentrouter"), 2)
            self.assertEqual(router.provider_state("agentrouter").trips, 0)


# ---------------------------------------------------------------------------
# R1 — global halt only when no healthy provider is left
# ---------------------------------------------------------------------------


class HaltTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeProvider()
        self.enterContext(mock.patch.object(llm, "provider_keys", return_value=two_keys()))
        self.enterContext(mock.patch.object(llm, "chat_completion", side_effect=self.fake))

    def test_all_providers_exhausted_halts(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.fake.behaviour.update({"agentrouter": "rate", "jw": "rate"})

        with self.assertRaises(GlobalHalt) as caught:
            chat_once(router)

        self.assertTrue(router.halted)
        self.assertIn("all providers exhausted", str(caught.exception))
        self.assertEqual(router.stats.rate_limit_signals, 2)
        self.assertEqual(router.provider_state("agentrouter").trips, 1)
        self.assertEqual(router.provider_state("jw").trips, 1)

    def test_halted_router_refuses_further_requests(self) -> None:
        router = router_mod.Router(make_settings())
        self.fake.behaviour.update({"agentrouter": "rate", "jw": "rate"})
        with self.assertRaises(GlobalHalt):
            chat_once(router)

        calls_before = len(self.fake.calls)
        with self.assertRaises(GlobalHalt):
            chat_once(router)
        self.assertEqual(len(self.fake.calls), calls_before)

    def test_consecutive_failure_threshold_halts(self) -> None:
        router = router_mod.Router(make_settings(consecutive_failure_threshold=4))
        self.fake.behaviour["agentrouter"] = "error"

        for _ in range(3):
            with self.assertRaises(llm.LlmError):
                router.chat(
                    model="m",
                    messages=[llm.ChatMessage("user", "hi")],
                    provider_order=("agentrouter",),
                )
        self.assertFalse(router.halted)

        with self.assertRaises(GlobalHalt):
            router.chat(
                model="m",
                messages=[llm.ChatMessage("user", "hi")],
                provider_order=("agentrouter",),
            )
        self.assertTrue(router.halted)
        self.assertIn("consecutive failures", router.stats.halt_reason)

    def test_success_resets_the_consecutive_failure_counter(self) -> None:
        router = router_mod.Router(make_settings(consecutive_failure_threshold=4))
        self.fake.behaviour["agentrouter"] = "error"

        def call_once() -> None:
            router.chat(
                model="m",
                messages=[llm.ChatMessage("user", "hi")],
                provider_order=("agentrouter",),
            )

        for _ in range(3):
            with self.assertRaises(llm.LlmError):
                call_once()
        self.fake.behaviour["agentrouter"] = "ok"
        call_once()  # one success clears the counter
        self.assertEqual(router.stats.consecutive_failures, 0)

        self.fake.behaviour["agentrouter"] = "error"
        for _ in range(3):
            with self.assertRaises(llm.LlmError):
                call_once()
        self.assertFalse(router.halted)

    def test_failures_absorbed_by_a_healthy_provider_never_halt(self) -> None:
        router = router_mod.Router(make_settings(consecutive_failure_threshold=4))
        self.fake.behaviour["agentrouter"] = "error"  # jw keeps serving

        for _ in range(6):
            result = chat_once(router)
            self.assertEqual(result.provider, "jw")

        self.assertFalse(router.halted)
        self.assertLess(router.stats.consecutive_failures, 4)

    def test_rate_limit_without_trip_when_signals_are_not_treated_as_outage(self) -> None:
        router = router_mod.Router(make_settings(halt_on_any_rate_limit_signal=False))
        self.fake.behaviour["agentrouter"] = "rate"

        chat_once(router)  # falls over to jw, provider stays in rotation

        self.assertFalse(router.provider_state("agentrouter").cooling_down())
        self.assertEqual(router.stats.rate_limit_signals, 1)
        self.assertEqual(self.fake.calls, ["agentrouter", "jw"])

    def test_no_keys_raises_without_halting(self) -> None:
        router = router_mod.Router(make_settings())
        with mock.patch.object(llm, "provider_keys", return_value={}):
            with self.assertRaises(llm.LlmError) as caught:
                chat_once(router)
        self.assertIn("no provider available", str(caught.exception))
        self.assertFalse(router.halted)


# ---------------------------------------------------------------------------
# reasoning truncation (empty ``content``, ``finish_reason=length``)
# ---------------------------------------------------------------------------


def truncated_body() -> str:
    return json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": "", "reasoning_content": "thinking..."},
                }
            ]
        }
    )


def complete_body() -> str:
    return json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok": true}'}}]}
    )


class ReasoningTruncationTests(unittest.TestCase):
    def test_truncated_reasoning_is_retried_with_a_bigger_budget(self) -> None:
        seen: List[Dict[str, Any]] = []

        def fake_post(**kwargs: Any):
            seen.append(kwargs["payload"])
            return (200, truncated_body() if len(seen) == 1 else complete_body())

        with mock.patch.object(llm, "_post_chat", side_effect=fake_post):
            result = llm.chat_completion(
                base_url="https://ar-rotator.opencode-5a3.workers.dev/v1",
                api_key="sk-test",
                model="deepseek-v4-flash",
                messages=[llm.ChatMessage("user", "hi")],
                max_tokens=700,
                truncation_retries=2,
            )

        self.assertEqual(result.text, '{"ok": true}')
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["max_tokens"], 700)
        self.assertGreaterEqual(seen[1]["max_tokens"], 4096)

    def test_truncated_reasoning_without_retries_is_an_error(self) -> None:
        with mock.patch.object(llm, "_post_chat", return_value=(200, truncated_body())):
            with self.assertRaises(llm.LlmError) as caught:
                llm.chat_completion(
                    base_url="https://ar-rotator.opencode-5a3.workers.dev/v1",
                    api_key="sk-test",
                    model="deepseek-v4-flash",
                    messages=[llm.ChatMessage("user", "hi")],
                    max_tokens=700,
                )
        self.assertIn("empty completion", str(caught.exception))

    def test_rate_limit_body_is_still_a_rate_limit(self) -> None:
        body = json.dumps({"error": {"type": "all_keys_exhausted", "message": "All upstream keys failed"}})
        with mock.patch.object(llm, "_post_chat", return_value=(503, body)):
            with self.assertRaises(llm.LlmRateLimited):
                llm.chat_completion(
                    base_url="https://jw-rotator.opencode-5a3.workers.dev/v1",
                    api_key="sk-test",
                    model="deepseek-v4-flash",
                    messages=[llm.ChatMessage("user", "hi")],
                )


if __name__ == "__main__":
    unittest.main()
