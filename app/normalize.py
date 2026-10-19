"""Turn a message sent by a collector into a normalized message.

Collectors (the Mac WhatsApp reader, or anything you write) send already-parsed messages, JSON or form-encoded:
  {source, chat, text|body, sender?, channel?, media_type?, external_id?, ts?, backfill?}
`source` names the collector, `chat` is the chat's name, `ts` is a Unix time (seconds or milliseconds) or ISO 8601.
Either `text` or `media_type` is required. Anything else is skipped.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

BIDI = re.compile(r"[‎‏‪-‮⁦-⁩﻿]")


@dataclass
class Normalized:
    skip: bool
    reason: str = ""
    collector: str = "unknown"
    channel: str = "whatsapp"
    chat: str = ""
    sender: str | None = None
    body: str = ""
    media_type: str | None = None
    external_id: str | None = None      # collector's own message id (idempotent re-sends)
    backfill: bool = False              # historical message: store for context/labeling, don't analyze
    sent_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


def clean(s) -> str:
    s = BIDI.sub("", str(s or "")).replace("\r\n", "\n")
    s = re.sub(r"[ \t ]+", " ", s)
    return s.strip()


def _pick(b: dict, *keys) -> str:
    for k in keys:
        v = b.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            return v
    return ""


def _parse_ts(raw: str) -> str:
    ts = None
    try:
        n = float(raw)
        if n > 0:
            ts = n / 1000 if n > 1e12 else n
    except (TypeError, ValueError):
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = None
    dt = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.isoformat(timespec="seconds")


def normalize(b: dict) -> Normalized:
    collector = clean(_pick(b, "source", "collector") or "unknown")
    chat = clean(_pick(b, "chat"))
    media = clean(_pick(b, "media_type")) or None
    text = clean(_pick(b, "text", "body"))
    if not chat or not (text or media):
        return Normalized(True, "needs a chat and a text or media_type", collector)
    return Normalized(False, collector=collector, channel=clean(_pick(b, "channel") or "whatsapp"), chat=chat,
                      sender=clean(_pick(b, "sender")) or None, body=text or f"[{media}]", media_type=media,
                      sent_at=_parse_ts(_pick(b, "ts")), external_id=clean(_pick(b, "external_id", "id")) or None,
                      backfill=str(b.get("backfill", "")).lower() in ("1", "true", "yes"))
