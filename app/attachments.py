"""Attachment storage, voice-note transcription, and turning attachments into Claude content blocks."""
import base64
import hashlib
import io
import logging
import mimetypes
import os
from pathlib import Path

import httpx

from . import db

log = logging.getLogger("radar.attachments")

FILES_DIR = Path(os.environ.get("RADAR_FILES", "/data/files"))
MAX_UPLOAD = int(os.environ.get("RADAR_MAX_UPLOAD_MB", "25")) * 1024 * 1024
MAX_PER_BATCH = int(os.environ.get("RADAR_MAX_ATTACHMENTS_PER_BATCH", "6"))
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 3_700_000      # base64 grows ~4/3; API limit is 5 MB per image
MAX_PDF_BYTES = 20 * 1024 * 1024

TRANSCRIBE_URL = os.environ.get("RADAR_TRANSCRIBE_URL", "")       # OpenAI-compatible /v1/audio/transcriptions
TRANSCRIBE_KEY = os.environ.get("RADAR_TRANSCRIBE_KEY", "")
TRANSCRIBE_MODEL = os.environ.get("RADAR_TRANSCRIBE_MODEL", "whisper-1")
TRANSCRIBE_LANG = os.environ.get("RADAR_TRANSCRIBE_LANGUAGE", "")   # e.g. "he"; empty = auto-detect


def kind_for(mime: str | None, filename: str | None, hint: str | None = None) -> str:
    mime = (mime or mimetypes.guess_type(filename or "")[0] or "").lower()
    if hint in ("voice", "audio") or mime.startswith("audio/") or (filename or "").lower().endswith((".opus", ".ogg", ".m4a")):
        return "voice"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf" or (filename or "").lower().endswith(".pdf"):
        return "document"
    return hint if hint in ("image", "document", "video", "voice") else "other"


def save(message_id: int, data: bytes, mime: str | None, filename: str | None, hint: str | None = None) -> dict | None:
    if not data or len(data) > MAX_UPLOAD:
        return None
    sha = hashlib.sha256(data).hexdigest()
    if (dup := db.row("SELECT * FROM attachments WHERE message_id=? AND sha256=?", (message_id, sha))):
        return dup
    mime = mime or mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
    ext = Path(filename or "").suffix or mimetypes.guess_extension(mime) or ".bin"
    rel = f"{sha[:2]}/{sha}{ext}"
    full = FILES_DIR / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    if not full.exists():
        full.write_bytes(data)
    with db.tx() as conn:
        aid = conn.execute(
            """INSERT INTO attachments (message_id, kind, mime, filename, path, size, sha256, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (message_id, kind_for(mime, filename, hint), mime, filename, rel, len(data), sha, db.now_iso())).lastrowid
    return db.row("SELECT * FROM attachments WHERE id=?", (aid,))


def path_of(att: dict) -> Path:
    return FILES_DIR / att["path"]


def for_messages(message_ids: list[int]) -> dict[int, list[dict]]:
    if not message_ids:
        return {}
    out: dict[int, list[dict]] = {}
    for a in db.rows(f"SELECT * FROM attachments WHERE message_id IN ({','.join('?' * len(message_ids))}) ORDER BY id",
                     tuple(message_ids)):
        out.setdefault(a["message_id"], []).append(a)
    return out


# ---------------------------------------------------------------- transcription
def transcription_enabled() -> bool:
    return bool(TRANSCRIBE_URL)


def transcribe(att: dict) -> str | None:
    if not TRANSCRIBE_URL or att["kind"] != "voice" or att.get("transcript"):
        return att.get("transcript")
    try:
        data = {"model": TRANSCRIBE_MODEL}
        if TRANSCRIBE_LANG:
            data["language"] = TRANSCRIBE_LANG
        headers = {"Authorization": f"Bearer {TRANSCRIBE_KEY}"} if TRANSCRIBE_KEY else {}
        with open(path_of(att), "rb") as f:
            r = httpx.post(TRANSCRIBE_URL, data=data, headers=headers, timeout=300,
                           files={"file": (att.get("filename") or Path(att["path"]).name, f, att.get("mime") or "audio/ogg")})
        r.raise_for_status()
        text = (r.json().get("text") or "").strip()
    except Exception as e:  # leave it untranscribed; the model sees a placeholder
        log.warning("transcription failed for attachment %s: %s", att["id"], e)
        return None
    with db.tx() as conn:
        conn.execute("UPDATE attachments SET transcript=? WHERE id=?", (text, att["id"]))
    att["transcript"] = text
    return text


# ---------------------------------------------------------------- content blocks for Claude
def _image_bytes(att: dict) -> tuple[bytes, str] | None:
    p = path_of(att)
    if not p.exists():
        return None
    data, mime = p.read_bytes(), (att.get("mime") or "").lower()
    if mime in IMAGE_TYPES and len(data) <= MAX_IMAGE_BYTES:
        return data, mime
    try:  # downscale / convert (HEIC etc. only if Pillow has a plugin for it)
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im = im.convert("RGB")
        im.thumbnail((1800, 1800))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        if buf.tell() <= MAX_IMAGE_BYTES:
            return buf.getvalue(), "image/jpeg"
    except Exception as e:
        log.info("cannot prepare image %s: %s", att["id"], e)
    return None


def block_for(att: dict) -> dict | None:
    """One Claude content block for an attachment, or None if it can't be shown."""
    if att["kind"] == "image":
        img = _image_bytes(att)
        if img:
            return {"type": "image", "source": {"type": "base64", "media_type": img[1],
                                                "data": base64.b64encode(img[0]).decode()}}
    if att["kind"] == "document" and (att.get("mime") == "application/pdf" or (att.get("filename") or "").lower().endswith(".pdf")):
        p = path_of(att)
        if p.exists() and p.stat().st_size <= MAX_PDF_BYTES:
            return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                   "data": base64.b64encode(p.read_bytes()).decode()}}
    return None


def placeholder(att: dict, shown: bool) -> str:
    name = f" {att['filename']}" if att.get("filename") else ""
    if att["kind"] == "voice":
        if att.get("transcript"):
            return f'[voice note transcript: "{att["transcript"]}"]'
        return "[voice note — not transcribed]"
    return f"[attachment: {att['kind']}{name}{' — shown below' if shown else ' — not visible to you'}]"
