"""Every ``AnthropicProvider`` in a process shares one SDK client per timeout (ledger I-044).

Parallel eval trials build one harness each. If every harness built its own client, each
would refresh an expired login-profile token on its own; refresh tokens rotate, so only one
grant can win and the rest fail with ``invalid_grant``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from scio.harness.providers import anthropic as mod


class _StubClient:
    instances = 0

    def __init__(self, **kwargs: object) -> None:
        type(self).instances += 1
        self.kwargs = kwargs


@pytest.fixture
def stub_sdk(monkeypatch: pytest.MonkeyPatch) -> type[_StubClient]:
    _StubClient.instances = 0
    monkeypatch.setattr(mod.anthropic, "Anthropic", _StubClient)
    monkeypatch.setattr(mod, "_shared_clients", {})
    return _StubClient


def test_same_timeout_shares_one_client(stub_sdk: type[_StubClient]) -> None:
    a = mod.AnthropicProvider(timeout_s=30.0)
    b = mod.AnthropicProvider(timeout_s=30.0)
    assert a._client is b._client
    assert stub_sdk.instances == 1
    assert a._client.kwargs == {"timeout": 30.0, "max_retries": 0}  # type: ignore[attr-defined]


def test_different_timeout_gets_its_own_client(stub_sdk: type[_StubClient]) -> None:
    a = mod.AnthropicProvider(timeout_s=30.0)
    b = mod.AnthropicProvider(timeout_s=60.0)
    assert a._client is not b._client
    assert stub_sdk.instances == 2


def test_injected_client_is_not_shared(stub_sdk: type[_StubClient]) -> None:
    own = object()
    p = mod.AnthropicProvider(client=own)  # type: ignore[arg-type]
    assert p._client is own
    assert stub_sdk.instances == 0
    assert mod.AnthropicProvider(timeout_s=5.0)._client is not own


def test_concurrent_construction_builds_one_client(stub_sdk: type[_StubClient]) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        providers = list(pool.map(lambda _: mod.AnthropicProvider(timeout_s=10.0), range(32)))
    assert len({id(p._client) for p in providers}) == 1
    assert stub_sdk.instances == 1
