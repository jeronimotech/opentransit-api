"""Pydantic response models: the public contract (docs/API.md). Keys are camelCase."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Out(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class ErrorBody(Out):
    code: str
    message: str


class ErrorEnvelope(Out):
    error: ErrorBody


class Geometry(Out):
    encoded: str
    precision: int = 5


class ServiceWindow(Out):
    start: str | None = None          # "HH:MM" local, first departure today
    end: str | None = None            # "HH:MM" local, last departure today (may be after midnight)
    ends_next_day: bool = False
    active: bool = False
    next_start: str | None = None     # "HH:MM" when not active
    next_start_day: Literal["today", "tomorrow"] | None = None
    has_service_today: bool = False
    source: str = "gtfs"


class RouteRef(Out):
    id: str
    short_name: str | None = None
    long_name: str | None = None
    color: str | None = None
    text_color: str | None = None
    mode: str = "BUS"
    agency_id: str | None = None
    component: str | None = None
    service_window: ServiceWindow | None = None


class AgencyRef(Out):
    id: str | None = None
    name: str | None = None


class Alert(Out):
    id: str
    cause: str | None = None
    effect: str | None = None
    severity: Literal["INFO", "WARNING", "SEVERE"] | None = None
    severity_source: Literal["feed", "inferred"] | None = None
    header: str | None = None
    description: str | None = None
    url: str | None = None
    start: str | None = None
    end: str | None = None
    route_ids: list[str] = []
    stop_ids: list[str] = []
    routes: list[RouteRef] = []


class Place(Out):
    name: str | None = None
    lat: float
    lon: float
    stop_id: str | None = None
    stop_code: str | None = None
    arrival: str | None = None
    departure: str | None = None
    component: str | None = None
    rental_station_id: str | None = None


class RentalStationRef(Out):
    station_id: str
    name: str | None = None
    lat: float | None = None
    lon: float | None = None
    vehicles_available: int | None = None
    docks_available: int | None = None
    last_reported: str | None = None


class PriceEstimate(Out):
    amount: float
    currency: str
    label: str | None = None
    estimated: bool = True


class RentalInfo(Out):
    """A leg on a shared vehicle (GBFS): where to pick it up, where to drop it, what it costs."""
    network_id: str
    network_name: str | None = None
    color: str | None = None
    vehicle_type: str | None = None          # bicycle | electric_assist | scooter | null
    pickup: RentalStationRef | None = None
    dropoff: RentalStationRef | None = None
    free_floating: bool = False
    price_estimate: PriceEstimate | None = None


class WalkStep(Out):
    instruction: str | None = None
    distance_meters: float = 0
    lat: float | None = None
    lon: float | None = None
    relative_direction: str | None = None
    absolute_direction: str | None = None
    street_name: str | None = None


class Leg(Out):
    mode: str
    transit: bool
    start_time: str
    end_time: str
    duration_seconds: int
    distance_meters: float
    from_: Place = Field(alias="from")
    to: Place
    route: RouteRef | None = None
    headsign: str | None = None
    agency: AgencyRef | None = None
    trip_id: str | None = None
    realtime: bool = False
    realtime_state: str | None = None
    delay_seconds: int | None = None
    geometry: Geometry | None = None
    intermediate_stops: list[Place] = []
    steps: list[WalkStep] = []
    alerts: list[Alert] = []
    rental: RentalInfo | None = None


class FareItem(Out):
    label: str
    amount: float
    route: str | None = None
    kind: Literal["transit", "rental"] = "transit"


class Fare(Out):
    amount: float
    currency: str
    estimated: bool = True
    breakdown: list[FareItem] = []


class Itinerary(Out):
    id: str
    start_time: str
    end_time: str
    duration_seconds: int
    walk_distance_meters: float
    walk_time_seconds: int
    waiting_time_seconds: int
    transfers: int
    fare: Fare | None = None
    accessible: bool | None = None
    rental_legs: int = 0
    modes_used: list[str] = []
    legs: list[Leg]


class RouterInfo(Out):
    engine: str = "otp"
    version: str | None = None
    realtime: bool = True


class PlanResponse(Out):
    from_: Place = Field(alias="from")
    to: Place
    itineraries: list[Itinerary]
    router: RouterInfo
    warnings: list[str] = []


class GeocodeResult(Out):
    id: str
    name: str
    label: str | None = None
    lat: float
    lon: float
    type: Literal["station", "stop", "address", "poi", "street", "place"]
    stop_id: str | None = None
    component: str | None = None
    source: Literal["gtfs", "photon"]
    distance_meters: int | None = None


class GeocodeResponse(Out):
    results: list[GeocodeResult]


class ReverseResponse(Out):
    name: str
    lat: float
    lon: float


class Stop(Out):
    id: str
    code: str | None = None
    name: str
    lat: float
    lon: float
    location_type: Literal["stop", "station", "entrance"] = "stop"
    component: str | None = None
    wheelchair: Literal["unknown", "accessible", "not_accessible"] = "unknown"
    accessibility: "Accessibility | None" = None
    parent_station_id: str | None = None


class Accessibility(Out):
    wheelchair: Literal["unknown", "accessible", "not_accessible"] = "unknown"
    source: Literal["gtfs", "osm", "none"] = "none"
    verified: bool = False
    note: str | None = None


class NearbyStop(Stop):
    distance_meters: int


class NearbyRentalStation(Out):
    id: str
    network_id: str
    kind: Literal["rental_station"] = "rental_station"
    name: str
    lat: float
    lon: float
    capacity: int | None = None
    vehicles_available: int | None = None
    ebikes_available: int | None = None
    docks_available: int | None = None
    is_installed: bool = True
    is_renting: bool = True
    is_returning: bool = True
    last_reported: str | None = None
    distance_meters: int


class NearbyResponse(Out):
    stops: list[NearbyStop]
    rental_stations: list[NearbyRentalStation] = []


class StopDetail(Stop):
    routes: list[RouteRef] = []
    parent_station: Stop | None = None
    children: list[Stop] = []


class Departure(Out):
    route: RouteRef
    headsign: str | None = None
    trip_id: str | None = None
    scheduled_time: str
    realtime_time: str | None = None
    realtime: bool = False
    delay_seconds: int | None = None
    canceled: bool = False
    vehicle_id: str | None = None
    stop_sequence: int | None = None


class DeparturesResponse(Out):
    stop: Stop
    generated_at: str
    departures: list[Departure]


class Freshness(Out):
    realtime: bool
    age_seconds: int | None = None
    stale_seconds: int | None = None
    stale: bool = False


class BoardTime(Out):
    time: str
    minutes: int
    realtime: bool = False
    delay_seconds: int | None = None
    trip_id: str | None = None
    vehicle_id: str | None = None


class BoardRow(Out):
    route: RouteRef
    headsign: str | None = None
    next: list[BoardTime] = []


class BoardResponse(Out):
    stop: Stop
    generated_at: str
    freshness: Freshness
    rows: list[BoardRow]


class NextBus(Out):
    minutes: int
    time: str
    source: Literal["live", "estimated", "scheduled"]
    vehicle: "Vehicle | None" = None
    stops_away: int | None = None
    distance_meters: int | None = None
    trip_id: str | None = None
    delay_seconds: int | None = None


class NextResponse(Out):
    stop: Stop
    route: RouteRef
    generated_at: str
    freshness: Freshness
    serves_stop: bool = True          # false when no pattern of the route calls at this stop
    vehicles_on_route: int = 0        # buses of this route in the current live frame (any direction)
    next: list[NextBus]


class RoutesResponse(Out):
    routes: list[RouteRef]


class Pattern(Out):
    id: str
    headsign: str | None = None
    direction_id: int | None = None
    geometry: Geometry | None = None
    stops: list[Stop] = []


class RouteDetail(RouteRef):
    patterns: list[Pattern] = []
    alerts: list[Alert] = []


class NetworkShape(Out):
    id: str
    route_id: str | None = None
    component: str | None = None
    color: str | None = None
    geometry: Geometry


class NetworkResponse(Out):
    feed_version: str
    shapes: list[NetworkShape]


class Vehicle(Out):
    id: str
    label: str | None = None
    route_id: str | None = None
    route_short_name: str | None = None
    trip_id: str | None = None
    trip_resolved: bool = False
    component: str | None = None
    lat: float
    lon: float
    bearing: float | None = None
    timestamp: str | None = None
    stop_id: str | None = None
    stop_sequence: int | None = None
    occupancy: str | None = None


class VehicleHealth(Out):
    entity_age_p50_seconds: int | None = None
    pct_trip_resolved: float | None = None
    http_status: int | None = None


class VehicleFrame(Out):
    type: Literal["full", "delta"] = "full"
    seq: int
    generated_at: str | None = None
    feed_timestamp: str | None = None
    count: int
    health: VehicleHealth
    vehicles: list[Vehicle] = []
    updated: list[Vehicle] | None = None
    removed: list[str] | None = None


class VehicleTrip(Out):
    id: str | None = None
    resolved: bool = False
    headsign: str | None = None


class VehicleHistory(Out):
    points: list[list[float]] = []   # [lon, lat, unix_ts]
    span_seconds: int = 0
    distance_meters: float = 0
    avg_kmh: float | None = None


class VehicleDetail(Vehicle):
    route: RouteRef | None = None
    trip: VehicleTrip
    shape: Geometry | None = None
    current_stop: Stop | None = None
    next_stop: Stop | None = None
    eta_seconds: int | None = None
    delay_seconds: int | None = None
    history: VehicleHistory
    alerts: list[Alert] = []


class AlertsResponse(Out):
    alerts: list[Alert]


class StaticHealth(Out):
    feed_version: str | None = None
    fetched_at: str | None = None
    routes: int | None = None
    stops: int | None = None
    trips: int | None = None


class RealtimeHealth(Out):
    enabled: bool
    last_fetch_at: str | None = None
    entity_age_p50_seconds: int | None = None
    vehicles: int = 0
    pct_trip_resolved: float | None = None
    alerts: int = 0
    http_status: int | None = None
    stale: bool = False
    stale_seconds: int | None = None


class RouterHealth(Out):
    up: bool
    version: str | None = None
    graph_built_at: str | None = None
    base_url: str | None = None


class RentalNetworkHealth(Out):
    id: str
    up: bool
    stations: int = 0
    vehicles_available: int = 0
    age_seconds: int | None = None
    error: str | None = None


class RentalHealth(Out):
    networks: list[RentalNetworkHealth] = []


class CityHealth(Out):
    static: StaticHealth
    realtime: RealtimeHealth
    router: RouterHealth
    rental: RentalHealth = RentalHealth()


# ------------------------------------------------------------------ v1.2 shared vehicles (GBFS)
class RentalVehicleType(Out):
    id: str
    form_factor: str | None = None
    propulsion: str | None = None
    name: str | None = None


class RentalPricingPlan(Out):
    id: str | None = None
    name: str | None = None
    price: float | None = None
    currency: str | None = None
    description: str | None = None
    is_taxable: bool = False


class RentalNetwork(Out):
    id: str
    name: str
    network: str
    gbfs_url: str
    color: str
    url: str | None = None
    apps: dict[str, str | None] = {}
    pricing_summary: str | None = None
    form_factors: list[str] = []
    system_id: str | None = None
    system_name: str | None = None
    timezone: str | None = None
    gbfs_version: str | None = None
    stations: int = 0
    vehicles_available: int = 0
    vehicle_types: list[RentalVehicleType] = []
    pricing_plans: list[RentalPricingPlan] = []
    last_fetch_at: str | None = None
    up: bool = False
    error: str | None = None


class RentalNetworksResponse(Out):
    networks: list[RentalNetwork]


class RentalStation(Out):
    id: str
    network_id: str
    kind: Literal["rental_station"] = "rental_station"
    name: str
    lat: float
    lon: float
    capacity: int | None = None
    vehicles_available: int | None = None
    ebikes_available: int | None = None
    docks_available: int | None = None
    is_installed: bool = True
    is_renting: bool = True
    is_returning: bool = True
    last_reported: str | None = None


class RentalStationsResponse(Out):
    generated_at: str
    ttl_seconds: int
    stations: list[RentalStation]


class RentalVehicleTypeCount(RentalVehicleType):
    count: int = 0


class RentalStationDetail(RentalStation):
    vehicle_types_available: list[RentalVehicleTypeCount] = []
    network: RentalNetwork | None = None


class Healthz(Out):
    status: str = "ok"
    version: str
    cities: list[str]


NextBus.model_rebuild()
NextResponse.model_rebuild()
Stop.model_rebuild()
