"""Google Maps directions links in itinerary DMs."""

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from app.delivery.itinerary import directions_url
from app.messaging.guard import MemberSecrets, PrivacyGuard
from app.models.private import LatLng
from app.models.routing import Mode

VENUE = LatLng(lat=42.43971234, lng=-76.49712345)


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


@pytest.mark.parametrize(
    ("mode", "travelmode"),
    [
        (Mode.WALK, "walking"),
        (Mode.BIKE, "bicycling"),
        (Mode.DRIVE, "driving"),
        (Mode.RIDESHARE, "driving"),
    ],
)
def test_directions_url_per_mode(mode: Mode, travelmode: str) -> None:
    url = directions_url(VENUE, mode)
    assert url.startswith("https://www.google.com/maps/dir/?")
    q = query(url)
    assert q == {"api": "1", "destination": "42.43971,-76.49712", "travelmode": travelmode}


def test_link_never_contains_a_starting_point() -> None:
    assert "origin" not in query(directions_url(VENUE, Mode.WALK))


def test_privacy_guard_does_not_block_a_dm_with_a_link() -> None:
    other = MemberSecrets(handle="+16075550102", spend_limit_usd=Decimal(50))
    guard = PrivacyGuard(members=[other])
    text = "🗺️ Directions: " + directions_url(VENUE, Mode.WALK)
    assert not guard.check_private("+16075550101", text).blocked
