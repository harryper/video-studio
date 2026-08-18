"""Provider contract tests for Content Studio.

These tests pin the boundary between Content Studio and external model /
search APIs. They run offline: the anthropic client is a hand-rolled
``FakeAnthropic`` and the httpx client is replaced with a stub transport.

Tests included (per the Task 4 brief):

* ``test_fake_provider_returns_operation_fixture`` — verbatim from brief.
* ``test_provider_repairs_format_only_once`` — verbatim from brief.
* ``test_fake_provider_raises_when_responses_exhausted``
* ``test_anthropic_provider_parses_valid_response_first_try``
* ``test_anthropic_provider_redacts_auth_in_errors``
* ``test_search_provider_normalizes_http_response``
* ``test_search_provider_handles_empty_results``
* ``test_provider_raises_when_repair_still_fails`` (extra: confirms the
  "repair only once" contract by raising on the second failed parse).
* ``test_search_provider_redacts_token_in_error_logs`` (extra: confirms the
  search adapter also keeps secrets out of error logs).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from studio.config import Settings
from studio.providers import (
    AnthropicProvider,
    FakeModelProvider,
    HttpSearchProvider,
    ModelProviderError,
)
from studio.schemas import SourceDocument, TopicDiagnosis

# ---------------------------------------------------------------------------
# FakeModelProvider fixtures
# ---------------------------------------------------------------------------


def _topic_diagnosis(core_question: str = "测试问题") -> TopicDiagnosis:
    """Build a valid ``TopicDiagnosis`` with all six fields populated.

    Task 5 extended ``TopicDiagnosis`` from a single ``core_question``
    stub to the full diagnostic shape used by ``studio.content``.
    Provider tests that pre-date Task 5 need a complete instance to
    construct FakeModelProvider fixtures; this helper centralises the
    boilerplate so the existing tests don't need to copy the other
    five field values at every site.
    """

    return TopicDiagnosis(
        core_question=core_question,
        audience_prior_knowledge="普通观众",
        central_tension="核心张力",
        misconceptions=["常见误解"],
        scope=["子主题 A"],
        excluded_topics=["排除 A"],
    )


@pytest.fixture
def fake_provider() -> FakeModelProvider:
    return FakeModelProvider({"diagnosis": [_topic_diagnosis()]})


# ---------------------------------------------------------------------------
# FakeAnthropic stub (a hand-rolled double — not a network client)
# ---------------------------------------------------------------------------


@dataclass
class _CallRecord:
    kwargs: dict[str, Any]
    metadata: dict[str, str] = field(default_factory=dict)


class FakeAnthropic:
    """Duck-typed stand-in for ``anthropic.Anthropic``.

    Each entry in ``responses`` is returned in order; the corresponding call
    kwargs are recorded on ``self.calls``. Raises if more calls are made than
    responses were queued — the test will see a clear IndexError rather than
    silently re-using the last response.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.calls: list[_CallRecord] = []

    @property
    def messages(self) -> FakeAnthropic:
        return self

    def create(self, **kwargs: Any) -> Any:
        record = _CallRecord(kwargs=kwargs)
        meta = kwargs.get("metadata") or {}
        record.metadata = dict(meta)
        self.calls.append(record)
        self.call_count += 1
        if self.call_count > len(self._responses):
            raise IndexError(
                f"FakeAnthropic exhausted: only {len(self._responses)} responses "
                f"prepared for {self.call_count} calls"
            )
        return self._responses[self.call_count - 1]


class _Message:
    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


def _message(text: str) -> _Message:
    return _Message(text)


def _diagnosis_payload(core_question: str = "ok") -> str:
    """Serialise a complete 6-field ``TopicDiagnosis`` payload."""

    return json.dumps(
        {
            "core_question": core_question,
            "audience_prior_knowledge": "普通观众",
            "central_tension": "核心张力",
            "misconceptions": ["常见误解"],
            "scope": ["子主题 A"],
            "excluded_topics": ["排除 A"],
        }
    )


@pytest.fixture
def broken_client() -> FakeAnthropic:
    """First response invalid JSON; second response valid JSON."""

    return FakeAnthropic(
        [
            _message("not json at all"),
            _message(_diagnosis_payload("fixed")),
        ]
    )


# ---------------------------------------------------------------------------
# httpx stub transport
# ---------------------------------------------------------------------------


class _StubTransport(httpx.BaseTransport):
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


# ===========================================================================
# FakeModelProvider
# ===========================================================================


def test_fake_provider_returns_operation_fixture(
    fake_provider: FakeModelProvider,
) -> None:
    """Brief body — verbatim."""

    result = fake_provider.generate(
        TopicDiagnosis, "system", "topic", operation="diagnosis"
    )
    assert result.core_question == "测试问题"


def test_fake_provider_raises_when_responses_exhausted(
    fake_provider: FakeModelProvider,
) -> None:
    """Once the queue for an operation is empty, raise ``ModelProviderError``.

    The error message names the operation so operators can spot the gap.
    """

    fake_provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    with pytest.raises(ModelProviderError, match="diagnosis"):
        fake_provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")


def test_fake_provider_returns_independent_copies() -> None:
    """Deep-copy the fixture so callers can mutate the result safely."""

    provider = FakeModelProvider(
        {
            "diagnosis": [
                _topic_diagnosis("first"),
                _topic_diagnosis("first"),
            ]
        }
    )
    a = provider.generate(
        TopicDiagnosis, "system", "topic", operation="diagnosis"
    )
    b = provider.generate(
        TopicDiagnosis, "system", "topic", operation="diagnosis"
    )
    assert a is not b
    assert a.model_dump() == b.model_dump()
    # Mutating ``a`` must not leak into ``b``'s backing state.
    a.core_question = "mutated"
    assert b.core_question == "first"


def test_fake_provider_queues_can_be_appended() -> None:
    """The ``queue`` helper accepts additional fixtures between calls."""

    provider = FakeModelProvider()
    provider.queue("diagnosis", _topic_diagnosis("first"))
    provider.queue("diagnosis", _topic_diagnosis("second"))
    first = provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    second = provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    assert first.core_question == "first"
    assert second.core_question == "second"


# ===========================================================================
# AnthropicProvider
# ===========================================================================


def test_anthropic_provider_parses_valid_response_first_try() -> None:
    """A well-formed first response is parsed without invoking repair."""

    client = FakeAnthropic([_message(_diagnosis_payload("ok"))])
    provider = AnthropicProvider(client=client)
    result = provider.generate(
        TopicDiagnosis, "system", "prompt", operation="diagnosis"
    )
    assert isinstance(result, TopicDiagnosis)
    assert result.core_question == "ok"
    assert client.call_count == 1


def test_anthropic_provider_strips_markdown_code_fences() -> None:
    """Live Anthropic responses wrap JSON in ``\\`\\`\\`json … \\`\\`\\```.

    The parser must accept that wrapper or every online call will fall through
    to repair and still fail.
    """

    wrapped = "```json\n" + _diagnosis_payload("ok") + "\n```"
    client = FakeAnthropic([_message(wrapped)])
    provider = AnthropicProvider(client=client)
    result = provider.generate(
        TopicDiagnosis, "system", "prompt", operation="diagnosis"
    )
    assert result.core_question == "ok"
    assert client.call_count == 1


def test_anthropic_provider_strips_code_fences_without_language_hint() -> None:
    """Some models omit the ``json`` language hint; the wrapper must still strip."""

    wrapped = "```\n" + _diagnosis_payload("ok") + "\n```"
    client = FakeAnthropic([_message(wrapped)])
    provider = AnthropicProvider(client=client)
    result = provider.generate(
        TopicDiagnosis, "system", "prompt", operation="diagnosis"
    )
    assert result.core_question == "ok"
    assert client.call_count == 1


def test_provider_repairs_format_only_once(
    broken_client: FakeAnthropic,
) -> None:
    """Brief body — verbatim.

    The adapter makes exactly two calls when the first response is invalid
    JSON; the second call carries ``mode="schema_repair"`` in its metadata.
    """

    provider = AnthropicProvider(client=broken_client)
    provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    assert broken_client.call_count == 2
    assert broken_client.calls[1].metadata["mode"] == "schema_repair"


def test_provider_raises_when_repair_still_fails() -> None:
    """If both attempts return invalid JSON, raise ``ModelProviderError``.

    The brief is explicit: do NOT retry a third time.
    """

    client = FakeAnthropic(
        [_message("nope"), _message("still nope")]
    )
    provider = AnthropicProvider(client=client)
    with pytest.raises(ModelProviderError):
        provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    assert client.call_count == 2


def test_anthropic_provider_repairs_inner_quotes_in_strings() -> None:
    """Common LLM failure: nested ASCII ``"..."`` inside a string value.

    Real Anthropic responses often contain Chinese phrases wrapped in
    ASCII double-quotes inside ``narrative_value`` etc. Without repair
    ``json.loads`` rejects the payload. The provider must accept the
    response after replacing the inner quotes with ``「」``.
    """

    # Plain JSON (no markdown fence) with two inner quotes inside a string.
    bad = (
        '{"core_question":"x",'
        '"audience_prior_knowledge":"让观众了解"糖"的历史",'
        '"central_tension":"y",'
        '"misconceptions":["m"],'
        '"scope":["s"],'
        '"excluded_topics":["e"]}'
    )
    client = FakeAnthropic([_message(bad)])
    provider = AnthropicProvider(client=client)
    result = provider.generate(
        TopicDiagnosis, "system", "prompt", operation="diagnosis"
    )
    assert result.audience_prior_knowledge == "让观众了解「糖」的历史"
    assert client.call_count == 1


def test_anthropic_provider_repair_prompt_warns_about_inner_quotes() -> None:
    """The repair call explicitly forbids nested ASCII quotes."""

    bad = (
        '{"core_question":"x",'
        '"audience_prior_knowledge":"让观众了解"糖"的历史",'
        '"central_tension":"y",'
        '"misconceptions":["m"],'
        '"scope":["s"],'
        '"excluded_topics":["e"]}'
    )
    client = FakeAnthropic(
        [_message("first try totally broken"), _message(bad)]
    )
    provider = AnthropicProvider(client=client)
    provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    repair_msg = client.calls[1].kwargs["messages"][-1]["content"]
    assert "「" in repair_msg and "」" in repair_msg


def test_anthropic_provider_redacts_auth_in_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception containing a secret must NOT appear in the log output."""

    secret = "sk-ant-secret-12345"

    class RaisingClient:
        @property
        def messages(self) -> Any:
            return self

        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError(f"auth failed with token={secret}")

    provider = AnthropicProvider(client=RaisingClient())
    # Alembic's fileConfig() disables existing loggers by default; re-enable
    # ours so caplog capture works when this test runs after test_migration.
    for name in (
        "studio.providers",
        "studio.providers.anthropic",
        "studio.providers.fake",
        "studio.providers.search",
        "studio.providers.base",
    ):
        logging.getLogger(name).disabled = False
    with (
        caplog.at_level(logging.ERROR, logger="studio.providers"),
        pytest.raises(ModelProviderError),
    ):
        provider.generate(
            TopicDiagnosis, "system", "prompt", operation="diagnosis"
        )
    # The exception's message carries the secret; the adapter must not
    # forward that message verbatim into the log.
    assert secret not in caplog.text
    # The operation name is still observable in the log so operators can
    # trace the failure to a specific stage.
    assert "diagnosis" in caplog.text


def test_anthropic_provider_passes_operation_in_metadata() -> None:
    """Every call records its ``operation`` so downstream tooling can correlate."""

    client = FakeAnthropic([_message(_diagnosis_payload("ok"))])
    provider = AnthropicProvider(client=client)
    provider.generate(TopicDiagnosis, "s", "p", operation="diagnosis")
    assert client.calls[0].metadata["operation"] == "diagnosis"


def test_from_settings_raises_when_api_key_missing() -> None:
    """``from_settings()`` fail-fasts so the operator sees the misconfig at startup.

    A worker that only fails on the first dispatched job would otherwise burn
    the lease window on every queued job before the operator noticed.
    """

    settings = Settings(anthropic_api_key="")
    with pytest.raises(ModelProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider.from_settings(settings)


def test_from_settings_reads_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``from_settings()`` reads ``STUDIO_ANTHROPIC_API_KEY`` via the Settings field."""

    monkeypatch.setenv("STUDIO_ANTHROPIC_API_KEY", "sk-test-from-settings")
    settings = Settings()
    provider = AnthropicProvider.from_settings(settings)
    # The constructed Anthropic client stores its key as ``api_key``; that's
    # the same surface the official SDK exposes, so a hand-rolled FakeAnthropic
    # that captures the kwarg can verify the wiring without a live API call.
    assert getattr(provider._client, "api_key", None) == "sk-test-from-settings"
    assert provider._model


def test_from_settings_honours_explicit_settings_override() -> None:
    """Passing a populated ``Settings`` overrides any ``STUDIO_*`` env var."""

    settings = Settings(anthropic_api_key="sk-explicit")
    provider = AnthropicProvider.from_settings(settings)
    assert getattr(provider._client, "api_key", None) == "sk-explicit"


# ===========================================================================
# HttpSearchProvider
# ===========================================================================


_SEARCH_URL = "https://search.example/search"


def _settings(**overrides: Any) -> Settings:
    return Settings(search_provider_url=_SEARCH_URL, **overrides)


def test_search_provider_normalizes_http_response() -> None:
    """Round-trip every field of ``SourceDocument`` from the HTTP payload."""

    captured_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(dict(request.headers))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Result Title",
                        "url": "https://example.com/a",
                        "snippet": "An informative snippet.",
                        "publisher": "Example",
                        "published_at": "2026-01-15T00:00:00+00:00",
                    }
                ]
            },
            request=request,
        )

    transport = _StubTransport(handler)
    client = httpx.Client(transport=transport)
    provider = HttpSearchProvider(
        settings=_settings(search_provider_token="sek"), client=client
    )
    docs = provider.search("hello", limit=5)

    assert len(docs) == 1
    doc = docs[0]
    assert isinstance(doc, SourceDocument)
    assert doc.title == "Result Title"
    assert doc.url == "https://example.com/a"
    assert doc.snippet == "An informative snippet."
    assert doc.publisher == "Example"
    assert doc.published_at is not None
    assert doc.published_at.year == 2026 and doc.published_at.month == 1
    assert doc.published_at.day == 15

    # Token travels in the Authorization header (header name is
    # case-insensitive on the wire).
    auth_values = [
        v for k, v in captured_headers[0].items() if k.lower() == "authorization"
    ]
    assert auth_values == ["Bearer sek"]

    # Query string carries ``q`` and ``limit``.
    request = transport.requests[0]
    assert "q=hello" in str(request.url)
    assert "limit=5" in str(request.url)


def test_search_provider_handles_empty_results() -> None:
    """Empty ``results`` returns an empty list — no exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []}, request=request)

    transport = _StubTransport(handler)
    client = httpx.Client(transport=transport)
    provider = HttpSearchProvider(settings=_settings(), client=client)
    docs = provider.search("nothing")
    assert docs == []


def test_search_provider_redacts_token_in_error_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bearer token must not appear in error logs even when the call fails."""

    secret = "search-token-shhh"

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"boom token={secret}")

    transport = _StubTransport(handler)
    client = httpx.Client(transport=transport)
    provider = HttpSearchProvider(
        settings=_settings(search_provider_token=secret), client=client
    )
    # Re-enable our loggers in case alembic's fileConfig() ran earlier.
    for name in (
        "studio.providers",
        "studio.providers.search",
        "studio.providers.base",
    ):
        logging.getLogger(name).disabled = False
    with caplog.at_level(logging.ERROR, logger="studio.providers"), pytest.raises(
        ModelProviderError
    ):
        provider.search("hello")

    assert secret not in caplog.text
