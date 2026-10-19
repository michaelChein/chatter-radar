"""Mac collector against a synthetic WhatsApp Core Data store and a live Radar server."""
import importlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CD = 978_307_200
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c63f8cfc0f00f0004850180848a8c210000000049454e44ae426082")


def make_store(container: Path, now: float):
    container.mkdir(parents=True)
    (container / "Message/Media/g1").mkdir(parents=True)
    (container / "Message/Media/g1/flyer.png").write_bytes(PNG)
    db = sqlite3.connect(container / "ChatStorage.sqlite")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER);
      CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT, ZCONTACTNAME TEXT);
      CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
      CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMEDIALOCALPATH TEXT, ZTITLE TEXT, ZAUTHORNAME TEXT, ZVCARDSTRING TEXT);
      CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZMESSAGEDATE REAL, ZMESSAGETYPE INTEGER,
                               ZTEXT TEXT, ZISFROMME INTEGER, ZFROMJID TEXT, ZGROUPMEMBER INTEGER, ZMEDIAITEM INTEGER);
    """)
    db.executemany("INSERT INTO ZWACHATSESSION VALUES (?,?,?)",
                   [(1, "הורים ד׳1", 1), (2, "חברים מהצבא", 1), (3, "ד״ר כהן - קלינאית", 0), (4, None, 3)])
    db.executemany("INSERT INTO ZWAGROUPMEMBER VALUES (?,?,?)", [(1, "111@lid", ""), (2, "222@lid", "")])
    db.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '222@lid', 'Dana')")
    db.execute("INSERT INTO ZWAMEDIAITEM VALUES (1, 'Media/g1/flyer.png', 'לוח הטיול', NULL, 'image/png')")
    db.execute("INSERT INTO ZWAMEDIAITEM VALUES (2, 'Media/g1/voice.opus', NULL, NULL, 'audio/ogg')")
    t = now - CD
    db.executemany("INSERT INTO ZWAMESSAGE VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, 1, t - 5 * 86400, 0, "הודעה ישנה מלפני חמישה ימים", 0, "g@g.us", 1, None),
        (2, 1, t - 600, 0, "מחר טקס, להגיע בחולצה לבנה", 0, "g@g.us", 1, None),
        (3, 1, t - 500, 1, None, 0, "g@g.us", 2, 1),
        (4, 1, t - 400, 3, None, 0, "g@g.us", 2, 2),             # voice note not downloaded yet
        (5, 1, t - 300, 6, None, 0, "g@g.us", None, None),         # system event
        (6, 1, t - 200, 0, "אני אביא עוגה", 1, None, None, None),  # from me
        (7, 2, t - 100, 0, "בירה בחמישי?", 0, "g2@g.us", 1, None),  # not a source
        (8, 3, t - 50, 0, "החשבונית: 1,200 ₪", 0, "972500000000@s.whatsapp.net", None, None),
    ])
    db.commit()
    db.close()   # the WAL may remain; the collector must fold it in
    c = sqlite3.connect(container / "ContactsV2.sqlite")
    c.execute("CREATE TABLE ZWAADDRESSBOOKCONTACT (ZLID TEXT, ZWHATSAPPID TEXT, ZFULLNAME TEXT)")
    c.executemany("INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (?,?,?)",
                  [("111@lid", None, "רונית המחנכת"), (None, "972500000000@s.whatsapp.net", "ד״ר כהן")])
    c.commit()
    c.close()


@pytest.fixture()
def server(tmp_path):
    port = socket.socket(); port.bind(("127.0.0.1", 0)); p = port.getsockname()[1]; port.close()
    env = {**os.environ, "RADAR_DB": str(tmp_path / "r.db"), "RADAR_FILES": str(tmp_path / "files"),
           "RADAR_INGEST_TOKEN": "tok", "RADAR_PASSWORD": "", "RADAR_DISABLE_ANALYZER": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(p)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import httpx
    for _ in range(50):
        try:
            httpx.get(f"http://127.0.0.1:{p}/api/health"); break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{p}"
    proc.terminate()


def test_collector_end_to_end(tmp_path, server, monkeypatch):
    import httpx
    now = time.time()
    container = tmp_path / "wa"
    make_store(container, now)
    c = httpx.Client(base_url=server)
    c.post("/api/sources", json={"name": "Class", "match_pattern": "הורים ד"}).raise_for_status()
    c.post("/api/sources", json={"name": "Therapist", "match_pattern": "קלינאית", "is_direct": True}).raise_for_status()

    monkeypatch.setenv("WA_CONTAINER", str(container))
    monkeypatch.setenv("RADAR_COLLECTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RADAR_URL", server)
    monkeypatch.setenv("RADAR_TOKEN", "tok")
    monkeypatch.setenv("COLLECTOR_NAME", "mac")
    monkeypatch.setenv("ME_NAME", "Alex")
    sys.path.insert(0, str(ROOT / "collectors"))
    import mac_whatsapp
    importlib.reload(mac_whatsapp)
    cfg = mac_whatsapp.load_config()

    stats = mac_whatsapp.run_once(cfg)
    assert stats == {"sent": 6, "files": 1, "chats": 2}
    msgs = {m["body"]: m for m in c.get("/api/messages?limit=50").json()}
    assert "בירה בחמישי?" not in msgs                                         # unwatched chat: not sent
    assert msgs["מחר טקס, להגיע בחולצה לבנה"]["sender"] == "רונית המחנכת"       # @lid → address book
    assert msgs["אני אביא עוגה"]["sender"] == "Alex"
    assert msgs["החשבונית: 1,200 ₪"]["sender"] == "ד״ר כהן"                    # direct chat, colon kept in text
    photo = msgs["לוח הטיול"]
    assert photo["sender"] == "Dana" and photo["attachments"][0]["kind"] == "image"
    assert c.get(photo["attachments"][0]["url"]).content == PNG
    # backfill: the 5-day-old message is stored but not queued for analysis; the recent ones are
    pending = {m["body"] for m in c.get("/api/messages?only=pending").json()}
    assert "הודעה ישנה מלפני חמישה ימים" not in pending and "מחר טקס, להגיע בחולצה לבנה" in pending
    chats = {x["name"] for x in c.get("/api/chats").json()}
    assert "חברים מהצבא" in chats                                             # discovery: names only

    # second run: nothing new
    assert mac_whatsapp.run_once(cfg)["sent"] == 0

    # WhatsApp downloads the voice note later → it's attached to the existing message
    (container / "Message/Media/g1/voice.opus").write_bytes(b"OggS fake audio")
    stats = mac_whatsapp.run_once(cfg)
    assert stats["files"] == 1 and stats["sent"] == 0
    voice = next(m for m in c.get("/api/messages?limit=50").json() if m["media_type"] == "voice")
    assert voice["attachments"][0]["kind"] == "voice"
    assert len(c.get("/api/messages?limit=50").json()) == 6

    # a new message in the live DB
    db = sqlite3.connect(container / "ChatStorage.sqlite")
    db.execute("INSERT INTO ZWAMESSAGE VALUES (9, 1, ?, 0, 'הגן נסגר מחר ב-12', 0, 'g@g.us', 1, NULL)", (time.time() - CD,))
    db.commit(); db.close()
    assert mac_whatsapp.run_once(cfg)["sent"] == 1


def test_collector_reports_blocked_access(tmp_path, server, monkeypatch):
    """Without Full Disk Access macOS answers EPERM; the collector must say so instead of just failing."""
    import httpx
    blocked = tmp_path / "wa"
    blocked.mkdir()
    monkeypatch.setenv("WA_CONTAINER", str(blocked))
    monkeypatch.setenv("RADAR_COLLECTOR_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RADAR_URL", server)
    monkeypatch.setenv("RADAR_TOKEN", "tok")
    sys.path.insert(0, str(ROOT / "collectors"))
    import mac_whatsapp
    importlib.reload(mac_whatsapp)

    # 1. WhatsApp not installed / not linked
    with pytest.raises(SystemExit):
        mac_whatsapp.main()
    st = httpx.get(f"{server}/api/setup").json()["collectors"][0]
    assert st["ok"] is False and st["error"] == "no_whatsapp"

    # 2. folder exists but is unreadable (what a missing permission looks like)
    (blocked / "ChatStorage.sqlite").write_text("x")
    blocked.chmod(0o000)
    try:
        if os.access(blocked / "ChatStorage.sqlite", os.R_OK):
            pytest.skip("running as a user that ignores file permissions")
        with pytest.raises(SystemExit):
            mac_whatsapp.main()
    finally:
        blocked.chmod(0o755)
    st = httpx.get(f"{server}/api/setup").json()["collectors"][0]
    assert st["error"] == "no_access" and st["python"]
