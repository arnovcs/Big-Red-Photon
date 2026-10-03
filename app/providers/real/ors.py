"""RoutingProvider on OpenRouteService (§9.4).

Request/response shapes checked against the ORS v2 API reference and its OpenAPI
models: POST /v2/matrix/{profile} with `locations` as [lng, lat], `sources`,
`destinations`, `metrics=["duration","distance"]`, `units="mi"`; durations come back
in seconds, distances in miles, and an unroutable pair is `null`.
POST /v2/directions/{profile} (JSON) returns routes[0].summary + segments[].steps[].

DRIVE and RIDESHARE are both derived from the one driving-car result, using the
shared formulas in providers/costs.py: no extra API calls.
"""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from app.logging import get_logger, kv
from app.models.candidates import Uncertain
from app.models.private import LatLng
from app.models.routing import Mode, RouteDetail, RouteEstimate, RouteStep
from app.providers.cache import RecordReplayCache
from app.providers.costs import DRIVE_PARKING_MIN, drive_cost, rideshare_fare
from app.providers.mock.routing import MockRouting
from app.settings import Settings

log = get_logger(__name__)

PROFILES: dict[Mode, str] = {
    Mode.WALK: "foot-walking",
    Mode.BIKE: "cycling-regular",
    Mode.DRIVE: "driving-car",
    Mode.RIDESHARE: "driving-car",
}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _lnglat(p: LatLng) -> list[float]:
    return [p.lng, p.lat]  # ORS wants [lng, lat]


def derive(
    mode: Mode, base_min: float, distance_mi: float, settings: Settings
) -> tuple[float, float, Uncertain[Decimal]]:
    """(duration_min, walk_min, fare) for a mode, from its profile's ORS time/distance."""
    if mode == Mode.WALK:
        free = Uncertain[Decimal](value=Decimal(0), status="known", source="formula")
        return base_min, base_min, free
    if mode == Mode.BIKE:
        free = Uncertain[Decimal](value=Decimal(0), status="known", source="formula")
        return base_min, 0.0, free
    if mode == Mode.DRIVE:
        fare = drive_cost(distance_mi, settings)
        duration = base_min + DRIVE_PARKING_MIN
    else:
        fare = rideshare_fare(distance_mi, base_min, settings)
        duration = base_min + settings.rideshare_pickup_wait_min
    return duration, 0.0, Uncertain[Decimal](value=fare, status="estimated", source="formula")


def parse_matrix(
    response: dict,
    source_pids: list[str],
    destination_ids: list[str],
    modes_by_pid: dict[str, list[Mode]],
    settings: Settings,
) -> list[RouteEstimate]:
    """Turn one profile's matrix response into estimates. `null` pairs are skipped."""
    durations = response.get("durations") or []
    distances = response.get("distances") or []
    estimates = []
    for i, pid in enumerate(source_pids):
        for j, candidate_id in enumerate(destination_ids):
            try:
                seconds, miles = durations[i][j], distances[i][j]
            except (IndexError, TypeError):
                continue
            if seconds is None or miles is None:
                continue  # no route for this pair: the mode is unavailable, never faked
            for mode in modes_by_pid[pid]:
                duration, walk, fare = derive(mode, seconds / 60, miles, settings)
                estimates.append(
                    RouteEstimate(
                        origin_pid=pid,
                        candidate_id=candidate_id,
                        mode=mode,
                        duration_min=round(duration, 2),
                        distance_mi=round(miles, 2),
                        walk_min=round(walk, 2),
                        fare_usd=fare,
                        source="ors",
                    )
                )
    return estimates


def parse_directions(response: dict) -> tuple[float, float, list[dict], str | None]:
    """(base_min, distance_mi, steps, encoded polyline) from a JSON directions response."""
    route = response["routes"][0]
    summary = route.get("summary", {})
    steps = [step for segment in route.get("segments", []) for step in segment.get("steps", [])]
    return (
        summary.get("duration", 0) / 60,
        summary.get("distance", 0),
        steps,
        route.get("geometry"),
    )


class OrsRouting:
    def __init__(self, settings: Settings, cache: RecordReplayCache) -> None:
        self.settings = settings
        self.cache = cache
        self.base_url = settings.ors_base_url.rstrip("/")
        self.fallback = MockRouting(settings)

    async def _post(self, method: str, path: str, body: dict) -> Any:
        """POST to ORS through the record/replay cache. Retries network errors / 5xx once."""

        async def live() -> Any:
            start = time.monotonic()
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(2),
                retry=retry_if_exception(_is_retryable),
                reraise=True,
            ):
                with attempt:
                    async with httpx.AsyncClient(timeout=self.settings.http_timeout_sec) as c:
                        response = await c.post(
                            f"{self.base_url}{path}",
                            json=body,
                            headers={"Authorization": self.settings.ors_api_key},
                        )
                        response.raise_for_status()
            log.info(
                kv(
                    "provider_call",
                    provider="ors",
                    method=method,
                    status=response.status_code,
                    latency_ms=round((time.monotonic() - start) * 1000),
                )
            )
            return response.json()

        # The body (and path) are the whole request: ORS results don't depend on time,
        # so no depart_at goes into the key and replay works at any time of day.
        return await self.cache.call("ors", method, {"path": path, "body": body}, live)

    async def matrix(
        self,
        origins: dict[str, LatLng],
        destinations: dict[str, LatLng],
        modes: dict[str, set[Mode]],
        depart_at: datetime,
    ) -> list[RouteEstimate]:
        # Canonical order (by place, not by the per-session random pid), so the same
        # people and venues always produce the same request and replay hits the cache.
        destination_ids = sorted(destinations)
        pids_in_order = sorted(origins, key=lambda pid: (origins[pid].lat, origins[pid].lng, pid))
        estimates: list[RouteEstimate] = []
        for profile in sorted(set(PROFILES.values())):
            # Only people who use a mode on this profile; skip profiles nobody needs.
            modes_by_pid = {
                pid: [m for m in Mode if m in modes.get(pid, set()) and PROFILES[m] == profile]
                for pid in pids_in_order
            }
            modes_by_pid = {pid: ms for pid, ms in modes_by_pid.items() if ms}
            if not modes_by_pid or not destination_ids:
                continue
            source_pids = list(modes_by_pid)
            body = {
                "locations": [_lnglat(origins[pid]) for pid in source_pids]
                + [_lnglat(destinations[cid]) for cid in destination_ids],
                "sources": [str(i) for i in range(len(source_pids))],
                "destinations": [str(len(source_pids) + j) for j in range(len(destination_ids))],
                "metrics": ["duration", "distance"],
                "units": "mi",
            }
            try:
                response = await self._post("matrix", f"/v2/matrix/{profile}", body)
                estimates += parse_matrix(
                    response, source_pids, destination_ids, modes_by_pid, self.settings
                )
            except Exception as exc:
                # Degrade (CLAUDE.md rule 8): haversine estimates for this profile only.
                log.warning(
                    kv("ors_matrix_failed_using_mock", profile=profile, error=type(exc).__name__)
                )
                for pid, pid_modes in modes_by_pid.items():
                    for cid in destination_ids:
                        for mode in pid_modes:
                            estimates.append(
                                self.fallback.estimate(
                                    origins[pid], destinations[cid], mode, pid, cid
                                )
                            )
        return estimates

    async def route(
        self,
        origin: LatLng,
        destination: LatLng,
        mode: Mode,
        arrive_by: datetime | None = None,
        depart_at: datetime | None = None,
    ) -> RouteDetail:
        """Turn-by-turn route for one winning leg. Raises if ORS has no route
        (delivery then falls back to the screening estimate, §10)."""
        profile = PROFILES[mode]
        body = {
            "coordinates": [_lnglat(origin), _lnglat(destination)],
            "instructions": True,
            "units": "mi",
        }
        response = await self._post("directions", f"/v2/directions/{profile}", body)
        base_min, distance, raw_steps, polyline = parse_directions(response)
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
                    instruction=str(s.get("instruction", "")),
                    duration_min=round(s.get("duration", 0) / 60, 2),
                    distance_mi=round(s.get("distance", 0), 2),
                )
                for s in raw_steps
                if s.get("instruction")
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
            source="ors",
            depart_at=depart,
            arrive_at=arrive,
            steps=steps,
            polyline=polyline,
        )
