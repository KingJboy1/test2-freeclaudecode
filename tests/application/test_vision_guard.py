"""Tests for graceful image handling on non-vision models (vision_guard)."""

from collections.abc import AsyncIterator

import pytest

from free_claude_code.application.execution import ProviderExecutor
from free_claude_code.application.routing import ResolvedModel, RoutedMessagesRequest
from free_claude_code.application.vision_guard import (
    build_image_refusal_sse,
    image_not_supported_text,
    looks_like_image_rejection,
    modality_supports_vision,
    model_name_suggests_vision,
    request_has_image_content,
)
from free_claude_code.config.reasoning import ReasoningPreference
from free_claude_code.core.anthropic.models import MessagesRequest
from free_claude_code.core.reasoning import ReasoningPolicy


# ────────────────────────────────────────────────────────────
# request_has_image_content
# ────────────────────────────────────────────────────────────
def _request_with_content(content) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": content}]}
    )


def test_request_has_image_content_detects_image_block() -> None:
    request = _request_with_content(
        [
            {"type": "text", "text": "what is this?"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": "AAAA",
                },
            },
        ]
    )
    assert request_has_image_content(request) is True


def test_request_has_image_content_text_only_is_false() -> None:
    request = _request_with_content([{"type": "text", "text": "hello"}])
    assert request_has_image_content(request) is False


def test_request_has_image_content_plain_string_is_false() -> None:
    request = _request_with_content("just a string")
    assert request_has_image_content(request) is False


# ────────────────────────────────────────────────────────────
# capability heuristics
# ────────────────────────────────────────────────────────────
def test_model_name_suggests_vision() -> None:
    assert model_name_suggests_vision("meta/llama-3.2-11b-vision-instruct") is True
    assert model_name_suggests_vision("google/gemini-2.0-flash") is True
    assert model_name_suggests_vision("meta/llama-3.1-8b-instruct") is False
    assert model_name_suggests_vision("deepseek/deepseek-chat") is False


def test_modality_supports_vision() -> None:
    assert modality_supports_vision("text+image->text") is True
    assert modality_supports_vision("text->text") is False
    assert modality_supports_vision("text->text+image") is False
    assert modality_supports_vision(None) is None
    assert modality_supports_vision("garbage") is None


# ────────────────────────────────────────────────────────────
# friendly refusal message + SSE stream
# ────────────────────────────────────────────────────────────
def test_image_not_supported_text_guides_user_to_send_text() -> None:
    text = image_not_supported_text("open_router/meta/llama-3.1-8b")
    assert "can't read images" in text
    assert "open_router/meta/llama-3.1-8b" in text
    assert "as text" in text


def test_build_image_refusal_sse_is_a_complete_assistant_turn() -> None:
    request = _request_with_content(
        [
            {"type": "text", "text": "describe"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "BBBB"},
            },
        ]
    )
    events = build_image_refusal_sse(request)
    joined = "".join(events)
    assert "event: message_start" in joined
    assert "event: content_block_delta" in joined
    assert "event: message_stop" in joined
    assert '"stop_reason": "end_turn"' in joined
    assert "can&#x27;t" not in joined  # plain text, not HTML-escaped
    assert "can't read images" in joined


# ────────────────────────────────────────────────────────────
# looks_like_image_rejection
# ────────────────────────────────────────────────────────────
def test_looks_like_image_rejection_matches_provider_message() -> None:
    assert (
        looks_like_image_rejection(ValueError("This model does not support images"))
        is True
    )
    assert (
        looks_like_image_rejection(
            ValueError("Invalid content: image input unsupported")
        )
        is True
    )


def test_looks_like_image_rejection_ignores_unrelated_errors() -> None:
    assert looks_like_image_rejection(ValueError("rate limit exceeded")) is False
    assert looks_like_image_rejection(ValueError("upstream timeout")) is False


# ────────────────────────────────────────────────────────────
# ProviderExecutor integration
# ────────────────────────────────────────────────────────────
class VisionFakeProvider:
    def __init__(self, *, supports_vision=None, stream_error=None) -> None:
        self._supports_vision = supports_vision
        self._stream_error = stream_error
        self.stream_calls: list[MessagesRequest] = []

    def preflight_stream(self, request, *, reasoning) -> None:
        return None

    def supports_vision_for(self, model_id: str):
        return self._supports_vision

    async def stream_response(
        self,
        request,
        input_tokens=0,
        *,
        request_id=None,
        reasoning,
    ) -> AsyncIterator[str]:
        self.stream_calls.append(request)
        if self._stream_error is not None:
            raise self._stream_error
        yield "event: message_stop\ndata: {}\n\n"


def _image_routed_request() -> RoutedMessagesRequest:
    request = MessagesRequest.model_validate(
        {
            "model": "provider-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is in this image?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": "AAAA",
                            },
                        },
                    ],
                }
            ],
        }
    )
    return RoutedMessagesRequest(
        request=request,
        resolved=ResolvedModel(
            original_model="gateway-model",
            provider_id="provider",
            provider_model="provider-model",
            provider_model_ref="provider/provider-model",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
    )


def _text_routed_request() -> RoutedMessagesRequest:
    request = MessagesRequest.model_validate(
        {"model": "provider-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    return RoutedMessagesRequest(
        request=request,
        resolved=ResolvedModel(
            original_model="gateway-model",
            provider_id="provider",
            provider_model="provider-model",
            provider_model_ref="provider/provider-model",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
    )


def _executor(provider: VisionFakeProvider) -> ProviderExecutor:
    return ProviderExecutor(
        lambda _provider_id: provider,
        token_counter=lambda _m, _s, _t: 1,
    )


@pytest.mark.asyncio
async def test_executor_proactively_refuses_image_for_known_non_vision_model() -> None:
    provider = VisionFakeProvider(supports_vision=False)
    stream = _executor(provider).stream(
        _image_routed_request(),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_proactive",
    )
    chunks = [chunk async for chunk in stream]
    joined = "".join(chunks)
    assert "can't read images" in joined
    assert "as text" in joined
    # The provider must NOT have been called.
    assert provider.stream_calls == []


@pytest.mark.asyncio
async def test_executor_allows_image_for_vision_model() -> None:
    provider = VisionFakeProvider(supports_vision=True)
    stream = _executor(provider).stream(
        _image_routed_request(),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_vision",
    )
    chunks = [chunk async for chunk in stream]
    assert chunks == ["event: message_stop\ndata: {}\n\n"]
    assert len(provider.stream_calls) == 1


@pytest.mark.asyncio
async def test_executor_reactive_refusal_when_unknown_model_rejects_image() -> None:
    provider = VisionFakeProvider(
        supports_vision=None,
        stream_error=ValueError("This model does not support images"),
    )
    stream = _executor(provider).stream(
        _image_routed_request(),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_reactive",
    )
    chunks = [chunk async for chunk in stream]
    joined = "".join(chunks)
    assert "can't read images" in joined
    assert "as text" in joined


@pytest.mark.asyncio
async def test_executor_propagates_non_image_errors() -> None:
    provider = VisionFakeProvider(
        supports_vision=None,
        stream_error=ValueError("rate limit exceeded"),
    )
    stream = _executor(provider).stream(
        _image_routed_request(),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_non_image_error",
    )
    with pytest.raises(ValueError, match="rate limit exceeded"):
        _ = [chunk async for chunk in stream]


@pytest.mark.asyncio
async def test_executor_text_request_unaffected_by_non_vision_model() -> None:
    provider = VisionFakeProvider(supports_vision=False)
    stream = _executor(provider).stream(
        _text_routed_request(),
        wire_api="messages",
        raw_log_label="FULL_PAYLOAD",
        raw_log_payload={},
        request_id="req_text",
    )
    chunks = [chunk async for chunk in stream]
    assert chunks == ["event: message_stop\ndata: {}\n\n"]
    assert len(provider.stream_calls) == 1
