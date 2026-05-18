#!/usr/bin/env python
"""Benchmark per-decision latency for the LLM clients the agents use.

Occasional-use diagnostic. Builds the *exact* prompt shape
`LLMReactAgent.act` sends per `/step`:

  - system: `agents.prompts.SYSTEM_PROMPT`  (≈7k tokens, RULES.md inlined)
  - tools : `agents.prompts.ACTION_TOOLS`   (the 7-tool action vocabulary)
  - user  : `agents.state_summary.summarize_state(world.state_dict())`
            from a fresh `World(seed=42)` — a realistic ≈1k-token state
            summary (with forecast).

Then fires N back-to-back chat calls per credentialed provider and
reports per-call wall time + the token-usage breakdown. For Anthropic
the first call writes the prompt cache and the rest hit it; comparing
cold vs warm is the direct answer to "why does each decision take >1s".

Credentials are read from env (set either or both to bench both in
one run):

  LLM_OPENAI_API_KEY        OpenAI key (default model `gpt-4o-mini`)
  LLM_ANTHROPIC_API_KEY     Anthropic key (default model
                            `claude-haiku-4-5-20251001`)

Back-compat: if neither pair is set the script falls back to the
agents' canonical `LLM_API_KEY` + `LLM_PROVIDER` and benches that
single provider.

Per-provider overrides:
  LLM_OPENAI_MODEL,    LLM_OPENAI_BASE_URL
  LLM_ANTHROPIC_MODEL, LLM_ANTHROPIC_BASE_URL

Usage:
  python scripts/bench_llm.py
  python scripts/bench_llm.py --iters 10
  python scripts/bench_llm.py --provider anthropic --iters 5
  python scripts/bench_llm.py --max-tokens 1024
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Make `agents.*` / `world.*` importable when this script is invoked
# directly (`python scripts/bench_llm.py`). The repo root is one level
# above `scripts/`; the editable install registers the packages but
# only when CWD is the repo root or PYTHONPATH includes it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Same `.env` loader the world server uses (see world/api.py). The
# repo's canonical setup keeps `LLM_PROVIDER` / `LLM_API_KEY` / etc
# in a top-level `.env`; without this the script would only see env
# vars exported in the current shell.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env")

from agents.api_client import ApiClient  # noqa: E402
from agents.llm import AnthropicLLM, LLMClient, LLMResponse, OpenAILLM, Usage  # noqa: E402
from agents.prompts import ACTION_TOOLS, SYSTEM_PROMPT  # noqa: E402
from agents.state_summary import summarize_state  # noqa: E402


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    llm: LLMClient
    model: str


@dataclass(frozen=True)
class CallRecord:
    seconds: float
    usage: Usage
    n_tool_calls: int


def _detect_providers(cli_provider: str | None) -> list[ProviderSpec]:
    """Build LLM clients from env. Honour `--provider` if given;
    otherwise bench every provider with credentials in env."""
    specs: list[ProviderSpec] = []

    anth_key = os.environ.get("LLM_ANTHROPIC_API_KEY")
    anth_model = os.environ.get("LLM_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    anth_base = os.environ.get("LLM_ANTHROPIC_BASE_URL")

    openai_key = os.environ.get("LLM_OPENAI_API_KEY")
    openai_model = os.environ.get("LLM_OPENAI_MODEL", "gpt-4o-mini")
    openai_base = os.environ.get("LLM_OPENAI_BASE_URL")

    # Back-compat: agents' single-provider env var.
    if not anth_key and not openai_key:
        fallback_key = os.environ.get("LLM_API_KEY")
        fallback_provider = (os.environ.get("LLM_PROVIDER") or "openai").lower()
        fallback_model = os.environ.get("LLM_MODEL") or ""
        fallback_base = os.environ.get("LLM_BASE_URL")
        if fallback_key and fallback_provider == "anthropic":
            anth_key = fallback_key
            anth_model = fallback_model or anth_model
            anth_base = fallback_base
        elif fallback_key:
            openai_key = fallback_key
            openai_model = fallback_model or openai_model
            openai_base = fallback_base

    if anth_key and cli_provider in (None, "anthropic"):
        specs.append(
            ProviderSpec(
                name="anthropic",
                llm=AnthropicLLM(api_key=anth_key, model=anth_model, base_url=anth_base),
                model=anth_model,
            )
        )
    if openai_key and cli_provider in (None, "openai"):
        specs.append(
            ProviderSpec(
                name="openai",
                llm=OpenAILLM(api_key=openai_key, model=openai_model, base_url=openai_base),
                model=openai_model,
            )
        )
    return specs


def _build_user_message() -> str:
    """Build the same realistic state summary the agents send. Run the
    world in-process so we don't need a live server."""
    from fastapi.testclient import TestClient

    from world.api import create_app
    from world.sim import World

    api = ApiClient(transport=TestClient(create_app(world=World())))
    api.reset(seed=42)
    state = api.state()
    try:
        forecast = api.forecast(hours=24)
    except RuntimeError:
        forecast = None
    return summarize_state(state, forecast)


def _bench_one(
    spec: ProviderSpec,
    *,
    user_msg: str,
    iters: int,
    max_tokens: int,
) -> list[CallRecord]:
    records: list[CallRecord] = []
    for i in range(iters):
        t0 = time.perf_counter()
        try:
            resp: LLMResponse = spec.llm.chat(
                system=SYSTEM_PROMPT,
                user=user_msg,
                tools=ACTION_TOOLS,
                max_tokens=max_tokens,
            )
        except RuntimeError as exc:
            print(f"  iter {i + 1}: ERROR {exc!r}", file=sys.stderr)
            break
        dt = time.perf_counter() - t0
        records.append(CallRecord(seconds=dt, usage=resp.usage, n_tool_calls=len(resp.tool_calls)))
        _print_call(i + 1, dt, resp)
    return records


def _print_call(idx: int, seconds: float, resp: LLMResponse) -> None:
    u = resp.usage
    parts = [
        f"iter {idx:>2}",
        f"t={seconds:5.2f}s",
        f"in={u.input_tokens:>5}",
        f"out={u.output_tokens:>4}",
    ]
    if u.cache_creation_input_tokens or u.cache_read_input_tokens:
        parts.append(f"cache_write={u.cache_creation_input_tokens:>5}")
        parts.append(f"cache_read={u.cache_read_input_tokens:>5}")
    parts.append(f"tools={len(resp.tool_calls)}")
    print("  " + "  ".join(parts))


def _summarise(name: str, records: list[CallRecord]) -> None:
    if not records:
        return
    times = [r.seconds for r in records]
    print()
    print(f"  --- {name} summary (n={len(times)}) ---")
    print(f"  cold (iter 1):  {times[0]:5.2f}s")
    warm = times[1:]
    if warm:
        median = statistics.median(warm)
        p95 = _pct(warm, 0.95)
        warm_min = min(warm)
        warm_max = max(warm)
        print(
            f"  warm:           "
            f"min={warm_min:.2f}s  "
            f"median={median:.2f}s  "
            f"p95={p95:.2f}s  "
            f"max={warm_max:.2f}s"
        )
        if median > 0:
            print(f"  cold/warm:      {times[0] / median:.1f}x")
    # Cache effectiveness: did the warm calls actually hit the cache?
    cache_writes = sum(r.usage.cache_creation_input_tokens for r in records)
    cache_reads = sum(r.usage.cache_read_input_tokens for r in records)
    if cache_writes or cache_reads:
        print(f"  prompt cache:   writes={cache_writes:,}  reads={cache_reads:,} tokens")
    total_in = sum(r.usage.input_tokens for r in records)
    total_out = sum(r.usage.output_tokens for r in records)
    print(f"  tokens:         input={total_in:,}  output={total_out:,}")


def _pct(values: list[float], p: float) -> float:
    """Inclusive percentile without numpy; values may be unsorted."""
    s = sorted(values)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _print_prompt_size() -> None:
    """One-time prologue: show how much static context every /step
    pays for, so the warm/cold gap has context."""
    sys_chars = len(SYSTEM_PROMPT)
    import json as _json

    tool_chars = len(_json.dumps(ACTION_TOOLS))
    # Rough chars/token for English + JSON is ~4. The actual count
    # depends on the tokenizer but this is good enough for a sanity
    # check against the per-call `input_tokens` the server reports.
    print(
        f"prompt size: system={sys_chars:,} chars (~{sys_chars // 4:,} tok)  "
        f"tools={tool_chars:,} chars (~{tool_chars // 4:,} tok)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iters", type=int, default=5, help="Chat calls per provider (default 5).")
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=512,
        help="`max_tokens` per chat call (default 512). Lower = quicker output phase.",
    )
    parser.add_argument(
        "--provider",
        choices=("anthropic", "openai"),
        default=None,
        help="Restrict to one provider. Default: bench every credentialed provider.",
    )
    args = parser.parse_args(argv)

    specs = _detect_providers(args.provider)
    if not specs:
        print(
            "no LLM credentials found. Set LLM_ANTHROPIC_API_KEY and/or "
            "LLM_OPENAI_API_KEY (or the agents' LLM_API_KEY + LLM_PROVIDER).",
            file=sys.stderr,
        )
        return 1

    _print_prompt_size()
    user_msg = _build_user_message()
    user_chars = len(user_msg)
    print(f"user msg:    {user_chars:,} chars (~{user_chars // 4:,} tok)")
    print(f"iters/provider: {args.iters}    max_tokens/call: {args.max_tokens}")
    print()

    all_records: dict[str, list[CallRecord]] = {}
    for spec in specs:
        print(f"== {spec.name} ({spec.model}) ==")
        records = _bench_one(spec, user_msg=user_msg, iters=args.iters, max_tokens=args.max_tokens)
        _summarise(spec.name, records)
        all_records[spec.name] = records
        print()

    # Side-by-side comparison when both providers ran.
    if len(all_records) > 1:
        _print_compare(all_records)
    return 0


def _print_compare(by_provider: dict[str, list[CallRecord]]) -> None:
    print("== comparison (warm-only median) ==")
    for name, records in by_provider.items():
        warm = [r.seconds for r in records[1:]]
        if not warm:
            continue
        print(
            f"  {name:<10}  cold={records[0].seconds:5.2f}s  warm_median={statistics.median(warm):5.2f}s"
        )


if __name__ == "__main__":
    raise SystemExit(main())
