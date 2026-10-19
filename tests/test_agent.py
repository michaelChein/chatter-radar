import base64
import importlib
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
H = {"X-Token": "tok"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RADAR_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("RADAR_PASSWORD", "pw")
    monkeypatch.setenv("RADAR_INGEST_TOKEN", "tok")
    monkeypatch.setenv("RADAR_DISABLE_ANALYZER", "1")
    monkeypatch.setenv("RADAR_SCHEDULE", "realtime")  # these tests exercise the realtime path; the default is scheduled
    monkeypatch.delenv("RADAR_FAKE_LLM", raising=False)
    import app.db
    importlib.reload(app.db)
    import app.attachments, app.store, app.instructions, app.analyzer, app.replay, app.agent, app.main
    for m in (app.attachments, app.store, app.instructions, app.analyzer, app.replay, app.agent, app.main):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(app.main.app) as c:
        assert c.post("/api/login", json={"password": "pw"}).status_code == 204
        yield c


def wa(title, text, source="mac", **kw):
    """A message as a collector sends it: chat name, sender, text (a leading "Name: " becomes the sender)."""
    sender, sep, body = text.partition(": ")
    if not sep or len(sender) > 40 or "\n" in sender:
        sender, body = None, text
    msg = {"source": source, "chat": title, "text": body, **kw}
    if sender:
        msg["sender"] = sender
    return msg


def text_of(user):
    return user if isinstance(user, str) else "\n".join(b.get("text", "") for b in user if b.get("type") == "text")


def setup_labeled(client, monkeypatch):
    """Kindergarten source with one false positive, one false negative and one confirmed task."""
    import app.analyzer as an, app.llm as llm
    sid = client.post("/api/sources", json={"name": "Kindergarten", "match_pattern": "גן החצב"}).json()["id"]
    for t in ["מיכל: ביום ראשון סוגרים ב-12:00", "אמא של נועה: צריך להביא כובע מחר?", "מיכל: מחר להביא בגדי החלפה"]:
        client.post("/api/ingest", headers=H, json=wa("גן החצב", t))
    ids = [m["id"] for m in client.get("/api/messages").json()][::-1]
    monkeypatch.setattr(llm, "call_tool", lambda s, u, t, **k: ({"tasks": [
        {"category": "meeting_changed", "title": "סגירה ב-12", "evidence_message_ids": [ids[0]], "confidence": .9},
        {"category": "action_required", "title": "כובע", "evidence_message_ids": [ids[1]], "confidence": .6}],
        "cancelled": []}, {"input_tokens": 100, "output_tokens": 20}))
    an.analyze_source(sid)
    tasks = {t["title"]: t["id"] for t in client.get("/api/tasks").json()}
    client.post(f"/api/tasks/{tasks['סגירה ב-12']}/done")
    client.post(f"/api/tasks/{tasks['כובע']}/irrelevant", json={"note": "another parent's question"})
    client.post(f"/api/messages/{ids[2]}/missed", json={"category_key": "action_required"})
    return sid, ids


# ---------------------------------------------------------------- instructions
def test_apply_ops_validation(client):
    from app import instructions as ins
    cur = ins.current()
    with pytest.raises(ins.OpError):
        ins.apply_ops(cur, [{"op": "add_rule", "source_id": 999, "text": "x"}])
    with pytest.raises(ins.OpError):
        ins.apply_ops(cur, [{"op": "edit_rule", "rule_id": 5, "text": "x"}])
    with pytest.raises(ins.OpError):
        ins.apply_ops(cur, [{"op": "drop_table"}])
    with pytest.raises(ins.OpError):
        ins.apply_ops(cur, [{"op": "add_rule", "source_id": None, "text": "x" * 500}])
    new = ins.apply_ops(cur, [{"op": "add_rule", "source_id": None, "text": "Ignore birthday wishes"},
                              {"op": "set_category_description", "category_key": "payment_request", "description": "Bills."}])
    assert new["rules"][-1]["text"] == "Ignore birthday wishes"
    assert ins.current()["rules"] == []  # pure


def test_rules_versions_and_restore(client):
    r = client.post("/api/rules", json={"source_id": None, "text": "Ignore birthday wishes"}).json()
    rid = r["rules"][0]["id"]
    client.put(f"/api/rules/{rid}", json={"text": "Ignore birthday and holiday greetings"})
    v = client.get("/api/instructions").json()["versions"]
    assert [x["note"] for x in v][:3] == ["rule edited by user", "rule added by user", "initial"]
    restored = client.post(f"/api/instructions/versions/{v[1]['id']}/restore").json()
    assert restored["rules"][0]["text"] == "Ignore birthday wishes"
    client.post(f"/api/instructions/versions/{v[-1]['id']}/restore")
    assert client.get("/api/instructions").json()["rules"] == []


def test_rules_reach_extractor_prompt(client, monkeypatch):
    sid, ids = setup_labeled(client, monkeypatch)
    client.post("/api/rules", json={"source_id": sid, "text": "Parents' questions are not tasks"})
    client.post("/api/rules", json={"source_id": None, "text": "Ignore birthday wishes"})
    other = client.post("/api/sources", json={"name": "Other", "match_pattern": "zzz"}).json()["id"]
    import app.analyzer as an
    src = client.get("/api/sources").json()
    msgs = [m for m in client.get("/api/messages").json()]
    system, _ = an.build_prompt(next(s for s in src if s["id"] == sid), msgs[::-1], datetime.now(timezone.utc))
    assert "Parents' questions are not tasks" in system and "Ignore birthday wishes (all sources)" in system
    system2, _ = an.build_prompt(next(s for s in src if s["id"] == other), msgs[::-1], datetime.now(timezone.utc))
    assert "Parents' questions" not in system2 and "Ignore birthday wishes" in system2


# ---------------------------------------------------------------- replay
def test_replay_ab_and_no_label_leakage(client, monkeypatch):
    sid, ids = setup_labeled(client, monkeypatch)
    import app.llm as llm, app.replay as rp
    seen = []

    def fake(system, user, tool, **k):
        seen.append(system)
        has_rule = "not tasks unless the teacher" in system
        tasks = [{"category": "meeting_changed", "title": "סגירה", "evidence_message_ids": [ids[0]], "confidence": .9},
                 {"category": "action_required", "title": "בגדי החלפה", "evidence_message_ids": [ids[2]], "confidence": .9}]
        if not has_rule:
            tasks.append({"category": "action_required", "title": "כובע", "evidence_message_ids": [ids[1]], "confidence": .6})
        return {"tasks": tasks, "cancelled": []}, {"input_tokens": 10, "output_tokens": 2}
    monkeypatch.setattr(llm, "call_tool", fake)

    ops = [{"op": "add_rule", "source_id": sid, "text": "Other parents' questions are not tasks unless the teacher confirms."}]
    rep = rp.run(ops)
    c, b = rep["variants"]["candidate"], rep["variants"]["baseline"]
    assert (c["fp"], c["fn"], c["tp"]) == ({"ok": 1, "total": 1}, {"ok": 1, "total": 1}, {"ok": 1, "total": 1})
    assert b["fp"] == {"ok": 0, "total": 1}
    assert len(seen) == 2  # one batch × (candidate, baseline)
    # the labeled examples being evaluated must not appear as few-shot feedback in the replay prompt
    assert all("another parent's question" not in s for s in seen)
    assert "false positives avoided: 1/1 (currently 0/1)" in rp.summarize(rep)


# ---------------------------------------------------------------- agent loop
class Script:
    """Scripted Messages API: each call returns the next response; records requests."""
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, body, timeout=180):
        self.requests.append(json.loads(json.dumps(body)))
        return self.responses.pop(0)


def tool_use(name, inp, i="t1"):
    return {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}


def say(text):
    return {"content": [{"type": "text", "text": text}]}


def test_agent_proposes_and_user_approves(client, monkeypatch):
    sid, ids = setup_labeled(client, monkeypatch)
    import app.llm as llm
    rule = "Other parents' questions are not tasks unless the teacher confirms."
    script = Script([
        tool_use("get_feedback", {"kind": "irrelevant"}),
        tool_use("propose_instruction_change", {"changes": [{"op": "add_rule", "source_id": sid, "text": rule}],
                                                "rationale": "The hat task came from a parent's question."}, "t2"),
        say("I proposed a rule — approve it on the card."),
        say("Great, it's live."),
    ])
    monkeypatch.setattr(llm, "post_messages", script)
    conv = client.post("/api/conversations").json()["id"]
    out = client.post(f"/api/conversations/{conv}/messages", json={"text": "Why did I get the hat task?"}).json()
    kinds = [i["type"] for i in out["items"]]
    assert kinds == ["user", "tool", "tool", "proposal", "assistant"]
    # tool results reached the model, including the user's note
    assert "another parent's question" in json.dumps(script.requests[1]["messages"][-1], ensure_ascii=False)
    pid = out["items"][3]["proposal_id"]
    p = client.get(f"/api/proposals/{pid}").json()
    assert p["status"] == "pending" and "Add rule (Kindergarten)" in p["lines"][0] and p["replay_summary"]
    # nothing changed yet
    assert client.get("/api/instructions").json()["rules"] == []
    assert client.post(f"/api/proposals/{pid}/approve").json()["status"] == "approved"
    assert client.get("/api/instructions").json()["rules"][0]["text"] == rule
    assert client.post(f"/api/proposals/{pid}/approve").status_code == 409
    # the approval is told to the model on the next turn
    client.post(f"/api/conversations/{conv}/messages", json={"text": "thanks"})
    first_user_block = script.requests[-1]["messages"][-1]["content"][0]["text"]
    assert "APPROVED proposal" in first_user_block


def test_agent_cannot_apply_directly_and_errors_are_reported(client, monkeypatch):
    sid, ids = setup_labeled(client, monkeypatch)
    import app.llm as llm
    script = Script([
        tool_use("propose_instruction_change", {"changes": [{"op": "add_rule", "source_id": 12345, "text": "x"}],
                                                "rationale": "r"}),
        say("Oops, fixed."),
    ])
    monkeypatch.setattr(llm, "post_messages", script)
    conv = client.post("/api/conversations").json()["id"]
    out = client.post(f"/api/conversations/{conv}/messages", json={"text": "add a rule"}).json()
    assert any(i["type"] == "tool_error" and "unknown source_id" in i["text"] for i in out["items"])
    assert client.get("/api/proposals").json() == []
    tool_names = {t["name"] for t in script.requests[0]["tools"]}
    assert "propose_instruction_change" in tool_names and not any("apply" in n or "approve" in n for n in tool_names)


def test_agent_task_actions_and_reject(client, monkeypatch):
    sid, ids = setup_labeled(client, monkeypatch)
    import app.llm as llm
    script = Script([
        tool_use("create_task", {"category": "action_required", "title": "בגדי החלפה", "message_ids": [ids[2]],
                                 "due_date": "2026-10-02"}),
        tool_use("list_tasks", {"status": "open"}, "t2"),
        say("Added."),
    ])
    monkeypatch.setattr(llm, "post_messages", script)
    conv = client.post("/api/conversations").json()["id"]
    out = client.post(f"/api/conversations/{conv}/messages", json={"text": "add the clothes task"}).json()
    assert out["changed"]
    t = next(t for t in client.get("/api/tasks").json() if t["title"] == "בגדי החלפה")
    assert t["origin"] == "agent" and t["messages"][0]["id"] == ids[2] and t["source_id"] == sid


def test_agent_api_error_is_shown(client, monkeypatch):
    import app.llm as llm

    def boom(body, timeout=180):
        raise RuntimeError("Anthropic API 529: overloaded")
    monkeypatch.setattr(llm, "post_messages", boom)
    conv = client.post("/api/conversations").json()["id"]
    out = client.post(f"/api/conversations/{conv}/messages", json={"text": "hi"}).json()
    assert "overloaded" in out["items"][-1]["text"]


def test_history_merges_events_and_trims(client):
    import app.agent as ag
    conv = client.post("/api/conversations").json()["id"]
    ag._store(conv, "user", [{"type": "text", "text": "a"}])
    ag._store(conv, "assistant", [{"type": "text", "text": "b"}])
    ag.add_event(conv, "User approved #1.")
    ag._store(conv, "user", [{"type": "text", "text": "c"}])
    h = ag.history(conv)
    assert [m["role"] for m in h] == ["user", "assistant", "user"]
    assert "approved #1" in h[2]["content"][0]["text"] and h[2]["content"][1]["text"] == "c"


# ---------------------------------------------------------------- attachments & collectors
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_multipart_attachment_reaches_prompt(client):
    sid = client.post("/api/sources", json={"name": "Class", "match_pattern": "הורים"}).json()["id"]
    msg = {"source": "mac", "chat": "הורים ד׳1", "sender": "רונית", "text": "מצורף לוח הטיול", "media_type": "image",
           "external_id": "123", "ts": datetime.now(timezone.utc).timestamp()}
    r = client.post("/api/ingest", headers=H, data={"message": json.dumps(msg, ensure_ascii=False)},
                    files=[("file", ("trip.png", PNG, "image/png"))]).json()["results"][0]
    assert r["stored"] and r["attachments"]
    m = client.get("/api/messages").json()[0]
    assert m["attachments"][0]["kind"] == "image"
    assert client.get(m["attachments"][0]["url"]).content == PNG
    import app.analyzer as an
    system, content = an.build_prompt(client.get("/api/sources").json()[0], [m], datetime.now(timezone.utc))
    assert any(b["type"] == "image" for b in content)
    assert "shown below" in content[0]["text"]
    # re-sent by the same collector: idempotent
    r2 = client.post("/api/ingest", headers=H, json=msg).json()["results"][0]
    assert r2["duplicate"] and len(client.get("/api/messages").json()) == 1


def test_backfill_not_analyzed_and_collector_config(client):
    client.post("/api/sources", json={"name": "Class", "match_pattern": "הורים"})
    cfg = client.get("/api/collector/config", headers=H).json()
    assert cfg["sources"][0]["match_pattern"] == "הורים"
    assert client.get("/api/collector/config").status_code == 403
    # collectors have the token but no login cookie
    cookies = dict(client.cookies)
    client.cookies.clear()
    assert client.get("/api/collector/config", headers=H).status_code == 200
    assert client.post("/api/collector/chats", headers=H, json={"collector": "mac", "chats": []}).status_code == 200
    client.cookies.update(cookies)
    client.post("/api/ingest", headers=H, json={"source": "mac", "chat": "הורים ד׳1", "sender": "x", "text": "old news",
                                                "ts": 1700000000, "backfill": True, "external_id": "1"})
    assert client.get("/api/messages?only=pending").json() == []
    client.post("/api/collector/chats", headers=H, json={"collector": "mac", "chats": [{"name": "חוג שחייה"}]})
    assert any(c["name"] == "חוג שחייה" for c in client.get("/api/chats").json())


def test_call_tool_uses_auto_choice_and_retries_text_reply(monkeypatch):
    """Sonnet 5.5 rejects a forced tool_choice: call_tool must use auto and retry when the model answers in text."""
    monkeypatch.delenv("RADAR_FAKE_LLM", raising=False)
    from app import llm, prompts
    bodies, replies = [], [
        {"content": [{"type": "text", "text": "Nothing here."}], "stop_reason": "end_turn"},
        {"content": [{"type": "tool_use", "name": "report_tasks", "input": {"tasks": [], "cancelled": []}}],
         "usage": {"input_tokens": 5}},
    ]
    monkeypatch.setattr(llm, "post_messages", lambda body: (bodies.append(body), replies.pop(0))[1])
    result, usage = llm.call_tool("sys", "hello", prompts.TOOL)
    assert result == {"tasks": [], "cancelled": []} and usage == {"input_tokens": 5}
    assert len(bodies) == 2
    assert bodies[0]["tool_choice"]["type"] == "auto"
    assert "report_tasks" in bodies[0]["messages"][0]["content"][-1]["text"]


def test_task_edit(client):
    client.post("/api/sources", json={"name": "Psych", "match_pattern": "פסיכולוג"})
    client.post("/api/ingest", headers=H, json={"source": "mac", "chat": "יובל - פסיכולוג", "sender": "גיא",
                                                "text": "האם ניתן להקדים את הפגישות לשעה 8:25?", "external_id": "1"})
    mid = client.get("/api/messages").json()[0]["id"]
    tid = client.post(f"/api/messages/{mid}/missed", json={"category_key": "meeting_changed", "create_task": True,
                                                            "title": "Wrong title"}).json()["task_id"]
    r = client.patch(f"/api/tasks/{tid}", json={"title": "פגישות ראשון עוברות ל-8:25", "start_time": "08:25",
                                                    "due_date": None})
    assert r.status_code == 200 and r.json()["title"] == "פגישות ראשון עוברות ל-8:25" and r.json()["start_time"] == "08:25"
