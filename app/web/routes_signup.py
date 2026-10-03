"""Web signup: GET/POST /signup, then /signup/done/{token} with a "text the bot" link.

The site never sends the first text (free Photon lines can't message a number that
hasn't texted them). The person texts "start <TOKEN>" from their phone, which also
proves they own the number (app/onboarding/web_claim.py).
"""

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversation import copy
from app.db import queries
from app.db.session import session_factory
from app.db.tables import PendingSignupRow
from app.deps import Deps
from app.logging import get_logger, kv
from app.messaging.outbound import send_private
from app.models.identity import OnboardingState
from app.models.outbound import PrivateMessage
from app.planning.session import JOIN_CODE_ALPHABET, JOIN_CODE_LENGTH
from app.private import vault
from app.web import templates
from app.web.forms import parse_signup
from app.web.qr import qr_svg

log = get_logger(__name__)
router = APIRouter()


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo; stored times are UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def display_phone(phone: str) -> str:
    """+16075550000 → (607) 555-0000; other countries stay as they are."""
    if phone.startswith("+1") and len(phone) == 12:
        return f"({phone[2:5]}) {phone[5:8]}-{phone[8:]}"
    return phone


async def _assigned_line(deps: Deps, signup: PendingSignupRow) -> str | None:
    """Register this phone with Photon (idempotent) and return the line they should text."""
    if deps.users is None:
        return None
    try:
        return await deps.users.register(signup.phone, signup.first_name)
    except Exception:
        log.warning(kv("assigned_line_failed"), exc_info=True)
        return None


def sms_link(bot_number: str, token: str) -> str:
    """Opens Messages with "start <TOKEN>" typed in. `?&body=` works on iOS and Android."""
    return f"sms:{bot_number}?&body={quote(f'start {token}')}"


async def _new_token(db: AsyncSession) -> str:
    """Same alphabet and length as join codes; never reused."""
    while True:
        token = "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))
        if await queries.signup_by_token(db, token) is None:
            return token


def _render_form(
    request: Request, values: dict, errors: dict, notice: str | None = None, status: int = 200
) -> HTMLResponse:
    deps: Deps = request.app.state.deps
    return templates.TemplateResponse(
        request,
        "signup.html",
        {
            "app_name": deps.settings.app_name,
            "banks": vault.bank_choices(),
            "values": values,
            "errors": errors,
            "notice": notice,
        },
        status_code=status,
    )


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request) -> HTMLResponse:
    return _render_form(request, values={}, errors={})


@router.post("/signup", response_model=None)
async def signup_submit(
    request: Request,
    first_name: str = Form(""),
    phone: str = Form(""),
    bank_code: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    deps: Deps = request.app.state.deps
    values = {"first_name": first_name, "phone": phone, "bank_code": bank_code}
    codes = {code for code, _ in vault.bank_choices()}
    form, errors = parse_signup(values, codes)
    if form is None:
        return _render_form(request, values, errors, status=422)

    now = deps.clock()
    async with session_factory()() as db:
        existing = await queries.user_by_handle(db, form.phone)
        if existing is not None and existing.onboarding_state == OnboardingState.READY:
            notice = "That number is already set up. Just text the bot @plan to start a plan."
            return _render_form(request, values, {}, notice=notice, status=409)
        # One live code per phone: an earlier unclaimed signup stops working.
        await db.execute(
            update(PendingSignupRow)
            .where(
                PendingSignupRow.phone == form.phone,
                PendingSignupRow.claimed.is_(False),
                PendingSignupRow.expires_at > now,
            )
            .values(expires_at=now)
        )
        token = await _new_token(db)
        signup = PendingSignupRow(
            token=token,
            first_name=form.first_name,
            phone=form.phone,
            bank_code=form.bank_code,
            created_at=now,
            expires_at=now + timedelta(hours=deps.settings.signup_token_ttl_hours),
        )
        db.add(signup)
        await db.commit()
        # Photon only lets the bot talk to registered numbers, and assigns each one the
        # line to text. If this fails, the done page falls back and retries on refresh.
        signup.bot_phone = await _assigned_line(deps, signup)
        await db.commit()
    log.info(kv("web_signup_created", has_line=signup.bot_phone is not None))

    if deps.settings.photon_can_initiate:
        nudge = PrivateMessage(text=copy.web_signup_nudge(form.first_name, token))
        await send_private(deps.messaging, form.phone, nudge)
    return RedirectResponse(f"/signup/done/{token}", status_code=303)


@router.get("/signup/done/{token}", response_class=HTMLResponse)
async def signup_done(request: Request, token: str) -> HTMLResponse:
    """Shows only the token, the number to text, and how: never the name, bank, or the
    person's own phone."""
    deps: Deps = request.app.state.deps
    bot_number = None
    async with session_factory()() as db:
        signup = await queries.signup_by_token(db, token)
        if signup is None:
            state = "unknown"
        elif signup.claimed:
            state = "claimed"
        elif _as_utc(signup.expires_at) <= deps.clock():
            state = "expired"
        else:
            state = "pending"
            if signup.bot_phone is None:  # Photon didn't answer at signup: try again
                signup.bot_phone = await _assigned_line(deps, signup)
                await db.commit()
            bot_number = signup.bot_phone or deps.settings.bot_phone_number or None

    link = sms_link(bot_number, token.upper()) if bot_number else None
    return templates.TemplateResponse(
        request,
        "signup_done.html",
        {
            "app_name": deps.settings.app_name,
            "state": state,
            "token": token.upper(),
            "bot_number": display_phone(bot_number) if bot_number else None,
            "sms_link": link,
            "qr_svg": qr_svg(link) if link else None,
        },
        status_code=404 if state == "unknown" else 200,
    )
