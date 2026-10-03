"""Bridge webhooks (§9.1). Return immediately; processing runs in the background."""

from fastapi import APIRouter, BackgroundTasks, Request
from pydantic import BaseModel

from app.decision.poll import LABELS
from app.models.conversation import InboundMessage

router = APIRouter(prefix="/webhooks/photon")


class PollVotePayload(BaseModel):
    poll_id: str
    chat_id: str
    voter_handle: str
    option_index: int


@router.post("/message")
async def message(
    payload: InboundMessage, request: Request, background: BackgroundTasks
) -> dict[str, bool]:
    background.add_task(request.app.state.chat_router.handle_message, payload)
    return {"ok": True}


@router.post("/poll_vote")
async def poll_vote(
    payload: PollVotePayload, request: Request, background: BackgroundTasks
) -> dict[str, bool]:
    if 0 <= payload.option_index < len(LABELS):
        background.add_task(
            request.app.state.chat_router.handle_vote,
            payload.chat_id,
            payload.voter_handle,
            LABELS[payload.option_index],
        )
    return {"ok": True}
