"""Company logos (D-067): image files kept by hand under config/logos/, named by ticker
(`MAR.png`), published to the app's Company.logo as data URIs. The pipeline never fetches them;
scripts/fetch_logos.py fills missing ones on a network that reaches Wikimedia."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

LOGO_DIR = "logos"  # under the config directory
EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}  # fmt: skip
MAX_BYTES = 200_000  # larger files are refused (the data URI is about 4/3 of this)


def image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None  # SVG and anything else: not stored (an SVG can carry scripts)


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG, GIF or JPEG image; None for other data."""
    kind = image_type(data)
    if kind == "image/png" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if kind == "image/gif" and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    if kind == "image/jpeg":
        i = 2
        while i + 9 < len(data) and data[i] == 0xFF:
            marker, length = data[i + 1], struct.unpack(">H", data[i + 2 : i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame
                height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                return width, height
            i += 2 + length
    return None


def data_uri(data: bytes | None, max_bytes: int) -> str | None:
    """`data:<type>;base64,...` for a PNG, JPEG, GIF or WebP image up to `max_bytes`."""
    if not data or len(data) > max_bytes:
        return None
    kind = image_type(data)
    return f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}" if kind else None


def logo_files(config_dir: Path) -> dict[str, Path]:
    """Ticker -> its logo file."""
    folder = config_dir / LOGO_DIR
    if not folder.is_dir():
        return {}
    return {p.stem.upper(): p for p in sorted(folder.iterdir()) if p.suffix.lower() in EXTENSIONS}


def check_logo(path: Path) -> str | None:
    """What is wrong with a logo file, or None when it can be published."""
    data = path.read_bytes()
    kind = image_type(data)
    if kind is None:
        return "not a PNG, JPEG, GIF or WebP image"
    if kind != EXTENSIONS[path.suffix.lower()]:
        return f"content is {kind}, extension says {EXTENSIONS[path.suffix.lower()]}"
    if len(data) > MAX_BYTES:
        return f"{len(data):,} bytes, over {MAX_BYTES:,}"
    return None


def load_logos(config_dir: Path) -> dict[str, str]:
    """Ticker -> data URI of every publishable logo file."""
    out = {}
    for ticker, path in logo_files(config_dir).items():
        uri = data_uri(path.read_bytes(), MAX_BYTES)
        if uri and check_logo(path) is None:
            out[ticker] = uri
    return out
