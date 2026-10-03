"""Simulator endpoints (§14.3). Mounted only when PROVIDER_MESSAGING=sim.

v3: every message is a DM; votes are just DMs ("A"). Unlike the real webhook, these
wait for processing (including the pipeline and delivery), so outboxes are ready on return.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.db import session as db_session
from app.models.conversation import InboundMessage

router = APIRouter(prefix="/sim")


class SimMessageIn(BaseModel):
    sender_handle: str
    text: str


@router.post("/message")
async def sim_message(payload: SimMessageIn, request: Request) -> dict[str, str]:
    deps = request.app.state.deps
    chat_router = request.app.state.chat_router
    msg = InboundMessage(
        message_id=f"sim-{uuid.uuid4().hex}",
        chat_id=f"any;-;{payload.sender_handle}",
        is_group=False,
        sender_handle=payload.sender_handle,
        text=payload.text,
        ts=deps.clock(),
    )
    await chat_router.handle_message(msg)
    await chat_router.drain()
    return {"message_id": msg.message_id}


@router.get("/outbox/{handle}")
async def sim_outbox(handle: str, request: Request) -> list[dict[str, Any]]:
    return request.app.state.deps.messaging.messages(handle)


@router.post("/reset")
async def sim_reset(request: Request) -> dict[str, bool]:
    request.app.state.chat_router.shutdown()
    await db_session.reset_db()
    request.app.state.deps.messaging.clear()
    return {"ok": True}
