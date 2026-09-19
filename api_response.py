"""Normalize text across Anthropic-style and OpenAI-style API responses."""

from __future__ import annotations

from typing import Any, Optional


def _collect_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(_collect_text(item))
        return parts
    if not isinstance(value, dict):
        return []

    # Reasoning/thinking blocks intentionally do not count as user-visible text.
    block_type = value.get("type")
    if block_type in {"thinking", "reasoning", "redacted_thinking"}:
        return []

    for key in ("text", "output_text"):
        text = value.get(key)
        if isinstance(text, str) and text:
            return [text]

    content = value.get("content")
    return _collect_text(content) if content is not None else []


def extract_response_text(response: Any) -> Optional[str]:
    """Return concatenated visible text, or ``None`` when no text block exists."""
    if not isinstance(response, dict):
        return None

    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        parts = _collect_text(message.get("content"))
        if parts:
            return "\n".join(parts)

    parts = _collect_text(response.get("content"))
    if parts:
        return "\n".join(parts)

    parts = _collect_text(response.get("output_text"))
    return "\n".join(parts) if parts else None

