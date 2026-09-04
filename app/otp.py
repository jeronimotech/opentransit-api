"""OpenTripPlanner 2.x client (GTFS GraphQL API at /otp/gtfs/v1), one per city."""
import logging
from typing import Any

import httpx

from .cities import City
from .config import settings
from .errors import RouterUnavailable

log = logging.getLogger("ot.otp")

ALERT_FIELDS = """
  id alertCause alertEffect alertSeverityLevel
  alertHeaderText alertDescriptionText alertUrl
  effectiveStartDate effectiveEndDate
  entities { __typename ... on Route { gtfsId } ... on Stop { gtfsId } }
"""
ROUTE_FIELDS = "gtfsId shortName longName color textColor mode agency { gtfsId name }"
STOP_FIELDS = """gtfsId code name lat lon locationType wheelchairBoarding parentStation { gtfsId }"""
PLACE_FIELDS = """
  name lat lon
  arrival { scheduledTime estimated { time delay } }
  departure { scheduledTime estimated { time delay } }
  stop { gtfsId code }
"""
LEG_FIELDS = f"""
  mode transitLeg duration distance headsign realTime realtimeState
  start {{ scheduledTime estimated {{ time delay }} }}
  end {{ scheduledTime estimated {{ time delay }} }}
  from {{ {PLACE_FIELDS} }}
  to {{ {PLACE_FIELDS} }}
  route {{ {ROUTE_FIELDS} }}
  agency {{ gtfsId name }}
  trip {{ gtfsId }}
  legGeometry {{ points length }}
  intermediateStops {{ gtfsId code name lat lon }}
  steps {{ distance relativeDirection absoluteDirection streetName lat lon }}
  alerts {{ {ALERT_FIELDS} }}
"""
PLAN_QUERY = f"""
query Plan($origin: PlanLabeledLocationInput!, $destination: PlanLabeledLocationInput!,
           $dateTime: PlanDateTimeInput, $modes: PlanModesInput, $first: Int,
           $preferences: PlanPreferencesInput, $locale: Locale) {{
  planConnection(origin: $origin, destination: $destination, dateTime: $dateTime, modes: $modes,
                 first: $first, preferences: $preferences, locale: $locale) {{
    searchDateTime
    routingErrors {{ code description }}
    edges {{ node {{
      start end duration numberOfTransfers walkDistance walkTime waitingTime accessibilityScore
      legs {{ {LEG_FIELDS} }}
    }} }}
  }}
}}
"""
STOP_QUERY = f"""
query StopDetail($id: String!) {{
  stop(id: $id) {{
    {STOP_FIELDS}
    routes {{ {ROUTE_FIELDS} }}
    parentStation {{ {STOP_FIELDS} }}
    stops {{ {STOP_FIELDS} }}
  }}
}}
"""
DEPARTURES_QUERY = f"""
query Departures($id: String!, $n: Int!, $range: Int!) {{
  stop(id: $id) {{
    {STOP_FIELDS}
    stoptimesWithoutPatterns(numberOfDepartures: $n, timeRange: $range, omitCanceled: false) {{
      scheduledDeparture realtimeDeparture departureDelay realtime realtimeState serviceDay
      headsign stopPositionInPattern
      trip {{ gtfsId tripHeadsign route {{ {ROUTE_FIELDS} }} }}
    }}
  }}
}}
"""
STATION_QUERY = STOP_QUERY.replace("query StopDetail", "query StationDetail") \
    .replace("stop(id: $id)", "station(id: $id)")
STATION_DEPARTURES_QUERY = DEPARTURES_QUERY.replace("query Departures", "query StationDepartures") \
    .replace("stop(id: $id)", "station(id: $id)")
ROUTE_QUERY = f"""
query RouteDetail($id: String!) {{
  route(id: $id) {{
    {ROUTE_FIELDS}
    patterns {{ code headsign directionId patternGeometry {{ points }} stops {{ {STOP_FIELDS} }} }}
    alerts {{ {ALERT_FIELDS} }}
  }}
}}
"""


class OtpClient:
    def __init__(self, city: City):
        self.city = city
        self.base = city.otp.base_url.rstrip("/")
        self.version: str | None = None
        self._cli = httpx.AsyncClient(timeout=settings().OTP_TIMEOUT_S)

    async def close(self) -> None:
        await self._cli.aclose()

    async def graphql(self, query: str, variables: dict[str, Any] | None = None,
                      locale: str | None = None) -> dict:
        headers = {"Accept-Language": locale} if locale else {}
        try:
            r = await self._cli.post(f"{self.base}/otp/gtfs/v1", json={"query": query, "variables": variables or {}},
                                     headers=headers)
        except httpx.HTTPError as e:
            raise RouterUnavailable(f"routing engine for {self.city.id} is not reachable: {e}") from e
        if r.status_code >= 500:
            raise RouterUnavailable(f"routing engine for {self.city.id} returned HTTP {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise RouterUnavailable("routing engine returned a non-JSON body") from e
        if body.get("errors") and not body.get("data"):
            msg = body["errors"][0].get("message", "unknown error")
            log.warning("[%s] OTP GraphQL error: %s", self.city.id, msg)
            raise RouterUnavailable(f"routing engine error: {msg}")
        return body.get("data") or {}

    async def server_info(self) -> dict | None:
        """OTP exposes version + graph build info at GET /otp/ (not in the GraphQL schema)."""
        try:
            r = await self._cli.get(f"{self.base}/otp/", timeout=5)
            if r.status_code != 200:
                return None
            info = r.json()
            v = (info.get("version") or {})
            self.version = v.get("version") if isinstance(v, dict) else str(v)
            return info
        except Exception:  # noqa: BLE001
            return None
