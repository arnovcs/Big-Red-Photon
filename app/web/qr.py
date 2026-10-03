"""QR codes as inline SVG (no image library needed)."""

import re

import qrcode
import qrcode.image.svg


def qr_svg(data: str) -> str:
    """An SVG that scales to its container (the fixed mm size is removed)."""
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    svg = image.to_string(encoding="unicode")
    scalable = '<svg role="img" aria-label="QR code"'
    return re.sub(r'^<svg width="[^"]*" height="[^"]*"', scalable, svg)
