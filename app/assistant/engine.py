"""The agentic loop: model → tools → model, until the model stops asking for tools.

Written by hand rather than with a provider's tool-runner helper because the loop has to behave the same
on four providers and has to emit a `card` the instant a tool returns — before the model has said a word
about it. That ordering is the whole user-visible point: the itinerary appears, then the sentence about it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from ..cities import AssistantConfig
from .base import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    EFFORT,
    MAX_TOKENS,
    CallsEvent,
    DoneEvent,
    Provider,
    TextEvent,
    ToolCall,
    ToolResult,
    Turn,
    UpstreamError,
    Usage,
)
from .tools import TOOL_NAMES, TOOLS, ToolContext, run_tool, system_prompt

log = logging.getLogger("ot.assistant.engine")

MAX_ROUNDS = 5          # hard stop on the loop itself, independent of the per-reply tool-call cap


def resolve_model(cfg: AssistantConfig) -> str:
    return cfg.model or DEFAULT_MODELS[cfg.provider]


def build_provider(cfg: AssistantConfig) -> Provider:
    """One place that knows which class speaks which dialect. DeepSeek is the OpenAI adapter + a base URL."""
    from .anthropic_provider import AnthropicProvider
    from .gemini_provider import GeminiProvider
    from .openai_provider import OpenAIProvider

    classes = {"anthropic": AnthropicProvider, "openai": OpenAIProvider,
               "deepseek": OpenAIProvider, "gemini": GeminiProvider}
    if not cfg.api_key:
        raise UpstreamError("the assistant has no API key configured")
    return classes[cfg.provider](
        api_key=cfg.api_key, model=resolve_model(cfg),
        base_url=cfg.base_url or DEFAULT_BASE_URLS.get(cfg.provider),
        max_tokens=MAX_TOKENS, effort=EFFORT)


def _too_many(name: str) -> str:
    return json.dumps({"error": "TOOL_BUDGET",
                       "message": f"{name} was not run: this reply has used all its tool calls. "
                                  "Answer with what you already have."})


async def converse(*, provider: Provider, ctx: ToolContext, turns: list[Turn],
                   max_tool_calls: int) -> AsyncIterator[dict]:
    """Yield SSE-shaped events: token, tool, card, then one final `done` carrying usage and tools used.

    Provider failures surface as UpstreamError for the router to turn into an `error` event; everything a
    tool can do wrong is already a tool result, so it never reaches here.
    """
    system = system_prompt(ctx)
    usage_total = Usage()
    tools_used: list[str] = []
    started = time.perf_counter()

    for _round in range(MAX_ROUNDS):
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        stop = "end_turn"
        async for ev in provider.stream(system=system, tools=TOOLS, turns=turns):
            if isinstance(ev, TextEvent):
                if ev.text:
                    text_parts.append(ev.text)
                    yield {"event": "token", "data": {"text": ev.text}}
            elif isinstance(ev, CallsEvent):
                calls = ev.calls
            elif isinstance(ev, DoneEvent):
                usage_total = usage_total + ev.usage
                stop = ev.stop

        turns.append(Turn("assistant", text="".join(text_parts), tool_calls=calls))
        if not calls:
            break

        allowed = max(max_tool_calls - len(tools_used), 0)
        results: dict[str, ToolResult] = {}
        tasks: list[asyncio.Task] = []

        for call in calls:
            if call.name not in TOOL_NAMES or len(tasks) >= allowed:
                results[call.id] = ToolResult(call.id, call.name, _too_many(call.name), is_error=True)
                continue
            yield {"event": "tool", "data": {"name": call.name, "args": call.args}}
            tools_used.append(call.name)
            tasks.append(asyncio.ensure_future(_run(ctx, call)))

        # Cards go out in completion order, so the fastest tool paints first and none of them waits for
        # the slowest. The model sees the results only after all of them are back.
        for future in asyncio.as_completed(tasks):
            call, content, card = await future
            results[call.id] = ToolResult(call.id, call.name, content)
            if card:
                yield {"event": "card", "data": card}

        # Order matters on the wire: the results must line up with the calls that asked for them.
        turns.append(Turn("tool_results", results=[results[c.id] for c in calls]))

        if stop == "refusal":
            break

    yield {"event": "done",
           "data": {"usage": {"inputTokens": usage_total.input_tokens,
                              "outputTokens": usage_total.output_tokens},
                    "toolsUsed": tools_used,
                    "latencyMs": int((time.perf_counter() - started) * 1000)},
           "usage": usage_total}


async def _run(ctx: ToolContext, call: ToolCall) -> tuple[ToolCall, str, dict | None]:
    """Carry the call alongside its result: `as_completed` hands back futures in completion order, so the
    result has to say which call it answers."""
    content, card = await run_tool(ctx, call.name, call.args)
    return call, content, card
