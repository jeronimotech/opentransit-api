"""Anthropic adapter — official `anthropic` SDK, Messages API.

Notes that are easy to get wrong and cost a debugging round each:
  * `strict: true` is a top-level field on the tool definition, not on `tool_choice`, and the schema must
    carry `additionalProperties: false` plus `required`.
  * Parallel tool calls arrive as several `tool_use` blocks in ONE assistant message, and every matching
    `tool_result` must go back in ONE user message — splitting them teaches the model to stop parallelising.
  * `effort` lives inside `output_config`, not at the top level.
  * No assistant prefill: it is a 400 on this model family.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from .base import CallsEvent, DoneEvent, ProviderEvent, TextEvent, ToolCall, Turn, UpstreamError, Usage

log = logging.getLogger("ot.assistant.anthropic")


class AnthropicProvider:
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None,
                 max_tokens: int, effort: str) -> None:
        try:
            import anthropic
        except ImportError as exc:                                   # pragma: no cover - import guard
            raise UpstreamError("the anthropic SDK is not installed") from exc
        self._sdk = anthropic
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)
        self.model, self._max_tokens, self._effort = model, max_tokens, effort

    # ---- wire conversion -----------------------------------------------------
    @staticmethod
    def _messages(turns: list[Turn]) -> list[dict]:
        out: list[dict] = []
        for t in turns:
            if t.role == "user":
                out.append({"role": "user", "content": t.text})
            elif t.role == "assistant":
                blocks: list[dict] = []
                if t.text:
                    blocks.append({"type": "text", "text": t.text})
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                           for c in t.tool_calls]
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            else:
                # every result of one parallel batch in a single user message
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r.id, "content": r.content,
                     **({"is_error": True} if r.is_error else {})}
                    for r in t.results]})
        return out

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        return [{"name": t["name"], "description": t["description"], "strict": True,
                 "input_schema": t["schema"]} for t in tools]

    async def stream(self, *, system: str, tools: list[dict],
                     turns: list[Turn]) -> AsyncIterator[ProviderEvent]:
        wire_tools = self._tools(tools)
        # Cache the stable prefix (tools -> system). The volatile conversation sits after it.
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        try:
            async with self._client.messages.stream(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system_blocks,
                tools=wire_tools,
                output_config={"effort": self._effort},
                messages=self._messages(turns),
            ) as stream:
                async for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield TextEvent(event.delta.text)
                final = await stream.get_final_message()
        except self._sdk.APIStatusError as exc:
            raise UpstreamError(f"anthropic returned {exc.status_code}") from exc
        except self._sdk.APIConnectionError as exc:
            raise UpstreamError("could not reach anthropic") from exc

        # A refusal is a normal 200 with no usable content; treat it as an end of turn, not a crash.
        if getattr(final, "stop_reason", None) == "refusal":
            yield DoneEvent(Usage(final.usage.input_tokens, final.usage.output_tokens), stop="refusal")
            return

        calls = [ToolCall(b.id, b.name, b.input if isinstance(b.input, dict) else json.loads(b.input or "{}"))
                 for b in final.content if b.type == "tool_use"]
        if calls:
            yield CallsEvent(calls)
        yield DoneEvent(Usage(final.usage.input_tokens, final.usage.output_tokens),
                        stop=final.stop_reason or "end_turn")
