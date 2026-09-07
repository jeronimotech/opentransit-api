"""Provider-neutral chat types.

The engine speaks only this vocabulary, so the clients never learn which provider a city configured and
swapping providers is a config change, not a code change. Each adapter converts `Turn`s to its own wire
shape on every call — we keep no provider-native history.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

# Verified against each provider's own documentation on 2026-09-07.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "openai": "gpt-6-astra",
    "deepseek": "deepseek-v4-pro",
    "gemini": "gemini-3.8-flash",
}
DEFAULT_BASE_URLS = {"deepseek": "https://api.deepseek.com"}

# USD per million tokens (input, output), read from each provider's own pricing page on 2026-09-07.
# Only used for the per-city daily budget: an unknown model costs 0.0 rather than blocking a city, and the
# health endpoint says so. Where a provider quotes a range we take the higher number, because this figure
# guards a hard cap and under-counting spend is the failure that costs money.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
    "gpt-6-astra": (10.0, 50.0),
    "deepseek-v4-pro": (1.32, 3.96),       # peak rate; off-peak is half
    "deepseek-v4-flash": (0.44, 1.32),     # peak rate
    "gemini-3.8-flash": (0.75, 3.75),      # rises to 1.50/7.50 on 2027-01-01
}

MAX_TOKENS = 1024          # replies are short by design; the tools carry the facts
EFFORT = "low"             # latency-sensitive chat: the tools do the work, not the reasoning


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass(slots=True)
class ToolResult:
    id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class Turn:
    """One entry of the neutral conversation. Exactly one of the three shapes is populated."""
    role: Literal["user", "assistant", "tool_results"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens)

    def cost_usd(self, model: str) -> float:
        price = PRICES.get(model)
        if not price:
            return 0.0
        return self.input_tokens / 1e6 * price[0] + self.output_tokens / 1e6 * price[1]


# ── stream events a provider yields ───────────────────────────────────────────
@dataclass(slots=True)
class TextEvent:
    text: str


@dataclass(slots=True)
class CallsEvent:
    """The turn ended asking for tools. Always the last event of a turn when it fires."""
    calls: list[ToolCall]


@dataclass(slots=True)
class DoneEvent:
    usage: Usage
    stop: str = "end_turn"


ProviderEvent = TextEvent | CallsEvent | DoneEvent


class UpstreamError(Exception):
    """Any provider failure, normalised so the router answers ASSISTANT_UPSTREAM without leaking detail."""


class Provider(Protocol):
    model: str

    def stream(self, *, system: str, tools: list[dict],
               turns: list[Turn]) -> AsyncIterator[ProviderEvent]:
        """One assistant turn. Yields text deltas, then either CallsEvent or nothing, then DoneEvent."""
        ...
