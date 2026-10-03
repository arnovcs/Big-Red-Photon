"""PrivacyGuard leak tests (§11): try to leak each private value through each group
channel and through personal itineraries, and assert it never reaches a phone."""

import asyncio
import logging
from decimal import Decimal

import pytest

from app.conversation import copy
from app.messaging.guard import MemberSecrets, PrivacyGuard
from app.messaging.outbound import send_group, send_private
from app.models.outbound import GroupPlanOption, GroupSafeMessage, PrivateMessage
from app.providers.mock.sim_messaging import SimMessaging

MAYA = MemberSecrets(
    handle="+16075550101",
    spend_limit_usd=Decimal(30),
    origin_label="Collegetown",
    nessie_customer_id="66fe1a2b9683f20dd5189f01",
    preference_texts=("I'm starving tonight",),
)
SAM = MemberSecrets(
    handle="+16075550102",
    spend_limit_usd=Decimal(50),
    origin_label="Robert Purcell Community Center",
    preference_texts=("no sushi pls", "something we haven't tried?"),
)
JORDAN = MemberSecrets(
    handle="jordan@example.com",
    spend_limit_usd=Decimal("15.50"),
    origin_label="Ithaca Commons",
    preference_texts=("I have to be back by 9, and nothing too far",),
)
MEMBERS = [MAYA, SAM, JORDAN]
HANDLES = [m.handle for m in MEMBERS]
VENUES = ["Collegetown Bagels", "415 College Ave", "Viva Taqueria", "101 N Aurora St"]

# Every private value, in the shapes it might take in text.
LEAKS = {
    "limit $": "$30",
    "limit $ cents": "$50.00",
    "limit words": "30 dollars",
    "limit bare cents": "50.00",
    "limit minus one": "$29",
    "limit plus one": "$51",
    "fractional limit": "$15.50",
    "origin": "Collegetown",
    "origin lowercase": "robert purcell community center",
    "phone handle": "+16075550102",
    "phone formatted": "(607) 555-0101",
    "email handle": "jordan@example.com",
    "nessie id": "66fe1a2b9683f20dd5189f01",
    "preference": "no sushi pls",
    "preference reworded case": "I'M STARVING TONIGHT",
    "stranger's phone": "607-555-9876",
}


def _option(label: str, title: str = "Viva Taqueria — Mexican", blurb: str = "") -> GroupPlanOption:
    return GroupPlanOption(
        label=label,
        title=title,
        max_travel_min=12,
        walking_level="low",
        price_tier="$",
        arrival_window_min=0,
        blurb=blurb or copy.plan_blurb(12),
    )


def _poll(options: list[GroupPlanOption]) -> GroupSafeMessage:
    return GroupSafeMessage(text=copy.poll_message(options), poll=options)


# Each channel builds the message the real code would send, with the leak injected
# where that channel takes outside input (LLM text, venue data, a display name). For
# fixed copy (vote counts, errors), the leak is appended, as a future copy bug would.
CHANNELS = {
    "explanation": lambda leak: _poll([_option("A", blurb=f"Great pick, under {leak}.")]),
    "poll title": lambda leak: _poll([_option("A", title=f"Viva ({leak})")]),
    "poll text": lambda leak: GroupSafeMessage(text=f"{copy.poll_message([_option('A')])} {leak}"),
    "join notice": lambda leak: GroupSafeMessage(text=copy.joined(leak, 2)),
    "vote count": lambda leak: GroupSafeMessage(text=f"{copy.votes_progress(1, 3)} ({leak})"),
    "nothing fits": lambda leak: GroupSafeMessage(text=copy.nothing_fits(f"Try {leak}.")),
    "pipeline error": lambda leak: GroupSafeMessage(text=f"{copy.PIPELINE_FAILED} {leak}"),
    "confirmation": lambda leak: GroupSafeMessage(text=f"🎉 Plan A: Viva Taqueria. {leak}"),
}


def _guard() -> PrivacyGuard:
    return PrivacyGuard(members=MEMBERS, public_terms=VENUES)


def _sent_group(msg: GroupSafeMessage) -> list[dict]:
    sim = SimMessaging()
    asyncio.run(send_group(sim, HANDLES, msg, _guard()))
    return [sim.messages(h) for h in HANDLES]


@pytest.mark.parametrize("leak", LEAKS.values(), ids=LEAKS.keys())
@pytest.mark.parametrize("channel", CHANNELS.keys())
def test_guard_blocks_every_private_value_on_every_group_channel(
    channel: str, leak: str, caplog: pytest.LogCaptureFixture
) -> None:
    msg = CHANNELS[channel](leak)
    with caplog.at_level(logging.WARNING):
        outboxes = _sent_group(msg)

    for outbox in outboxes:
        (sent,) = outbox
        seen = sent["text"] + " ".join(f"{o['title']} {o['blurb']}" for o in sent["poll"] or [])
        assert leak.lower() not in seen.lower()
    # Logged as a privacy block, with the kind of value but never the value itself.
    assert "privacy_block" in caplog.text
    assert leak.lower() not in caplog.text.lower()


def test_blocked_poll_keeps_its_options_when_the_template_blurb_is_clean() -> None:
    msg = _poll([_option("A", blurb="Cheap enough for the $15.50 budget."), _option("B")])
    (sent, *_) = _sent_group(msg)
    assert [o["label"] for o in sent[0]["poll"]] == ["A", "B"]
    assert sent[0]["poll"][0]["blurb"] == copy.plan_blurb(12)
    assert "$15" not in sent[0]["text"]


def test_blocked_poll_title_falls_back_to_a_notice() -> None:
    msg = _poll([_option("A", title="Viva (near Ithaca Commons)")])
    (sent, *_) = _sent_group(msg)
    assert sent[0] == {"kind": "group", "text": copy.PRIVACY_HELD_BACK, "poll": None}


@pytest.mark.parametrize(
    "text",
    [
        copy.poll_message([_option("A", title="Collegetown Bagels — Bagels"), _option("B")]),
        copy.joined("Sam", 2),
        copy.votes_progress(2, 3),
        "🎉 Plan A: Collegetown Bagels. Everyone arrives around 6:30. Your route is below 👇",
        "A: Viva Taqueria — Mexican · ≤30 min for everyone · $$ · arrive together",
        copy.nothing_fits("Being open to more than thai could help."),
        copy.CANCELLED,
    ],
)
def test_normal_group_messages_are_not_blocked(text: str) -> None:
    assert not _guard().check_group(text).blocked


# --- personal itineraries --------------------------------------------------------


ITINERARY = (
    "Your plan for tonight: Viva Taqueria, 101 N Aurora St.\n"
    "🚲 Leave by 6:13 and bike about 5 min.\n"
    "Arrive ~6:18. Estimated total: ~$12 (food)."
)


@pytest.mark.parametrize(
    "leak",
    [
        "$50",  # Sam's limit
        "Robert Purcell Community Center",  # Sam's origin
        "+16075550102",  # Sam's handle
        "jordan@example.com",  # Jordan's handle
        "no sushi pls",  # Sam's preference
        "I have to be back by 9, and nothing too far",  # Jordan's preference
    ],
)
def test_itinerary_never_carries_another_members_private_value(
    leak: str, caplog: pytest.LogCaptureFixture
) -> None:
    sim = SimMessaging()
    msg = PrivateMessage(text=f"{ITINERARY}\n(Sam said: {leak})")
    with caplog.at_level(logging.WARNING):
        asyncio.run(send_private(sim, MAYA.handle, msg, _guard(), frozenset({12})))
    (sent,) = sim.messages(MAYA.handle)
    assert sent["text"] == copy.PRIVACY_HELD_BACK_PRIVATE
    assert "privacy_block" in caplog.text and leak not in caplog.text


def test_itinerary_may_show_the_recipients_own_values() -> None:
    own = f"{ITINERARY}\nYou said: I'm starving tonight. Leaving from Collegetown. Budget $30."
    assert not _guard().check_private(MAYA.handle, own, frozenset({12})).blocked
    assert not _guard().check_private(MAYA.handle, ITINERARY, frozenset({12})).blocked


def test_itinerary_amount_equal_to_someone_elses_limit_is_fine_if_it_is_its_own() -> None:
    # Sam's ride-share total happens to be ~$30, which is also Maya's limit.
    text = "Arrive ~6:18. Estimated total: ~$30 (food ~$15 + ride ~$15)."
    guard = _guard()
    assert not guard.check_private(SAM.handle, text, frozenset({15, 30})).blocked
    assert guard.check_private(SAM.handle, text, frozenset()).reasons == ("limit",)


def test_members_sharing_a_start_do_not_block_each_other() -> None:
    twin = MemberSecrets(handle="+16075550199", origin_label="Collegetown")
    guard = PrivacyGuard(members=[MAYA, twin])
    assert not guard.check_private(twin.handle, "Head out from Collegetown.").blocked
