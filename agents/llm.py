"""Provider-agnostic LLM client for the ReAct agent.

Two concrete adapters ship: `OpenAILLM` (OpenAI chat-completions REST,
also compatible with Anthropic's OpenAI-compatible endpoint and any
other vendor that mirrors the same wire format) and `AnthropicLLM`
(Anthropic Messages REST). Both speak raw `httpx` — no vendor SDK
dependency. The translation layer normalizes OpenAI's `tool_calls`
objects and Anthropic's `tool_use` blocks into one `ToolCall` shape
so `LLMReactAgent` doesn't care which vendor is on the other end.

Configure via env:
  LLM_PROVIDER   "openai" (default) | "anthropic"
  LLM_BASE_URL   override the vendor's default base URL
  LLM_API_KEY    bearer / x-api-key value (required for live calls)
  LLM_MODEL      model id, e.g. "gpt-4o-mini" or "claude-haiku-4-5-20251001"

Tests + offline runs use `MockLLM` which serves canned tool calls from
a queue and tracks call counts.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

# ---------- Public types ----------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """Normalized tool invocation. `name` matches an `ACTION_TOOLS` entry,
    `arguments` is the parsed JSON object the model emitted."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Per-call token usage. Vendors report this on every response."""

    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class LLMResponse:
    """Normalized LLM response — what the agent loop consumes."""

    tool_calls: list[ToolCall]
    text: str
    usage: Usage


class LLMClient(Protocol):
    """Minimal contract every provider adapter implements."""

    def chat(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse: ...


# ---------- OpenAI adapter --------------------------------------------------


class OpenAILLM:
    """OpenAI chat-completions adapter. Also targets any OpenAI-compatible
    endpoint (e.g. Anthropic's `https://api.anthropic.com/v1/`)."""

    DEFAULT_BASE_URL = "https://api.openai.com/v1"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        import httpx

        self.model = model
        # `_client` is typed `Any` so tests can replace it with an httpx
        # stub without fighting httpx.Client's overloaded `.post` signature.
        self._client: Any = httpx.Client(
            base_url=(base_url or self.DEFAULT_BASE_URL).rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def chat(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [_openai_tool_schema(t) for t in tools],
        }
        r = self._client.post("/chat/completions", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI HTTP {r.status_code}: {r.text}")
        payload = r.json()
        choice = payload["choices"][0]
        message = choice.get("message", {})
        text = message.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(name=name, arguments=args))
        usage_payload = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_payload.get("prompt_tokens", 0)),
            output_tokens=int(usage_payload.get("completion_tokens", 0)),
        )
        return LLMResponse(tool_calls=tool_calls, text=text, usage=usage)


def _openai_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Wrap our flat tool schema into OpenAI's `{type: function, function: {...}}`."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        },
    }


# ---------- Anthropic adapter -----------------------------------------------


class AnthropicLLM:
    """Anthropic Messages API adapter."""

    DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        import httpx

        self.model = model
        # `_client` is typed `Any` so tests can replace it with an httpx
        # stub without fighting httpx.Client's overloaded `.post` signature.
        self._client: Any = httpx.Client(
            base_url=(base_url or self.DEFAULT_BASE_URL).rstrip("/"),
            timeout=timeout,
            headers={
                "x-api-key": api_key,
                "anthropic-version": self.ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
        )

    def chat(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse:
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [_anthropic_tool_schema(t) for t in tools],
        }
        r = self._client.post("/messages", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic HTTP {r.status_code}: {r.text}")
        payload = r.json()
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in payload.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(
                    ToolCall(
                        name=block.get("name", ""),
                        arguments=dict(block.get("input") or {}),
                    )
                )
        usage_payload = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_payload.get("input_tokens", 0)),
            output_tokens=int(usage_payload.get("output_tokens", 0)),
        )
        return LLMResponse(tool_calls=tool_calls, text="".join(text_parts), usage=usage)


def _anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic uses `input_schema` instead of OpenAI's `parameters`."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
    }


# ---------- Mock for tests --------------------------------------------------


@dataclass
class MockLLM:
    """Replays a pre-canned sequence of `LLMResponse` objects. The agent
    calls `chat()` once per turn; if the queue empties, the mock keeps
    returning the final response (so a long game can drain to step-only
    turns at the tail)."""

    responses: list[LLMResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)
    _idx: int = 0

    def chat(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "tools": tools})
        if not self.responses:
            return LLMResponse(tool_calls=[], text="", usage=Usage(0, 0))
        i = min(self._idx, len(self.responses) - 1)
        self._idx += 1
        return self.responses[i]


# ---------- Factory ---------------------------------------------------------


def make_llm_from_env(env: dict[str, str] | None = None) -> LLMClient:
    """Build an LLM client from environment variables.

    Required: LLM_API_KEY. Optional: LLM_PROVIDER (default "openai"),
    LLM_BASE_URL (provider-specific default), LLM_MODEL (default
    "gpt-4o-mini" for openai, "claude-haiku-4-5-20251001" for anthropic).
    """
    e = env if env is not None else os.environ
    provider = (e.get("LLM_PROVIDER") or "openai").lower()
    api_key = e.get("LLM_API_KEY") or ""
    if not api_key:
        raise RuntimeError("LLM_API_KEY is required to construct an LLM client")
    base_url = e.get("LLM_BASE_URL") or None
    model = e.get("LLM_MODEL") or ""
    if provider == "openai":
        return OpenAILLM(api_key=api_key, model=model or "gpt-4o-mini", base_url=base_url)
    if provider == "anthropic":
        return AnthropicLLM(
            api_key=api_key,
            model=model or "claude-haiku-4-5-20251001",
            base_url=base_url,
        )
    raise RuntimeError(f"unknown LLM_PROVIDER: {provider!r} (want 'openai' or 'anthropic')")
