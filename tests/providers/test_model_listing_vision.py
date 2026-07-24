"""Vision-capability parsing in provider model listings."""

from free_claude_code.providers.model_listing import (
    extract_openai_model_infos,
    extract_openrouter_tool_model_infos,
)


def test_openrouter_modality_drives_vision_capability() -> None:
    payload = {
        "data": [
            {
                "id": "openrouter/vision-model",
                "supported_parameters": ["tools"],
                "architecture": {"modality": "text+image->text"},
            },
            {
                "id": "openrouter/text-model",
                "supported_parameters": ["tools"],
                "architecture": {"modality": "text->text"},
            },
            {
                "id": "openrouter/unknown-modality",
                "supported_parameters": ["tools"],
            },
        ]
    }
    infos = extract_openrouter_tool_model_infos(payload, provider_name="open_router")
    by_id = {info.model_id: info for info in infos}
    assert by_id["openrouter/vision-model"].supports_vision is True
    assert by_id["openrouter/text-model"].supports_vision is False
    # No advertised modality -> unknown (None), never a confident denial.
    assert by_id["openrouter/unknown-modality"].supports_vision is None


def test_openai_compatible_listing_leaves_vision_unknown() -> None:
    payload = {
        "data": [
            {"id": "meta/llama-3.2-11b-vision-instruct"},
            {"id": "meta/llama-3.1-8b-instruct"},
        ]
    }
    infos = extract_openai_model_infos(payload, provider_name="nvidia_nim")
    by_id = {info.model_id: info for info in infos}
    # Generic listings don't advertise vision; it stays unknown (None) and the
    # provider applies a name heuristic at request time instead.
    assert by_id["meta/llama-3.2-11b-vision-instruct"].supports_vision is None
    assert by_id["meta/llama-3.1-8b-instruct"].supports_vision is None
