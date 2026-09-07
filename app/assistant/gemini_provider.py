"""Gemini adapter — `google-genai`, Interactions API.

Gemini differs from the other two in a way worth stating: it keeps the conversation server-side and
chains turns with `previous_interaction_id` instead of resending history. We still hold the neutral
history (the engine needs it for the cards and for the other providers), but a follow-up call sends only
the new function results plus the id of the interaction that asked for them.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from .base import CallsEvent, DoneEvent, ProviderEvent, TextEvent, ToolCall, Turn, UpstreamError, Usage

log = logging.getLogger("ot.assistant.gemini")


class GeminiProvider:
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None,
                 max_tokens: int, effort: str) -> None:
        try:
            from google import genai
        except ImportError as exc:                                   # pragma: no cover - import guard
            raise UpstreamError("the google-genai SDK is not installed") from exc
        self._genai = genai
        self._client = genai.Client(api_key=api_key)
        self.model, self._max_tokens = model, max_tokens
        self._last_interaction: str | None = None

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "name": t["name"], "description": t["description"],
                 "parameters": t["schema"]} for t in tools]

    def _input(self, turns: list[Turn]) -> tuple[list | str, str | None]:
        """Either the new function results (chained to the interaction that asked) or the latest user text."""
        last = turns[-1] if turns else None
        if last and last.role == "tool_results":
            return ([{"type": "function_result", "name": r.name, "call_id": r.id,
                      "result": [{"type": "text", "text": r.content}]} for r in last.results],
                    self._last_interaction)
        self._last_interaction = None
        text = next((t.text for t in reversed(turns) if t.role == "user"), "")
        return text, None

    async def stream(self, *, system: str, tools: list[dict],
                     turns: list[Turn]) -> AsyncIterator[ProviderEvent]:
        payload, previous = self._input(turns)
        kwargs: dict = {"model": self.model, "input": payload, "tools": self._tools(tools),
                        "stream": True, "system_instruction": system}
        if previous:
            kwargs["previous_interaction_id"] = previous

        calls: list[ToolCall] = []
        usage = Usage()
        try:
            stream = await self._client.aio.interactions.create(**kwargs)
            async for event in stream:
                kind = getattr(event, "event_type", "")
                if kind == "step.delta":
                    text = getattr(getattr(event, "delta", None), "text", None)
                    if text:
                        yield TextEvent(text)
                elif kind == "interaction.completed":
                    interaction = getattr(event, "interaction", None)
                    if interaction is None:
                        continue
                    self._last_interaction = getattr(interaction, "id", None)
                    u = getattr(interaction, "usage", None)
                    if u:
                        usage = Usage(getattr(u, "input_tokens", 0) or 0,
                                      getattr(u, "output_tokens", 0) or 0)
                    for step in getattr(interaction, "steps", []) or []:
                        if getattr(step, "type", "") != "function_call":
                            continue
                        raw = getattr(step, "arguments", {}) or {}
                        args = json.loads(raw) if isinstance(raw, str) else raw
                        calls.append(ToolCall(getattr(step, "id", step.name), step.name, args))
        except Exception as exc:                                     # the SDK's error tree is not public
            raise UpstreamError("could not reach gemini") from exc

        if calls:
            yield CallsEvent(calls)
        yield DoneEvent(usage, stop="tool_use" if calls else "end_turn")
