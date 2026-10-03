"""Haversine-based routing estimates (§14.2). Used in tests and as a fallback."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models.candidates import Uncertain
from app.models.private import LatLng
from app.models.routing import Mode, RouteDetail, RouteEstimate, RouteStep
from app.providers.costs import DRIVE_PARKING_MIN, drive_cost, rideshare_fare
from app.settings import Settings

DETOUR_FACTOR = 1.3
WALK_MPH = 3.0
BIKE_MPH = 10.0
DRIVE_MPH = 18.0
EARTH_RADIUS_MI = 3958.8

_VERBS = {Mode.WALK: "Walk", Mode.BIKE: "Bike", Mode.DRIVE: "Drive"}


def haversine_mi(a: LatLng, b: LatLng) -> float:
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(h))


class MockRouting:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def estimate(
        self, origin: LatLng, destination: LatLng, mode: Mode, origin_pid: str, candidate_id: str
    ) -> RouteEstimate:
        distance = haversine_mi(origin, destination) * DETOUR_FACTOR
        drive_min = distance / DRIVE_MPH * 60
        free = Uncertain[Decimal](value=Decimal(0), status="known", source="formula")

        if mode == Mode.WALK:
            duration, fare = distance / WALK_MPH * 60, free
        elif mode == Mode.BIKE:
            duration, fare = distance / BIKE_MPH * 60, free
        elif mode == Mode.DRIVE:
            duration = drive_min + DRIVE_PARKING_MIN
            fare = Uncertain[Decimal](
                value=drive_cost(distance, self.settings), status="estimated", source="formula"
            )
        else:
            duration = drive_min + self.settings.rideshare_pickup_wait_min
            fare = Uncertain[Decimal](
                value=rideshare_fare(distance, drive_min, self.settings),
                status="estimated",
                source="formula",
            )

        return RouteEstimate(
            origin_pid=origin_pid,
            candidate_id=candidate_id,
            mode=mode,
            duration_min=round(duration, 2),
            distance_mi=round(distance, 2),
            walk_min=round(duration, 2) if mode == Mode.WALK else 0.0,
            fare_usd=fare,
            source="mock",
        )

    async def matrix(
        self,
        origins: dict[str, LatLng],
        destinations: dict[str, LatLng],
        modes: dict[str, set[Mode]],
        depart_at: datetime,
    ) -> list[RouteEstimate]:
        estimates = []
        for pid, origin in origins.items():
            for candidate_id, destination in destinations.items():
                for mode in Mode:
                    if mode in modes.get(pid, set()):
                        estimates.append(
                            self.estimate(origin, destination, mode, pid, candidate_id)
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
        est = self.estimate(origin, destination, mode, origin_pid="", candidate_id="")
        duration = timedelta(minutes=est.duration_min)
        if depart_at is not None:
            depart, arrive = depart_at, depart_at + duration
        elif arrive_by is not None:
            depart, arrive = arrive_by - duration, arrive_by
        else:
            depart = datetime.now(UTC)
            arrive = depart + duration

        if mode == Mode.RIDESHARE:
            instruction = f"Ride {est.distance_mi:.1f} mi to the venue"
        else:
            instruction = f"{_VERBS[mode]} {est.distance_mi:.1f} mi to the venue"
        step = RouteStep(
            mode=mode.value,
            instruction=instruction,
            duration_min=est.duration_min,
            distance_mi=est.distance_mi,
        )
        return RouteDetail(**est.model_dump(), depart_at=depart, arrive_at=arrive, steps=[step])
