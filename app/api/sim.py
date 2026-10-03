"""Simulator endpoints (§14.3). Mounted only when PROVIDER_MESSAGING=sim.

Unlike the real webhook, these await processing, so the outbox is ready on return.
"""

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.db import session as db_session
from app.models.conversation import InboundMessage

router = APIRouter(prefix="/sim")


class SimMessageIn(BaseModel):
    chat_id: str
    is_group: bool
    sender_handle: str
    sender_name: str | None = None
    text: str


class SimVoteIn(BaseModel):
    chat_id: str
    voter_handle: str
    option_label: Literal["A", "B", "C"]


@router.post("/message")
async def sim_message(payload: SimMessageIn, request: Request) -> dict[str, str]:
    deps = request.app.state.deps
    msg = InboundMessage(
        message_id=f"sim-{uuid.uuid4().hex}",
        ts=deps.clock(),
        **payload.model_dump(),
    )
    await request.app.state.chat_router.handle_message(msg)
    return {"message_id": msg.message_id}


@router.post("/vote")
async def sim_vote(payload: SimVoteIn, request: Request) -> dict[str, bool]:
    await request.app.state.chat_router.handle_vote(
        payload.chat_id, payload.voter_handle, payload.option_label
    )
    return {"ok": True}


@router.get("/outbox/{chat_id_or_handle}")
async def sim_outbox(chat_id_or_handle: str, request: Request) -> list[dict[str, Any]]:
    return request.app.state.deps.messaging.messages(chat_id_or_handle)


@router.post("/reset")
async def sim_reset(request: Request) -> dict[str, bool]:
    request.app.state.chat_router.shutdown()
    await db_session.reset_db()
    request.app.state.deps.messaging.clear()
    return {"ok": True}
