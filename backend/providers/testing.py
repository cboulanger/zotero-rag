"""Test kit for backend plugins that provide inference providers.

Shipped with core so a plugin's own tests (``<plugin>/tests/``) need nothing from
``backend/tests``:

- ``FakeHTTP`` / ``FakeResponse`` / ``DeadHTTP`` stand in for the blocking
  ``httpx.Client`` a provider uses for its management API (assign one to
  ``provider._http_client``);
- ``make_preset(**overrides)`` builds a minimal two-sided remote preset;
- ``ProviderContractSuite`` is the contract every provider must pass, configured
  by a ``ContractFixture``.
"""

import json
import json as _json
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional

from backend.config.presets import HardwarePreset
from backend.providers import (
    PROVIDERS,
    SIDES,
    Credentials,
    ProviderConfigError,
    ProvisionContext,
    discover_providers,
    get_providers,
)


class FakeResponse:
    """Minimal ``httpx.Response`` stand-in."""

    def __init__(self, status_code: int = 200, body: Any = None, headers: Optional[dict] = None, text: Optional[str] = None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        if text is not None:
            self.text = text
        elif body is None:
            self.text = ""
        elif isinstance(body, (dict, list)):
            self.text = _json.dumps(body)
        else:
            self.text = str(body)

    def json(self) -> Any:
        if self._body is None:
            raise _json.JSONDecodeError("Expecting value", "", 0)
        return self._body

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class FakeHTTP:
    """Routes ``(method, url-regex)`` to canned responses and records every call.

    ``routes`` maps ``(METHOD, pattern)`` to a ``FakeResponse``, a list of them
    (consumed in order, the last one repeating), an ``Exception`` to raise, or a
    callable ``(method, url, json) -> FakeResponse``. An unmatched call raises
    ``AssertionError`` so a test never silently passes on a wrong request.
    """

    def __init__(self, routes: Optional[dict] = None):
        self.routes: dict = dict(routes or {})
        self.calls: list[tuple[str, str, Any]] = []

    def add(self, method: str, pattern: str, response: Any) -> None:
        self.routes[(method.upper(), pattern)] = response

    def request(self, method: str, url: str, headers: Optional[dict] = None, json: Any = None, **kwargs) -> FakeResponse:
        self.calls.append((method.upper(), url, json))
        for (m, pattern), response in self.routes.items():
            if m == method.upper() and re.search(pattern, url):
                if isinstance(response, list):
                    item = response.pop(0) if len(response) > 1 else response[0]
                else:
                    item = response
                if isinstance(item, Exception):
                    raise item
                if callable(item):
                    return item(method, url, json)
                return item
        raise AssertionError(f"Unexpected request: {method} {url}")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def put(self, url, **kw):
        return self.request("PUT", url, **kw)

    def patch(self, url, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    def calls_matching(self, method: str, pattern: str) -> list:
        return [c for c in self.calls if c[0] == method.upper() and re.search(pattern, c[1])]


class DeadHTTP:
    """A client for which every request fails, to check that nothing raises."""

    def request(self, *a, **kw):
        raise ConnectionError("network is down (test)")

    get = post = put = patch = delete = request


@dataclass
class ContractFixture:
    """How the generic contract suite exercises one provider."""

    provider_id: str
    #: ``model_kwargs`` for a remote side using this provider.
    model_kwargs: dict = field(default_factory=lambda: {"api_key_env": "CONTRACT_KEY"})
    valid_options: list[dict] = field(default_factory=lambda: [{}])
    invalid_options: list[dict] = field(default_factory=list)
    #: ``(headers, minimum number of meters)`` samples for ``parse_usage``.
    usage_samples: list[tuple[dict, int]] = field(default_factory=list)
    #: Called with the provider; installs a client whose every request fails.
    break_network: Callable[[Any], None] = lambda provider: setattr(provider, "_http_client", DeadHTTP())
    #: Wraps a test so any module-level HTTP use is neutralised (default: none).
    patch_http: Callable[[], ContextManager] = nullcontext


def make_preset(**overrides) -> HardwarePreset:
    data = {
        "name": "t",
        "description": "t",
        "version": 2,
        "embedding": {"model_type": "remote", "model_name": "m", "model_kwargs": {"api_key_env": "K"}},
        "llm": {"model_type": "remote", "model_names": ["m"], "model_kwargs": {"api_key_env": "K"}},
        "rag": {},
        "memory_budget_gb": 1.0,
    }
    for key, value in overrides.items():
        side, _, field = key.partition("__")
        if field:
            data[side][field] = value
        else:
            data[key] = value
    return HardwarePreset.model_validate(data)


def preset_with(provider_id: str, side: str = "llm", scope=None, kwargs=None) -> HardwarePreset:
    """``make_preset()`` with ``provider_id`` on ``side`` (the other side stays generic)."""
    preset = make_preset()
    cfg = preset.llm if side == "llm" else preset.embedding
    cfg.provider.id = provider_id
    cfg.provider.scope = scope
    cfg.model_kwargs = kwargs if kwargs is not None else {"api_key_env": "SIDE_KEY"}
    other = preset.embedding if side == "llm" else preset.llm
    other.model_kwargs = {"api_key_env": "OTHER_KEY"}
    return preset


def build_contract_preset(provider_id: str, side: str, fixture, options: dict, scope=None) -> HardwarePreset:
    """A preset where ``side`` uses the provider and the other side is generic."""
    def remote(env: str, side_scope: str) -> dict:
        # The key kind must agree with the scope (user: own key; else admin-set).
        kwargs = {k: v for k, v in fixture.model_kwargs.items() if k not in ("api_key_env", "shared_api_key_env")}
        kwargs["api_key_env" if side_scope == "user" else "shared_api_key_env"] = env
        return kwargs

    # A remote re-ranker always runs on the user's own account.
    own_scope = scope or ("user" if side == "rerank" else PROVIDERS[provider_id].default_scope)
    emb_scope = own_scope if side == "embedding" else "user"
    llm_scope = own_scope if side == "llm" else "user"
    emb = {"model_type": "remote", "model_name": "m", "model_kwargs": remote("CONTRACT_EMB_KEY", emb_scope)}
    llm = {"model_type": "remote", "model_names": ["m"], "model_kwargs": remote("CONTRACT_LLM_KEY", llm_scope)}
    provider_block = {"id": provider_id, "options": options}
    if scope or side == "rerank":
        provider_block["scope"] = own_scope
    data = {
        "name": "contract", "description": "c", "version": 2,
        "embedding": emb, "llm": llm, "rag": {}, "memory_budget_gb": 1.0,
    }
    if side == "rerank":
        data["rerank"] = {"model_type": "remote", "model_name": "m",
                          "model_kwargs": remote("CONTRACT_RERANK_KEY", own_scope), "provider": provider_block}
    else:
        (emb if side == "embedding" else llm)["provider"] = provider_block
    return HardwarePreset.model_validate(data)


class ProviderContractSuite:
    """Mixin with the contract every provider must pass; combine with ``unittest.TestCase``.

    Set ``fixture`` to the provider's :class:`ContractFixture`::

        class TestContract(ProviderContractSuite, unittest.TestCase):
            fixture = ContractFixture(provider_id="acme", ...)
    """

    fixture: ContractFixture
    def each(self):
        discover_providers()
        pid = self.fixture.provider_id
        for side in sorted(PROVIDERS[pid].supported_sides):
            with self.subTest(provider=pid, side=side):
                yield pid, side, self.fixture

    def provider_for(self, pid, side, fixture, options=None):
        preset = build_contract_preset(pid, side, fixture, options if options is not None else fixture.valid_options[0])
        return get_providers(preset)[side], preset

    def test_valid_options_load_and_invalid_options_are_rejected(self):
        for pid, side, fixture in self.each():
            for options in fixture.valid_options:
                get_providers(build_contract_preset(pid, side, fixture, options))
            for options in fixture.invalid_options:
                with self.assertRaises(ProviderConfigError):
                    get_providers(build_contract_preset(pid, side, fixture, options))

    def test_unsupported_sides_are_rejected(self):
        discover_providers()
        for pid in (self.fixture.provider_id,):
            fixture = self.fixture
            for side in SIDES:
                if side in PROVIDERS[pid].supported_sides:
                    continue
                with self.subTest(provider=pid, side=side):
                    with self.assertRaises(ProviderConfigError):
                        get_providers(build_contract_preset(pid, side, fixture, fixture.valid_options[0]))

    def test_scopes_outside_key_scopes_are_rejected_and_inside_accepted(self):
        for pid, side, fixture in self.each():
            cls = PROVIDERS[pid]
            for scope in ("user", "shared", "managed"):
                preset = build_contract_preset(pid, side, fixture, fixture.valid_options[0], scope=scope)
                if scope in cls.key_scopes and (side != "rerank" or scope == "user"):
                    self.assertEqual(get_providers(preset)[side].scope, scope)
                else:
                    with self.assertRaises(ProviderConfigError):
                        get_providers(preset)

    def test_default_scope_is_an_allowed_scope(self):
        discover_providers()
        cls = PROVIDERS[self.fixture.provider_id]
        self.assertIn(cls.default_scope, cls.key_scopes, self.fixture.provider_id)

    def test_apply_defaults_is_idempotent_and_keeps_explicit_values(self):
        for pid, side, fixture in self.each():
            provider, preset = self.provider_for(pid, side, fixture)
            cfg = provider.side_config
            cfg.model_kwargs["api_key_pattern"] = "^explicit$"
            cfg.model_kwargs["shared_api_key_pattern"] = "^explicit$"
            provider.apply_defaults()
            once = json.dumps(cfg.model_kwargs, sort_keys=True)
            provider.apply_defaults()
            self.assertEqual(json.dumps(cfg.model_kwargs, sort_keys=True), once)
            self.assertEqual(cfg.model_kwargs["api_key_pattern"], "^explicit$")
            self.assertEqual(cfg.model_kwargs["shared_api_key_pattern"], "^explicit$")

    def test_describe_is_well_formed_and_holds_no_secret(self):
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            d = provider.describe()
            self.assertEqual(d.id, pid)
            self.assertTrue(d.label)
            self.assertIn(d.key_scope, ("user", "shared", "managed"))
            self.assertEqual(d.supports_provisioning, provider.supports_provisioning)
            self.assertEqual(d.supports_suspend, provider.supports_suspend)
            # A login form names its password field (it holds no value), so it is checked apart.
            blob = d.model_dump_json(exclude={"provisioning": {"login"}}).lower()
            if d.provisioning is not None and d.provisioning.login is not None:
                self.assertNotIn("bearer ", d.provisioning.login.model_dump_json().lower())
            for forbidden in ("bearer ", "secret", "password"):
                self.assertNotIn(forbidden, blob)
            if provider.supports_provisioning:
                self.assertIsNotNone(d.provisioning, "a provisioning provider must describe its credential and hint")

    def test_health_never_raises_even_when_the_network_is_down(self):
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            fixture.break_network(provider)
            with fixture.patch_http():
                result = provider.health(Credentials(api_key="k", base_url="https://example.invalid/v1"))
                provider.health(Credentials(api_key=None, base_url=None))
            self.assertTrue(result is None or result.status in ("ready", "cold", "throttled", "paused", "unreachable"))

    def test_endpoint_url_and_live_models_never_raise(self):
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            fixture.break_network(provider)
            with fixture.patch_http():
                url = provider.endpoint_url("k")
                models = provider.live_models("https://example.invalid/v1", "k")
            self.assertTrue(url is None or isinstance(url, str))
            self.assertTrue(models is None or isinstance(models, list))

    def test_parse_usage_never_raises_and_is_well_formed(self):
        garbage = [{}, None, {"x-ratelimit-limit-requests": "x"}, {"ratelimit-limit": "-1"}, {"": ""}]
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            for headers in garbage:
                self.assertIsInstance(provider.parse_usage(headers or {}), list)
            self.assertEqual(provider.parse_usage({"content-type": "application/json"}), [])
            for headers, at_least in fixture.usage_samples:
                meters = provider.parse_usage(headers)
                self.assertGreaterEqual(len(meters), at_least)
                for m in meters:
                    self.assertEqual(m.side, side)
                    self.assertGreaterEqual(m.limit, 1)
                    self.assertGreaterEqual(m.remaining, 0)
                    self.assertLessEqual(m.remaining, m.limit)

    def test_unsupported_capabilities_raise_not_implemented(self):
        for pid, side, fixture in self.each():
            provider, preset = self.provider_for(pid, side, fixture)
            ctx = ProvisionContext(side=side, preset=preset, credential="k", data_path=__import__("pathlib").Path("."))
            if not provider.supports_provisioning:
                with self.assertRaises(NotImplementedError):
                    provider.provision(ctx, lambda msg: None)
                with self.assertRaises(NotImplementedError):
                    provider.teardown(ctx)
            if not provider.supports_suspend:
                with self.assertRaises(NotImplementedError):
                    provider.suspend(ctx, lambda msg: None)

    def test_suspend_capable_providers_never_raise_from_is_paused_and_fail_cleanly_without_a_network(self):
        from backend.providers import ProvisionError
        for pid, side, fixture in self.each():
            provider, preset = self.provider_for(pid, side, fixture)
            fixture.break_network(provider)
            with fixture.patch_http():
                if not provider.supports_suspend:
                    self.assertFalse(provider.is_paused("k"))  # the default: nothing to pause
                    continue
                self.assertFalse(provider.is_paused("k"))      # unknown state counts as not paused
                ctx = ProvisionContext(side=side, preset=preset, credential="k", data_path=__import__("pathlib").Path("."))
                with self.assertRaises(Exception) as caught:   # a clear failure, never a hang or NotImplementedError
                    provider.suspend(ctx, lambda msg: None)
                self.assertNotIsInstance(caught.exception, NotImplementedError)

    def test_suspend_capable_providers_are_also_provisionable_so_resume_needs_no_extra_code(self):
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            if provider.supports_suspend:
                self.assertTrue(provider.supports_provisioning, pid)
