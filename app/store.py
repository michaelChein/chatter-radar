"""Ingest: record the chat, route to a source, store the message, and don't store the same message twice.

A collector re-sending a message is recognised by its own id (external_id). The same message arriving from two
collectors (say two Macs linked to the same group) is recognised by chat + sender + text + day.
"""
import hashlib
import json
import os
import re
from datetime import datetime

from zoneinfo import ZoneInfo

from . import db
from .normalize import Normalized

def _system_tz() -> ZoneInfo:
    """RADAR_TZ, else the machine's zone (from /etc/localtime), else UTC."""
    name = os.environ.get("RADAR_TZ")
    if not name:
        target = os.path.realpath("/etc/localtime")
        name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


TZ = _system_tz()
# Messages that might change today's plans. A match makes the analyzer run within minutes even in scheduled mode.
URGENT = re.compile(os.environ.get("RADAR_URGENT_PATTERN", (
    r"היום|הערב|עכשיו|דחוף|בוטל|מבוטל|ביטול|לא יתקיים|לא מתקיים|מוקדם|איסוף|לאסוף|הסעה|שינוי|נדחה|הוקדם|"
    r"\b(?:today|tonight|now|urgent|cancel\w*|postpone\w*|early|pick ?up|moved)\b")), re.I)


def match_source(conn, channel: str, chat: str) -> dict | None:
    best = None
    for s in conn.execute("SELECT * FROM sources WHERE enabled = 1 AND channel = ?", (channel,)).fetchall():
        pat = (s["match_pattern"] or "").strip().lower()
        if pat and pat in chat.lower() and (best is None or len(pat) > len(best["match_pattern"])):
            best = dict(s)
    return best


def ingest(n: Normalized) -> dict:
    """Returns {stored, duplicate, source_id, message_id}."""
    now = db.now_iso()
    sent = datetime.fromisoformat(n.sent_at)
    local_day = sent.astimezone(TZ).date().isoformat()
    result = {"stored": False, "duplicate": False, "source_id": None, "message_id": None}

    with db.tx() as conn:
        chat = conn.execute("SELECT id, seen_by FROM chats WHERE channel=? AND name=?", (n.channel, n.chat)).fetchone()
        if chat:
            seen = sorted(set(db.jload(chat["seen_by"], [])) | {n.collector})
            conn.execute("UPDATE chats SET last_seen=?, msg_count=msg_count+1, seen_by=? WHERE id=?",
                         (now, json.dumps(seen, ensure_ascii=False), chat["id"]))
        else:
            conn.execute("INSERT INTO chats (channel, name, first_seen, last_seen, msg_count, seen_by) VALUES (?,?,?,?,1,?)",
                         (n.channel, n.chat, now, now, json.dumps([n.collector], ensure_ascii=False)))

        src = match_source(conn, n.channel, n.chat)
        if not src:
            return result
        result["source_id"] = src["id"]

        sender, body = n.sender, n.body
        if src["is_direct"]:
            sender = sender or n.chat
        key = hashlib.md5("|".join([n.channel, n.chat, sender or "", body, local_day]).encode()).hexdigest()
        ext_key = f"{n.collector}:{n.external_id}" if n.external_id else None

        existing = None
        if ext_key:
            existing = conn.execute("SELECT * FROM messages WHERE external_key=?", (ext_key,)).fetchone()
            if existing:  # the same collector re-sent the same message: nothing new
                result.update(duplicate=True, message_id=existing["id"])
                return result
        existing = conn.execute("SELECT * FROM messages WHERE dedup_key=?", (key,)).fetchone()
        if existing:  # the same message seen by another collector
            cols = sorted(set(db.jload(existing["collectors"], [])) | {n.collector})
            conn.execute("""UPDATE messages SET collectors=?, media_type=coalesce(media_type, ?),
                            external_key=coalesce(external_key, ?) WHERE id=?""",
                         (json.dumps(cols, ensure_ascii=False), n.media_type, ext_key, existing["id"]))
            result.update(duplicate=True, message_id=existing["id"])
            return result

        cur = conn.execute(
            """INSERT INTO messages (source_id, channel, chat_name, sender, body, media_type, sent_at, ingested_at,
                                     collectors, dedup_key, external_key, analyzed_at, urgent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (src["id"], n.channel, n.chat, sender, body, n.media_type, n.sent_at, now,
             json.dumps([n.collector], ensure_ascii=False), key, ext_key, now if n.backfill else None,
             0 if n.backfill else int(bool(URGENT.search(body)))))
        result.update(stored=True, message_id=cur.lastrowid)
        return result


def requeue_for_attachment(message_id: int) -> None:
    """A file arrived for an already-analyzed message: analyze it again (the model sees the open tasks)."""
    with db.tx() as conn:
        conn.execute("""UPDATE messages SET analyzed_at=NULL, ingested_at=? WHERE id=? AND analyzed_at IS NOT NULL
                        AND batch_id IS NOT NULL""", (db.now_iso(), message_id))
