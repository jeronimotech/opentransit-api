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


class TestADriftedGraphIsToldOnce:
    """The bug the first version shipped with, found 2026-10-23.

    `graph_drift` keyed on the feed version, so Bogotá — which republishes daily — would have sent a
    fresh alert every day for as long as the graph stayed stale. Four cities drifted for nine days:
    about thirty-six messages, which is the noise this alarm exists to replace.
    """
    URL = "https://github.com/x/opentransit-api/releases/download/graph-bogota-2026-10-13/trip-ids.txt.gz"
    DRIFT = {"rebuild": True, "overlapPct": 56.1, "sharedTrips": 101_544, "feedTrips": 181_067}

    def test_a_new_feed_version_does_not_re_alert(self):
        a = alerts_for("bogota", {"feedVersionId": 97}, self.DRIFT, self.URL)[0]
        b = alerts_for("bogota", {"feedVersionId": 98}, self.DRIFT, self.URL)[0]
        assert a["key"] == b["key"] == "bogota:graph_drift:graph-bogota-2026-10-13"

    def test_a_rebuild_arms_it_again(self):
        """After a rebuild the baseline moves, so the *next* time this graph falls behind it is news."""
        newer = self.URL.replace("2026-10-13", "2026-10-24")
        a = alerts_for("bogota", {"feedVersionId": 98}, self.DRIFT, self.URL)[0]
        b = alerts_for("bogota", {"feedVersionId": 98}, self.DRIFT, newer)[0]
        assert a["key"] != b["key"]

    def test_a_partial_feed_still_keys_on_the_feed_version(self):
        """The other condition is about that version, and a new bad feed must get through."""
        shrink = {"previousTrips": 179_919, "trips": 110_648, "lostPct": 38.5, "note": ""}
        a = alerts_for("bogota", {"feedVersionId": 93, "shrink": shrink}, None, self.URL)[0]
        b = alerts_for("bogota", {"feedVersionId": 94, "shrink": shrink}, None, self.URL)[0]
        assert a["key"] != b["key"]

    def test_a_baseline_without_a_release_tag_still_keys_on_something(self):
        from app.ops_alerts import baseline_tag
        assert baseline_tag(None) == "?"
        assert baseline_tag("https://h/x/trip-ids.txt.gz") == "x"
        assert baseline_tag(self.URL) == "graph-bogota-2026-10-13"
