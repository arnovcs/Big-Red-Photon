"""Bridge webhooks (§9.1). Return immediately; processing runs in the background."""

from fastapi import APIRouter, BackgroundTasks, Request

from app.models.conversation import InboundMessage

router = APIRouter(prefix="/webhooks/photon")


@router.post("/message")
async def message(
    payload: InboundMessage, request: Request, background: BackgroundTasks
) -> dict[str, bool]:
    background.add_task(request.app.state.chat_router.handle_message, payload)
    return {"ok": True}


@router.post("/poll_vote")
async def poll_vote() -> dict[str, bool]:
    """Unused in v3 (text polls only). Accepted so the bridge doesn't log errors."""
    return {"ok": True}
