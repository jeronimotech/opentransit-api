"""The alarm that fires where the ingest happens.

Both failures it exists for were detected on time by machinery nobody was watching: the partial feed
of 13 October (a day of dead buses on the map in Bogotá) and four graphs under the drift threshold
for ten days. The workflow cannot be the morning alarm — measured, GitHub ran it four to nine hours
late every day — so the ingest tells someone itself.
"""
import httpx
import pytest

from app.ops_alerts import TELEGRAM_API, alerts_for, send

SHRINK = {"previousTrips": 179_919, "trips": 110_648, "lostPct": 38.5, "note": "…"}


def test_a_partial_feed_is_reported_with_both_numbers():
    a = alerts_for("bogota", {"feedVersionId": 93, "shrink": SHRINK}, None)
    assert [x["kind"] for x in a] == ["feed_shrank"]
    assert "110,648" in a[0]["text"] and "179,919" in a[0]["text"]
    # It has to say what to do, or it is just bad news at six in the morning.
    assert "reingestar" in a[0]["text"]


def test_a_drifted_graph_is_reported_and_says_to_measure_first():
    drift = {"rebuild": True, "overlapPct": 40.4, "sharedTrips": 49_598, "feedTrips": 122_647,
             "feedVersion": 77}
    a = alerts_for("brisbane", None, drift)
    assert [x["kind"] for x in a] == ["graph_drift"]
    # The 6 October lesson: rebuilding before realtime moves causes the outage this prevents.
    assert "rt_match" in a[0]["text"] and "esperar" in a[0]["text"]


def test_a_healthy_city_says_nothing():
    assert alerts_for("roma", {"feedVersionId": 5, "shrink": None}, {"rebuild": False}) == []
    assert alerts_for("roma", None, None) == []


def test_both_at_once_are_two_alerts():
    a = alerts_for("bogota", {"feedVersionId": 93, "shrink": SHRINK}, {"rebuild": True,
                   "overlapPct": 52.2, "sharedTrips": 93_933, "feedTrips": 179_919})
    assert [x["kind"] for x in a] == ["feed_shrank", "graph_drift"]


def test_the_key_carries_the_feed_version_so_the_same_feed_is_told_once():
    """A redeploy re-ingests and would otherwise re-alert; a new bad feed must still get through."""
    first = alerts_for("bogota", {"feedVersionId": 93, "shrink": SHRINK}, None)[0]["key"]
    same = alerts_for("bogota", {"feedVersionId": 93, "shrink": SHRINK}, None)[0]["key"]
    later = alerts_for("bogota", {"feedVersionId": 94, "shrink": SHRINK}, None)[0]["key"]
    assert first == same
    assert first != later
    assert first.startswith("bogota:feed_shrank:")


@pytest.mark.anyio
async def test_nothing_is_sent_when_nothing_is_configured():
    assert await send("x", token=None, chat_id=None, webhook=None) is False


@pytest.mark.anyio
async def test_telegram_gets_the_text_and_the_token_stays_in_the_url():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.read().decode()
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cli:
        assert await send("hola", token="T", chat_id="42", webhook=None, client=cli) is True
    assert seen["url"] == f"{TELEGRAM_API}/botT/sendMessage"
    assert '"chat_id": "42"' in seen["json"] or '"chat_id":"42"' in seen["json"]
    assert "hola" in seen["json"]


@pytest.mark.anyio
async def test_a_refusing_channel_is_not_a_crash():
    """An alert that breaks an ingest would be worse than the silence it replaces."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cli:
        assert await send("x", token="T", chat_id="42", webhook=None, client=cli) is False


@pytest.mark.anyio
async def test_a_webhook_alone_is_enough():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "text" in request.read().decode()
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cli:
        assert await send("x", token=None, chat_id=None, webhook="https://h/x", client=cli) is True


@pytest.mark.anyio
async def test_a_dead_host_is_not_a_crash_either():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as cli:
        assert await send("x", token="T", chat_id="42", webhook=None, client=cli) is False


def test_channels_are_named_never_valued():
    """`/health` is public: it may say that Telegram is armed, never with what."""
    from app.ops_alerts import channels
    assert channels("secret-token", "42", None) == ["telegram"]
    assert channels("secret-token", None, None) == []        # half-configured is not armed
    assert channels(None, None, "https://h/x") == ["webhook"]
    assert channels("t", "42", "https://h/x") == ["telegram", "webhook"]
    assert channels(None, None, None) == []
