"""Test doubles and the per-provider contract fixtures for the provider layer.

``FakeHTTP`` stands in for the blocking ``httpx.Client`` a provider uses for its
management API (assign it to ``provider._http_client``). ``ContractFixture``
tells the generic contract suite (test_provider_contract.py) how to exercise one
provider; adding a provider without registering a fixture here fails that suite.
"""

import json as _json
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Optional


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


CONTRACT_FIXTURES: dict[str, ContractFixture] = {}


def register_fixture(fixture: ContractFixture) -> ContractFixture:
    CONTRACT_FIXTURES[fixture.provider_id] = fixture
    return fixture


register_fixture(ContractFixture(
    provider_id="generic",
    valid_options=[{}],
    invalid_options=[{"anything": 1}],
    usage_samples=[
        ({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "5"}, 1),
        ({"RateLimit-Limit": "100", "RateLimit-Remaining": "50"}, 1),
    ],
))


register_fixture(ContractFixture(
    provider_id="kisski",
    model_kwargs={"api_key_env": "KISSKI_API_KEY", "base_url": "https://chat-ai.academiccloud.de/v1"},
    valid_options=[{}, {"models_url": "https://example.invalid/models"}],
    invalid_options=[{"models_urll": "x"}],
    usage_samples=[
        ({"x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40"}, 1),
        ({"x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40",
          "x-ratelimit-limit-day": "1000", "x-ratelimit-remaining-day": "10"}, 2),
    ],
))

register_fixture(ContractFixture(
    provider_id="openai",
    model_kwargs={"api_key_env": "OPENAI_API_KEY"},
    invalid_options=[{"anything": 1}],
    usage_samples=[({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "9"}, 1)],
))

register_fixture(ContractFixture(
    provider_id="anthropic",
    model_kwargs={"api_key_env": "ANTHROPIC_API_KEY"},
    invalid_options=[{"anything": 1}],
))

register_fixture(ContractFixture(
    provider_id="mpcdf",
    model_kwargs={"shared_api_key_env": "MPCDF_KEY", "shared_base_url_env": "MPCDF_URL"},
    invalid_options=[{"anything": 1}],
    usage_samples=[({"RateLimit-Limit": "100", "RateLimit-Remaining": "50"}, 1)],
))
