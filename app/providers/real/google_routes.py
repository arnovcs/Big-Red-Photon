"""RoutingProvider on the Google Routes API: every mode, live traffic for driving.

Checked against developers.google.com/maps/documentation/routes (reference v2):
- POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix and
  POST https://routes.googleapis.com/directions/v2:computeRoutes, headers
  X-Goog-Api-Key and X-Goog-FieldMask (required).
- A Waypoint is {"location": {"latLng": {"latitude", "longitude"}}}: computeRoutes
  takes it directly as origin/destination; matrix origins wrap it in "waypoint".
- routingPreference (TRAFFIC_AWARE) is only allowed for DRIVE / TWO_WHEELER, and a
  departureTime in the past is only allowed for TRANSIT. So we send departureTime
  only when it's in the future; otherwise Google uses "now".
- The matrix response is a JSON array of elements {originIndex, destinationIndex,
  condition: ROUTE_EXISTS | ROUTE_NOT_FOUND, distanceMeters, duration: "160s"}.
  Zero values (index 0, 0 m) may be omitted, as usual for these APIs.
- Billed per element (origins × destinations). DRIVE with TRAFFIC_AWARE is the "Pro"
  SKU; WALK / BICYCLE (no routing preference) are basic requests.

One matrix call per Google travel mode, like ORS profiles. Walking and biking don't
depend on traffic, so they're sent without a time (and cached without one).
If Google fails for a mode (or has no bike coverage there), that mode falls back to
ORS, then mock: never silent.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from app.logging import get_logger, kv
from app.models.private import LatLng
from app.models.routing import Mode, RouteDetail, RouteEstimate, RouteStep
from app.providers.cache import RecordReplayCache
from app.providers.real.ors import OrsRouting, derive
from app.settings import Settings

log = get_logger(__name__)

MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
MATRIX_FIELD_MASK = "originIndex,destinationIndex,duration,distanceMeters,status,condition"
ROUTES_FIELD_MASK = ",".join(
    [
        "routes.duration",
        "routes.distanceMeters",
        "routes.polyline.encodedPolyline",
        "routes.legs.steps.navigationInstruction.instructions",
        "routes.legs.steps.distanceMeters",
        "routes.legs.steps.staticDuration",
    ]
)
# Our modes → Google travel modes. Rideshare is derived from the driving result.
TRAVEL_MODES: dict[Mode, str] = {
    Mode.WALK: "WALK",
    Mode.BIKE: "BICYCLE",
    Mode.DRIVE: "DRIVE",
    Mode.RIDESHARE: "DRIVE",
}
METERS_PER_MILE = 1609.344
# Google rejects a past departureTime for DRIVE; leave a margin for request latency.
MIN_LEAD = timedelta(minutes=1)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _location(p: LatLng) -> dict:
    """A Waypoint: computeRoutes' origin/destination."""
    return {"location": {"latLng": {"latitude": p.lat, "longitude": p.lng}}}


def _waypoint(p: LatLng) -> dict:
    """A RouteMatrixOrigin/Destination: the Waypoint wrapped in "waypoint"."""
    return {"waypoint": _location(p)}


def _seconds(duration: str | None) -> float:
    """Google durations are strings like "160s" (or "3.5s")."""
    return float((duration or "0s").removesuffix("s"))


def _miles(meters: float | None) -> float:
    return (meters or 0) / METERS_PER_MILE


def departure_time(depart_at: datetime | None, now: datetime) -> str | None:
    """RFC 3339 UTC timestamp, or None when it isn't safely in the future."""
    if depart_at is None or depart_at <= now + MIN_LEAD:
        return None
    return depart_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_matrix(
    elements: list[dict],
    source_pids: list[str],
    destination_ids: list[str],
    modes_by_pid: dict[str, list[Mode]],
    settings: Settings,
) -> list[RouteEstimate]:
    """One travel mode's matrix elements → estimates. ROUTE_NOT_FOUND pairs are skipped."""
    estimates = []
    for element in elements:
        if element.get("condition") != "ROUTE_EXISTS":
            continue  # no route for this pair: the mode is unavailable, never faked
        i, j = element.get("originIndex", 0), element.get("destinationIndex", 0)
        if i >= len(source_pids) or j >= len(destination_ids):
            continue
        pid, candidate_id = source_pids[i], destination_ids[j]
        base_min = _seconds(element.get("duration")) / 60
        miles = _miles(element.get("distanceMeters"))
        for mode in modes_by_pid[pid]:
            duration, walk, fare = derive(mode, base_min, miles, settings)
            estimates.append(
                RouteEstimate(
                    origin_pid=pid,
                    candidate_id=candidate_id,
                    mode=mode,
                    duration_min=round(duration, 2),
                    distance_mi=round(miles, 2),
                    walk_min=round(walk, 2),
                    fare_usd=fare,
                    source="google",
                )
            )
    return estimates


def parse_route(response: dict) -> tuple[float, float, list[dict], str | None]:
    """(base_min, distance_mi, steps, encoded polyline) from a computeRoutes response."""
    routes = response.get("routes") or []
    if not routes:
        raise LookupError("no route")
    route = routes[0]
    steps = [step for leg in route.get("legs", []) for step in leg.get("steps", [])]
    return (
        _seconds(route.get("duration")) / 60,
        _miles(route.get("distanceMeters")),
        steps,
        (route.get("polyline") or {}).get("encodedPolyline"),
    )


class GoogleRoutes:
    def __init__(self, settings: Settings, cache: RecordReplayCache) -> None:
        self.settings = settings
        self.cache = cache
        self.api_key = settings.google_routes_api_key or settings.google_places_api_key
        self.ors = OrsRouting(settings, cache)  # fallback when Google fails

    async def _post(
        self, method: str, url: str, field_mask: str, body: dict, depart_at: datetime | None
    ) -> Any:
        """POST through the record/replay cache. Retries network errors / 5xx once.

        Traffic depends on the time, so depart_at is part of the cache key (rounded to
        15 minutes by the cache); the exact departureTime is only added to the live body.
        Pass depart_at=None for modes that don't depend on traffic.
        """

        async def live() -> Any:
            live_body = dict(body)
            when = departure_time(depart_at, datetime.now(UTC))
            if when:
                live_body["departureTime"] = when
            start = time.monotonic()
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    async with httpx.AsyncClient(timeout=self.settings.http_timeout_sec) as c:
                        response = await c.post(
                            url,
                            json=live_body,
                            headers={
                                "X-Goog-Api-Key": self.api_key,
                                "X-Goog-FieldMask": field_mask,
                            },
                        )
                        response.raise_for_status()
            log.info(
                kv(
                    "provider_call",
                    provider="google_routes",
                    method=method,
                    status=response.status_code,
                    latency_ms=round((time.monotonic() - start) * 1000),
                )
            )
            return response.json()

        request = {"body": body, "field_mask": field_mask, "depart_at": depart_at}
        return await self.cache.call("google_routes", method, request, live)

    async def matrix(
        self,
        origins: dict[str, LatLng],
        destinations: dict[str, LatLng],
        modes: dict[str, set[Mode]],
        depart_at: datetime,
    ) -> list[RouteEstimate]:
        # Canonical order (by place, not the random pid) so replay hits the cache.
        destination_ids = sorted(destinations)
        pids_in_order = sorted(origins, key=lambda pid: (origins[pid].lat, origins[pid].lng, pid))
        estimates: list[RouteEstimate] = []
        for travel_mode in sorted(set(TRAVEL_MODES.values())):
            # Only people who use a mode on this travel mode; skip ones nobody needs.
            modes_by_pid = {
                pid: [
                    m for m in Mode if m in modes.get(pid, set()) and TRAVEL_MODES[m] == travel_mode
                ]
                for pid in pids_in_order
            }
            modes_by_pid = {pid: ms for pid, ms in modes_by_pid.items() if ms}
            if not modes_by_pid or not destination_ids:
                continue
            source_pids = list(modes_by_pid)
            body = {
                "origins": [_waypoint(origins[pid]) for pid in source_pids],
                "destinations": [_waypoint(destinations[cid]) for cid in destination_ids],
                "travelMode": travel_mode,
            }
            when = None
            if travel_mode == "DRIVE":
                body["routingPreference"] = "TRAFFIC_AWARE"
                when = depart_at
            try:
                elements = await self._post("matrix", MATRIX_URL, MATRIX_FIELD_MASK, body, when)
                estimates += parse_matrix(
                    elements, source_pids, destination_ids, modes_by_pid, self.settings
                )
            except Exception as exc:
                log.warning(
                    kv("google_routes_failed_using_ors", mode=travel_mode, error=type(exc).__name__)
                )
                fallback_modes = {pid: set(ms) for pid, ms in modes_by_pid.items()}
                estimates += await self.ors.matrix(origins, destinations, fallback_modes, depart_at)
        return estimates

    async def route(
        self,
        origin: LatLng,
        destination: LatLng,
        mode: Mode,
        arrive_by: datetime | None = None,
        depart_at: datetime | None = None,
    ) -> RouteDetail:
        """Turn-by-turn route for one winning leg. Driving uses live traffic."""
        travel_mode = TRAVEL_MODES[mode]
        body = {
            "origin": _location(origin),
            "destination": _location(destination),
            "travelMode": travel_mode,
            "languageCode": "en-US",
        }
        when = None
        if travel_mode == "DRIVE":
            body["routingPreference"] = "TRAFFIC_AWARE"
            when = depart_at
        try:
            response = await self._post("route", ROUTES_URL, ROUTES_FIELD_MASK, body, when)
            base_min, distance, raw_steps, polyline = parse_route(response)
        except Exception as exc:
            log.warning(
                kv("google_route_failed_using_ors", mode=travel_mode, error=type(exc).__name__)
            )
            return await self.ors.route(origin, destination, mode, arrive_by, depart_at)
        duration, walk, fare = derive(mode, base_min, distance, self.settings)

        if mode == Mode.RIDESHARE:
            steps = [
                RouteStep(
                    mode="rideshare",
                    instruction="Request a ride to the venue",
                    duration_min=round(base_min, 2),
                    distance_mi=round(distance, 2),
                )
            ]
        else:
            steps = [
                RouteStep(
                    mode=mode.value,
                    # Google sometimes adds a second line ("Destination will be on the right").
                    instruction=" ".join(instruction.split()),
                    duration_min=round(_seconds(s.get("staticDuration")) / 60, 2),
                    distance_mi=round(_miles(s.get("distanceMeters")), 2),
                )
                for s in raw_steps
                if (instruction := (s.get("navigationInstruction") or {}).get("instructions"))
            ]

        span = timedelta(minutes=duration)
        if depart_at is not None:
            depart, arrive = depart_at, depart_at + span
        elif arrive_by is not None:
            depart, arrive = arrive_by - span, arrive_by
        else:
            depart = datetime.now(UTC)
            arrive = depart + span
        return RouteDetail(
            origin_pid="",
            candidate_id="",
            mode=mode,
            duration_min=round(duration, 2),
            distance_mi=round(distance, 2),
            walk_min=round(walk, 2),
            fare_usd=fare,
            source="google",
            depart_at=depart,
            arrive_at=arrive,
            steps=steps,
            polyline=polyline,
        )
