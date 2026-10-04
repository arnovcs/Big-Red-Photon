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
            {"id": "u0", "phoneNumber": "+16075559999", "assignedPhoneNumber": "+14155550001"},
            {"id": "u9", "phoneNumber": PHONE, "assignedPhoneNumber": "+14155550002"},
        ]
    )
    user = asyncio.run(provider(transport).register(PHONE, "Sam"))
    assert (user.user_id, user.line) == ("u9", "+14155550002")
    assert [c.method for c in calls] == ["GET"]
    assert calls[0].url.params["search"] == PHONE
    expected = base64.b64encode(f"{PROJECT}:{SECRET}".encode()).decode()
    assert calls[0].headers["authorization"] == f"Basic {expected}"


def test_new_user_is_created_as_shared_and_returns_line() -> None:
    created = {"id": "u1", "phoneNumber": PHONE, "assignedPhoneNumber": "+14155550003"}
    transport, calls = users_api([], created)
    user = asyncio.run(provider(transport).register(PHONE, "Sam", "sam@example.com"))
    assert (user.user_id, user.line) == ("u1", "+14155550003")
    assert [c.method for c in calls] == ["GET", "POST"]
    assert json.loads(calls[1].content) == {
        "type": "shared",
        "phoneNumber": PHONE,
        "firstName": "Sam",
        "email": "sam@example.com",  # stored on the user (the API sends no invite)
    }


def test_no_email_means_no_email_field() -> None:
    created = {"id": "u1", "phoneNumber": PHONE, "assignedPhoneNumber": "+14155550005"}
    transport, calls = users_api([], created)
    asyncio.run(provider(transport).register(PHONE, "Sam"))
    assert "email" not in json.loads(calls[1].content)


def test_existing_user_is_not_recreated() -> None:
    existing = {"id": "u2", "phoneNumber": PHONE, "assignedPhoneNumber": "+1415555000"}
    transport, calls = users_api([existing])
    asyncio.run(provider(transport).register(PHONE, "Sam", "sam@example.com"))
    assert [c.method for c in calls] == ["GET"]


def test_no_line_yet_still_returns_the_user_and_errors_return_none() -> None:
    transport, _ = users_api([], {"id": "u1", "phoneNumber": PHONE, "assignedPhoneNumber": None})
    user = asyncio.run(provider(transport).register(PHONE, "Sam"))
    assert (user.user_id, user.line) == ("u1", None)  # the opt-in link still works
    transport, _ = users_api([], {"phoneNumber": PHONE})  # no id: unusable
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
    assert asyncio.run(provider(transport).register(PHONE, "Sam")).line == "+14155550004"


def test_succeed_false_is_a_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"succeed": False, "error": "nope"})

    p = provider(httpx.MockTransport(handler))
    assert asyncio.run(p.register(PHONE, "Sam")) is None


def test_opt_in_link_is_photons_public_redirect() -> None:
    p = provider(httpx.MockTransport(lambda r: httpx.Response(500)))
    assert p.opt_in_link("6660-ab/c", "start K7QP") == (
        "https://spectrum.photon.codes/users/6660-ab%2Fc/redirect?msg=start%20K7QP"
    )


# --- invite path: Photon's dashboard API with an account token -------------------------------


def invite_api(dashboard_status: int = 200):
    """Fake dashboard + Spectrum APIs: the dashboard create makes the user appear."""
    calls: list[httpx.Request] = []
    users: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "app.photon.codes":
            if dashboard_status != 200:
                return httpx.Response(dashboard_status, json={"error": "unauthorized"})
            body = json.loads(request.content)
            user = {
                "id": "u7",
                "phoneNumber": body["phoneNumber"],
                "assignedPhoneNumber": "+14155550007",
            }
            users.append(user)
            return httpx.Response(200, json={"user": user})
        if request.method == "GET":
            return httpx.Response(200, json={"succeed": True, "data": {"users": users}})
        user = {"id": "u8", "phoneNumber": PHONE, "assignedPhoneNumber": "+14155550008"}
        return httpx.Response(200, json={"succeed": True, "data": user})

    return httpx.MockTransport(handler), calls


def test_with_a_token_new_users_are_added_with_photons_invite() -> None:
    transport, calls = invite_api()
    p = provider(transport, photon_token="acct-token")
    user = asyncio.run(p.register(PHONE, "Sam", "sam@example.com", "Rivera"))
    assert (user.user_id, user.line) == ("u7", "+14155550007")
    lookup, create, relookup = calls
    assert create.method == "POST"
    assert str(create.url) == f"https://app.photon.codes/api/projects/{PROJECT}/spectrum/users"
    assert create.headers["authorization"] == "Bearer acct-token"
    assert json.loads(create.content) == {
        "firstName": "Sam",
        "lastName": "Rivera",  # required: without it Photon answers 422
        "email": "sam@example.com",
        "phoneNumber": PHONE,
        "sendInvite": True,
    }
    assert lookup.method == relookup.method == "GET"  # Spectrum API, for id + line


def test_an_expired_token_falls_back_to_creating_without_an_invite() -> None:
    transport, calls = invite_api(dashboard_status=401)
    p = provider(transport, photon_token="expired")
    user = asyncio.run(p.register(PHONE, "Sam", "sam@example.com", "Rivera"))
    assert (user.user_id, user.line) == ("u8", "+14155550008")  # still registered
    assert [(c.url.host, c.method) for c in calls] == [
        ("spectrum.photon.codes", "GET"),
        ("app.photon.codes", "POST"),
        ("spectrum.photon.codes", "POST"),
    ]


def test_opted_in_users_are_left_alone() -> None:
    transport, calls = users_api(
        [
            {
                "id": "u2",
                "phoneNumber": PHONE,
                "assignedPhoneNumber": "+14155550002",
                "meta": {"opt_in": True},
            }
        ]
    )
    p = provider(transport, photon_token="acct-token")
    asyncio.run(p.register(PHONE, "Sam", "sam@example.com", "Rivera"))
    assert [c.url.host for c in calls] == ["spectrum.photon.codes"]  # no invite


def test_registered_but_not_opted_in_gets_the_invite_again() -> None:
    existing = {"id": "u3", "phoneNumber": PHONE, "assignedPhoneNumber": "+14155550003", "meta": {}}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "app.photon.codes":
            return httpx.Response(200, json={"success": True, "user": existing})
        return httpx.Response(200, json={"succeed": True, "data": {"users": [existing]}})

    p = provider(httpx.MockTransport(handler), photon_token="acct-token")
    user = asyncio.run(p.register(PHONE, "Sam", "sam@example.com", "Rivera"))
    assert (user.user_id, user.line) == ("u3", "+14155550003")
    invite = next(c for c in calls if c.url.host == "app.photon.codes")
    assert json.loads(invite.content)["sendInvite"] is True
    assert [c.method for c in calls if c.url.host != "app.photon.codes"] == ["GET", "GET"]


def test_invite_needs_every_field_so_a_missing_last_name_skips_it() -> None:
    transport, calls = invite_api()
    p = provider(transport, photon_token="acct-token")
    user = asyncio.run(p.register(PHONE, "Sam", "sam@example.com"))  # no last name
    assert user is not None  # still registered, without an invite
    assert "app.photon.codes" not in [c.url.host for c in calls]
