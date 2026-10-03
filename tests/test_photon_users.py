"""PhotonUsers against a fake Spectrum API (shapes from spectrum.photon.codes/openapi/json)."""

import asyncio
import base64
import json

import httpx

from app.providers.real.photon_users import PhotonUsers
from app.settings import Settings

PROJECT, SECRET = "proj-123", "s3cret"
PHONE = "+16075550102"


def users_api(
    existing: list[dict],
    created: dict | None = None,
    fail: int | None = None,
    envelope: bool = True,
):
    """Fake Spectrum users API. Live responses are {"succeed": true, "data": {...}}."""

    def wrap(body):
        return {"succeed": True, "data": body} if envelope else body

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if fail:
            return httpx.Response(fail, json={"error": "nope"})
        assert request.url.path == f"/projects/{PROJECT}/users/"
        if request.method == "GET":
            return httpx.Response(200, json=wrap({"users": existing, "total": len(existing)}))
        return httpx.Response(200, json=wrap(created))

    return httpx.MockTransport(handler), calls


def provider(transport, **overrides) -> PhotonUsers:
    settings = Settings(
        _env_file=None,
        photon_project_id=PROJECT,
        photon_project_secret=SECRET,
        **overrides,
    )
    return PhotonUsers(settings, transport=transport)


def test_existing_user_returns_their_assigned_line_without_creating() -> None:
    transport, calls = users_api(
        [
            {"phoneNumber": "+16075559999", "assignedPhoneNumber": "+14155550001"},
            {"phoneNumber": PHONE, "assignedPhoneNumber": "+14155550002"},
        ]
    )
    assert asyncio.run(provider(transport).register(PHONE, "Sam")) == "+14155550002"
    assert [c.method for c in calls] == ["GET"]
    assert calls[0].url.params["search"] == PHONE
    expected = base64.b64encode(f"{PROJECT}:{SECRET}".encode()).decode()
    assert calls[0].headers["authorization"] == f"Basic {expected}"


def test_new_user_is_created_as_shared_and_returns_line() -> None:
    created = {"id": "u1", "phoneNumber": PHONE, "assignedPhoneNumber": "+14155550003"}
    transport, calls = users_api([], created)
    assert asyncio.run(provider(transport).register(PHONE, "Sam")) == "+14155550003"
    assert [c.method for c in calls] == ["GET", "POST"]
    assert json.loads(calls[1].content) == {
        "type": "shared",
        "phoneNumber": PHONE,
        "firstName": "Sam",
    }


def test_no_line_yet_or_errors_return_none() -> None:
    transport, _ = users_api([], {"id": "u1", "phoneNumber": PHONE, "assignedPhoneNumber": None})
    assert asyncio.run(provider(transport).register(PHONE, "Sam")) is None
    for status in (401, 500):
        transport, _ = users_api([], fail=status)
        assert asyncio.run(provider(transport).register(PHONE, "Sam")) is None


def test_not_configured_makes_no_call() -> None:
    transport, calls = users_api([])
    p = PhotonUsers(Settings(_env_file=None), transport=transport)
    assert asyncio.run(p.register(PHONE, "Sam")) is None
    assert calls == []


def test_bare_bodies_without_the_envelope_also_work() -> None:
    created = {"id": "u1", "phoneNumber": PHONE, "assignedPhoneNumber": "+14155550004"}
    transport, _ = users_api([], created, envelope=False)
    assert asyncio.run(provider(transport).register(PHONE, "Sam")) == "+14155550004"


def test_succeed_false_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"succeed": False, "error": "nope"})

    p = provider(httpx.MockTransport(handler))
    assert asyncio.run(p.register(PHONE, "Sam")) is None
