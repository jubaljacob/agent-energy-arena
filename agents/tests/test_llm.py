"""Provider-abstraction unit tests for `agents.llm`.

Both `OpenAILLM` and `AnthropicLLM` are exercised with a stubbed httpx
client — we want to pin the wire-level request shape (so we don't drift
out of compatibility with either vendor) and the parsing of tool calls
back into the normalized `ToolCall` shape. Real network calls are HITL
verification, not AFK tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from agents.llm import (
    AnthropicLLM,
    LLMResponse,
    MockLLM,
    OpenAILLM,
    ToolCall,
    Usage,
    make_llm_from_env,
)

# ---------- httpx stub ------------------------------------------------------


@dataclass
class _StubResponse:
    status_code: int
    payload: dict[str, Any]
    text: str = ""

    def json(self) -> dict[str, Any]:
        return self.payload


class _StubHTTPX:
    """Records POSTs and returns a queued response. Replaces httpx.Client
    on adapter instances during tests."""

    def __init__(self, response: _StubResponse) -> None:
        self.response = response
        self.last_url: str | None = None
        self.last_json: dict[str, Any] | None = None
        self.last_headers: dict[str, str] = {}

    def post(self, url: str, json: dict[str, Any] | None = None) -> _StubResponse:
        self.last_url = url
        self.last_json = json
        return self.response


def _toy_tool() -> dict[str, Any]:
    return {
        "name": "build",
        "description": "Place a tile.",
        "parameters": {
            "type": "object",
            "properties": {"tile_type": {"type": "string"}},
            "required": ["tile_type"],
        },
    }


# ---------- OpenAI adapter --------------------------------------------------


def test_openai_chat_sends_chat_completions_payload() -> None:
    """OpenAILLM POSTs to /chat/completions with the chat-completions schema."""
    stub = _StubHTTPX(
        _StubResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_0",
                                    "type": "function",
                                    "function": {
                                        "name": "build",
                                        "arguments": json.dumps(
                                            {"tile_type": "house", "x": 4, "y": 5}
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 18},
            },
        )
    )
    llm = OpenAILLM.__new__(OpenAILLM)  # bypass __init__ (which calls httpx)
    llm.model = "gpt-test"
    llm._client = stub
    resp = llm.chat(system="sys", user="usr", tools=[_toy_tool()], max_tokens=128)

    assert stub.last_url == "/chat/completions"
    body = stub.last_json
    assert body is not None
    assert body["model"] == "gpt-test"
    assert body["max_tokens"] == 128
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "usr"},
    ]
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "build"

    assert resp.tool_calls == [
        ToolCall(name="build", arguments={"tile_type": "house", "x": 4, "y": 5})
    ]
    assert resp.usage == Usage(input_tokens=120, output_tokens=18)
    assert resp.text == ""


def test_openai_chat_parses_string_arguments_safely() -> None:
    """Tool-call arguments arrive as JSON strings; a malformed string should
    yield an empty dict, not a parser crash."""
    stub = _StubHTTPX(
        _StubResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": "Plain reply",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "step",
                                        "arguments": "not-json-{",
                                    }
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            },
        )
    )
    llm = OpenAILLM.__new__(OpenAILLM)
    llm.model = "x"
    llm._client = stub
    resp = llm.chat(system="", user="", tools=[])
    assert resp.tool_calls == [ToolCall(name="step", arguments={})]
    assert resp.text == "Plain reply"


def test_openai_chat_raises_on_http_error() -> None:
    """Non-2xx → RuntimeError surfaces the status + body."""
    stub = _StubHTTPX(_StubResponse(429, {}, text="rate limited"))
    llm = OpenAILLM.__new__(OpenAILLM)
    llm.model = "x"
    llm._client = stub
    with pytest.raises(RuntimeError, match="OpenAI HTTP 429"):
        llm.chat(system="", user="", tools=[])


# ---------- Anthropic adapter -----------------------------------------------


def test_anthropic_chat_sends_messages_payload() -> None:
    """AnthropicLLM POSTs to /messages with system promoted out of the
    `messages` array (as a cache-controlled text block) and tools using
    `input_schema` (not `parameters`)."""
    stub = _StubHTTPX(
        _StubResponse(
            200,
            {
                "content": [
                    {"type": "text", "text": "thinking..."},
                    {
                        "type": "tool_use",
                        "id": "tu_0",
                        "name": "survey",
                        "input": {"x": 16, "y": 16, "size": 8},
                    },
                ],
                "usage": {"input_tokens": 200, "output_tokens": 24},
            },
        )
    )
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.model = "claude-test"
    llm._client = stub
    resp = llm.chat(system="sys", user="usr", tools=[_toy_tool()], max_tokens=256)

    assert stub.last_url == "/messages"
    body = stub.last_json
    assert body is not None
    assert body["model"] == "claude-test"
    # System ships as a cache-controlled text block so Anthropic caches
    # the static prefix (tools + system) for ~5 minutes; subsequent /step
    # calls hit the cache instead of re-paying for the 7k-token prefix.
    assert body["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [{"role": "user", "content": "usr"}]
    assert "input_schema" in body["tools"][0]
    assert "parameters" not in body["tools"][0]

    assert resp.tool_calls == [ToolCall(name="survey", arguments={"x": 16, "y": 16, "size": 8})]
    assert resp.text == "thinking..."
    assert resp.usage == Usage(input_tokens=200, output_tokens=24)


def test_anthropic_chat_records_prompt_cache_stats_in_usage() -> None:
    """Second-and-later /step calls hit the cache: Anthropic returns
    `cache_read_input_tokens` and the Usage dataclass surfaces it so
    the agent's cumulative-token counter can see how much was cached."""
    stub = _StubHTTPX(
        _StubResponse(
            200,
            {
                "content": [{"type": "text", "text": ""}],
                "usage": {
                    "input_tokens": 80,
                    "output_tokens": 12,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 7400,
                },
            },
        )
    )
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.model = "claude-test"
    llm._client = stub
    resp = llm.chat(system="sys", user="usr", tools=[_toy_tool()])
    assert resp.usage.input_tokens == 80
    assert resp.usage.cache_read_input_tokens == 7400
    assert resp.usage.cache_creation_input_tokens == 0
    # `total` includes cache reads so the 1M-token budget stays
    # honest across cached and uncached calls.
    assert resp.usage.total == 80 + 12 + 7400


def test_anthropic_chat_raises_on_http_error() -> None:
    stub = _StubHTTPX(_StubResponse(401, {}, text="invalid key"))
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.model = "x"
    llm._client = stub
    with pytest.raises(RuntimeError, match="Anthropic HTTP 401"):
        llm.chat(system="", user="", tools=[])


# ---------- MockLLM ---------------------------------------------------------


def test_mock_llm_replays_responses_in_order() -> None:
    r1 = LLMResponse(tool_calls=[ToolCall("step", {"days": 1})], text="", usage=Usage(1, 1))
    r2 = LLMResponse(tool_calls=[ToolCall("step", {"days": 7})], text="", usage=Usage(2, 2))
    mock = MockLLM(responses=[r1, r2])
    a = mock.chat(system="s", user="u1", tools=[])
    b = mock.chat(system="s", user="u2", tools=[])
    assert a is r1 and b is r2
    assert len(mock.calls) == 2
    assert mock.calls[0]["user"] == "u1"


def test_mock_llm_repeats_final_response_when_drained() -> None:
    r = LLMResponse(tool_calls=[ToolCall("step", {"days": 7})], text="", usage=Usage(0, 0))
    mock = MockLLM(responses=[r])
    a = mock.chat(system="", user="", tools=[])
    b = mock.chat(system="", user="", tools=[])
    assert a is r and b is r


def test_mock_llm_empty_returns_empty_response() -> None:
    mock = MockLLM(responses=[])
    resp = mock.chat(system="", user="", tools=[])
    assert resp.tool_calls == []
    assert resp.usage.total == 0


# ---------- Factory ---------------------------------------------------------


def test_make_llm_from_env_requires_api_key() -> None:
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        make_llm_from_env(env={})


def test_make_llm_from_env_defaults_to_openai() -> None:
    llm = make_llm_from_env(env={"LLM_API_KEY": "k"})
    assert isinstance(llm, OpenAILLM)
    assert llm.model == "gpt-4o-mini"


def test_make_llm_from_env_selects_anthropic() -> None:
    llm = make_llm_from_env(
        env={"LLM_API_KEY": "k", "LLM_PROVIDER": "anthropic", "LLM_MODEL": "claude-x"}
    )
    assert isinstance(llm, AnthropicLLM)
    assert llm.model == "claude-x"


def test_make_llm_from_env_rejects_unknown_provider() -> None:
    with pytest.raises(RuntimeError, match="unknown LLM_PROVIDER"):
        make_llm_from_env(env={"LLM_API_KEY": "k", "LLM_PROVIDER": "google"})
