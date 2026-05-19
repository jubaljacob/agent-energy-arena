"""Live integration test for NVIDIA's ChatNVIDIA client (non-streaming).

A pytest version of the snippet NVIDIA publishes in their
build.nvidia.com model cards, but using `.invoke()` instead of
`.stream()`. The streaming endpoint authenticates fine (verified by
raw curl) but takes ~1 minute end-to-end on Kimi K2; the non-stream
path returns the same `AIMessage` shape (with `additional_kwargs.
reasoning_content` populated when the thinking chat-template is on)
without the streaming overhead in test runs.

Skipped automatically when:
  * `NVIDIA_API_KEY` is not in the environment (or in `.env`), or
  * `langchain_nvidia_ai_endpoints` isn't installed (i.e. the `[llm]`
    extra wasn't included at install time).

Always tagged `@pytest.mark.live`, so `make check` (which runs with
`-m 'not live'`) skips it. Opt in with `pytest -m live`.

Failure modes worth reading by hand:
  * `403 Authorization failed` — the API key is not entitled for
    inference on this model. Visit build.nvidia.com → the model's
    page, accept terms, or generate a new key from that page.
  * `404 Not Found` — the model id has changed or your account
    doesn't have access. Pass `NVIDIA_TEST_MODEL=...` to override.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Load `.env` from the repo root so a `NVIDIA_API_KEY=nvapi-...` line
# there is picked up without exporting it in every shell.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

try:
    import langchain_nvidia_ai_endpoints as _lc_nvidia  # noqa: F401

    _HAS_LANGCHAIN_NVIDIA = True
except ImportError:
    _HAS_LANGCHAIN_NVIDIA = False

pytestmark = [
    # Excluded from `make check` by the `-m 'not live'` filter in
    # pyproject. Opt in explicitly with `pytest -m live`.
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("NVIDIA_API_KEY"),
        reason="NVIDIA_API_KEY not set — live NVIDIA test skipped",
    ),
    pytest.mark.skipif(
        not _HAS_LANGCHAIN_NVIDIA,
        reason="langchain_nvidia_ai_endpoints not installed (install with `pip install -e .[llm]`)",
    ),
]


def test_chat_nvidia_invoke_returns_reasoning_and_content() -> None:
    """Mirror of the build.nvidia.com sample, with `.invoke()` instead
    of `.stream()`. The non-streaming path returns a single
    `AIMessage`; the model's chain-of-thought trace (when
    `chat_template_kwargs={"thinking": True}` is set) lands in
    `additional_kwargs["reasoning_content"]`, the user-visible answer
    in `content`.

    Asserts only that the call produced some text — the exact wording
    is non-deterministic. Kimi K2 with thinking=True reliably emits
    both lanes; non-thinking models would only emit `content`. We
    require at least one of the two to be non-empty.
    """
    from langchain_nvidia_ai_endpoints import ChatNVIDIA

    model = os.environ.get("NVIDIA_TEST_MODEL", "moonshotai/kimi-k2.6")
    client = ChatNVIDIA(
        model=model,
        api_key=os.environ["NVIDIA_API_KEY"],
        temperature=1,
        top_p=1,
        max_completion_tokens=16384,
    )

    # Verbatim from NVIDIA's sample. The empty user content is the
    # sample's literal default — the model responds with an opener.
    lc_messages = [{"role": "user", "content": ""}]
    ai_msg = client.invoke(lc_messages, chat_template_kwargs={"thinking": True})

    content = getattr(ai_msg, "content", "")
    content = content if isinstance(content, str) else ""
    extra = getattr(ai_msg, "additional_kwargs", None) or {}
    reasoning = extra.get("reasoning_content")
    reasoning = reasoning if isinstance(reasoning, str) else ""

    assert (len(content) + len(reasoning)) > 0, (
        "ChatNVIDIA.invoke returned an empty AIMessage — "
        "likely an entitlement / model-routing issue on the NVIDIA side."
    )
