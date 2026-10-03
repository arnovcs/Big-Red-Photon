"""Web signup → "start <TOKEN>" by text → READY, plus the tunnel guard."""

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.conversation import copy
from app.db import queries
from app.db.session import session_factory
from app.deps import build_deps
from app.main import create_app
from app.onboarding.web_claim import normalize_phone, parse_start_token
from app.settings import Settings
from tests.places_stub import StubPlaces
from tests.test_end_to_end import MAYA, dm, onboard, outbox

NOW = datetime(2026, 10, 3, 22, 0, tzinfo=UTC)
BOT_NUMBER = "+16075550000"
SAM_PHONE = "+16075550102"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def make_client(tmp_path, users=None, **overrides) -> tuple[TestClient, Clock]:
    values = {
        "provider_messaging": "sim",
        "nessie_api_key": "",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'web.db'}",
        "bot_phone_number": BOT_NUMBER,
        "app_name": "TestApp",
    }
    settings = Settings(_env_file=None, **(values | overrides))
    clock = Clock()
    app = create_app(build_deps(settings, clock=clock, places=StubPlaces(), users=users))
    return TestClient(app), clock


@pytest.fixture
def web(tmp_path):
    client, clock = make_client(tmp_path)
    with client:
        yield client, clock


def sign_up(client: TestClient, name="Sam", phone="(607) 555-0102", bank="SAM1") -> str:
    """Submit the form and return the token from the done page."""
    resp = client.post("/signup", data={"first_name": name, "phone": phone, "bank_code": bank})
    assert resp.status_code == 200, resp.text  # after the 303 redirect
    return re.search(r"data-token>([A-Z0-9]{4})<", resp.text).group(1)


def user_state(client: TestClient, handle: str) -> tuple[str, str] | None:
    """(display name, onboarding state) for this handle, read from the database."""

    async def load():
        async with session_factory()() as db:
            user = await queries.user_by_handle(db, handle)
            return (user.display_name, user.onboarding_state) if user else None

    return client.portal.call(load)


# --- helpers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(607) 555-0102", "+16075550102"),
        ("607.555.0102", "+16075550102"),
        ("1 607 555 0102", "+16075550102"),
        ("+44 20 7946 0958", "+442079460958"),
        ("555-0102", None),
        ("sam@example.com", None),
        ("call me", None),
    ],
)
def test_phone_numbers_normalize_to_imessage_handles(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


def test_start_token_parsing() -> None:
    assert parse_start_token("start k7qp") == "K7QP"
    assert parse_start_token("  Start K7QP! ") == "K7QP"
    assert parse_start_token("start") is None
    assert parse_start_token("start the plan") is None


# --- signup form -----------------------------------------------------------------------


def test_signup_page_lists_demo_banks_without_private_data(web) -> None:
    client, _ = web
    html = client.get("/signup").text
    assert "TestApp" in html
    for name in ("Maya", "Sam", "Jordan"):
        assert f"{name}'s account" in html
    # Persona balances, limits, and Nessie ids stay in the vault.
    personas = json.loads(Path("fixtures/personas.json").read_text())["personas"]
    for p in personas:
        for secret in (
            p.get("nessie_customer_id"),
            f"${p['checking_balance']}",
            f"${p['expected_limit']}",
        ):
            if secret:
                assert str(secret) not in html


@pytest.mark.parametrize(
    ("data", "field_error"),
    [
        ({"first_name": "Sam", "phone": "555-0102", "bank_code": "SAM1"}, "phone number you use"),
        ({"first_name": "Sam", "phone": "sam@example.com", "bank_code": "SAM1"}, "phone number"),
        ({"first_name": "", "phone": "6075550102", "bank_code": "SAM1"}, "enter your first name"),
        ({"first_name": "Sam", "phone": "6075550102", "bank_code": "NOPE"}, "Choose one of"),
    ],
)
def test_signup_rejects_bad_input_and_keeps_what_was_typed(web, data, field_error) -> None:
    client, _ = web
    resp = client.post("/signup", data=data)
    assert resp.status_code == 422
    assert field_error in resp.text
    assert "Please fix the highlighted fields." in resp.text
    if data["phone"]:
        assert f'value="{data["phone"]}"' in resp.text


def test_signup_for_a_phone_that_is_already_set_up(web) -> None:
    client, _ = web
    onboard(client, MAYA)  # set up by text
    resp = client.post(
        "/signup", data={"first_name": "Maya", "phone": "(607) 555-0101", "bank_code": "MAYA1"}
    )
    assert resp.status_code == 409
    assert "already set up" in resp.text
    assert "data-token" not in resp.text


def test_done_page_shows_only_the_token_and_how_to_send_it(web) -> None:
    client, _ = web
    token = sign_up(client, name="Sam")
    html = client.get(f"/signup/done/{token}").text
    assert f'start <span class="tracking-[0.15em]" data-token>{token}</span>' in html
    assert "data-bot-number>(607) 555-0000<" in html
    assert f'href="sms:{BOT_NUMBER}?&amp;body=start%20{token}"' in html
    assert "<svg" in html  # QR code of the same link
    # Never the name, phone, or bank on this page.
    for private in ("Sam", "555-0102", "6075550102", "SAM1"):
        assert private not in html


def test_done_page_for_unknown_and_expired_tokens(web) -> None:
    client, clock = web
    assert client.get("/signup/done/ZZZZ").status_code == 404
    token = sign_up(client)
    clock.now += timedelta(hours=25)
    assert "This code expired" in client.get(f"/signup/done/{token}").text


def test_a_new_signup_replaces_the_earlier_code_for_that_phone(web) -> None:
    client, _ = web
    first = sign_up(client)
    second = sign_up(client)
    assert first != second
    assert dm(client, SAM_PHONE, f"start {first}").startswith(copy.CLAIM_EXPIRED)
    assert dm(client, SAM_PHONE, f"start {second}").startswith("thanks Sam!")


def test_no_text_is_sent_from_the_form_unless_photon_can_initiate(tmp_path) -> None:
    client, _ = make_client(tmp_path)
    with client:
        sign_up(client)
        assert outbox(client, SAM_PHONE) == []

    (tmp_path / "initiate").mkdir()
    client, _ = make_client(tmp_path / "initiate", photon_can_initiate=True)
    with client:
        token = sign_up(client)
        (nudge,) = outbox(client, SAM_PHONE)
        assert nudge["text"] == copy.web_signup_nudge("Sam", token)


# --- claiming the token by text -------------------------------------------------------------


def test_claim_success_links_bank_then_confirms_budget_then_intro(web) -> None:
    client, _ = web
    token = sign_up(client, name="Sam", bank="SAM1")
    reply = dm(client, SAM_PHONE, f"start {token}")
    assert reply == copy.claim_linked("Sam", Decimal(50))  # Sam's demo bank suggests $50
    assert dm(client, SAM_PHONE, "40") == copy.web_intro("Sam")  # override, then the intro
    # READY: plan commands work, with no setup location step.
    assert "join " in dm(client, SAM_PHONE, "@plan")


def test_claim_with_an_expired_token(web) -> None:
    client, clock = web
    token = sign_up(client)
    clock.now += timedelta(hours=24, minutes=1)
    dm(client, SAM_PHONE, f"start {token}")
    texts = [m["text"] for m in outbox(client, SAM_PHONE)]
    assert texts[0] == copy.CLAIM_EXPIRED
    assert copy.ASK_NAME in texts[1]  # falls back to normal setup


def test_claim_twice(web) -> None:
    client, _ = web
    token = sign_up(client)
    dm(client, SAM_PHONE, f"start {token}")
    dm(client, SAM_PHONE, f"start {token}")
    *_, used, reprompt = outbox(client, SAM_PHONE)
    assert used["text"] == copy.CLAIM_USED
    assert reprompt["text"] == copy.LIMIT_INVALID  # still on the budget question


def test_claim_from_a_different_phone(web) -> None:
    client, _ = web
    token = sign_up(client, phone="(607) 555-0102")
    intruder = "+16075550199"
    dm(client, intruder, f"start {token}")
    texts = [m["text"] for m in outbox(client, intruder)]
    assert texts[0] == copy.CLAIM_WRONG_PHONE
    assert copy.ASK_NAME in texts[1]
    # The real owner can still claim it.
    assert "demo bank's linked" in dm(client, SAM_PHONE, f"start {token}")


def test_unknown_token_and_plain_start(web) -> None:
    client, _ = web
    dm(client, SAM_PHONE, "start ZZZZ")
    assert outbox(client, SAM_PHONE)[0]["text"] == copy.CLAIM_UNKNOWN
    other = "+16075550198"
    assert copy.ASK_NAME in dm(client, other, "start")  # plain "start": normal setup


# --- tunnel guard ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Cf-Connecting-Ip", "X-Forwarded-Host"])
def test_tunneled_requests_only_reach_the_web_pages(web, header: str) -> None:
    client, _ = web
    via_tunnel = {header: "203.0.113.7"}
    fake_dm = {
        "message_id": "x1",
        "chat_id": "any;-;+16075550102",
        "is_group": False,
        "sender_handle": SAM_PHONE,
        "text": "start ABCD",
        "ts": NOW.isoformat(),
    }
    assert (
        client.post("/webhooks/photon/message", json=fake_dm, headers=via_tunnel).status_code == 404
    )
    assert (
        client.post(
            "/sim/message", json={"sender_handle": SAM_PHONE, "text": "hi"}, headers=via_tunnel
        ).status_code
        == 404
    )
    assert client.get("/health", headers=via_tunnel).status_code == 404
    assert client.get("/signup", headers=via_tunnel).status_code == 200
    assert client.get("/signup/done/ZZZZ", headers=via_tunnel).status_code == 404  # page's own 404
    assert client.get("/dashboard", headers=via_tunnel).status_code == 404  # no dashboard any more
    assert client.get("/static/app.css", headers=via_tunnel).status_code == 200
    # Locally (the bridge), everything still works.
    assert client.get("/health").status_code == 200


# --- end to end ---------------------------------------------------------------------------------


def test_web_signup_to_ready_end_to_end(web) -> None:
    client, _ = web
    token = sign_up(client, name="Sam", phone="607-555-0102", bank="SAM1")
    assert user_state(client, SAM_PHONE) is None  # nobody texted yet

    assert "demo bank's linked" in dm(client, SAM_PHONE, f"start {token.lower()}")
    assert dm(client, SAM_PHONE, "yes") == copy.web_intro("Sam")
    intro = outbox(client, SAM_PHONE)[-1]
    assert intro == {"kind": "private", "text": copy.web_intro("Sam"), "image_path": None}

    assert user_state(client, SAM_PHONE) == ("Sam", "ready")
    assert "You're all set" in client.get(f"/signup/done/{token}").text


def test_there_is_no_team_dashboard(web) -> None:
    client, _ = web
    assert client.get("/dashboard").status_code == 404
    assert client.get("/dashboard/login").status_code == 404


# --- the number to text (Photon users) -------------------------------------------------------


class FakeUsers:
    """Stands in for Photon: assigns a line, or (line=None) has none yet."""

    def __init__(self, line: str | None) -> None:
        self.line = line
        self.calls: list[tuple[str, str]] = []

    async def register(self, phone: str, first_name: str) -> str | None:
        self.calls.append((phone, first_name))
        return self.line


def test_signup_registers_the_phone_and_shows_its_assigned_line(tmp_path) -> None:
    users = FakeUsers("+14155550123")
    client, _ = make_client(tmp_path, users=users)
    with client:
        token = sign_up(client, name="Sam", phone="607-555-0102")
        html = client.get(f"/signup/done/{token}").text
    assert users.calls[0] == (SAM_PHONE, "Sam")  # registered with Photon at signup
    assert "data-bot-number>(415) 555-0123<" in html
    assert f'href="sms:+14155550123?&amp;body=start%20{token}"' in html
    assert BOT_NUMBER not in html and "(607) 555-0000" not in html  # not the fallback


def test_no_assigned_line_falls_back_to_bot_phone_number(tmp_path) -> None:
    client, _ = make_client(tmp_path, users=FakeUsers(None))
    with client:
        html = client.get(f"/signup/done/{sign_up(client)}").text
    assert "data-bot-number>(607) 555-0000<" in html


def test_no_number_at_all_asks_to_refresh_and_retries(tmp_path) -> None:
    users = FakeUsers(None)
    client, _ = make_client(tmp_path, users=users, bot_phone_number="")
    with client:
        token = sign_up(client)
        html = client.get(f"/signup/done/{token}").text
        assert "still getting your number" in html
        assert "sms:" not in html
        users.line = "+14155550124"  # Photon catches up
        html = client.get(f"/signup/done/{token}").text
    assert "data-bot-number>(415) 555-0124<" in html
    assert len(users.calls) == 4  # signup, its redirect to the done page, two more views
