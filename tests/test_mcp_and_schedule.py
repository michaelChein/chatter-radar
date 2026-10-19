import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
H = {"X-Token": "tok"}
MCP_H = {"Authorization": "Bearer mt", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def load(tmp_path, monkeypatch, **env):
    base = dict(RADAR_DB=str(tmp_path / "t.db"), RADAR_FILES=str(tmp_path / "f"), RADAR_PASSWORD="pw",
                RADAR_INGEST_TOKEN="tok", RADAR_DISABLE_ANALYZER="1", RADAR_MCP_TOKEN="mt", RADAR_SCHEDULE="realtime")
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RADAR_FAKE_LLM", raising=False)
    import app.db
    importlib.reload(app.db)
    import app.attachments, app.store, app.instructions, app.analyzer, app.replay, app.agent, app.mcp_server, app.main
    for m in (app.attachments, app.store, app.instructions, app.analyzer, app.replay, app.agent, app.mcp_server, app.main):
        importlib.reload(m)
    return app.main


def wa(title, text, source="mac", **kw):
    """A message as a collector sends it: chat name, sender, text (a leading "Name: " becomes the sender)."""
    sender, sep, body = text.partition(": ")
    if not sep or len(sender) > 40 or "\n" in sender:
        sender, body = None, text
    msg = {"source": source, "chat": title, "text": body, **kw}
    if sender:
        msg["sender"] = sender
    return msg


@pytest.fixture()
def client(tmp_path, monkeypatch):
    main = load(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.post("/api/login", json={"password": "pw"})
        yield c


def rpc(c, method, params=None, i=1, headers=MCP_H, path="/mcp/"):
    r = c.post(path, json={"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}, headers=headers)
    return r


def call(c, name, args):
    r = rpc(c, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    return json.loads(res["content"][0]["text"]), res.get("isError")


# ---------------------------------------------------------------- MCP connector
def test_mcp_auth(client):
    client.cookies.clear()
    assert rpc(client, "tools/list", headers={**MCP_H, "Authorization": "Bearer nope"}).status_code == 401
    assert rpc(client, "tools/list", headers={k: v for k, v in MCP_H.items() if k != "Authorization"}).status_code == 401
    no_auth = {k: v for k, v in MCP_H.items() if k != "Authorization"}
    assert rpc(client, "tools/list", headers=no_auth, path="/mcp/?key=mt").status_code == 200
    assert rpc(client, "tools/list", path="/mcp").status_code == 200   # no trailing slash, no redirect


def test_mcp_disabled_without_token(tmp_path, monkeypatch):
    main = load(tmp_path, monkeypatch, RADAR_MCP_TOKEN="")
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        assert rpc(c, "tools/list", headers={**MCP_H, "Authorization": "Bearer "}).status_code == 401


def test_mcp_tools(client, monkeypatch):
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert "propose_instruction_change" in tools
    assert not any("approve" in n or "apply" in n for n in tools)          # approval stays in the Radar UI
    assert tools["search_messages"]["annotations"]["readOnlyHint"] is True
    assert tools["create_task"]["annotations"]["readOnlyHint"] is False

    sid = client.post("/api/sources", json={"name": "Class", "match_pattern": "הורים"}).json()["id"]
    client.post("/api/ingest", headers=H, json=wa("הורים ד׳1", "רונית: מחר להביא חולצה לבנה"))
    out, err = call(client, "search_messages", {"query": "חולצה"})
    assert not err and out["messages"][0]["text"] == "מחר להביא חולצה לבנה"
    mid = out["messages"][0]["id"]
    out, _ = call(client, "create_task", {"category": "action_required", "title": "חולצה לבנה", "message_ids": [mid]})
    tid = out["task_id"]
    out, _ = call(client, "list_tasks", {})
    assert out["tasks"][0]["title"] == "חולצה לבנה"
    out, _ = call(client, "update_task", {"task_id": tid, "action": "done"})
    assert out["task"]["status"] == "done"

    import app.llm as llm
    monkeypatch.setattr(llm, "call_tool", lambda *a, **k: ({"tasks": [], "cancelled": []}, {}))
    out, _ = call(client, "propose_instruction_change", {
        "changes": [{"op": "add_rule", "source_id": sid, "text": "Birthday wishes are never tasks"}], "rationale": "noise"})
    assert out["proposal_id"] and "pending" in out["status"]
    assert client.get("/api/instructions").json()["rules"] == []        # nothing applied until approved in the UI
    assert client.get("/api/proposals?status=pending").json()[0]["id"] == out["proposal_id"]
    out, err = call(client, "propose_instruction_change", {"changes": [{"op": "nope"}], "rationale": "x"})
    assert "invalid change" in out["error"]


# ---------------------------------------------------------------- scheduled mode
def test_scheduled_mode_with_urgent_fast_path(tmp_path, monkeypatch):
    main = load(tmp_path, monkeypatch, RADAR_SCHEDULE="07:00,20:00", RADAR_URGENT_DEBOUNCE_SECONDS="60")
    import app.analyzer as an, app.db as db
    from fastapi.testclient import TestClient
    tz = an.TZ
    with TestClient(main.app) as c:
        c.post("/api/login", json={"password": "pw"})
        a = c.post("/api/sources", json={"name": "A", "match_pattern": "groupA"}).json()["id"]
        b = c.post("/api/sources", json={"name": "B", "match_pattern": "groupB"}).json()["id"]
        c.post("/api/ingest", headers=H, json=wa("groupA", "x: תודה רבה!"))
        c.post("/api/ingest", headers=H, json=wa("groupB", "y: היום סיום ב-12:00 במקום 13:30"))
        msgs = {m["body"]: m for m in c.get("/api/messages").json()}
        assert db.row("SELECT urgent FROM messages WHERE id=?", (msgs["תודה רבה!"]["id"],))["urgent"] == 0
        assert db.row("SELECT urgent FROM messages WHERE id=?", (msgs["היום סיום ב-12:00 במקום 13:30"]["id"],))["urgent"] == 1

        now = datetime.now(timezone.utc)
        # pretend the last scheduled run already happened
        db.kv_set("last_scheduled_slot", an.current_slot(now) or "x")
        assert an.due_sources(now) == []                                     # urgent, but chat not quiet yet
        assert an.due_sources(now + timedelta(seconds=61)) == [b]            # urgent fast path: only B
        # at the next scheduled time everything pending is due
        nxt = datetime.strptime(an.next_slot(now), "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        later = nxt.astimezone(timezone.utc) + timedelta(minutes=1)
        assert sorted(an.due_sources(later)) == sorted([a, b])
        an.mark_slot_done(later)
        assert an.due_sources(later + timedelta(minutes=5)) == [b]           # B still urgent+pending, A waits
        info = c.get("/api/info").json()
        assert info["schedule"] == ["07:00", "20:00"] and info["next_run"]


def test_urgent_pattern_word_boundaries():
    from app.store import URGENT
    assert URGENT.search("והיום אין חוג") and URGENT.search("Practice is cancelled")
    assert not URGENT.search("I know the answer") and not URGENT.search("snowman")


def test_realtime_mode_unchanged(client):
    import app.analyzer as an
    assert an.schedule() == []
    sid = client.post("/api/sources", json={"name": "A", "match_pattern": "groupA"}).json()["id"]
    client.post("/api/ingest", headers=H, json=wa("groupA", "x: hello"))
    now = datetime.now(timezone.utc)
    assert an.due_sources(now) == [] and an.due_sources(now + timedelta(seconds=an.DEBOUNCE + 1)) == [sid]


def test_default_is_four_runs_a_day_and_user_can_switch(tmp_path, monkeypatch):
    main = load(tmp_path, monkeypatch)
    monkeypatch.delenv("RADAR_SCHEDULE", raising=False)
    importlib.reload(sys.modules["app.analyzer"])
    import app.analyzer as an
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.post("/api/login", json={"password": "pw"})
        assert an.schedule() == ["07:00", "12:00", "16:00", "20:00"]
        assert c.get("/api/info").json()["schedule"] == ["07:00", "12:00", "16:00", "20:00"]

        assert c.post("/api/schedule", json={"mode": "realtime"}).json()["schedule"] == []
        assert an.schedule() == [] and c.get("/api/info").json()["schedule"] == []

        r = c.post("/api/schedule", json={"mode": "scheduled", "times": ["8:30", "21:00", "8:30"]}).json()
        assert r["schedule"] == ["08:30", "21:00"] and r["next_run"]
        # choosing a schedule must not immediately fire a slot that already passed today
        assert an.due_sources(datetime.now(timezone.utc)) == []

        for bad in (["25:00"], ["noon"], ["07:00"] * 0 + [f"{h:02d}:00" for h in range(13)]):
            assert c.post("/api/schedule", json={"mode": "scheduled", "times": bad}).status_code == 400
        assert an.schedule() == ["08:30", "21:00"]  # unchanged by the bad requests
