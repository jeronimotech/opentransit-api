"""v2.0 conversational assistant: the loop, the SSE contract, the limits and the privacy rules.

No network anywhere. A fake provider replays a scripted conversation, so the tests assert what our own
code does with a model's answer rather than what a model happens to say today.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admin_config import MemoryConfigStore, effective_city
from app.analytics import SCHEMAS
from app.assistant.anthropic_provider import AnthropicProvider
from app.assistant.base import CallsEvent, DoneEvent, TextEvent, ToolCall, ToolResult, Turn, Usage
from app.assistant.budget import BudgetStore, SessionLimiter
from app.assistant.tools import TOOLS
from app.cities import City
from app.errors import install_error_handlers
from app.ondemand import MASK
from app.routers import admin, chat, platform
from app.rt import RTCache
from app.runtime import CityRuntime

H = {"X-Admin-Token": "test-token"}
ASK = {"sessionId": "session-0001", "messages": [{"role": "user", "content": "¿Cómo llego al Portal Sur?"}],
       "context": {"lat": 4.6767, "lon": -74.0483, "locale": "es"}}


class FakeProvider:
    """Replays scripted turns. Each script entry is (text, [ToolCall...]) — exactly what a real adapter
    yields, so the loop cannot tell the difference."""

    model = "fake-model"

    def __init__(self, script: list[tuple[str, list[ToolCall]]], usage: Usage | None = None) -> None:
        self.script, self.usage, self.seen = list(script), usage or Usage(1000, 200), []

    async def stream(self, *, system, tools, turns):
        self.seen.append([t for t in turns])
        text, calls = self.script.pop(0) if self.script else ("", [])
        for chunk in text.split(" ") if text else []:
            yield TextEvent(chunk + " ")
        if calls:
            yield CallsEvent(calls)
        yield DoneEvent(self.usage, stop="tool_use" if calls else "end_turn")


def _app(bogota: City, provider=None, *, enabled: bool = True, **overrides) -> tuple[FastAPI, CityRuntime]:
    app = FastAPI()
    install_error_handlers(app)
    for r in (platform, chat, admin):
        app.include_router(r.router)
    city = effective_city(bogota, {"config": {"assistant": {"enabled": enabled, "apiKey": "sk-secret-key-1234",
                                                            **overrides}}})
    rt = CityRuntime(city=city, rt=RTCache(city), otp=None)  # type: ignore[arg-type]
    rt.base_city = bogota
    app.state.cities = {"bogota": rt}
    rt.override = {"config": {"assistant": {"enabled": enabled, "apiKey": "sk-secret-key-1234",
                                            **overrides}}}
    app.state.config_store = MemoryConfigStore()
    app.state.assistant_budget = BudgetStore()
    app.state.assistant_limiter = SessionLimiter(60)
    if provider is not None:
        app.state.assistant_provider_factory = lambda cfg: provider
    return app, rt


async def _events(app: FastAPI, body: dict | None = None) -> list[tuple[str, dict]]:
    """POST the chat and parse the SSE stream into (event, data) pairs."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/chat", json=body or ASK)
        assert r.status_code == 200, r.text
        out = []
        for block in r.text.split("\n\n"):
            if not block.strip():
                continue
            name = next(ln[7:] for ln in block.splitlines() if ln.startswith("event: "))
            data = next(ln[6:] for ln in block.splitlines() if ln.startswith("data: "))
            out.append((name, json.loads(data)))
        return out


def _stub_tools(monkeypatch, cards: dict | None = None) -> list[str]:
    """Replace the tool dispatch: these tests are about the loop, not about OTP."""
    ran: list[str] = []

    async def fake_run(ctx, name, args):
        ran.append(name)
        card = (cards or {}).get(name, {"kind": name, "payload": {"ok": True}})
        return json.dumps({"result": name}), card

    monkeypatch.setattr("app.assistant.engine.run_tool", fake_run)
    return ran


# ------------------------------------------------------------------ the loop


async def test_model_answers_without_tools(bogota: City, monkeypatch):
    provider = FakeProvider([("El Portal Sur está al sur.", [])])
    app, _ = _app(bogota, provider)
    _stub_tools(monkeypatch)
    events = await _events(app)
    assert [e for e, _ in events if e in ("tool", "card")] == []
    assert "".join(d["text"] for e, d in events if e == "token").strip() == "El Portal Sur está al sur."
    assert events[-1][0] == "done" and events[-1][1]["toolsUsed"] == []


async def test_single_tool_call_drives_a_second_turn(bogota: City, monkeypatch):
    provider = FakeProvider([("", [ToolCall("c1", "find_place", {"query": "Portal Sur"})]),
                             ("Está a 20 minutos.", [])])
    app, _ = _app(bogota, provider)
    ran = _stub_tools(monkeypatch)
    events = await _events(app)
    assert ran == ["find_place"]
    assert [e for e, _ in events] == ["tool", "card", "token", "token", "token", "token", "done"]
    assert events[0][1] == {"name": "find_place", "args": {"query": "Portal Sur"}}
    assert events[-1][1]["toolsUsed"] == ["find_place"]


async def test_card_precedes_the_prose_about_it(bogota: City, monkeypatch):
    """The contract's ordering rule: the structured answer paints before the sentence describing it."""
    provider = FakeProvider([("", [ToolCall("c1", "plan_trip", {})]), ("Sale en cinco minutos.", [])])
    app, _ = _app(bogota, provider)
    _stub_tools(monkeypatch)
    names = [e for e, _ in await _events(app)]
    assert names.index("card") < names.index("token")


async def test_parallel_tool_calls_run_together_and_emit_a_card_each(bogota: City, monkeypatch):
    calls = [ToolCall("c1", "find_place", {"query": "a"}), ToolCall("c2", "service_alerts", {"routeId": None})]
    provider = FakeProvider([("", calls), ("Listo.", [])])
    app, _ = _app(bogota, provider)
    ran = _stub_tools(monkeypatch)
    events = await _events(app)
    assert sorted(ran) == ["find_place", "service_alerts"]
    assert len([e for e, _ in events if e == "card"]) == 2
    # the second turn saw one tool_results turn carrying both results, not two turns
    results_turns = [t for t in provider.seen[-1] if t.role == "tool_results"]
    assert len(results_turns) == 1 and [r.id for r in results_turns[0].results] == ["c1", "c2"]


async def test_a_failing_tool_becomes_a_result_not_a_dead_stream(bogota: City, monkeypatch):
    provider = FakeProvider([("", [ToolCall("c1", "locate_bus", {})]), ("No pude consultarlo.", [])])
    app, _ = _app(bogota, provider)

    async def boom(ctx, name, args):
        raise RuntimeError("OTP is down")

    monkeypatch.setattr("app.assistant.engine.run_tool", boom)
    events = await _events(app)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "ASSISTANT_UPSTREAM"


async def test_tool_errors_are_reported_to_the_model_as_json(bogota: City):
    """`run_tool` swallows the failure so the model can explain it; nothing propagates."""
    from app.assistant import tools as tools_mod

    async def broken(ctx, name, args):
        raise RuntimeError("no database here")

    original, tools_mod._dispatch = tools_mod._dispatch, broken
    try:
        content, card = await tools_mod.run_tool(None, "route_info", {"routeQuery": "J74"})
    finally:
        tools_mod._dispatch = original
    assert card is None and json.loads(content)["error"] == "TOOL_FAILED"


async def test_the_per_reply_tool_budget_is_enforced(bogota: City, monkeypatch):
    many = [ToolCall(f"c{i}", "find_place", {"query": str(i)}) for i in range(5)]
    provider = FakeProvider([("", many), ("Listo.", [])])
    app, _ = _app(bogota, provider, maxToolCallsPerReply=2)
    ran = _stub_tools(monkeypatch)
    events = await _events(app)
    assert len(ran) == 2 and len([e for e, _ in events if e == "tool"]) == 2
    refused = [r for r in [t for t in provider.seen[-1] if t.role == "tool_results"][0].results if r.is_error]
    assert len(refused) == 3 and "TOOL_BUDGET" in refused[0].content


# ------------------------------------------------------------------ limits


async def test_disabled_city_answers_404(bogota: City):
    app, _ = _app(bogota, FakeProvider([]), enabled=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/chat", json=ASK)
    assert r.status_code == 404 and r.json()["error"]["code"] == "ASSISTANT_DISABLED"


async def test_exhausted_budget_refuses_with_503(bogota: City):
    app, rt = _app(bogota, FakeProvider([]), dailyBudgetUsd=1.0)
    app.state.assistant_budget.charge("bogota", 1.5)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/cities/bogota/chat", json=ASK)
    assert r.status_code == 503 and r.json()["error"]["code"] == "ASSISTANT_BUDGET"


async def test_rate_limit_per_session(bogota: City, monkeypatch):
    provider = FakeProvider([("hola", []), ("hola", []), ("hola", [])])
    app, _ = _app(bogota, provider, rateLimitPerMinute=2)
    _stub_tools(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        codes = [(await c.post("/v1/cities/bogota/chat", json=ASK)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_spend_is_metered_from_the_reported_usage(bogota: City, monkeypatch):
    provider = FakeProvider([("hola", [])], usage=Usage(1_000_000, 1_000_000))
    app, _ = _app(bogota, provider, model="claude-opus-5")
    _stub_tools(monkeypatch)
    done = (await _events(app))[-1]
    assert done[1]["costUsd"] == pytest.approx(30.0)          # 5 in + 25 out per million
    assert app.state.assistant_budget.today("bogota").usd == pytest.approx(30.0)


async def test_health_reports_spend_without_the_key(bogota: City):
    app, _ = _app(bogota, FakeProvider([]))
    app.state.assistant_budget.charge("bogota", 0.25)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/v1/cities/bogota/chat/health", headers=H)
        assert (await c.get("/v1/cities/bogota/chat/health")).status_code == 401
    body = r.json()
    assert body["spentUsd"] == 0.25 and body["hasKey"] is True and body["model"] == "claude-opus-5"
    assert "sk-secret-key-1234" not in r.text


# ------------------------------------------------------------------ the key never leaves


async def test_admin_get_masks_the_key_and_an_omitted_key_keeps_it(bogota: City):
    app, rt = _app(bogota, FakeProvider([]))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        got = await c.get("/v1/admin/cities/bogota/config", headers=H)
        assert "sk-secret-key-1234" not in got.text
        shown = got.json()["override"]["config"]["assistant"]["apiKey"]
        assert shown.startswith(MASK) and shown.endswith("1234")

        # the panel echoes the mask back while changing something else: the stored key survives
        put = await c.put("/v1/admin/cities/bogota/config", headers=H,
                          json={"config": {"assistant": {"apiKey": shown, "dailyBudgetUsd": 9.0}}})
        assert put.status_code == 200
        assert rt.city.config.assistant.api_key == "sk-secret-key-1234"
        assert rt.city.config.assistant.daily_budget_usd == 9.0
        assert "sk-secret-key-1234" not in put.text

        # omitting the key entirely also keeps it
        await c.put("/v1/admin/cities/bogota/config", headers=H,
                    json={"config": {"assistant": {"enabled": True}}})
        assert rt.city.config.assistant.api_key == "sk-secret-key-1234"


def test_the_public_city_payload_never_carries_the_key(bogota: City):
    city = effective_city(bogota, {"config": {"assistant": {"enabled": True, "apiKey": "sk-secret"}}})
    assert city.public()["config"]["assistant"] == {"enabled": True, "provider": "anthropic"}
    assert "sk-secret" not in json.dumps(city.public())


# ------------------------------------------------------------------ privacy


def test_the_analytics_event_cannot_carry_text_or_coordinates():
    model = SCHEMAS["assistant_query"]
    props = model.model_validate({"toolsUsed": 2, "latencyMs": 1800, "ok": True,
                                  "question": "¿Cómo llego al Portal Sur?", "lat": 4.6, "lon": -74.0,
                                  "answer": "toma el J74"}).model_dump()
    assert props == {"toolsUsed": 2, "latencyMs": 1800, "ok": True}


# ------------------------------------------------------------------ wire shapes


def test_anthropic_returns_every_parallel_result_in_one_user_message():
    """Splitting them teaches the model to stop parallelising, so this is worth a test of its own."""
    turns = [Turn("user", text="hola"),
             Turn("assistant", tool_calls=[ToolCall("a", "find_place", {}), ToolCall("b", "nearby_stops", {})]),
             Turn("tool_results", results=[ToolResult("a", "find_place", "{}"),
                                           ToolResult("b", "nearby_stops", "{}")])]
    messages = AnthropicProvider._messages(turns)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [b["type"] for b in messages[-1]["content"]] == ["tool_result", "tool_result"]
    assert [b["tool_use_id"] for b in messages[-1]["content"]] == ["a", "b"]


def test_every_tool_schema_is_strict():
    for tool in TOOLS:
        schema = tool["schema"]
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == sorted(schema["properties"]), tool["name"]
    assert len(TOOLS) == 10


def test_anthropic_tool_definitions_carry_strict_at_the_top_level():
    wire = AnthropicProvider._tools(TOOLS)
    assert all(t["strict"] is True and "input_schema" in t for t in wire)
