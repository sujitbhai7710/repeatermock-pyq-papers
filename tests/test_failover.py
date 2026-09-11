"""Regression tests for the multi-route provider chain (R1), per-model route
failover (R2) and the configurable route order (R3).

The live failures these cover:

* ``jw-worker`` answered ``503 all_keys_exhausted`` for ``deepseek-v4-flash``
  while the direct ``agentrouter`` endpoint was healthy — the run must fail over
  instead of halting;
* the same worker served ``gpt-5.6-sol`` — a ``deepseek`` failure must **not**
  take the worker out of rotation for the other model (breakers are keyed by
  ``(provider, model)``, never by provider);
* the direct endpoints need their own user agents (``cline/2.0.0`` for
  agentrouter) and ``api.justwoker.icu`` speaks the Anthropic Messages API, so a
  single "post bearer JSON" client cannot serve the whole chain;
* ``deepseek-v4-flash`` truncated its reasoning before writing ``content``, which
  used to be reported as a route failure.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from typing import Any, Dict, List, Tuple
from unittest import mock

from agent import config, llm
from agent import router as router_mod
from agent.router import GlobalHalt

PROPOSER = "deepseek-v4-flash"
CRITIC = "gpt-5.6-sol"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


#: the four routes these scenarios exercise, in the order they walk them
FIXTURE_PROVIDERS: Tuple[str, ...] = ("agentrouter", "ar-worker", "jw-worker", "justwoker")

#: candidate models per role, in failover order (the scenarios below were written
#: against this matrix: ``deepseek-v4-pro`` and ``claude-sonnet-5`` are the
#: fallbacks a dead primary model must reach)
FIXTURE_PROPOSER_MODELS: Tuple[str, ...] = (PROPOSER, "deepseek-v4-pro", CRITIC, "claude-sonnet-5")
FIXTURE_CRITIC_MODELS: Tuple[str, ...] = (CRITIC, "claude-sonnet-5", "deepseek-v4-pro", PROPOSER)


def make_settings(**policy_overrides: Any) -> config.Settings:
    """A settings object pinned to the scenario fixture, not to the shipped file.

    ``config/settings.json`` is a production knob: routes are added/removed and
    reordered as providers come and go.  These tests are about *mechanics* — the
    chain walk, the per-``(provider, model)`` breaker, the cooldown probe — so
    they start from the shipped settings and then pin everything they assert on:

    * the four fixture routes, in the fixture's walk order (any route the fixture
      does not name is filtered out, and the fixture route order is applied to
      both the top-level ``provider_order`` and the role-level orders that
      ``role_routes`` actually reads);
    * the candidate model lists of both roles;
    * ``halt_on_any_rate_limit_signal`` — the shipped policy is free to leave a
      rate-limited route in rotation, but every breaker scenario here needs the
      signal to trip the route, so the fixture pins it on.  The test that
      exercises the *other* choice passes the flag explicitly.

    A shipped edit that drops one of the fixture routes fails here loudly instead
    of surfacing as an unrelated ``unknown provider`` deep inside a scenario.
    """

    order = tuple(policy_overrides.pop("provider_order", FIXTURE_PROVIDERS))
    policy: Dict[str, Any] = {"halt_on_any_rate_limit_signal": True}
    policy.update(policy_overrides)
    base = config.load_settings()
    missing = [name for name in order if base.provider_by_name(name) is None]
    if missing:
        raise AssertionError(
            "the scenario fixture needs route(s) that config/settings.json no longer "
            f"declares: {', '.join(missing)} (fixture: {', '.join(order)})"
        )
    return replace(
        base,
        rate_limit=replace(base.rate_limit, **policy),
        provider_order=order,
        # keep the shipped specs (base_url / auth / user agent / path), drop the
        # routes the fixture does not exercise
        providers=tuple(p for name in order for p in base.providers if p.name == name),
        debate=replace(
            base.debate,
            proposer_model=PROPOSER,
            critic_model=CRITIC,
            proposer_models=FIXTURE_PROPOSER_MODELS,
            critic_models=FIXTURE_CRITIC_MODELS,
            provider_order=order,
            proposer_provider_order=order,
            critic_provider_order=order,
            model_provider_orders={},
        ),
    )


def all_keys() -> Dict[str, List[str]]:
    """A credential for every route of the default chain."""

    return {
        "agentrouter": ["key-ar1", "key-ar2"],
        "ar-worker": ["token-arw"],
        "jw-worker": ["token-jww"],
        "justwoker": ["key-jw1"],
    }


class FakeChain:
    """Stand-in for :func:`agent.llm.chat_completion`, keyed by (provider, model).

    Behaviour values: ``ok``, ``rate`` (503 all_keys_exhausted), ``error``
    (HTTP 500) or ``forbidden`` (HTTP 403, WAF).
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []
        self.kwargs: List[Dict[str, Any]] = []
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
        self.kwargs.append(kwargs)
        action = self.behaviour.get((name, model), self.behaviour.get(("", model), "ok"))
        if action == "rate":
            raise llm.LlmRateLimited(
                f"HTTP 503 from {kwargs['base_url']}: "
                '{"error":{"type":"all_keys_exhausted","message":"All upstream keys failed or are on cooldown."}}'
            )
        if action == "forbidden":
            raise llm.LlmError(f"HTTP 403 from {kwargs['base_url']}: browser integrity check")
        if action == "error":
            raise llm.LlmError(f"HTTP 500 from {kwargs['base_url']}: upstream boom")
        return llm.ChatResult(
            text='{"ok": true}',
            model=model,
            provider="",
            key_ref="...abcd",
            latency_ms=1,
        )

    def calls_for(self, provider: str, model: str) -> int:
        return self.calls.count((provider, model))


def chat_once(router: router_mod.Router, *, model: str = PROPOSER) -> llm.ChatResult:
    return router.chat(model=model, messages=[llm.ChatMessage("user", "hi")])


class RouteTestCase(unittest.TestCase):
    """Shared wiring: a router whose chain is fully keyed and fully fake."""

    def setUp(self) -> None:
        self.fake = FakeChain()
        self.enterContext(mock.patch.object(llm, "provider_keys", return_value=all_keys()))
        self.enterContext(mock.patch.object(llm, "chat_completion", side_effect=self.fake))
        router_mod.reset_router()

    def tearDown(self) -> None:
        router_mod.reset_router()


# ---------------------------------------------------------------------------
# R1 — the chain itself
# ---------------------------------------------------------------------------


class ChainConfigTests(unittest.TestCase):
    def test_default_chain_matches_the_spec(self) -> None:
        """The built-in chain (used when ``providers`` is absent) is the spec table.

        The four routes and their wire format are the contract with the upstreams:
        the shipped file may add routes, but these four must keep being served the
        same way (a wrong user agent or request path breaks a live run).
        """

        self.assertEqual(
            [provider.name for provider in config.DEFAULT_PROVIDERS],
            ["agentrouter", "ar-worker", "jw-worker", "justwoker"],
        )
        self.assertEqual(
            tuple(config.DEFAULT_PROVIDER_ORDER),
            ("agentrouter", "ar-worker", "jw-worker", "justwoker"),
        )

        by_name = {provider.name: provider for provider in config.DEFAULT_PROVIDERS}
        self.assertEqual(by_name["agentrouter"].base_url, "https://agentrouter.org/v1")
        self.assertEqual(by_name["agentrouter"].auth_style, "bearer")
        self.assertEqual(by_name["agentrouter"].user_agent, "cline/2.0.0")
        self.assertEqual(by_name["agentrouter"].env_keys, ("AGENTROUTER_KEYS", "OPENAI_KEYS"))

        self.assertEqual(
            by_name["ar-worker"].base_url, "https://ar-rotator.opencode-5a3.workers.dev/v1"
        )
        self.assertEqual(by_name["jw-worker"].base_url, "https://jw-rotator.opencode-5a3.workers.dev/v1")
        for name in ("ar-worker", "jw-worker"):
            self.assertEqual(by_name[name].auth_style, "bearer")
            self.assertEqual(by_name[name].user_agent, "browser")

        self.assertEqual(by_name["justwoker"].base_url, "https://api.justwoker.icu/v1")
        self.assertEqual(by_name["justwoker"].auth_style, "anthropic")
        self.assertEqual(by_name["justwoker"].user_agent, "browser")
        self.assertEqual(by_name["justwoker"].env_keys, ("JUSTWOKER_KEYS", "DEEPSEEK_KEYS"))

        # the table exists twice (config + llm); the copies must not drift apart
        self.assertEqual(
            [(p.name, p.base_url, p.auth_style, p.user_agent) for p in config.DEFAULT_PROVIDERS],
            [(p.name, p.base_url, p.auth_style, p.user_agent) for p in llm.PROVIDERS],
        )

        # ... and the shipped file must still serve these four identically
        shipped = config.load_settings()
        for spec in config.DEFAULT_PROVIDERS:
            route = shipped.provider_by_name(spec.name)
            self.assertIsNotNone(route, f"config/settings.json dropped the {spec.name!r} route")
            expected = llm.provider_from_spec(spec)
            actual = llm.provider_from_spec(route)
            self.assertEqual(
                (actual.base_url, actual.auth_style, actual.user_agent, actual.env_keys),
                (expected.base_url, expected.auth_style, expected.user_agent, expected.env_keys),
                f"config/settings.json changed the wire format of {spec.name!r}",
            )
            self.assertEqual(
                actual.request_path(),
                expected.request_path(),
                f"config/settings.json changed the request path of {spec.name!r}",
            )

        # api.justwoker.icu speaks the Anthropic Messages API
        self.assertEqual(
            llm.provider_from_spec(shipped.provider_by_name("justwoker")).request_path(), "/messages"
        )

    def test_shipped_config_is_internally_consistent(self) -> None:
        """Every shipped route / order / model list must line up.

        A config edit used to be discovered through a dozen unrelated scenario
        failures (an order naming a missing route, a role order naming an
        undeclared provider, an empty candidate list).  Fail cheaply here instead.
        """

        settings = config.load_settings()
        raw = settings.raw.get("providers") or {}
        routes = raw.get("routes") or {}
        order = [str(name) for name in (raw.get("order") or [])]
        names = [provider.name for provider in settings.providers]

        self.assertTrue(routes, "providers.routes must declare at least one route")
        self.assertTrue(order, "providers.order must not be empty")
        for name in order:
            self.assertIn(name, routes, f"providers.order names {name!r}, which providers.routes lacks")
        self.assertEqual(
            sorted(names), sorted(routes), "every declared route must resolve to a provider spec"
        )
        for name, body in routes.items():
            self.assertTrue(
                str(body.get("base_url") or "").strip(),
                f"route {name!r} has no base_url (it would be dropped silently)",
            )
            self.assertTrue(body.get("env_keys"), f"route {name!r} declares no env_keys")

        policy = settings.debate
        raw_debate = settings.raw.get("debate") or {}
        role_orders = {
            key: (getattr(policy, key), raw_debate.get(key))
            for key in ("provider_order", "proposer_provider_order", "critic_provider_order")
        }
        for key, (effective, declared) in role_orders.items():
            # the effective order is what the router walks; the declared list is
            # what an operator edits — both must name declared routes only
            self.assertTrue(effective, f"debate.{key} must not be empty")
            for name in effective:
                self.assertIn(
                    name, settings.provider_order, f"debate.{key} names undeclared route {name!r}"
                )
            for name in declared or ():
                self.assertIn(str(name), order, f"debate.{key} names undeclared route {name!r}")

        self.assertTrue(policy.proposer_models, "debate.proposer_models must not be empty")
        self.assertTrue(policy.critic_models, "debate.critic_models must not be empty")
        # a blanked list silently falls back to the code defaults, so check the
        # declared value as well
        for key in ("proposer_models", "critic_models"):
            if key in raw_debate:
                self.assertTrue(raw_debate[key], f"debate.{key} must not be empty when declared")
        for role in ("proposer", "critic"):
            for model in policy.candidates_for(role):
                self.assertTrue(str(model).strip(), f"{role} declares a blank candidates_for entry")

    def test_providers_block_overrides_the_builtin_chain(self) -> None:
        base = config.load_settings()
        trimmed = replace(
            base,
            providers=tuple(p for p in base.providers if p.name in ("agentrouter", "justwoker")),
            provider_order=("justwoker", "agentrouter"),
        )
        provider = llm.resolve_providers(trimmed)
        self.assertEqual([p.name for p in provider], ["justwoker", "agentrouter"])
        router = router_mod.Router(trimmed)
        # declaration order is kept for the chain, the configured order for walking it
        self.assertEqual(router.provider_order, ("justwoker", "agentrouter"))

    def test_keys_are_read_per_route_with_legacy_aliases(self) -> None:
        env = {
            "AGENTROUTER_KEYS": "k1,k2",
            "OPENAI_KEYS": "k2,k3",  # alias, k2 de-duplicated
            "AR_PROXY_TOKEN": "arw",
            "JW_PROXY_TOKEN": "jww",
            "JUSTWOKER_KEYS": "w1",
            "DEEPSEEK_KEYS": "w2",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            pools = llm.provider_keys()
        self.assertEqual(pools["agentrouter"], ["k1", "k2", "k3"])
        self.assertEqual(pools["ar-worker"], ["arw"])
        self.assertEqual(pools["jw-worker"], ["jww"])
        self.assertEqual(pools["justwoker"], ["w1", "w2"])

    def test_history_aliases_still_work_alone(self) -> None:
        with mock.patch.dict(
            "os.environ", {"OPENAI_KEYS": "legacy", "DEEPSEEK_KEYS": "legacy2"}, clear=True
        ):
            pools = llm.provider_keys()
        self.assertEqual(pools["agentrouter"], ["legacy"])
        self.assertEqual(pools["justwoker"], ["legacy2"])

    def test_route_wire_format_and_user_agent_come_from_the_route(self) -> None:
        fake = FakeChain()
        with mock.patch.object(llm, "provider_keys", return_value=all_keys()), mock.patch.object(
            llm, "chat_completion", side_effect=fake
        ):
            router = router_mod.Router(make_settings())
            chat_once(router)

        sent = fake.kwargs[0]
        self.assertEqual(sent["base_url"], "https://agentrouter.org/v1")
        self.assertEqual(sent["auth_style"], llm.AUTH_BEARER)
        self.assertEqual(sent["user_agent_value"], "cline/2.0.0")
        self.assertEqual(sent["path"], "/chat/completions")

    def test_anthropic_route_uses_the_messages_path(self) -> None:
        fake = FakeChain()
        settings = make_settings()
        with mock.patch.object(llm, "provider_keys", return_value={"justwoker": ["k"]}), mock.patch.object(
            llm, "chat_completion", side_effect=fake
        ):
            router = router_mod.Router(settings)
            router.chat(
                model=PROPOSER,
                messages=[llm.ChatMessage("user", "hi")],
                provider_order=("justwoker",),
            )

        sent = fake.kwargs[0]
        self.assertEqual(sent["auth_style"], llm.AUTH_ANTHROPIC)
        self.assertEqual(sent["path"], "/messages")
        self.assertNotEqual(sent["user_agent_value"], "cline/2.0.0")


class AnthropicTranslationTests(unittest.TestCase):
    def test_openai_payload_is_translated_to_messages(self) -> None:
        payload = {
            "model": "claude-sonnet-4-5-20250929",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
            "temperature": 0.0,
            "max_tokens": 512,
            "response_format": {"type": "json_object"},
        }
        wire = llm.anthropic_payload(payload)
        self.assertEqual(wire["system"], "be terse")
        self.assertEqual(wire["max_tokens"], 512)
        self.assertEqual(
            wire["messages"],
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        )
        self.assertNotIn("response_format", wire)

    def test_max_tokens_is_supplied_when_the_caller_omits_it(self) -> None:
        wire = llm.anthropic_payload({"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(wire["max_tokens"], llm.DEFAULT_ANTHROPIC_MAX_TOKENS)

    def test_messages_reply_is_translated_back(self) -> None:
        reply = {
            "model": "claude-sonnet-4-5-20250929",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": '{"ok": true}'}],
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }
        openai = llm.anthropic_to_openai(reply, "m")
        self.assertEqual(openai["choices"][0]["message"]["content"], '{"ok": true}')
        self.assertEqual(llm.extract_text(openai), '{"ok": true}')
        self.assertEqual(openai["usage"]["total_tokens"], 18)

    def test_anthropic_headers(self) -> None:
        headers = llm._auth_headers("k", llm.AUTH_ANTHROPIC, "browser-ua")
        self.assertEqual(headers["x-api-key"], "k")
        self.assertEqual(headers["anthropic-version"], llm.ANTHROPIC_VERSION)
        self.assertNotIn("Authorization", headers)

        bearer = llm._auth_headers("k", llm.AUTH_BEARER, "cline/2.0.0")
        self.assertEqual(bearer["Authorization"], "Bearer k")
        self.assertEqual(bearer["User-Agent"], "cline/2.0.0")
        self.assertNotIn("x-api-key", bearer)


# ---------------------------------------------------------------------------
# R1/R2 — failover walks the chain and stops at the first working route
# ---------------------------------------------------------------------------


class FailoverTests(RouteTestCase):
    def test_a_503ing_route_is_skipped_and_the_next_serves_the_model(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        # the live case: ar-worker + jw-worker 503 for deepseek-v4-flash
        self.fake.behaviour[("ar-worker", PROPOSER)] = "rate"
        self.fake.behaviour[("jw-worker", PROPOSER)] = "rate"

        result = chat_once(router)

        self.assertEqual(result.provider, "agentrouter")
        self.assertEqual(
            self.fake.calls,
            [("agentrouter", PROPOSER)],
            "ar-worker and jw-worker must not be called again while cooling down",
        )
        self.assertFalse(router.halted)

    def test_failover_reaches_the_fourth_route_when_the_first_three_fail(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        for name in ("agentrouter", "ar-worker", "jw-worker"):
            self.fake.behaviour[(name, PROPOSER)] = "rate"

        result = chat_once(router)

        self.assertEqual(result.provider, "justwoker")
        self.assertEqual(
            self.fake.calls,
            [
                ("agentrouter", PROPOSER),
                ("ar-worker", PROPOSER),
                ("jw-worker", PROPOSER),
                ("justwoker", PROPOSER),
            ],
        )

    def test_failed_routes_are_not_retried_every_item(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.fake.behaviour[("agentrouter", PROPOSER)] = "rate"

        for _ in range(3):
            self.assertEqual(chat_once(router).provider, "ar-worker")

        # only the first request pays the failover cost: once tripped, the
        # exhausted route is skipped instead of being hammered once per item
        self.assertEqual(self.fake.calls_for("agentrouter", PROPOSER), 1)
        self.assertEqual(self.fake.calls_for("ar-worker", PROPOSER), 3)
        self.assertFalse(router.halted)

    def test_route_is_probed_once_after_the_cooldown(self) -> None:
        clock = {"t": 1000.0}
        settings = make_settings(provider_cooldown_seconds=30, provider_cooldown_max_seconds=120)
        with mock.patch.object(router_mod, "monotonic", side_effect=lambda: clock["t"]):
            router = router_mod.Router(settings)
            self.fake.behaviour[("agentrouter", PROPOSER)] = "rate"
            chat_once(router)  # trips agentrouter, ar-worker serves
            self.assertEqual(self.fake.calls_for("agentrouter", PROPOSER), 1)

            self.fake.behaviour[("agentrouter", PROPOSER)] = "ok"
            clock["t"] += 5.0
            chat_once(router)  # still cooling down -> skipped, no call
            self.assertEqual(self.fake.calls_for("agentrouter", PROPOSER), 1)

            clock["t"] += 30.0
            result = chat_once(router)  # cooldown elapsed -> single probe succeeds
            self.assertEqual(result.provider, "agentrouter")
            self.assertEqual(self.fake.calls_for("agentrouter", PROPOSER), 2)
            self.assertEqual(router.route_state("agentrouter", PROPOSER).trips, 0)

    def test_a_dead_route_is_dropped_after_the_error_strike_limit(self) -> None:
        router = router_mod.Router(make_settings(provider_error_strike_limit=2))
        self.fake.behaviour[("agentrouter", PROPOSER)] = "forbidden"

        for _ in range(2):
            chat_once(router)
        self.assertEqual(self.fake.calls_for("agentrouter", PROPOSER), 2)
        state = router.route_state("agentrouter", PROPOSER)
        self.assertGreater(state.cooldown_seconds, 0)
        self.assertEqual(state.last_reason[:21], "2 consecutive errors:")

        chat_once(router)  # breaker open -> no third call
        self.assertEqual(self.fake.calls_for("agentrouter", PROPOSER), 2)

    def test_broken_routes_do_not_disable_healthy_ones_for_other_models(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.fake.behaviour[("agentrouter", PROPOSER)] = "forbidden"
        self.fake.behaviour[("agentrouter", CRITIC)] = "forbidden"

        first = chat_once(router, model=PROPOSER)
        second = chat_once(router, model=CRITIC)

        self.assertEqual(first.provider, "ar-worker")
        self.assertEqual(second.provider, "ar-worker")
        self.assertEqual(router.stats.per_route["agentrouter/" + PROPOSER]["fail"], 1)
        self.assertEqual(router.stats.per_route["agentrouter/" + CRITIC]["fail"], 1)


# ---------------------------------------------------------------------------
# R2 — the breaker is keyed by (provider, model)
# ---------------------------------------------------------------------------


class RouteKeyedBreakerTests(RouteTestCase):
    def test_jw_worker_failure_for_one_model_keeps_it_for_the_other(self) -> None:
        """The live case: jw-rotator 503s deepseek-v4-flash but serves gpt-5.6-sol."""

        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        for name in ("agentrouter", "ar-worker"):
            self.fake.behaviour[(name, PROPOSER)] = "rate"
            self.fake.behaviour[(name, CRITIC)] = "rate"
        self.fake.behaviour[("jw-worker", PROPOSER)] = "rate"  # 503 for deepseek
        self.fake.behaviour[("jw-worker", CRITIC)] = "ok"  # ... but fine for gpt-5.6-sol

        self.assertEqual(chat_once(router, model=PROPOSER).provider, "justwoker")
        self.assertTrue(router.route_state("jw-worker", PROPOSER).cooling_down())

        critic = chat_once(router, model=CRITIC)

        self.assertEqual(critic.provider, "jw-worker")
        self.assertFalse(router.route_state("jw-worker", CRITIC).cooling_down())
        self.assertEqual(router.route_state("jw-worker", CRITIC).trips, 0)

    def test_route_report_is_keyed_by_provider_and_model(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.fake.behaviour[("agentrouter", PROPOSER)] = "rate"
        self.fake.behaviour[("ar-worker", PROPOSER)] = "rate"
        self.assertEqual(chat_once(router).provider, "jw-worker")

        report = router.route_report()
        failing = report[f"ar-worker/{PROPOSER}"]
        self.assertEqual(failing["state"], "open")
        self.assertEqual(failing["model"], PROPOSER)
        self.assertEqual(failing["provider"], "ar-worker")
        self.assertEqual(failing["trips"], 1)
        # the same provider was never asked for the other model, so its route
        # exists (and is healthy) without carrying a cooldown
        self.assertNotIn(f"ar-worker/{CRITIC}", report)
        self.assertTrue(router.provider_report()[f"ar-worker/{CRITIC}"]["healthy"])
        self.assertEqual(router.route_state("ar-worker", CRITIC).trips, 0)

    def test_provider_report_covers_every_configured_route(self) -> None:
        router = router_mod.Router(make_settings())
        report = router.provider_report()
        for model in (PROPOSER, CRITIC):
            for name in ("agentrouter", "justwoker", "ar-worker", "jw-worker"):
                key = f"{name}/{model}"
                self.assertIn(key, report)
                self.assertTrue(report[key]["configured"], key)
                self.assertTrue(report[key]["healthy"], key)


# ---------------------------------------------------------------------------
# R2 — GLOBAL HALT only when no (provider, model) route is healthy
# ---------------------------------------------------------------------------


class HaltTests(RouteTestCase):
    def test_every_route_down_halts_the_model(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        for name in ("agentrouter", "justwoker", "ar-worker", "jw-worker"):
            self.fake.behaviour[(name, PROPOSER)] = "rate"

        with self.assertRaises(GlobalHalt) as caught:
            chat_once(router)

        self.assertTrue(router.halted)
        self.assertIn("no healthy route", str(caught.exception))
        self.assertEqual(router.stats.rate_limit_signals, 4)

    def test_halted_router_refuses_further_requests(self) -> None:
        router = router_mod.Router(make_settings())
        for name in ("agentrouter", "justwoker", "ar-worker", "jw-worker"):
            self.fake.behaviour[(name, PROPOSER)] = "rate"
        with self.assertRaises(GlobalHalt):
            chat_once(router)

        calls_before = len(self.fake.calls)
        with self.assertRaises(GlobalHalt):
            chat_once(router)
        self.assertEqual(len(self.fake.calls), calls_before)

    def test_a_model_specific_outage_does_not_halt_the_other_model(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        for name in ("agentrouter", "justwoker", "ar-worker", "jw-worker"):
            self.fake.behaviour[(name, PROPOSER)] = "rate"
        with self.assertRaises(GlobalHalt):
            chat_once(router, model=PROPOSER)

        # the critic still has healthy routes: it must be served, not refused
        result = chat_once(router, model=CRITIC)
        self.assertEqual(result.provider, "agentrouter")

    def test_consecutive_failure_threshold_halts(self) -> None:
        # the strike limit is disabled so only the consecutive-failure counter can
        # stop the run: this is the last-resort rule, not the breaker
        router = router_mod.Router(
            make_settings(consecutive_failure_threshold=4, provider_error_strike_limit=0)
        )
        self.fake.behaviour[("", PROPOSER)] = "forbidden"

        for _ in range(3):
            with self.assertRaises(llm.LlmError):
                router.chat(
                    model=PROPOSER,
                    messages=[llm.ChatMessage("user", "hi")],
                    provider_order=("agentrouter",),
                )
        self.assertFalse(router.halted)

        with self.assertRaises(GlobalHalt):
            router.chat(
                model=PROPOSER,
                messages=[llm.ChatMessage("user", "hi")],
                provider_order=("agentrouter",),
            )
        self.assertTrue(router.halted)
        self.assertIn("consecutive failures", router.stats.halt_reason)

    def test_counter_is_per_model(self) -> None:
        router = router_mod.Router(make_settings(consecutive_failure_threshold=3))
        self.fake.behaviour[("", PROPOSER)] = "forbidden"
        self.fake.behaviour[("", CRITIC)] = "forbidden"

        for _ in range(2):
            for model in (PROPOSER, CRITIC):
                with self.assertRaises(llm.LlmError):
                    router.chat(
                        model=model,
                        messages=[llm.ChatMessage("user", "hi")],
                        provider_order=("agentrouter",),
                    )
        self.assertFalse(router.halted, "per-model counters must not add up")
        self.assertEqual(router.stats.consecutive_by_model[PROPOSER], 2)
        self.assertEqual(router.stats.consecutive_by_model[CRITIC], 2)

    def test_success_resets_the_consecutive_failure_counter(self) -> None:
        router = router_mod.Router(
            make_settings(consecutive_failure_threshold=4, provider_error_strike_limit=0)
        )
        self.fake.behaviour[("", PROPOSER)] = "forbidden"

        def call_once() -> None:
            router.chat(
                model=PROPOSER,
                messages=[llm.ChatMessage("user", "hi")],
                provider_order=("agentrouter",),
            )

        for _ in range(3):
            with self.assertRaises(llm.LlmError):
                call_once()
        self.fake.behaviour[("", PROPOSER)] = "ok"
        call_once()
        self.assertEqual(router.stats.consecutive_by_model[PROPOSER], 0)

        self.fake.behaviour[("", PROPOSER)] = "forbidden"
        for _ in range(3):
            with self.assertRaises(llm.LlmError):
                call_once()
        self.assertFalse(router.halted)

    def test_failures_absorbed_by_a_healthy_route_never_halt(self) -> None:
        router = router_mod.Router(make_settings(consecutive_failure_threshold=4))
        self.fake.behaviour[("agentrouter", PROPOSER)] = "forbidden"

        for _ in range(6):
            self.assertEqual(chat_once(router).provider, "ar-worker")

        self.assertFalse(router.halted)
        self.assertLess(router.stats.consecutive_by_model.get(PROPOSER, 0), 4)

    def test_rate_limit_without_trip_keeps_the_route_in_rotation(self) -> None:
        router = router_mod.Router(make_settings(halt_on_any_rate_limit_signal=False))
        self.fake.behaviour[("agentrouter", PROPOSER)] = "rate"

        chat_once(router)

        self.assertFalse(router.route_state("agentrouter", PROPOSER).cooling_down())
        self.assertEqual(router.stats.rate_limit_signals, 1)
        self.assertEqual(
            self.fake.calls,
            [("agentrouter", PROPOSER), ("ar-worker", PROPOSER)],
        )

    def test_no_keys_raises_without_halting(self) -> None:
        router = router_mod.Router(make_settings())
        with mock.patch.object(llm, "provider_keys", return_value={}):
            with self.assertRaises(llm.LlmError) as caught:
                chat_once(router)
        self.assertIn("no provider credentials", str(caught.exception))
        self.assertFalse(router.halted)

    def test_model_availability_helpers(self) -> None:
        router = router_mod.Router(make_settings(provider_cooldown_seconds=300))
        self.assertTrue(router.model_available(PROPOSER))
        self.assertEqual(
            router.models_available([PROPOSER, CRITIC]), {PROPOSER: True, CRITIC: True}
        )

        for name in ("agentrouter", "justwoker", "ar-worker", "jw-worker"):
            self.fake.behaviour[(name, PROPOSER)] = "rate"
        with self.assertRaises(GlobalHalt):
            chat_once(router, model=PROPOSER)

        self.assertFalse(router.model_available(PROPOSER))
        self.assertTrue(router.model_available(CRITIC))


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
                base_url="https://agentrouter.org/v1",
                api_key="sk-test",
                model=PROPOSER,
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
                    base_url="https://agentrouter.org/v1",
                    api_key="sk-test",
                    model=PROPOSER,
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
                    model=PROPOSER,
                    messages=[llm.ChatMessage("user", "hi")],
                )

    def test_anthropic_reply_is_parsed_end_to_end(self) -> None:
        body = json.dumps(
            {
                "model": "claude-sonnet-4-5-20250929",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            }
        )
        with mock.patch.object(llm, "_post_chat", return_value=(200, body)) as post:
            result = llm.chat_completion(
                base_url="https://api.justwoker.icu/v1",
                api_key="sk-test",
                model="claude-sonnet-4-5-20250929",
                messages=[llm.ChatMessage("user", "hi")],
                auth_style=llm.AUTH_ANTHROPIC,
                path="/messages",
                user_agent_value="browser-ua",
                max_tokens=64,
            )

        self.assertEqual(result.text, "ok")
        sent = post.call_args.kwargs
        self.assertEqual(sent["payload"]["max_tokens"], 64)
        self.assertNotIn("response_format", sent["payload"])
        self.assertEqual(sent["auth_style"], "anthropic")

    def test_a_403_waf_answer_is_not_a_rate_limit(self) -> None:
        with mock.patch.object(llm, "_post_chat", return_value=(403, "")):
            with self.assertRaises(llm.LlmError) as caught:
                llm.chat_completion(
                    base_url="https://api.justwoker.icu/v1",
                    api_key="sk-test",
                    model="claude-sonnet-4-5-20250929",
                    messages=[llm.ChatMessage("user", "hi")],
                    auth_style=llm.AUTH_ANTHROPIC,
                    path="/messages",
                )
        self.assertNotIsInstance(caught.exception, llm.LlmRateLimited)

    def test_key_material_never_reaches_the_error_message(self) -> None:
        secret = "sk-supersecret-value-1234"
        with mock.patch.object(llm, "_post_chat", return_value=(500, "boom")):
            try:
                llm.chat_completion(
                    base_url="https://agentrouter.org/v1",
                    api_key=secret,
                    model=PROPOSER,
                    messages=[llm.ChatMessage("user", "hi")],
                )
            except llm.LlmError as exc:
                self.assertNotIn(secret, str(exc))
            else:  # pragma: no cover - the call must raise
                self.fail("expected LlmError")


if __name__ == "__main__":
    unittest.main()
