"""Model-level failover: a route is ``(provider, model)``, so a model whose
providers are all down must fall back to the next **model** of its role.

The live failure this covers (run #4, ``status=ai_unavailable``):

* on a GitHub runner the direct ``agentrouter`` endpoint is blocked by the Aliyun
  WAF (``invalid JSON ... <meta name="aliyun_waf_aa" ...>``);
* ``ar-worker`` is out of credits (402/503 ``all_keys_exhausted``);
* ``jw-worker`` answers 503 for ``deepseek-v4-flash`` but serves ``gpt-5.6-sol``;
* direct ``justwoker`` is 403.

``deepseek-v4-flash`` therefore had **zero** working routes and the whole AI step
was skipped, although ``gpt-5.6-sol`` was reachable.  These tests pin the fix:
walk the candidate models of the role, record which ``(provider, model)`` served
the request, and flag a debate that only survived because one model had to speak
for both roles (``same_model_fallback``).
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from agent import config, debate, llm, verify
from agent import router as router_mod
from agent.debate import ordered_candidates, same_model_fallback
from agent.router import GlobalHalt
from agent.util import Log

QUIET = Log("test", quiet=True)

PROPOSER = "deepseek-v4-flash"
CRITIC = "gpt-5.6-sol"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_settings(**policy_overrides: Any) -> config.Settings:
    base = config.load_settings()
    if policy_overrides:
        base = replace(base, rate_limit=replace(base.rate_limit, **policy_overrides))
    return base


def all_keys() -> Dict[str, List[str]]:
    return {
        "agentrouter": ["key-ar1"],
        "ar-worker": ["token-arw"],
        "jw-worker": ["token-jww"],
        "justwoker": ["key-jw1"],
    }


def proposal_reply() -> str:
    return json.dumps({"items": [], "reason": "proposal"})


def critique_reply() -> str:
    return json.dumps({"verdict": "agree", "final": {"items": []}, "reason": "fine"})


class FakeChain:
    """``llm.chat_completion`` stand-in keyed by ``(provider, model)``.

    Behaviour: ``ok`` (a valid JSON reply for the role in the system prompt),
    ``rate`` (503 ``all_keys_exhausted``), ``waf`` (HTML challenge page instead of
    JSON — the agentrouter-on-the-runner case) or ``forbidden`` (HTTP 403).
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self.behaviour: Dict[Tuple[str, str], str] = {}

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
        if action == "waf":
            raise llm.LlmError(
                f"invalid JSON from {kwargs['base_url']}: <!doctype html> "
                '<meta name="aliyun_waf_aa" content="ff92">'
            )
        if action == "forbidden":
            raise llm.LlmError(f"HTTP 403 from {kwargs['base_url']}: ")
        system = ""
        for message in kwargs.get("messages") or ():
            if message.role == "system":
                system = message.content
                break
        text = critique_reply() if system == debate.SYSTEM_CRITIC else proposal_reply()
        return llm.ChatResult(
            text=text, model=model, provider="", key_ref="...abcd", latency_ms=1
        )

    def models_served(self) -> List[str]:
        return [model for _provider, model in self.calls]

    def calls_for(self, provider: str, model: str) -> int:
        return self.calls.count((provider, model))


class RouteTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeChain()
        self.enterContext(mock.patch.object(llm, "provider_keys", return_value=all_keys()))
        self.enterContext(mock.patch.object(llm, "chat_completion", side_effect=self.fake))
        router_mod.reset_router()

    def tearDown(self) -> None:
        router_mod.reset_router()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class CandidateConfigTests(unittest.TestCase):
    def test_settings_declare_the_candidate_models(self) -> None:
        policy = config.load_settings().debate
        self.assertEqual(
            list(policy.candidates_for("proposer")),
            ["deepseek-v4-flash", "deepseek-v4-pro", "gpt-5.6-sol", "claude-sonnet-5", "glm-5.3"],
        )
        self.assertEqual(
            list(policy.candidates_for("critic")),
            ["gpt-5.6-sol", "claude-opus-5", "claude-sonnet-5", "gemini-3.5-flash", "grok-4.6"],
        )
        self.assertEqual(policy.all_models()[0], PROPOSER)
        self.assertEqual(len(policy.all_models()), 8, "the union de-duplicates gpt-5.6-sol")

    def test_primary_model_always_leads_its_candidates(self) -> None:
        policy = replace(
            config.load_settings().debate,
            proposer_model="deepseek-v4-flash",
            proposer_models=("glm-5.3", "deepseek-v4-flash"),  # primary declared second
        )
        self.assertEqual(
            list(policy.candidates_for("proposer")), ["deepseek-v4-flash", "glm-5.3"]
        )

    def test_a_list_without_the_primary_still_starts_with_it(self) -> None:
        policy = replace(
            config.load_settings().debate,
            proposer_model="onprem-model",
            proposer_models=("glm-5.3",),
        )
        self.assertEqual(list(policy.candidates_for("proposer")), ["onprem-model", "glm-5.3"])

    def test_environment_overrides_the_declared_candidates(self) -> None:
        with mock.patch.dict("os.environ", {"PYQ_PROPOSER_MODELS": "a-model, b-model"}, clear=False):
            policy = config.load_settings().debate
        self.assertEqual(list(policy.candidates_for("proposer")), [PROPOSER, "a-model", "b-model"])

    def test_ordered_candidates_moves_first_and_last(self) -> None:
        self.assertEqual(ordered_candidates(["a", "b", "c"], first="c"), ["c", "a", "b"])
        self.assertEqual(ordered_candidates(["a", "b", "c"], last="a"), ["b", "c", "a"])
        self.assertEqual(
            ordered_candidates(["a", "b", "c"], first="c", last="a"), ["c", "b", "a"]
        )
        self.assertEqual(ordered_candidates(["a", "a", "b"]), ["a", "b"])


# ---------------------------------------------------------------------------
# the candidate walk
# ---------------------------------------------------------------------------


class ModelFallbackTests(RouteTestCase):
    def test_a_model_with_no_route_falls_back_to_the_next_candidate(self) -> None:
        """The live case: every route for the proposer model is dead."""

        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        for provider in ("agentrouter", "ar-worker", "jw-worker", "justwoker"):
            self.fake.behaviour[(provider, PROPOSER)] = "rate"
            self.fake.behaviour[(provider, "deepseek-v4-pro")] = "waf"
        # the fallback model is only served by jw-worker, as on the runner
        for provider in ("agentrouter", "ar-worker", "justwoker"):
            self.fake.behaviour[(provider, CRITIC)] = "rate"
        self.fake.behaviour[("jw-worker", CRITIC)] = "ok"

        result = router.chat(
            model=PROPOSER,
            models=(PROPOSER, "deepseek-v4-pro", CRITIC),
            messages=[llm.ChatMessage("user", "hi")],
            role="proposer",
        )

        self.assertEqual(result.model, CRITIC)
        self.assertEqual(result.provider, "jw-worker")
        self.assertEqual(router.stats.model_fallbacks, {CRITIC: 1})
        # the dead model is out of the way, its routes are cooling down
        self.assertTrue(router.route_state("agentrouter", PROPOSER).cooling_down())
        self.assertTrue(router.is_halted(PROPOSER))
        # the WAF-blocked model was never asked again once its breaker tripped
        self.assertEqual(self.fake.calls_for("agentrouter", "deepseek-v4-pro"), 1)

    def test_the_first_healthy_candidate_is_used(self) -> None:
        router = router_mod.Router(make_settings())
        result = router.chat(
            model=PROPOSER,
            models=(PROPOSER, CRITIC),
            messages=[llm.ChatMessage("user", "hi")],
            role="proposer",
        )
        self.assertEqual(result.model, PROPOSER)
        self.assertEqual(result.provider, "agentrouter")
        self.assertEqual(router.stats.model_fallbacks, {})

    def test_a_dead_model_costs_one_candidate_not_the_whole_role(self) -> None:
        """The proposer role keeps working while its primary model is dark."""

        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        for provider in ("agentrouter", "ar-worker", "jw-worker", "justwoker"):
            self.fake.behaviour[(provider, PROPOSER)] = "rate"
        self.fake.behaviour[("jw-worker", CRITIC)] = "ok"

        self.assertTrue(router.role_available("proposer"))
        router.chat(
            model=PROPOSER,
            models=router.role_models("proposer"),
            messages=[llm.ChatMessage("user", "hi")],
            role="proposer",
        )
        # the halt of one model does not take the role down
        self.assertTrue(router.is_halted(PROPOSER))
        self.assertTrue(router.role_available("proposer"))

    def test_global_halt_only_when_every_candidate_model_is_dead(self) -> None:
        """Every ``(provider, model)`` candidate of the role is down -> halt."""

        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        candidates = router.role_models("proposer")
        for name in ("agentrouter", "ar-worker", "jw-worker", "justwoker"):
            for model in candidates:
                self.fake.behaviour[(name, model)] = "rate"

        with self.assertRaises(GlobalHalt) as caught:
            router.chat(
                model=candidates[0],
                models=candidates,
                messages=[llm.ChatMessage("user", "hi")],
                role="proposer",
            )

        self.assertIn("no healthy route for any candidate model", str(caught.exception))
        for model in candidates:
            self.assertTrue(router.is_halted(model), model)
        self.assertFalse(router.role_available("proposer"))

    def test_a_transient_failure_does_not_halt_the_router(self) -> None:
        """A stopped route that is still healthy is a retry, not an outage."""

        router = router_mod.Router(make_settings(halt_on_any_rate_limit_signal=False))
        self.fake.behaviour[("agentrouter", PROPOSER)] = "rate"

        result = router.chat(
            model=PROPOSER,
            models=(PROPOSER,),
            messages=[llm.ChatMessage("user", "hi")],
        )
        self.assertEqual(result.provider, "ar-worker")
        self.assertFalse(router.halted)

    def test_no_credentials_raises_without_halting_a_candidate(self) -> None:
        router = router_mod.Router(make_settings())
        with mock.patch.object(llm, "provider_keys", return_value={}):
            with self.assertRaises(llm.LlmError) as caught:
                router.chat(
                    model=PROPOSER,
                    models=(PROPOSER, CRITIC),
                    messages=[llm.ChatMessage("user", "hi")],
                )
        self.assertIn("no provider credentials", str(caught.exception))
        self.assertFalse(router.halted)

    def test_a_dead_route_for_one_model_keeps_serving_the_other(self) -> None:
        """jw-worker 503s the proposer model but serves the critic model (R2)."""

        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.fake.behaviour[("jw-worker", PROPOSER)] = "rate"
        self.fake.behaviour[("jw-worker", CRITIC)] = "ok"

        with self.assertRaises(GlobalHalt):
            router.chat(
                model=PROPOSER,
                messages=[llm.ChatMessage("user", "hi")],
                provider_order=("jw-worker",),
            )
        self.assertTrue(router.route_state("jw-worker", PROPOSER).cooling_down())

        second = router.chat(
            model=CRITIC,
            messages=[llm.ChatMessage("user", "hi")],
            provider_order=("jw-worker",),
        )
        self.assertEqual(second.provider, "jw-worker")
        self.assertFalse(router.route_state("jw-worker", CRITIC).cooling_down())

    def test_role_routes_walk_models_then_providers(self) -> None:
        router = router_mod.Router(make_settings())
        routes = router.role_routes("proposer")
        self.assertEqual(routes[:4], [
            ("agentrouter", PROPOSER),
            ("ar-worker", PROPOSER),
            ("jw-worker", PROPOSER),
            ("justwoker", PROPOSER),
        ])
        self.assertEqual(routes[4][1], "deepseek-v4-pro")
        # every candidate model is in the matrix, and the report covers them all
        report = router.provider_report()
        self.assertIn("agentrouter/glm-5.3", report)
        self.assertIn(f"jw-worker/{CRITIC}", report)

    def test_a_halted_candidate_is_skipped_without_a_request(self) -> None:
        router = router_mod.Router(make_settings())
        router.halt("out of quota", model=PROPOSER)
        self.fake.behaviour[("jw-worker", CRITIC)] = "ok"

        result = router.chat(
            model=PROPOSER,
            models=(PROPOSER, CRITIC),
            messages=[llm.ChatMessage("user", "hi")],
            role="proposer",
        )
        self.assertEqual(result.model, CRITIC)
        self.assertEqual(self.fake.calls_for("agentrouter", PROPOSER), 0)


# ---------------------------------------------------------------------------
# the debate: two independent opinions
# ---------------------------------------------------------------------------


def _run_verify(*, fake: FakeChain, records: List[Dict[str, Any]], **kwargs: Any):
    """``verify_database`` against the fake chain, with the state tree mocked out."""

    with mock.patch.object(llm, "provider_keys", return_value=all_keys()), mock.patch.object(
        llm, "chat_completion", side_effect=fake
    ), mock.patch.object(verify.indexer, "read_index", return_value=records), mock.patch.object(
        verify, "load_state", return_value={"phases": {}}
    ), mock.patch.object(verify, "save_state"), mock.patch.object(
        verify.ckpt, "load_checkpoint", return_value={}
    ), mock.patch.object(verify.ckpt, "save_checkpoint"), mock.patch.object(
        verify.tracking, "record_ai"
    ) as record_ai, mock.patch.object(
        verify.tracking, "record_routes"
    ), mock.patch.object(
        verify.tracking, "write_progress_md"
    ), mock.patch.object(verify.debate_mod, "append_jsonl"):
        result = verify.verify_database(make_settings(), phase="phase1", log=QUIET, **kwargs)
    return result, record_ai


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


class DebateRoleTests(RouteTestCase):
    def test_the_two_roles_use_different_models_when_both_are_reachable(self) -> None:
        router = router_mod.Router(make_settings())
        session = debate.Debate(make_settings(), router, log=QUIET)

        outcome = session.run(item_id="i1", task="verify", payload={"batch": []})

        self.assertEqual(outcome.provenance["proposer"]["model"], PROPOSER)
        self.assertEqual(outcome.provenance["critic"]["model"], CRITIC)
        self.assertFalse(outcome.provenance["same_model_fallback"])
        self.assertEqual(outcome.notes, [])

    def test_the_critic_prefers_another_model_over_the_proposer_s(self) -> None:
        """The proposer's model is reachable for the critic too — but it is last."""

        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        # only the proposer's model answers: every other candidate model is down
        for model in ("gpt-5.6-sol", "claude-opus-5", "claude-sonnet-5", "gemini-3.5-flash", "grok-4.6"):
            self.fake.behaviour[("", model)] = "rate"
        session = debate.Debate(make_settings(), router, log=QUIET)

        outcome = session.run(item_id="i1", task="verify", payload={"batch": []})

        self.assertEqual(outcome.provenance["proposer"]["model"], PROPOSER)
        self.assertEqual(outcome.provenance["critic"]["model"], PROPOSER)
        self.assertTrue(outcome.provenance["same_model_fallback"])
        self.assertTrue(any("same_model_fallback" in note for note in outcome.notes))

    def test_same_model_fallback_detection(self) -> None:
        self.assertFalse(same_model_fallback({"proposer": {"model": "a"}, "critic": {"model": "b"}}))
        self.assertTrue(same_model_fallback({"proposer": {"model": "a"}, "critic": {"model": "a"}}))
        self.assertTrue(
            same_model_fallback(
                {"proposer": {"model": "a"}, "rebuttal": {"model": "b"}, "critic": {"model": "a"}}
            )
        )
        self.assertFalse(same_model_fallback({"proposer": {"model": "a"}, "critic": None}))


class SameModelFallbackReportTests(RouteTestCase):
    def test_verify_records_the_flag_in_progress(self) -> None:
        fake = FakeChain()
        # the proposer's model answers, every other model is down: the debate
        # survives, but one model ends up speaking for both sides
        for model in ("gpt-5.6-sol", "claude-opus-5", "claude-sonnet-5", "gemini-3.5-flash", "grok-4.6"):
            fake.behaviour[("", model)] = "rate"

        result, record_ai = _run_verify(
            fake=fake, records=records_for("ENG", 3), batch_size=3
        )

        self.assertEqual(result.status, verify.STATUS_OK)
        self.assertEqual(result.counters["batches_same_model_fallback"], 1)
        self.assertTrue(
            any("same_model_fallback: true" in note for note in result.notes), result.notes
        )
        payloads = [call.args[1] for call in record_ai.call_args_list if len(call.args) > 1]
        self.assertTrue(any(p.get("same_model_fallback") for p in payloads))
        notes = [
            note
            for call in record_ai.call_args_list
            for note in (call.kwargs.get("notes") or [])
        ]
        self.assertTrue(any("same_model_fallback: true" in note for note in notes), notes)

    def test_verify_is_silent_when_the_two_models_differ(self) -> None:
        fake = FakeChain()
        result, record_ai = _run_verify(
            fake=fake, records=records_for("ENG", 3), batch_size=3
        )
        self.assertEqual(result.status, verify.STATUS_OK)
        self.assertNotIn("batches_same_model_fallback", result.counters)
        notes = [
            note
            for call in record_ai.call_args_list
            for note in (call.kwargs.get("notes") or [])
        ]
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
