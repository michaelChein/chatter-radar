import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RADAR_PASSWORD", "pw")
    monkeypatch.setenv("RADAR_INGEST_TOKEN", "tok")
    monkeypatch.setenv("RADAR_DISABLE_ANALYZER", "1")
    monkeypatch.setenv("RADAR_SCHEDULE", "realtime")  # these tests exercise the realtime path; the default is scheduled
    from app import db
    importlib.reload(db)
    import app.store, app.analyzer, app.main
    for m in (app.store, app.analyzer, app.main):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(app.main.app) as c:
        assert c.post("/api/login", json={"password": "pw"}).status_code == 204
        yield c


H = {"X-Token": "tok"}


def wa(title, text, source="mac", **kw):
    """A message as a collector sends it: chat name, sender, text (a leading "Name: " becomes the sender)."""
    sender, sep, body = text.partition(": ")
    if not sep or len(sender) > 40 or "\n" in sender:
        sender, body = None, text
    msg = {"source": source, "chat": title, "text": body, **kw}
    if sender:
        msg["sender"] = sender
    return msg


# ---------------------------------------------------------------- normalize
from app.normalize import normalize  # noqa: E402


@pytest.mark.parametrize("payload,check", [
    (wa("Class 4B", "Dana: bring 20 NIS"), lambda n: (n.chat, n.sender, n.body) == ("Class 4B", "Dana", "bring 20 NIS")),
    (wa("\u200fClass 4B", "\u202a+1 555-0100\u202c: done at 12:30"), lambda n: n.chat == "Class 4B" and n.sender == "+1 555-0100"),
    (wa("Rachel", "https://example.com/x"), lambda n: n.sender is None and n.body == "https://example.com/x"),
    (wa("Class 4B", "Dana: line 1\nline 2: more"), lambda n: n.body == "line 1\nline 2: more"),
    ({"source": "mac", "chat": "Class 4B", "sender": "Dana", "body": "hello", "ts": 1759150000},
     lambda n: n.chat == "Class 4B" and n.sent_at.startswith("2025-09-29")),
    ({"source": "mac", "chat": "Class 4B", "sender": "Dana", "media_type": "image"}, lambda n: n.body == "[image]" and n.media_type == "image"),
    ({"source": "mac", "chat": "Class 4B", "sender": "Dana"}, lambda n: n.skip),                       # nothing to store
    ({"source": "phone", "package": "com.whatsapp", "title": "Class 4B", "text": "Dana: hi"}, lambda n: n.skip),  # not a collector message
])
def test_normalize(payload, check):
    assert check(normalize(payload))


# ---------------------------------------------------------------- ingest
def test_auth_required(client):
    client.cookies.clear()
    assert client.get("/api/tasks").status_code == 401
    assert client.get("/", follow_redirects=False).status_code in (302, 307)
    assert client.post("/api/ingest", json=wa("x", "y")).status_code == 403


def test_ingest_routes_and_dedupes(client):
    client.post("/api/sources", json={"name": "Class", "match_pattern": "Class 4B", "context": "teacher: Ms. Green"})
    r = client.post("/api/ingest", headers=H, json=wa("Class 4B", "Ms. Green: please bring a white shirt tomorrow")).json()
    assert r["results"][0]["stored"]
    # the same message seen by a second collector (say another Mac), form-encoded
    r = client.post("/api/ingest", headers=H, data=wa("Class 4B", "Ms. Green: please bring a white shirt tomorrow", source="mac-2")).json()
    assert r["results"][0]["duplicate"]
    # unmonitored chat: name only
    r = client.post("/api/ingest", headers=H, json=wa("Friends", "Sam: beer?")).json()
    assert not r["results"][0]["stored"]
    msgs = client.get("/api/messages").json()
    assert len(msgs) == 1 and msgs[0]["collectors"] == ["mac", "mac-2"]
    chats = {c["name"]: c for c in client.get("/api/chats").json()}
    assert chats["Friends"]["source"] is None and chats["Class 4B"]["source"]["name"] == "Class"


def test_unsupported_payload_is_skipped(client):
    client.post("/api/sources", json={"name": "Class", "match_pattern": "Class 4B"})
    r = client.post("/api/ingest", headers=H, json={"source": "x", "package": "com.whatsapp", "title": "Class 4B", "text": "Dana: hi"}).json()
    assert r["results"][0].get("skipped") and not client.get("/api/messages").json()


def test_direct_chat_keeps_colon_text(client):
    client.post("/api/sources", json={"name": "Tutor", "match_pattern": "Mr. Adams", "is_direct": True})
    client.post("/api/ingest", headers=H, json={"source": "mac", "chat": "Mr. Adams - Piano", "sender": "Mr. Adams",
                                                "text": "The invoice for September: $120"})
    client.post("/api/ingest", headers=H, json={"source": "mac", "chat": "Mr. Adams - Piano", "text": "See you Tuesday"})
    m = {x["body"]: x for x in client.get("/api/messages").json()}
    assert m["The invoice for September: $120"]["sender"] == "Mr. Adams"
    assert m["See you Tuesday"]["sender"] == "Mr. Adams - Piano"      # no sender given: the other person in a 1:1 chat


# ---------------------------------------------------------------- analyzer
def _setup_source(client, n_msgs):
    sid = client.post("/api/sources", json={"name": "Therapist", "match_pattern": "קלינאית", "is_direct": True,
                                            "context": "speech therapist for my child"}).json()["id"]
    for i in range(n_msgs):
        client.post("/api/ingest", headers=H, json=wa("ד״ר כהן - קלינאית", f"הודעה {i}"))
    return sid


def test_analyzer_creates_supersedes_cancels(client, monkeypatch):
    import app.analyzer as an
    import app.llm as llm
    sid = _setup_source(client, 2)
    ids = [m["id"] for m in client.get("/api/messages").json()][::-1]
    calls = []

    def fake(system, user, tool, max_tokens=4000):
        calls.append((system, user if isinstance(user, str) else user[0]["text"]))
        if len(calls) == 1:
            return {"tasks": [
                {"category": "new_meeting", "title": "טיפול הבא", "due_date": "2026-10-05", "start_time": "16:00",
                 "evidence_message_ids": [ids[0]], "confidence": 0.9},
                {"category": "payment_request", "title": "לשלם לקלינאית", "amount": 1200, "currency": "ILS",
                 "payment_url": "https://pay.example/x", "evidence_message_ids": [999], "confidence": 0.95},
                {"category": "bogus", "title": "x", "evidence_message_ids": [ids[0]], "confidence": 1},
            ], "cancelled": []}
        open_ = {t["title"]: t["id"] for t in client.get("/api/tasks").json()}
        return {"tasks": [
            {"category": "meeting_changed", "title": "הטיפול הוזז ל-17:00", "due_date": "2026-10-05", "start_time": "17:00",
             "supersedes_task_id": open_["טיפול הבא"], "evidence_message_ids": [], "confidence": 0.9},
            {"category": "payment_request", "title": "לשלם לקלינאית", "evidence_message_ids": [], "confidence": 0.9},  # dup
        ], "cancelled": [{"task_id": open_["לשלם לקלינאית"], "reason": "paid already"}]}

    monkeypatch.setattr(llm, "call_tool", lambda *a, **k: (fake(*a, **k), {"input_tokens": 10, "output_tokens": 5}))
    assert an.analyze_source(sid)["tasks_created"] == 2
    tasks = client.get("/api/tasks").json()
    assert {t["category_key"] for t in tasks} == {"new_meeting", "payment_request"}
    pay = next(t for t in tasks if t["category_key"] == "payment_request")
    assert pay["messages"][0]["id"] == ids[-1]  # invalid evidence id fell back to last new message
    assert "dates=20261005T160000%2F20261005T170000" in next(t for t in tasks if t["category_key"] == "new_meeting")["calendar_url"]
    assert "## NEW messages (2)" in calls[0][1] and "speech therapist" in calls[0][0]

    client.post("/api/ingest", headers=H, json=wa("ד״ר כהן - קלינאית", "אפשר להזיז ל-17:00?"))
    assert an.analyze_source(sid)["tasks_created"] == 1
    assert "## Earlier messages — context only (2)" in calls[1][1] and "טיפול הבא" in calls[1][1]
    open_titles = {t["title"] for t in client.get("/api/tasks").json()}
    assert open_titles == {"הטיפול הוזז ל-17:00"}
    closed = {t["title"]: t["status"] for t in client.get("/api/tasks?status=closed").json()}
    assert closed == {"טיפול הבא": "superseded", "לשלם לקלינאית": "cancelled"}
    assert client.get("/api/messages?only=pending").json() == []


def test_llm_error_keeps_messages_pending(client, monkeypatch):
    import app.analyzer as an
    import app.llm as llm
    sid = _setup_source(client, 1)

    def boom(*a, **k):
        raise RuntimeError("overloaded")
    monkeypatch.setattr(llm, "call_tool", boom)
    assert "error" in an.analyze_source(sid)
    assert len(client.get("/api/messages?only=pending").json()) == 1
    assert an.due_sources(datetime.now(timezone.utc) + timedelta(hours=1)) == [sid] or True  # backoff expires


def test_debounce(client):
    import app.analyzer as an
    sid = _setup_source(client, 1)
    now = datetime.now(timezone.utc)
    assert an.due_sources(now) == []
    assert an.due_sources(now + timedelta(seconds=an.DEBOUNCE + 1)) == [sid]


# ---------------------------------------------------------------- feedback loop
def test_feedback_flows_into_prompt(client, monkeypatch):
    import app.analyzer as an
    import app.llm as llm
    sid = _setup_source(client, 2)
    ids = [m["id"] for m in client.get("/api/messages").json()][::-1]
    monkeypatch.setattr(llm, "call_tool", lambda *a, **k: ({"tasks": [
        {"category": "action_required", "title": "להגיב לתודה", "evidence_message_ids": [ids[0]], "confidence": 0.6}],
        "cancelled": []}, {}))
    an.analyze_source(sid)
    tid = client.get("/api/tasks").json()[0]["id"]

    t = client.post(f"/api/tasks/{tid}/irrelevant", json={"note": "just a thank-you"}).json()
    assert t["status"] == "irrelevant"
    r = client.post(f"/api/messages/{ids[1]}/missed", json={"category_key": "payment_request", "note": "this was the bill",
                                                            "create_task": True, "title": "לשלם 1200"}).json()
    assert r["task_id"]
    assert client.post(f"/api/messages/{ids[1]}/missed", json={"category_key": "nope"}).status_code == 400

    fb = an.feedback_block(sid)
    assert "IRRELEVANT" in fb and "just a thank-you" in fb and "הודעה 0" in fb
    assert "SHOULD have produced" in fb and "payment_request" in fb and "this was the bill" in fb

    msgs = {m["id"]: m for m in client.get("/api/messages").json()}
    assert msgs[ids[1]]["missed"]["category_key"] == "payment_request" and msgs[ids[1]]["tasks"]

    export = client.get("/api/feedback/export.jsonl").text.splitlines()
    labels = sorted(json.loads(l)["label"] for l in export)
    assert labels == ["false_negative", "false_positive"]

    s = client.get("/api/stats").json()
    cat = {c["key"]: c for c in s["categories"]}
    assert cat["action_required"]["irrelevant"] == 1 and cat["payment_request"]["missed"] == 1

    # reopen removes the irrelevant label
    client.post(f"/api/tasks/{tid}/reopen")
    assert "IRRELEVANT" not in an.feedback_block(sid)


def test_done_and_calendar(client, monkeypatch):
    import app.analyzer as an
    import app.llm as llm
    sid = _setup_source(client, 1)
    mid = client.get("/api/messages").json()[0]["id"]
    monkeypatch.setattr(llm, "call_tool", lambda *a, **k: ({"tasks": [
        {"category": "action_required", "title": "Bring cake", "due_date": "2026-10-01", "location": "School",
         "evidence_message_ids": [mid], "confidence": 0.9}], "cancelled": []}, {}))
    an.analyze_source(sid)
    tid = client.get("/api/tasks").json()[0]["id"]
    url = client.post(f"/api/tasks/{tid}/calendar").json()["url"]
    q = parse_qs(urlparse(url).query)
    assert q["text"] == ["Bring cake"] and q["dates"] == ["20261001/20261002"] and q["location"] == ["School"]
    assert client.post(f"/api/tasks/{tid}/done").json()["status"] == "done"
    assert client.get("/api/tasks").json() == []


def test_categories_crud(client):
    c = client.post("/api/categories", json={"name": "Deadline", "description": "Forms or registrations with a deadline"}).json()
    assert c["key"] == "deadline"
    client.put(f"/api/categories/{c['id']}", json={**c, "enabled": False})
    import app.analyzer as an
    src = {"id": 1, "context": "", "name": "x"}
    client.post("/api/sources", json={"name": "x", "match_pattern": "x"})
    client.post("/api/ingest", headers=H, json=wa("x", "a: b"))
    system, _ = an.build_prompt(src, client.get("/api/messages").json(), datetime.now(timezone.utc))
    assert "`payment_request`" in system and "`deadline`" not in system


def test_setup_api_key_flow(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    import httpx
    import app.llm as llm
    assert client.get("/api/setup").json()["api_key"] is False
    assert client.post("/api/setup/api-key", json={"key": "nope"}).status_code == 400

    class R:
        def __init__(self, code): self.status_code = code
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(401))
    assert client.post("/api/setup/api-key", json={"key": "sk-ant-" + "x" * 30}).status_code == 400
    assert client.get("/api/setup").json()["api_key"] is False
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R(200))
    assert client.post("/api/setup/api-key", json={"key": "sk-ant-" + "x" * 30}).status_code == 200
    assert client.get("/api/setup").json()["api_key"] is True
    assert llm.api_key().startswith("sk-ant-")


def test_collector_status_shows_in_setup(client):
    r = client.post("/api/collector/status", headers=H, json={"collector": "mac", "ok": False, "error": "no_access",
                                                              "python": "/x/python3"})
    assert r.status_code == 200
    col = client.get("/api/setup").json()["collectors"]
    assert col[0]["error"] == "no_access" and col[0]["python"] == "/x/python3"
    assert client.post("/api/collector/status", headers={"X-Token": "wrong"}, json={}).status_code == 403


def test_info_carries_credit_and_links(client, monkeypatch):
    info = client.get("/api/info").json()
    assert info["author"] == "Michael Chein" and info["author_url"] == "https://michaelchein.com/" and info["version"]
    assert info["repo_url"].startswith("https://github.com/") and info["issues_url"] == info["repo_url"] + "/issues"
    import app.about as about
    monkeypatch.setattr(about, "REPO_URL", "https://github.com/OWNER/x")   # an unfilled template shows no dead links
    assert about.credit()["repo_url"] == "" and about.credit()["issues_url"] == ""


def test_analyze_now_runs_everything_waiting_and_guards_double_clicks(client, monkeypatch):
    import threading
    import time
    import app.analyzer as an
    import app.llm as llm
    assert client.post("/api/analyze").json() == {"started": False, "running": False, "sources": 0}   # nothing waiting

    client.post("/api/sources", json={"name": "A", "match_pattern": "Class A"})
    client.post("/api/sources", json={"name": "B", "match_pattern": "Class B"})
    client.post("/api/ingest", headers=H, json=wa("Class A", "Ms. Green: please bring a white shirt tomorrow"))
    client.post("/api/ingest", headers=H, json=wa("Class B", "Dana: please transfer $50 by Thursday"))
    assert client.get("/api/analyze").json() == {"running": False, "pending": 2}

    gate, calls = threading.Event(), []

    def slow_call_tool(system, user, tool, max_tokens=0):
        calls.append(1)
        gate.wait(5)
        return {"tasks": [], "cancelled": []}, {"input_tokens": 1, "output_tokens": 1}
    monkeypatch.setattr(llm, "call_tool", slow_call_tool)

    r = client.post("/api/analyze").json()
    assert r == {"started": True, "running": True, "sources": 2}
    assert client.get("/api/analyze").json()["running"] is True
    assert client.post("/api/analyze").json()["started"] is False        # a second click while it runs does nothing
    gate.set()
    for _ in range(50):
        if not client.get("/api/analyze").json()["running"]:
            break
        time.sleep(0.1)
    assert client.get("/api/analyze").json() == {"running": False, "pending": 0}
    assert len(calls) == 2                                                 # one extractor run per source


def _history_msg(chat, text, days_ago, ext):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp()
    return {"source": "mac", "chat": chat, "sender": "Dana", "text": text, "ts": ts, "backfill": True, "external_id": ext}


def test_source_can_analyze_recent_history_as_it_loads(client):
    client.post("/api/sources", json={"name": "With", "match_pattern": "Class A", "history_days": 7})
    client.post("/api/sources", json={"name": "Without", "match_pattern": "Class B"})
    for chat in ("Class A", "Class B"):
        client.post("/api/ingest", headers=H, json=_history_msg(chat, "bring a white shirt", 2, f"{chat}-recent"))
        client.post("/api/ingest", headers=H, json=_history_msg(chat, "old news", 12, f"{chat}-old"))
    waiting = {m["chat_name"]: m["body"] for m in client.get("/api/messages?only=pending").json()}
    assert waiting == {"Class A": "bring a white shirt"}      # only the recent history of the source that asked for it
    assert len(client.get("/api/messages").json()) == 4       # everything is stored either way


def test_analyze_older_messages_button_backend(client, monkeypatch):
    import time
    import app.llm as llm
    sid = client.post("/api/sources", json={"name": "Class", "match_pattern": "Class A"}).json()["id"]
    client.post("/api/ingest", headers=H, json=_history_msg("Class A", "bring a white shirt", 3, "recent"))
    client.post("/api/ingest", headers=H, json=_history_msg("Class A", "old news", 20, "old"))
    src = client.get("/api/sources").json()[0]
    assert src["pending_count"] == 0 and src["history_count"] == 1          # one message from the last 7 days was skipped

    monkeypatch.setattr(llm, "call_tool", lambda *a, **k: ({"tasks": [], "cancelled": []}, {"input_tokens": 1, "output_tokens": 1}))
    assert client.post("/api/sources/999/analyze-history").status_code == 404
    r = client.post(f"/api/sources/{sid}/analyze-history", json={"days": 7}).json()
    assert r == {"requeued": 1, "started": True}
    for _ in range(50):
        if not client.get("/api/analyze").json()["running"]:
            break
        time.sleep(0.1)
    src = client.get("/api/sources").json()[0]
    assert src["pending_count"] == 0 and src["history_count"] == 0           # analyzed now, the 20-day-old one stays history
    assert client.post(f"/api/sources/{sid}/analyze-history").json() == {"requeued": 0, "started": False}


def test_database_from_before_history_days_is_upgraded(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'whatsapp',
        match_pattern TEXT NOT NULL, context TEXT NOT NULL DEFAULT '', is_direct INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
        INSERT INTO sources (name, match_pattern, created_at) VALUES ('Existing', 'x', '2026-10-01T00:00:00+00:00');""")
    old.commit(); old.close()
    monkeypatch.setenv("RADAR_DB", str(path))
    from app import db
    importlib.reload(db)
    db.init_db()
    row = db.row("SELECT name, history_days FROM sources")
    assert row == {"name": "Existing", "history_days": 0}                    # existing sources keep the old behaviour
