"""Basic structured logging.

Never log phone numbers, dollar amounts, coordinates, or Nessie IDs at INFO or above.
Pass such values only at DEBUG, or use `mask_handle()`.
"""

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_app_handler", False) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._app_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def kv(event: str, **fields: object) -> str:
    """Format a log line as `event key=value ...`."""
    parts = [event] + [f"{k}={v}" for k, v in fields.items()]
    return " ".join(parts)


def mask_handle(handle: str) -> str:
    """Show only the last 4 characters of a phone/email handle."""
    return f"…{handle[-4:]}" if len(handle) > 4 else "…"
