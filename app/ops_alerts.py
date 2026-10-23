"""Tell someone, in the minute it happens, when a feed or a graph needs a person.

The two failures this exists for both happened in October 2026 and both were detected on time by
machinery nobody was watching:

- 13 October: our 24-hourly refresh read TransMilenio mid-publication and stored a partial feed. The
  trip ids in it appear nowhere in the realtime feed, so Bogotá showed a map of buses that never
  moved for a day. `/health` knew within seconds.
- 3 to 13 October: four cities' graphs fell under the drift threshold. The daily workflow said so
  every morning and the notification arrived every morning, inside a run that was red for an
  unrelated reason, so it stopped being read.

The workflow cannot be the morning alarm anyway: measured over eleven days, GitHub ran it four to
nine hours late every time and dropped about half of the 3-hourly samples. The ingest, on the other
hand, happens the moment the agency publishes — so the alarm belongs here.

Inert until `OPS_TELEGRAM_BOT_TOKEN` + `OPS_TELEGRAM_CHAT_ID` or `OPS_WEBHOOK_URL` are set, and it
never raises: an alert that breaks an ingest would be worse than the silence it replaces.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("ot.ops")

TELEGRAM_API = "https://api.telegram.org"


def channels(token: str | None, chat_id: str | None, webhook: str | None) -> list[str]:
    """Which channels are configured, by name. Never a value: /health is public."""
    out = []
    if token and chat_id:
        out.append("telegram")
    if webhook:
        out.append("webhook")
    return out


def baseline_tag(trip_ids_url: str | None) -> str:
    """The graph's identity, from the release its baseline came from: `graph-bogota-2026-10-13`.

    This is what a drift alert is keyed on, and getting that wrong is what made the first version of
    this noisy. See `alerts_for`.
    """
    if not trip_ids_url:
        return "?"
    parts = [p for p in trip_ids_url.split("/") if p]
    for p in reversed(parts):
        if p.startswith("graph-"):
            return p
    return parts[-2] if len(parts) > 1 else "?"


def alerts_for(city_id: str, ingest: dict | None, drift: dict | None,
               trip_ids_url: str | None = None) -> list[dict]:
    """What a person needs to know about this city right now, and what to do about it.

    `key` is what makes it say so once, and the two conditions key on different things because they
    are resolved by different acts:

    - a partial feed is about **that feed version**: a redeploy must stay silent, a new bad feed must
      get through.
    - a drifted graph is about **the graph**, so it keys on the release the baseline came from. The
      first version of this keyed it on the feed version too, which meant a city publishing daily
      re-alerted every day for as long as the graph stayed stale — four cities would have sent about
      thirty-six messages in the nine days to 23 October. That is the same noise this alarm exists to
      replace, so: once per stale graph, and again only after a rebuild has moved the baseline.
    """
    out: list[dict] = []
    fv = str((ingest or {}).get("feedVersionId") or (drift or {}).get("feedVersion") or "?")

    shrink = (ingest or {}).get("shrink")
    if shrink:
        out.append({
            "kind": "feed_shrank",
            "key": f"{city_id}:feed_shrank:{fv}",
            "text": (f"⚠️ {city_id}: el feed estático que acabamos de ingerir trae "
                     f"{shrink['trips']:,} viajes contra {shrink['previousTrips']:,} de la versión "
                     f"anterior ({shrink['lostPct']} % menos).\n\n"
                     "Una publicación a medias se ve exactamente así y cuesta las llegadas en vivo "
                     "hasta el próximo ingest. Qué hacer: comparar los ids vivos contra el feed "
                     "publicado; si coinciden, reingestar (un redeploy del api lo hace)."),
        })

    if drift and drift.get("rebuild"):
        out.append({
            "kind": "graph_drift",
            "key": f"{city_id}:graph_drift:{baseline_tag(trip_ids_url)}",
            "text": (f"⚠️ {city_id}: el grafo reconoce {drift.get('overlapPct')} % del feed "
                     f"({drift.get('sharedTrips'):,} de {drift.get('feedTrips'):,} viajes).\n\n"
                     "Qué hacer: medir primero si el realtime ya habla la numeración nueva "
                     "(`scripts/rt_match.py`); si sí, reconstruir el grafo. Si no, esperar — "
                     "reconstruir antes de que el vivo se mueva causa la caída que esto previene."),
        })
    return out


async def send(text: str, *, token: str | None, chat_id: str | None, webhook: str | None,
               client: httpx.AsyncClient | None = None) -> bool:
    """Best effort, to whichever channels are configured. Returns whether anything went out."""
    sent = False
    own = client is None
    cli = client or httpx.AsyncClient(timeout=10)
    try:
        if token and chat_id:
            r = await cli.post(f"{TELEGRAM_API}/bot{token}/sendMessage",
                               json={"chat_id": chat_id, "text": text,
                                     "disable_web_page_preview": True})
            # Never log the body: a Telegram error echoes the request, token included.
            if r.status_code < 300:
                sent = True
            else:
                log.warning("ops alert: telegram answered HTTP %s", r.status_code)
        if webhook:
            r = await cli.post(webhook, json={"text": text})
            if r.status_code < 300:
                sent = True
            else:
                log.warning("ops alert: webhook answered HTTP %s", r.status_code)
    except Exception as e:  # noqa: BLE001
        log.warning("ops alert not delivered: %s: %s", type(e).__name__, e)
    finally:
        if own:
            await cli.aclose()
    return sent
