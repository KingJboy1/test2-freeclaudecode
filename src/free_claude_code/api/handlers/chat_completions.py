"""Chat Completions handler — receives OpenAI Chat Completions format,
converts to Anthropic Messages, processes through the existing provider
pipeline, and converts the Anthropic SSE response back to OpenAI SSE."""

import base64
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from free_claude_code.application.execution import ProviderExecutor
from free_claude_code.application.ports import ProviderResolver
from free_claude_code.application.routing import ModelRouter
from free_claude_code.config.settings import Settings
from free_claude_code.core.anthropic.models import (
    MessagesRequest,
    SystemContent,
)

# Maximum base64 image payload: 20 MB decoded (~27 MB encoded).
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.DOTALL
)


def _convert_image_url_block(url: str) -> dict:
    """Convert an OpenAI image_url block to an Anthropic image content block.

    Handles both data-URI (``data:image/png;base64,...``) and plain HTTP(S)
    URLs.  For data URIs the media type is extracted from the URI itself
    instead of being hardcoded.  Plain URLs are forwarded as ``url`` source
    blocks so the upstream provider can fetch them.
    """
    match = _DATA_URI_RE.match(url)
    if match:
        mime = match.group("mime")
        data = match.group("data")
        # Validate base64 and enforce a size limit.
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:
            return {
                "type": "text",
                "text": "[invalid image: could not decode base64 payload]",
            }
        if len(raw) > _MAX_IMAGE_BYTES:
            return {
                "type": "text",
                "text": "[invalid image: payload exceeds 20 MB limit]",
            }
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": data},
        }
    # Plain HTTP(S) URL — forward as a URL source so the provider fetches it.
    if url.startswith(("http://", "https://")):
        return {
            "type": "image",
            "source": {"type": "url", "url": url},
        }
    # Unrecognised format — degrade gracefully.
    return {"type": "text", "text": "[invalid image: unrecognised image_url format]"}


def _chat_completions_to_messages(cc_request: dict) -> MessagesRequest:
    """Convert an OpenAI Chat Completions request to an Anthropic MessagesRequest."""
    messages = cc_request.get("messages", [])
    system = None
    anthropic_messages = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str):
                system = SystemContent(type="text", text=content)
            else:
                system = SystemContent(type="text", text=json.dumps(content))
            continue

        anthropic_content = []
        if isinstance(content, str):
            anthropic_content = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    anthropic_content.append(
                        {"type": "text", "text": block.get("text", "")}
                    )
                elif block.get("type") == "image_url":
                    url = block.get("image_url", {}).get("url", "")
                    anthropic_content.append(_convert_image_url_block(url))
                else:
                    anthropic_content.append({"type": "text", "text": str(block)})

        anthropic_messages.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": anthropic_content,
            }
        )

        # Tool calls in assistant messages
        if role == "assistant" and "tool_calls" in msg:
            for tc in msg["tool_calls"]:
                tc_input = {}
                try:
                    tc_input = json.loads(tc.get("function", {}).get("arguments", "{}"))
                except json.JSONDecodeError, ValueError:
                    tc_input = {}
                anthropic_messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tc.get("id", ""),
                                "name": tc.get("function", {}).get("name", ""),
                                "input": tc_input,
                            }
                        ],
                    }
                )

        # Tool results
        if role == "tool":
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.get("tool_call_id", ""),
                            "content": msg.get("content", ""),
                        }
                    ],
                }
            )

    return MessagesRequest.model_validate(
        {
            "model": cc_request.get("model", ""),
            "messages": anthropic_messages,
            "system": system,
            "max_tokens": cc_request.get("max_tokens", 4096),
            "stream": cc_request.get("stream", False),
            "temperature": cc_request.get("temperature"),
            "top_p": cc_request.get("top_p"),
            "stop_sequences": cc_request.get("stop"),
        }
    )


def _anthropic_sse_to_openai_sse(anthropic_chunk: str) -> list[str]:
    """Convert a single Anthropic SSE line to OpenAI Chat Completions SSE lines."""
    if not anthropic_chunk.startswith("data: "):
        return []

    try:
        data = json.loads(anthropic_chunk[6:])
    except json.JSONDecodeError, ValueError:
        return []

    event_type = data.get("type", "")
    openai_chunks = []

    if event_type == "message_start":
        msg = data.get("message", {})
        openai_chunks.append(
            json.dumps(
                {
                    "id": msg.get("id", "chatcmpl-unknown"),
                    "object": "chat.completion.chunk",
                    "created": int(msg.get("created_at", 0)),
                    "model": msg.get("model", ""),
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                }
            )
        )

    elif event_type == "content_block_delta":
        delta = data.get("delta", {})
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            openai_chunks.append(
                json.dumps(
                    {
                        "id": "chatcmpl-unknown",
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            )

    elif event_type == "message_delta":
        delta = data.get("delta", {})
        stop_reason = delta.get("stop_reason")
        finish_reason = None
        if stop_reason in ("end_turn", "stop_sequence"):
            finish_reason = "stop"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        openai_chunks.append(
            json.dumps(
                {
                    "id": "chatcmpl-unknown",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": finish_reason}
                    ],
                }
            )
        )

    return openai_chunks


async def _convert_anthropic_stream_to_openai(
    anthropic_stream: AsyncIterator[str],
) -> AsyncIterator[str]:
    """Convert an Anthropic SSE stream to an OpenAI SSE stream."""
    async for chunk in anthropic_stream:
        for openai_chunk in _anthropic_sse_to_openai_sse(chunk):
            yield f"data: {openai_chunk}\n\n"
    yield "data: [DONE]\n\n"


class ChatCompletionsHandler:
    """Handle OpenAI Chat Completions by routing through the FCC provider pipeline."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider_resolver: ProviderResolver,
        generation_id: int | None = None,
    ):
        self._executor = ProviderExecutor(
            provider_resolver,
            generation_id=generation_id,
        )
        self._model_router = ModelRouter(settings)

    async def create(
        self,
        request_data: dict[str, Any],
        *,
        request_id: str,
    ) -> dict | AsyncIterator[str]:
        """Create a chat completion. Returns a dict (non-streaming) or AsyncIterator (streaming)."""
        stream = request_data.get("stream", False)
        messages_request = _chat_completions_to_messages(request_data)
        routed_request = self._model_router.resolve_messages_request(messages_request)

        anthropic_stream = self._executor.stream(
            routed_request,
            wire_api="messages",
            raw_log_label="CHAT_COMPLETIONS",
            raw_log_payload=routed_request.request.model_dump(),
            request_id=request_id,
        )

        if stream:
            return _convert_anthropic_stream_to_openai(anthropic_stream)

        full_text = ""
        finish_reason = "stop"
        async for chunk in anthropic_stream:
            for line in _anthropic_sse_to_openai_sse(chunk):
                try:
                    data = json.loads(line)
                    for choice in data.get("choices", []):
                        delta = choice.get("delta", {})
                        full_text += delta.get("content", "")
                    if data.get("choices") and data["choices"][0].get("finish_reason"):
                        finish_reason = data["choices"][0]["finish_reason"]
                except json.JSONDecodeError, ValueError:
                    pass

        return {
            "id": "chatcmpl-unknown",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": messages_request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": full_text},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
