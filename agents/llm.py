"""Provider-agnostic LLM client for the ReAct agent.

Four concrete adapters ship: `OpenAILLM` (OpenAI chat-completions REST,
also compatible with any vendor that mirrors the same wire format),
`AnthropicLLM` (Anthropic Messages REST), `OllamaLLM` (local Ollama
`/api/chat`), and `NvidiaLLM` (NVIDIA NIM via the recommended
`langchain_nvidia_ai_endpoints.ChatNVIDIA` client). The first three
speak raw `httpx` — no vendor SDK. The NVIDIA adapter is the one
exception: NVIDIA recommends ChatNVIDIA for their hosted NIM and
private deployments, so we lazy-import it (gated behind the `[llm]`
extra) rather than reproduce its auth and routing quirks.

The translation layer normalizes every vendor's tool-call shape into
the same flat `ToolCall(name, arguments=dict)` so `LLMReactAgent`
doesn't care which vendor is on the other end.

Configure via env:
  LLM_PROVIDER     "openai" (default) | "anthropic" | "ollama" | "nvidia"
  LLM_BASE_URL     override the vendor's default base URL
  LLM_API_KEY      bearer / x-api-key value (required for
                   openai/anthropic/nvidia; not used by ollama)
  LLM_MODEL        model id — provider-specific default if unset
  NVIDIA_API_KEY   accepted as a fallback for nvidia when LLM_API_KEY
                   is unset (the name `.env` files typically use)

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
    """Per-call token usage. Vendors report this on every response.

    `cache_creation_input_tokens` and `cache_read_input_tokens` are
    Anthropic-only — they default to 0 for vendors that don't surface
    prompt-cache stats. `input_tokens` is the count of *non-cached*
    input tokens (Anthropic returns cached-prefix tokens separately);
    `total` sums everything that hit the wire so a 1M-token budget
    stays meaningful across cached and uncached calls.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


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
        """Anthropic Messages call with prompt caching.

        The reference agent's static prefix — `SYSTEM_PROMPT` (≈7k
        tokens once `RULES.md` is appended) plus the 7-tool schema
        (~700 tokens) — is the dominant input cost. We mark the
        system block with `cache_control: ephemeral` so Anthropic
        caches it (and everything earlier in the request, i.e. the
        tools array) for ~5 minutes. The first attach-mode `/step`
        writes the cache; subsequent `/step`s read it back at ~10%
        of the input cost and a fraction of the wall-clock latency.

        This is what "context initialized at attach time, called
        every day" means in a stateless-API world: the static prefix
        is processed once, the per-day delta (`user`, the freshly
        summarised state) is the only thing the model has to chew
        through on the hot path.
        """
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
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
            cache_creation_input_tokens=int(usage_payload.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(usage_payload.get("cache_read_input_tokens", 0)),
        )
        return LLMResponse(tool_calls=tool_calls, text="".join(text_parts), usage=usage)


def _anthropic_tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic uses `input_schema` instead of OpenAI's `parameters`."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
    }


# ---------- Ollama adapter --------------------------------------------------


class OllamaLLM:
    """Local Ollama adapter targeting `/api/chat` with native tools.

    Ollama's `/api/chat` mirrors OpenAI's wire shape closely but differs
    in two places we have to handle:

    * `message.tool_calls[].function.arguments` is already a parsed
      JSON object, not a JSON-encoded string (OpenAI returns a string).
    * Token usage lives in `prompt_eval_count` / `eval_count` at the
      top level of the response, not under a `usage` object.

    Tool-capable models only — gemma/llama/qwen variants that Ollama
    lists with the "tools" capability. Models without tool support
    return `tool_calls: []` regardless of the request shape.

    No API key is sent; Ollama's local daemon is unauthenticated.
    """

    DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(
        self,
        *,
        model: str,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        import httpx

        self.model = model
        # See `OpenAILLM` for why `_client` is typed `Any`.
        self._client: Any = httpx.Client(
            base_url=(base_url or self.DEFAULT_BASE_URL).rstrip("/"),
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    def chat(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [_openai_tool_schema(t) for t in tools],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        r = self._client.post("/api/chat", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Ollama HTTP {r.status_code}: {r.text}")
        payload = r.json()
        message = payload.get("message") or {}
        text = message.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments")
            # Ollama returns a parsed dict; some forks pass through a
            # JSON string. Accept both so we stay forward-compatible.
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = dict(raw_args)
            else:
                args = {}
            tool_calls.append(ToolCall(name=name, arguments=args))
        usage = Usage(
            input_tokens=int(payload.get("prompt_eval_count", 0)),
            output_tokens=int(payload.get("eval_count", 0)),
        )
        return LLMResponse(tool_calls=tool_calls, text=text, usage=usage)


# ---------- NVIDIA adapter (LangChain ChatNVIDIA) ---------------------------


class NvidiaLLM:
    """NVIDIA NIM adapter built on `langchain_nvidia_ai_endpoints.ChatNVIDIA`.

    NVIDIA recommends ChatNVIDIA for their hosted NIM endpoint and
    private deployments; this adapter wraps it behind the same flat
    `chat()` contract the OpenAI/Anthropic/Ollama adapters expose so
    the agent loop doesn't see the LangChain object model.

    LangChain's `AIMessage.tool_calls` arrives pre-normalized as
    `[{name, args, id, type}]` with `args` already a parsed dict — so
    there's no JSON-string decoding to worry about. Token counts come
    from `AIMessage.usage_metadata` (LangChain's vendor-agnostic shape).

    Streaming and Kimi-style `reasoning_content` are not surfaced; the
    agent consumes one full response per turn, so we call `.invoke()`.

    `langchain_nvidia_ai_endpoints` is imported lazily so the bare
    install (without the `[llm]` extra) still loads `agents.llm`.
    """

    DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
    ) -> None:
        from langchain_nvidia_ai_endpoints import ChatNVIDIA

        self.model = model
        # `_client` is typed `Any` so tests can swap in a stub with
        # `bind_tools(...).invoke(...)` semantics without depending on
        # langchain_core type internals.
        self._client: Any = ChatNVIDIA(
            model=model,
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
        )

    def chat(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int = 2048,
    ) -> LLMResponse:
        from langchain_core.messages import HumanMessage, SystemMessage

        runnable = self._client
        if tools:
            runnable = runnable.bind_tools([_openai_tool_schema(t) for t in tools])
        runnable = runnable.bind(max_completion_tokens=max_tokens)
        ai_msg = runnable.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        tool_calls: list[ToolCall] = [
            ToolCall(name=str(tc.get("name", "")), arguments=dict(tc.get("args") or {}))
            for tc in (getattr(ai_msg, "tool_calls", None) or [])
        ]
        raw_content = getattr(ai_msg, "content", "")
        text = raw_content if isinstance(raw_content, str) else ""
        usage_meta = getattr(ai_msg, "usage_metadata", None) or {}
        usage = Usage(
            input_tokens=int(usage_meta.get("input_tokens", 0)),
            output_tokens=int(usage_meta.get("output_tokens", 0)),
        )
        return LLMResponse(tool_calls=tool_calls, text=text, usage=usage)


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

    LLM_PROVIDER (default "openai") selects the adapter. LLM_API_KEY is
    required for openai/anthropic/nvidia and ignored for ollama (local
    daemon is unauthenticated). For convenience the nvidia branch also
    accepts NVIDIA_API_KEY (the name `.env` files typically use) when
    LLM_API_KEY is unset. LLM_BASE_URL and LLM_MODEL override the
    provider-specific defaults: "gpt-4o-mini" (openai),
    "claude-haiku-4-5-20251001" (anthropic), "gemma4" (ollama),
    "moonshotai/kimi-k2.6" (nvidia).
    """
    e = env if env is not None else os.environ
    provider = (e.get("LLM_PROVIDER") or "openai").lower()
    base_url = e.get("LLM_BASE_URL") or None
    model = e.get("LLM_MODEL") or ""
    if provider == "ollama":
        return OllamaLLM(model=model or "gemma4", base_url=base_url)
    api_key = e.get("LLM_API_KEY") or ""
    if provider == "nvidia" and not api_key:
        api_key = e.get("NVIDIA_API_KEY") or ""
    if not api_key:
        raise RuntimeError(
            "LLM_API_KEY is required to construct an LLM client"
            + (" (or set NVIDIA_API_KEY)" if provider == "nvidia" else "")
        )
    if provider == "openai":
        return OpenAILLM(api_key=api_key, model=model or "gpt-4o-mini", base_url=base_url)
    if provider == "anthropic":
        return AnthropicLLM(
            api_key=api_key,
            model=model or "claude-haiku-4-5-20251001",
            base_url=base_url,
        )
    if provider == "nvidia":
        return NvidiaLLM(
            api_key=api_key,
            model=model or "moonshotai/kimi-k2.6",
            base_url=base_url,
        )
    raise RuntimeError(
        f"unknown LLM_PROVIDER: {provider!r} (want 'openai', 'anthropic', 'ollama', or 'nvidia')"
    )
