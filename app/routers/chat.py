"""v2.0 conversational assistant: one SSE endpoint for the chat, one admin endpoint for what it costs.

Everything that can refuse a request is checked before the stream opens, so a refusal is an ordinary HTTP
error with the usual envelope rather than a 200 whose first event is bad news. Once the stream is open the
only thing that can still go wrong is the provider, and that arrives as an `error` event.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..assistant.base import Turn, UpstreamError
from ..assistant.engine import build_provider, converse, resolve_model
from ..assistant.tools import ToolContext
from ..errors import ApiError
from ..runtime import CityRuntime, city_runtime
from .admin import require_admin

router = APIRouter(tags=["assistant"])
log = logging.getLogger("ot.assistant")


class AssistantDisabled(ApiError):
    status, code = 404, "ASSISTANT_DISABLED"


class AssistantBudget(ApiError):
    status, code = 503, "ASSISTANT_BUDGET"


class AssistantRateLimited(ApiError):
    status, code = 429, "ASSISTANT_RATE_LIMITED"


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=2000)


class Context(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    locale: str = Field("es", max_length=10)
    favorites: list[dict] | None = Field(None, max_length=20)


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sessionId: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    messages: list[Message] = Field(min_length=1, max_length=20)
    context: Context = Context()


def _turns(messages: list[Message]) -> list[Turn]:
    """Client history, flattened. Only text survives: past tool calls are not replayed, so each reply
    re-reads live data instead of paraphrasing a stale board from ten minutes ago."""
    return [Turn("user" if m.role == "user" else "assistant", text=m.content) for m in messages]


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode()


@router.post("/v1/cities/{city}/chat")
async def chat(body: ChatIn, request: Request, rt: CityRuntime = Depends(city_runtime)):
    cfg = rt.city.config.assistant
    if not cfg.enabled:
        raise AssistantDisabled("the assistant is not enabled for this city")

    budget = request.app.state.assistant_budget
    limiter = request.app.state.assistant_limiter
    if not limiter.allow(f"{rt.city.id}:{body.sessionId}", cfg.rate_limit_per_minute):
        raise AssistantRateLimited("too many questions; wait a moment")
    if budget.exhausted(rt.city.id, cfg.daily_budget_usd):
        raise AssistantBudget("the assistant's daily budget for this city is spent; it resets at midnight UTC")
    if budget.replies(rt.city.id, body.sessionId) >= cfg.max_replies_per_session:
        raise AssistantRateLimited("this conversation has reached its reply limit; start a new one")

    # Tests (and any future self-hosted stub) can swap the factory without touching the loop.
    factory = getattr(request.app.state, "assistant_provider_factory", None) or build_provider
    try:
        provider = factory(cfg)
    except UpstreamError as exc:
        raise ApiError(str(exc), status=503, code="ASSISTANT_UPSTREAM") from None

    ctx = ToolContext(rt=rt, request=request, locale=body.context.locale or rt.city.locale,
                      user=body.context.model_dump(exclude_none=True))
    turns = _turns(body.messages)
    budget.count_reply(rt.city.id, body.sessionId)

    async def gen():
        model = resolve_model(cfg)
        ok = True
        try:
            async for ev in converse(provider=provider, ctx=ctx, turns=turns,
                                     max_tool_calls=cfg.max_tool_calls_per_reply):
                if ev["event"] == "done":
                    usage = ev["usage"]
                    cost = usage.cost_usd(model)
                    budget.charge(rt.city.id, cost, ok=True)
                    yield _sse("done", {**ev["data"], "costUsd": round(cost, 6)})
                else:
                    yield _sse(ev["event"], ev["data"])
        except UpstreamError as exc:
            ok = False
            log.warning("assistant upstream failure for %s: %s", rt.city.id, exc)
            budget.charge(rt.city.id, 0.0, ok=False)
            yield _sse("error", {"code": "ASSISTANT_UPSTREAM", "message": str(exc)})
        except Exception:                                    # never leak a traceback down an open stream
            ok = False
            log.exception("assistant failed for %s", rt.city.id)
            budget.charge(rt.city.id, 0.0, ok=False)
            yield _sse("error", {"code": "ASSISTANT_UPSTREAM", "message": "the assistant failed"})
        finally:
            if cfg.log_conversations and ok:
                log.info("assistant conversation city=%s session=%s turns=%d",
                         rt.city.id, body.sessionId, len(turns))

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@router.get("/v1/cities/{city}/chat/health", dependencies=[Depends(require_admin)])
async def chat_health(request: Request, rt: CityRuntime = Depends(city_runtime)):
    """What the assistant costs today. Never includes the key, the prompts or the answers."""
    cfg = rt.city.config.assistant
    day = request.app.state.assistant_budget.today(rt.city.id)
    return {"enabled": cfg.enabled, "provider": cfg.provider, "model": resolve_model(cfg),
            "hasKey": bool(cfg.api_key),
            "dailyBudgetUsd": cfg.daily_budget_usd, "spentUsd": round(day.usd, 6),
            "calls": day.calls, "errors": day.errors, "startedAt": day.started_at}
