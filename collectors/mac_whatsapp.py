#!/usr/bin/env python3
"""Radar collector for WhatsApp for Mac — reads the app's local database, never talks to WhatsApp.

Runs every couple of minutes (launchd). Each run:
  1. snapshots ChatStorage.sqlite (+ contacts) so WhatsApp's live files are never locked or modified,
  2. asks Radar which chats are sources,
  3. sends new messages from those chats (full text, real sender, timestamps) with their photos, PDFs and
     voice notes when WhatsApp has downloaded them,
  4. reports the names of all chats (no content) so you can pick new sources in the UI.

Standard library only (works with macOS's /usr/bin/python3). Needs Full Disk Access for the Python binary
that runs it, because the database lives in ~/Library/Group Containers.

Config (env vars or ~/.radar-collector/config.json):
  RADAR_URL          e.g. http://127.0.0.1:8080 or https://radar.example.com
  RADAR_TOKEN        the server's RADAR_INGEST_TOKEN
  COLLECTOR_NAME     default "mac"
  ME_NAME            how your own messages are labelled, default "Me"
  BACKFILL_DAYS      history to load the first time a chat becomes a source, default 14 (stored, not analyzed)
  WA_CONTAINER       override the WhatsApp container path (testing)
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CORE_DATA_EPOCH = 978_307_200
HOME = Path.home()
STATE_DIR = Path(os.environ.get("RADAR_COLLECTOR_DIR", HOME / ".radar-collector"))
CONTAINER = Path(os.environ.get("WA_CONTAINER", HOME / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"))
MAX_UPLOAD = 20 * 1024 * 1024
MEDIA_RETRY_HOURS = 48
LIVE_WINDOW = 30 * 60          # first sight of a chat: messages newer than this are analyzed, older are backfill
BATCH = 200

TYPES = {0: "text", 1: "image", 2: "video", 3: "voice", 4: "contact", 5: "location", 7: "link", 8: "document",
         15: "sticker"}
UPLOAD_KINDS = {"image", "voice", "document"}

SENDER_SQL = """
    CASE WHEN m.ZISFROMME = 1 THEN :me ELSE COALESCE(
        NULLIF(cg.ZFULLNAME, ''), NULLIF(cd.ZFULLNAME, ''),
        NULLIF(ppl.ZPUSHNAME, ''), NULLIF(ppd.ZPUSHNAME, ''),
        NULLIF(gm.ZMEMBERJID, ''), NULLIF(m.ZFROMJID, ''), '(unknown)') END
"""
SENDER_JOINS = """
    LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
    LEFT JOIN contacts.ZWAADDRESSBOOKCONTACT cg ON cg.ZLID = gm.ZMEMBERJID
    LEFT JOIN contacts.ZWAADDRESSBOOKCONTACT cd ON cd.ZWHATSAPPID = m.ZFROMJID
    LEFT JOIN ZWAPROFILEPUSHNAME ppl ON ppl.ZJID = gm.ZMEMBERJID
    LEFT JOIN ZWAPROFILEPUSHNAME ppd ON ppd.ZJID = m.ZFROMJID
"""


class CollectorError(Exception):
    """A problem the user can fix. `code` is shown by Radar's setup screen: no_access | no_whatsapp."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def load_config() -> dict:
    cfg = {}
    f = STATE_DIR / "config.json"
    if f.exists():
        cfg = json.loads(f.read_text())
    for k in ("RADAR_URL", "RADAR_TOKEN", "COLLECTOR_NAME", "ME_NAME", "BACKFILL_DAYS"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if not cfg.get("RADAR_URL") or not cfg.get("RADAR_TOKEN"):
        sys.exit("Set RADAR_URL and RADAR_TOKEN (env or ~/.radar-collector/config.json)")
    cfg.setdefault("COLLECTOR_NAME", "mac")
    cfg.setdefault("ME_NAME", "Me")
    cfg["BACKFILL_DAYS"] = float(cfg.get("BACKFILL_DAYS", 14))
    return cfg


# ---------------------------------------------------------------- snapshot
def snapshot(dest: Path) -> sqlite3.Connection:
    """Copy the DBs and their WAL, fold the WAL in, open read-only with contacts attached."""
    chat_db = CONTAINER / "ChatStorage.sqlite"
    try:
        found = chat_db.exists()
    except PermissionError:
        raise CollectorError("no_access", "macOS is blocking access to WhatsApp's data (Full Disk Access).")
    if not found:
        raise CollectorError("no_whatsapp", f"{chat_db} not found: is WhatsApp for Mac installed and linked to your phone?")
    for stem in ("ChatStorage", "ContactsV2"):
        for suffix in ("", "-wal"):
            src = CONTAINER / f"{stem}.sqlite{suffix}"
            if src.exists():
                shutil.copy2(src, dest / src.name)
        copied = dest / f"{stem}.sqlite"
        if copied.exists():
            c = sqlite3.connect(copied)
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.execute("PRAGMA journal_mode=DELETE")
            c.close()
    conn = sqlite3.connect(f"file:{dest / 'ChatStorage.sqlite'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    contacts = dest / "ContactsV2.sqlite"
    if contacts.exists():
        conn.execute(f"ATTACH DATABASE 'file:{contacts}?mode=ro' AS contacts")
    else:  # keep the joins valid
        conn.execute("ATTACH DATABASE ':memory:' AS contacts")
        conn.execute("CREATE TABLE contacts.ZWAADDRESSBOOKCONTACT (ZLID TEXT, ZWHATSAPPID TEXT, ZFULLNAME TEXT)")
    return conn


# ---------------------------------------------------------------- HTTP
def _req(cfg, method, path, body=None, headers=None, timeout=60):
    url = cfg["RADAR_URL"].rstrip("/") + path
    h = {"X-Token": cfg["RADAR_TOKEN"], **(headers or {})}
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def get_json(cfg, path):
    return _req(cfg, "GET", path)


def post_json(cfg, path, obj):
    return _req(cfg, "POST", path, json.dumps(obj, ensure_ascii=False).encode(), {"Content-Type": "application/json"})


def post_multipart(cfg, path, message: dict, files: list[Path]):
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="message"\r\n'
             f'Content-Type: application/json; charset=utf-8\r\n\r\n'.encode() +
             json.dumps(message, ensure_ascii=False).encode() + b"\r\n"]
    for f in files:
        mime = mimetypes.guess_type(f.name)[0] or ("audio/ogg" if f.suffix == ".opus" else "application/octet-stream")
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{f.name}"\r\n'
                     f'Content-Type: {mime}\r\n\r\n'.encode() + f.read_bytes() + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    return _req(cfg, "POST", path, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)


# ---------------------------------------------------------------- state
def load_state() -> dict:
    f = STATE_DIR / "state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"chats": {}, "pending_media": {}, "last_chat_report": 0}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_DIR / "state.json")


# ---------------------------------------------------------------- main
def media_file(rel: str | None) -> Path | None:
    if not rel:
        return None
    p = CONTAINER / "Message" / rel
    return p if p.exists() and p.stat().st_size <= MAX_UPLOAD else None


def message_payload(cfg, row, chat_name: str, backfill: bool) -> tuple[dict, str | None]:
    kind = TYPES.get(row["kind"], f"type:{row['kind']}")
    text = row["text"] or row["caption"] or ""
    media = kind if kind in ("image", "video", "voice", "document", "sticker", "contact", "location") else None
    if media == "document" and row["filename"]:
        text = text or f"📄 {row['filename']}"
    payload = {"source": cfg["COLLECTOR_NAME"], "channel": "whatsapp", "chat": chat_name, "sender": row["sender"],
               "text": text, "media_type": media, "external_id": str(row["pk"]),
               "ts": row["date"] + CORE_DATA_EPOCH, "backfill": backfill}
    return payload, (row["media_path"] if media in UPLOAD_KINDS else None)


def run_once(cfg: dict) -> dict:
    state = load_state()
    stats = {"sent": 0, "files": 0, "chats": 0}
    sources = [s for s in get_json(cfg, "/api/collector/config")["sources"] if s["channel"] == "whatsapp"]
    with tempfile.TemporaryDirectory(prefix="radar-wa-") as tmp:
        db = snapshot(Path(tmp))
        chats = db.execute("""SELECT cs.Z_PK pk, cs.ZPARTNERNAME name, cs.ZSESSIONTYPE kind,
                                     (SELECT max(ZMESSAGEDATE) FROM ZWAMESSAGE WHERE ZCHATSESSION = cs.Z_PK) last
                              FROM ZWACHATSESSION cs WHERE cs.ZSESSIONTYPE IN (0, 1) AND cs.ZPARTNERNAME IS NOT NULL""").fetchall()

        # chat discovery (names only), at most every 30 minutes
        if time.time() - state.get("last_chat_report", 0) > 1800:
            post_json(cfg, "/api/collector/chats", {"collector": cfg["COLLECTOR_NAME"], "chats": [
                {"name": c["name"], "channel": "whatsapp",
                 "last_message_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(c["last"] + CORE_DATA_EPOCH))
                 if c["last"] else None} for c in chats]})
            state["last_chat_report"] = time.time()

        watched = [c for c in chats if any(s["match_pattern"].lower() in c["name"].lower() for s in sources)]
        now_cd = time.time() - CORE_DATA_EPOCH
        for c in watched:
            key = str(c["pk"])
            cursor = state["chats"].get(key)
            first_time = cursor is None
            if first_time:
                since = now_cd - cfg["BACKFILL_DAYS"] * 86400
                where, params = "m.ZMESSAGEDATE >= :since", {"since": since}
            else:
                where, params = "m.Z_PK > :cursor", {"cursor": cursor}
            rows = db.execute(f"""
                SELECT m.Z_PK pk, m.ZMESSAGEDATE date, m.ZMESSAGETYPE kind, m.ZTEXT text, {SENDER_SQL} sender,
                       mi.ZMEDIALOCALPATH media_path, mi.ZTITLE caption, mi.ZAUTHORNAME filename
                FROM ZWAMESSAGE m {SENDER_JOINS}
                LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                WHERE m.ZCHATSESSION = :chat AND {where}
                ORDER BY m.Z_PK LIMIT {BATCH * 10}""", {"chat": c["pk"], "me": cfg["ME_NAME"], **params}).fetchall()
            for row in rows:
                if row["kind"] not in TYPES or (TYPES[row["kind"]] == "text" and not row["text"]):
                    state["chats"][key] = row["pk"]
                    continue  # system events, empty rows
                backfill = first_time and (now_cd - row["date"]) > LIVE_WINDOW
                payload, rel = message_payload(cfg, row, c["name"], backfill)
                f = media_file(rel)
                if f:
                    post_multipart(cfg, "/api/ingest", payload, [f])
                    stats["files"] += 1
                else:
                    post_json(cfg, "/api/ingest", payload)
                    if rel is not None or payload["media_type"] in UPLOAD_KINDS:
                        state["pending_media"][key + ":" + str(row["pk"])] = {"payload": payload, "rel": rel,
                                                                               "since": time.time()}
                state["chats"][key] = row["pk"]
                stats["sent"] += 1
            if first_time and not rows:
                last = db.execute("SELECT max(Z_PK) FROM ZWAMESSAGE WHERE ZCHATSESSION=?", (c["pk"],)).fetchone()[0]
                state["chats"][key] = last or 0
            save_state(state)  # per chat, so a crash doesn't resend everything

        # media WhatsApp downloaded since we first saw the message (it fetches lazily)
        for k, p in list(state["pending_media"].items()):
            pk = int(k.split(":")[1])
            r = db.execute("""SELECT mi.ZMEDIALOCALPATH FROM ZWAMESSAGE m JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
                              WHERE m.Z_PK = ?""", (pk,)).fetchone()
            f = media_file(r[0] if r else None)
            if f:
                post_multipart(cfg, "/api/ingest", p["payload"], [f])
                stats["files"] += 1
                del state["pending_media"][k]
            elif time.time() - p["since"] > MEDIA_RETRY_HOURS * 3600:
                del state["pending_media"][k]
        stats["chats"] = len(watched)
        db.close()
    save_state(state)
    return stats


def report_status(cfg: dict, ok: bool, error: str = "", message: str = "") -> None:
    """Tell Radar how this run went (best effort), so its setup screen can explain what to fix."""
    try:
        post_json(cfg, "/api/collector/status", {"collector": cfg["COLLECTOR_NAME"], "ok": ok, "error": error,
                                                 "message": message, "python": os.path.realpath(sys.executable)})
    except Exception:
        pass


def main():
    cfg = load_config()
    try:
        stats = run_once(cfg)
    except urllib.error.URLError as e:
        log(f"Radar not reachable: {e}")
        sys.exit(1)
    except (CollectorError, PermissionError) as e:
        code = e.code if isinstance(e, CollectorError) else "no_access"
        log(f"{code}: {e}")
        report_status(cfg, False, code, str(e))
        sys.exit(1)
    except Exception as e:
        log(f"error: {e!r}")
        report_status(cfg, False, "error", repr(e))
        raise
    log(f"ok: {stats['sent']} messages, {stats['files']} files from {stats['chats']} watched chats")
    report_status(cfg, True)


if __name__ == "__main__":
    main()
