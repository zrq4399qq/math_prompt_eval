"""Thin wrapper around the Anthropic Messages API for JSON-returning calls.

Everything the grader needs from the model is a JSON object matching a schema,
so there is exactly one call shape here.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Optional, Tuple

import anthropic

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 32000

# Test seam. When set to a callable (system, user, schema) -> dict, no network
# call is made. Used by tests/test_offline.py to exercise the plumbing.
FAKE_RESPONDER: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None


class MissingAPIKey(RuntimeError):
    pass


class ModelRefusal(RuntimeError):
    pass


def get_client(api_key: Optional[str] = None) -> anthropic.Anthropic:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise MissingAPIKey(
            "ANTHROPIC_API_KEY is not set.\n"
            "  PowerShell (this session):  $env:ANTHROPIC_API_KEY = 'sk-ant-...'\n"
            "  PowerShell (persistent):    setx ANTHROPIC_API_KEY 'sk-ant-...'\n"
            "Get a key at https://platform.claude.com/settings/keys"
        )
    return anthropic.Anthropic(api_key=key)


def complete_json(
    system: str,
    user: str,
    schema: Dict[str, Any],
    *,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    cache_system: bool = True,
    client: Optional[anthropic.Anthropic] = None,
) -> Tuple[Dict[str, Any], Optional[Any]]:
    """Ask the model for a JSON object matching `schema`. Returns (obj, usage).

    Streams so that large `max_tokens` never hits an HTTP timeout, and so that
    thinking has room without truncating the answer.
    """
    if FAKE_RESPONDER is not None:
        return FAKE_RESPONDER(system, user, schema), None

    client = client or get_client()

    system_blocks: list = [{"type": "text", "text": system}]
    if cache_system:
        # The rubric is byte-stable across calls, so it is worth a breakpoint.
        system_blocks[0]["cache_control"] = {"type": "ephemeral"}

    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": user}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        detail = getattr(response, "stop_details", None)
        raise ModelRefusal(f"model declined this request ({detail})")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "response hit max_tokens before finishing; raise max_tokens or lower effort"
        )

    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text), response.usage
