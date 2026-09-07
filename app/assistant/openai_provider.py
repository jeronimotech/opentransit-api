"""OpenAI-compatible adapter — covers OpenAI and DeepSeek (DeepSeek speaks the OpenAI shape at
`https://api.deepseek.com`), so the two differ only by `base_url` and model id.

Streaming tool calls arrive fragmented: `index` identifies the call, `id`/`name` come once, and
`arguments` accumulate character by character across deltas. Assembling them by index is the whole trick.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from .base import CallsEvent, DoneEvent, ProviderEvent, TextEvent, ToolCall, Turn, UpstreamError, Usage

log = logging.getLogger("ot.assistant.openai")


class OpenAIProvider:
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None,
                 max_tokens: int, effort: str) -> None:
        try:
            import openai
        except ImportError as exc:                                   # pragma: no cover - import guard
            raise UpstreamError("the openai SDK is not installed") from exc
        self._sdk = openai
        self._client = openai.AsyncOpenAI(api_key=api_key, **({"base_url": base_url} if base_url else {}))
        self.model, self._max_tokens = model, max_tokens

    @staticmethod
    def _messages(system: str, turns: list[Turn]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for t in turns:
            if t.role == "user":
                out.append({"role": "user", "content": t.text})
            elif t.role == "assistant":
                msg: dict = {"role": "assistant", "content": t.text or None}
                if t.tool_calls:
                    msg["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                        for c in t.tool_calls]
                out.append(msg)
            else:
                # one `tool` message per result, each keyed by the call it answers
                out += [{"role": "tool", "tool_call_id": r.id, "content": r.content} for r in t.results]
        return out

    @staticmethod
    def _tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["schema"], "strict": True}} for t in tools]

    async def stream(self, *, system: str, tools: list[dict],
                     turns: list[Turn]) -> AsyncIterator[ProviderEvent]:
        partial: dict[int, dict] = {}
        usage = Usage()
        finish = "stop"
        try:
            stream = await self._client.chat.completions.create(
                model=self.model, max_completion_tokens=self._max_tokens,
                messages=self._messages(system, turns), tools=self._tools(tools),
                stream=True, stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if chunk.usage:
                    usage = Usage(chunk.usage.prompt_tokens or 0, chunk.usage.completion_tokens or 0)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                if delta.content:
                    yield TextEvent(delta.content)
                for tc in (delta.tool_calls or []):
                    slot = partial.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
        except self._sdk.APIStatusError as exc:
            raise UpstreamError(f"{self.model.split('-')[0]} returned {exc.status_code}") from exc
        except self._sdk.APIConnectionError as exc:
            raise UpstreamError("could not reach the provider") from exc

        calls = []
        for _, slot in sorted(partial.items()):
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                log.warning("discarding a tool call with unparseable arguments: %s", slot["name"])
                continue
            calls.append(ToolCall(slot["id"] or slot["name"], slot["name"], args))
        if calls:
            yield CallsEvent(calls)
        yield DoneEvent(usage, stop="tool_use" if calls else finish)
