"""Keep tunnel visitors on the signup pages.

The bridge → backend webhook has no auth: it trusts localhost (§2). A tunnel (cloudflared,
ngrok) also connects from localhost, so requests that came through one, recognisable by
the forwarding headers tunnels add, may only reach the signup pages. Otherwise anyone
could post a fake DM "from" any phone and claim its signup code.
"""

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_PREFIXES = ("/signup", "/static")
TUNNEL_HEADERS = {b"x-forwarded-for", b"cf-connecting-ip", b"x-forwarded-host", b"forwarded"}


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PUBLIC_PREFIXES)


class PublicPathGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not _is_public(scope["path"]):
            if any(name in TUNNEL_HEADERS for name, _ in scope["headers"]):
                await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
                return
        await self.app(scope, receive, send)
