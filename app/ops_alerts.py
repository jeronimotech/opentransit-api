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


def alerts_for(city_id: str, ingest: dict | None, drift: dict | None) -> list[dict]:
    """What a person needs to know about this city right now, and what to do about it.

    `key` is what makes it say so once: it carries the feed version, so a redeploy or a second
    refresh of the same condition is silent while a *new* bad feed is a new alert.
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
            "key": f"{city_id}:graph_drift:{fv}",
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
