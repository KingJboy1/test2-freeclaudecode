"""Graceful handling of images sent to models that cannot read them.

Providers such as NVIDIA NIM and OpenRouter add new models constantly, and not
every model accepts image input. Instead of letting the upstream reject the
request and break the proxy, we:

* **proactively** refuse when we *know* a model has no vision support (e.g.
  OpenRouter advertises a text-only modality), and
* **reactively** catch an upstream image rejection and turn it into a friendly
  assistant reply that asks the user to send the image content as text.

Both paths emit a normal Anthropic SSE assistant message (``end_turn``) so the
client simply sees a reply and waits for the next user turn — the proxy never
errors out.
"""

import base64 as _base64
import contextlib
import uuid
from collections.abc import Mapping
from typing import Any

from free_claude_code.core.anthropic import format_sse_event

# Words that, combined with "the request contained an image", suggest the
# upstream rejected the request *because of the image*. Matched case-insensitively
# against the stringified error and its cause chain.
_IMAGE_REJECTION_KEYWORDS = (
    "image",
    "vision",
    "multimodal",
    "multi-modal",
    "picture",
    "visual",
)
_IMAGE_REJECTION_CONTEXT = (
    "not support",
    "does not support",
    "doesn't support",
    "unsupported",
    "cannot process",
    "can't process",
    "unable to process",
    "could not process",
    "not able to process",
    "invalid content",
    "invalid_request",
    "not capable",
    "not enabled",
    "not allowed",
    "image input is not",
    "does not accept image",
    "cannot accept image",
    "vision is not",
    "multimodal input is not",
)

# Model-name fragments that imply vision capability. This is a heuristic
# *allowlist*: it only ever confirms vision, never denies it, so we never
# proactively block a model that might actually accept images.
_VISION_NAME_HINTS = (
    "vision",
    "-vl",
    "vl-",
    "/vl",
    "llava",
    "cogvlm",
    "qwen-vl",
    "qwenvl",
    "gpt-4o",
    "gpt-4.1",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gemini",
    "claude-3",
    "claude-sonnet",
    "claude-opus",
    "claude-haiku",
    "pixtral",
    "minicpm-v",
    "minicpmv",
    "internvl",
    "yi-vision",
    "nova",
    "phi-3-vision",
    "phi-3.5-vision",
    "molmo",
    "aya-vision",
    "granite-vision",
    "smolvlm",
    "idefics",
    "fuyu",
)


def _block_type(block: Any) -> str | None:
    """Return the ``type`` of a content block whether it is a dict or a model."""
    if isinstance(block, Mapping):
        value = block.get("type")
    else:
        value = getattr(block, "type", None)
    return value if isinstance(value, str) else None


def request_has_image_content(request: Any) -> bool:
    """Return True when any message in the request carries an image block."""
    messages = getattr(request, "messages", None)
    if messages is None and isinstance(request, Mapping):
        messages = request.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, Mapping):
            content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if _block_type(block) == "image":
                return True
    return False


def model_name_suggests_vision(model_id: str) -> bool:
    """Heuristic: does the model name imply vision support?"""
    lowered = (model_id or "").lower()
    return any(hint in lowered for hint in _VISION_NAME_HINTS)


def modality_supports_vision(modality: Any) -> bool | None:
    """Parse an OpenRouter-style ``modality`` string (e.g. ``text+image->text``).

    Returns True/False when the input side is known, else None (unknown).
    """
    if not isinstance(modality, str) or "->" not in modality:
        return None
    input_part = modality.split("->", 1)[0].lower()
    return "image" in input_part


def image_not_supported_text(model: str) -> str:
    """The friendly assistant reply used when a model cannot read images."""
    name = model or "This model"
    return (
        f"I can't read images — {name} doesn't support image input.\n\n"
        "Please send the content of the image as text instead: describe what's "
        "in it, or paste any text from the image, and I'll help you with that."
    )


def _approx_output_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_image_refusal_sse(request: Any) -> list[str]:
    """Build a complete Anthropic SSE stream carrying the friendly refusal."""
    model = getattr(request, "model", None)
    if not isinstance(model, str) or not model:
        model = "free-claude-code"
    text = image_not_supported_text(model)
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    output_tokens = _approx_output_tokens(text)
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        format_sse_event(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


def _error_text_chain(error: BaseException) -> str:
    """Collect a lowercase blob of the error text plus its cause chain."""
    parts: list[str] = [str(error)]
    cause = error.__cause__
    seen = 0
    while cause is not None and seen < 5:
        parts.append(str(cause))
        cause = cause.__cause__
        seen += 1
    response = getattr(error, "response", None)
    if response is not None:
        with contextlib.suppress(Exception):
            parts.append(str(getattr(response, "text", "")))
    return " \n ".join(parts).lower()


def looks_like_image_rejection(error: BaseException) -> bool:
    """Best-effort: did the upstream reject the request because of an image?

    Requires both an image-related keyword and a rejection-context phrase.
    The generic "bad request" context was removed to reduce false positives;
    specific image-rejection phrases are matched instead.
    """
    text = _error_text_chain(error)
    if not text:
        return False
    has_image_word = any(keyword in text for keyword in _IMAGE_REJECTION_KEYWORDS)
    has_rejection_word = any(context in text for context in _IMAGE_REJECTION_CONTEXT)
    return has_image_word and has_rejection_word


# --- Image payload validation (BUG #7 fix) -----------------------------------


# Maximum decoded image size: 20 MB.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Recognised image media types.
_VALID_IMAGE_MEDIA_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "image/bmp",
        "image/tiff",
    }
)


def validate_image_block(block: dict) -> str | None:
    """Validate an Anthropic-format image content block.

    Returns None if valid, or a human-readable error string if invalid.
    """
    source = block.get("source")
    if not isinstance(source, dict):
        return "image block has no source"
    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type", "")
        if media_type not in _VALID_IMAGE_MEDIA_TYPES:
            return f"unsupported image media type: {media_type!r}"
        data = source.get("data", "")
        if not isinstance(data, str) or not data:
            return "image base64 data is empty"
        try:
            raw = _base64.b64decode(data, validate=True)
        except Exception:
            return "image base64 data is not valid base64"
        if len(raw) > MAX_IMAGE_BYTES:
            return f"image exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit"
    elif source_type == "url":
        url = source.get("url", "")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return "image URL must be an http(s) URL"
    else:
        return f"unknown image source type: {source_type!r}"
    return None
