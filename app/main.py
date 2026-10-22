"""Radar — watch chats, surface what needs doing."""
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import about, agent, analyzer, attachments, db, instructions, llm, mcp_server, replay
from .calendar_link import gcal_url
from .normalize import normalize
from .store import ingest as store_ingest, requeue_for_attachment

logging.basicConfig(level=os.environ.get("RADAR_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("radar")

STATIC = Path(__file__).parent / "static"
PASSWORD = os.environ.get("RADAR_PASSWORD", "")
INGEST_TOKEN = os.environ.get("RADAR_INGEST_TOKEN", "")
SECRET = os.environ.get("RADAR_SECRET") or hashlib.sha256(("radar:" + PASSWORD).encode()).hexdigest()
LOCAL_MODE = os.environ.get("RADAR_LOCAL") == "1"  # one-command Mac install: bound to 127.0.0.1, no password
COOKIE = "radar_session"
OPEN_PATHS = {"/login", "/api/login", "/api/ingest", "/api/collector/config", "/api/collector/chats",
              "/api/collector/status", "/api/health",
              "/static/style.css", "/static/login.js", "/static/icon.svg", "/manifest.webmanifest"}


def _session_value() -> str:
    return hmac.new(SECRET.encode(), b"radar-session-v1", hashlib.sha256).hexdigest()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    instructions.ensure_baseline()
    if not PASSWORD and not LOCAL_MODE:
        log.warning("RADAR_PASSWORD is not set — the web UI is open to anyone who can reach it")
    if not INGEST_TOKEN:
        log.warning("RADAR_INGEST_TOKEN is not set — /api/ingest accepts anything")
    if not mcp_server.MCP_TOKEN:
        log.info("RADAR_MCP_TOKEN is not set — the /mcp connector endpoint rejects all requests")
    if os.environ.get("RADAR_DISABLE_ANALYZER") != "1":
        analyzer.start()
    async with MCP_INNER.router.lifespan_context(MCP_INNER):
        yield
    analyzer.stop()


app = FastAPI(title="Chatter Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
MCP_APP, MCP_INNER = mcp_server.asgi_app()
app.mount("/mcp", MCP_APP)


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if path == "/mcp":  # serve the connector at both /mcp and /mcp/ (some clients don't follow redirects)
        request.scope["path"] = "/mcp/"
    if LOCAL_MODE:
        # Bound to localhost without a password, so refuse anything a web page could use to reach us:
        # DNS rebinding (foreign Host header) and cross-site writes (foreign Origin).
        host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        origin = request.headers.get("origin")
        if host not in ("127.0.0.1", "localhost", "::1") or (
                origin and request.method not in ("GET", "HEAD", "OPTIONS")
                and origin.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]") not in ("127.0.0.1", "localhost", "::1")):
            return Response("forbidden", status_code=403)
    if PASSWORD and path not in OPEN_PATHS and not (path == "/mcp" or path.startswith("/mcp/")):
        if not hmac.compare_digest(request.cookies.get(COOKIE, ""), _session_value()):
            if path.startswith("/api/"):
                return Response(status_code=401)
            return RedirectResponse("/login")
    response = await call_next(request)
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate, so a redeploy shows up without a hard reload
    return response


# ---------------------------------------------------------------- pages & auth
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.post("/api/login")
def login(payload: dict = Body(...)):
    if not PASSWORD or hmac.compare_digest(str(payload.get("password", "")), PASSWORD):
        resp = Response(status_code=204)
        resp.set_cookie(COOKIE, _session_value(), max_age=60 * 60 * 24 * 180, httponly=True, samesite="lax")
        return resp
    raise HTTPException(401, "wrong password")


@app.post("/api/logout")
def logout():
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/health")
def health():
    return {"ok": True}


def _iso_slot(slot: str | None) -> str | None:
    return datetime.strptime(slot, "%Y-%m-%d %H:%M").replace(tzinfo=analyzer.TZ).isoformat() if slot else None


@app.get("/api/info")
def info(request: Request):
    base = str(request.base_url).rstrip("/")
    return {"ingest_url": f"{base}/api/ingest", "token_set": bool(INGEST_TOKEN), "model": llm.model_name(),
            "fake_llm": os.environ.get("RADAR_FAKE_LLM") == "1", "debounce_seconds": analyzer.DEBOUNCE,
            "max_wait_seconds": analyzer.MAX_WAIT, "timezone": str(analyzer.TZ), "agent_model": agent.agent_model(),
            "transcription": attachments.transcription_enabled(),
            "schedule": analyzer.schedule(), "next_run": _iso_slot(analyzer.next_slot(datetime.now(timezone.utc))),
            **about.credit(), "local": LOCAL_MODE, "mcp_enabled": bool(mcp_server.MCP_TOKEN), "mcp_url": f"{mcp_server.PUBLIC_URL or base}/mcp/"}


@app.post("/api/schedule")
def set_schedule(payload: dict = Body(...)):
    """{"mode": "realtime"} or {"mode": "scheduled", "times": ["07:00", "12:00"]}."""
    try:
        if payload.get("mode") == "realtime":
            times = analyzer.set_schedule(None)
        else:
            times = analyzer.set_schedule(payload.get("times") or analyzer.parse_schedule(analyzer.DEFAULT_SCHEDULE))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"schedule": times, "next_run": _iso_slot(analyzer.next_slot(datetime.now(timezone.utc)))}


# ---------------------------------------------------------------- ingest
def _check_token(request: Request) -> None:
    token = request.headers.get("x-token") or request.query_params.get("token") or ""
    auth_h = request.headers.get("authorization", "")
    if auth_h.lower().startswith("bearer "):
        token = token or auth_h[7:]
    if INGEST_TOKEN and not hmac.compare_digest(token, INGEST_TOKEN):
        raise HTTPException(403, "bad token")


@app.post("/api/ingest")
async def ingest(request: Request):
    """Messages from a collector (JSON or form). Multipart requests may carry files named "file",
    which are attached to the (single) message in the request; message metadata can also come as a JSON
    string in a "message" field."""
    _check_token(request)
    ctype = request.headers.get("content-type", "")
    files = []
    if "json" in ctype:
        payload = await request.json()
    else:
        form = await request.form()
        files = [f for k, f in form.multi_items() if k == "file" and hasattr(f, "read")]
        payload = {k: v for k, v in form.items() if not hasattr(v, "read")}
        if "message" in payload:
            payload = json.loads(payload["message"])
        if not payload and not files:
            try:
                payload = json.loads((await request.body()) or b"{}")
            except ValueError:
                payload = {}
    items = payload if isinstance(payload, list) else [payload]
    results = []
    for p in items:
        n = normalize(p)
        if n.skip:
            results.append({"skipped": n.reason})
            continue
        r = store_ingest(n)
        if files and r.get("message_id") and len(items) == 1:
            r["attachments"] = []
            for f in files:
                data = await f.read()
                att = attachments.save(r["message_id"], data, f.content_type, f.filename, hint=n.media_type)
                if att:
                    r["attachments"].append(att["id"])
            if r["attachments"] and not r["stored"]:
                requeue_for_attachment(r["message_id"])
        results.append(r)
    return {"results": results}


@app.get("/api/collector/config")
def collector_config(request: Request):
    """What a collector should send: only chats that match an enabled source."""
    _check_token(request)
    return {"sources": db.rows("SELECT channel, match_pattern, is_direct FROM sources WHERE enabled=1"),
            "timezone": str(analyzer.TZ)}


@app.post("/api/collector/chats")
def collector_chats(request: Request, payload: dict = Body(...)):
    """Chat discovery: a collector reports chat names it can see (names only, no content)."""
    _check_token(request)
    collector = str(payload.get("collector") or "unknown")
    now = db.now_iso()
    with db.tx() as conn:
        for c in payload.get("chats") or []:
            name, channel = str(c.get("name") or "").strip(), str(c.get("channel") or "whatsapp")
            if not name:
                continue
            row = conn.execute("SELECT id, seen_by FROM chats WHERE channel=? AND name=?", (channel, name)).fetchone()
            last = c.get("last_message_at") or now
            if row:
                seen = sorted(set(db.jload(row["seen_by"], [])) | {collector})
                conn.execute("UPDATE chats SET seen_by=?, last_seen=max(last_seen, ?) WHERE id=?",
                             (json.dumps(seen, ensure_ascii=False), last, row["id"]))
            else:
                conn.execute("""INSERT INTO chats (channel, name, first_seen, last_seen, msg_count, seen_by)
                                VALUES (?,?,?,?,?,?)""",
                             (channel, name, now, last, int(c.get("message_count") or 0),
                              json.dumps([collector], ensure_ascii=False)))
    return {"ok": True}


@app.get("/api/attachments/{aid}")
def get_attachment(aid: int):
    a = db.row("SELECT * FROM attachments WHERE id=?", (aid,))
    if not a or not attachments.path_of(a).exists():
        raise HTTPException(404, "not found")
    return FileResponse(attachments.path_of(a), media_type=a["mime"] or "application/octet-stream",
                        filename=a["filename"] or None, content_disposition_type="inline")


# ---------------------------------------------------------------- tasks
TASK_SELECT = """SELECT t.*, s.name AS source_name, c.name AS category_name, c.color AS category_color
                 FROM tasks t LEFT JOIN sources s ON s.id = t.source_id
                 LEFT JOIN categories c ON c.key = t.category_key"""


def _att_out(a: dict) -> dict:
    return {"id": a["id"], "kind": a["kind"], "mime": a["mime"], "filename": a["filename"], "size": a["size"],
            "transcript": a["transcript"], "url": f"/api/attachments/{a['id']}"}


def _task_out(t: dict) -> dict:
    t["messages"] = db.rows("""SELECT m.id, m.sender, m.body, m.sent_at, m.media_type, m.chat_name FROM task_messages tm
                               JOIN messages m ON m.id = tm.message_id WHERE tm.task_id=? ORDER BY m.id""", (t["id"],))
    atts = attachments.for_messages([m["id"] for m in t["messages"]])
    for m in t["messages"]:
        m["attachments"] = [_att_out(a) for a in atts.get(m["id"], [])]
    t["calendar_url"] = gcal_url(t, t.get("source_name"))
    return t


@app.get("/api/tasks")
def list_tasks(status: str = "open", category: str | None = None, source_id: int | None = None, limit: int = 300):
    where, params = [], []
    if status == "closed":
        where.append("t.status IN ('done','irrelevant','superseded','cancelled')")
    elif status != "all":
        where.append("t.status = ?"); params.append(status)
    if category:
        where.append("t.category_key = ?"); params.append(category)
    if source_id:
        where.append("t.source_id = ?"); params.append(source_id)
    order = ("CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END, t.due_date, t.start_time, t.id DESC"
             if status == "open" else "t.updated_at DESC")
    sql = TASK_SELECT + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY {order} LIMIT ?"
    return [_task_out(t) for t in db.rows(sql, (*params, limit))]


def _get_task(tid: int) -> dict:
    t = db.row(TASK_SELECT + " WHERE t.id = ?", (tid,))
    if not t:
        raise HTTPException(404, "task not found")
    return t


def _set_status(tid: int, status: str, note: str | None = None) -> dict:
    _get_task(tid)
    with db.tx() as conn:
        conn.execute("UPDATE tasks SET status=?, status_note=?, updated_at=? WHERE id=?", (status, note, db.now_iso(), tid))
    return _task_out(_get_task(tid))


@app.post("/api/tasks/{tid}/done")
def task_done(tid: int):
    return _set_status(tid, "done")


@app.post("/api/tasks/{tid}/reopen")
def task_reopen(tid: int):
    t = _get_task(tid)
    if t["status"] == "irrelevant":
        with db.tx() as conn:
            conn.execute("DELETE FROM feedback WHERE kind='irrelevant' AND task_id=?", (tid,))
    return _set_status(tid, "open")


@app.post("/api/tasks/{tid}/irrelevant")
def task_irrelevant(tid: int, payload: dict = Body(default={})):
    t = _task_out(_get_task(tid))
    note = (payload.get("note") or "").strip() or None
    snap = {"title": t["title"], "details": t["details"], "category_key": t["category_key"],
            "messages": [m["body"] for m in t["messages"]]}
    with db.tx() as conn:
        conn.execute("DELETE FROM feedback WHERE kind='irrelevant' AND task_id=?", (tid,))
        conn.execute("""INSERT INTO feedback (kind, task_id, source_id, category_key, note, snapshot, created_at)
                        VALUES ('irrelevant',?,?,?,?,?,?)""",
                     (tid, t["source_id"], t["category_key"], note, json.dumps(snap, ensure_ascii=False), db.now_iso()))
    return _set_status(tid, "irrelevant", note)


@app.post("/api/tasks/{tid}/calendar")
def task_calendar(tid: int):
    t = _get_task(tid)
    with db.tx() as conn:
        conn.execute("UPDATE tasks SET calendar_sent_at=?, updated_at=? WHERE id=?", (db.now_iso(), db.now_iso(), tid))
    return {"url": gcal_url(t, t.get("source_name"))}


@app.patch("/api/tasks/{tid}")
def task_edit(tid: int, payload: dict = Body(...)):
    _get_task(tid)
    allowed = ["title", "details", "due_date", "start_time", "end_time", "location", "amount", "currency",
               "payment_url", "category_key"]
    sets = {k: payload[k] for k in allowed if k in payload}
    if sets:
        with db.tx() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(f'{k}=?' for k in sets)}, updated_at=? WHERE id=?",
                         (*sets.values(), db.now_iso(), tid))
    return _task_out(_get_task(tid))


# ---------------------------------------------------------------- messages & false negatives
@app.get("/api/messages")
def list_messages(source_id: int | None = None, q: str | None = None, before_id: int | None = None,
                  only: str | None = None, limit: int = 100):
    where, params = [], []
    if source_id:
        where.append("m.source_id = ?"); params.append(source_id)
    if q:
        where.append("(m.body LIKE ? OR m.sender LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
    if before_id and (b := db.row("SELECT sent_at FROM messages WHERE id=?", (before_id,))):
        where.append("(m.sent_at < ? OR (m.sent_at = ? AND m.id < ?))"); params += [b["sent_at"], b["sent_at"], before_id]
    if only == "untasked":
        where.append("NOT EXISTS (SELECT 1 FROM task_messages tm WHERE tm.message_id = m.id)")
    if only == "pending":
        where.append("m.analyzed_at IS NULL")
    sql = """SELECT m.*, s.name AS source_name FROM messages m JOIN sources s ON s.id = m.source_id""" + \
          (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY m.sent_at DESC, m.id DESC LIMIT ?"
    msgs = db.rows(sql, (*params, min(limit, 500)))
    atts = attachments.for_messages([m["id"] for m in msgs])
    for m in msgs:
        m["attachments"] = [_att_out(a) for a in atts.get(m["id"], [])]
        m["collectors"] = db.jload(m["collectors"], [])
        m["tasks"] = db.rows("""SELECT t.id, t.title, t.status, t.category_key, c.color AS category_color FROM task_messages tm
                                JOIN tasks t ON t.id = tm.task_id LEFT JOIN categories c ON c.key = t.category_key
                                WHERE tm.message_id = ?""", (m["id"],))
        m["missed"] = db.row("SELECT id, category_key, note FROM feedback WHERE kind='missed' AND message_id=?", (m["id"],))
    return msgs


@app.post("/api/messages/{mid}/missed")
def mark_missed(mid: int, payload: dict = Body(default={})):
    m = db.row("SELECT * FROM messages WHERE id=?", (mid,))
    if not m:
        raise HTTPException(404, "message not found")
    cat = payload.get("category_key")
    if cat and not db.row("SELECT 1 FROM categories WHERE key=?", (cat,)):
        raise HTTPException(400, "unknown category")
    note = (payload.get("note") or "").strip() or None
    snap = {"body": m["body"], "sender": m["sender"], "chat": m["chat_name"], "sent_at": m["sent_at"]}
    now = db.now_iso()
    task_id = None
    with db.tx() as conn:
        conn.execute("DELETE FROM feedback WHERE kind='missed' AND message_id=?", (mid,))
        conn.execute("""INSERT INTO feedback (kind, message_id, source_id, category_key, note, snapshot, created_at)
                        VALUES ('missed',?,?,?,?,?,?)""",
                     (mid, m["source_id"], cat, note, json.dumps(snap, ensure_ascii=False), now))
        if payload.get("create_task") and cat:
            title = (payload.get("title") or "").strip() or m["body"][:80]
            task_id = conn.execute(
                """INSERT INTO tasks (source_id, category_key, title, details, due_date, start_time, origin, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,'manual',?,?)""",
                (m["source_id"], cat, title, note, payload.get("due_date") or None, payload.get("start_time") or None,
                 now, now)).lastrowid
            conn.execute("INSERT INTO task_messages VALUES (?,?)", (task_id, mid))
    return {"ok": True, "task_id": task_id}


@app.delete("/api/feedback/{fid}")
def delete_feedback(fid: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM feedback WHERE id=?", (fid,))
    return {"ok": True}


@app.get("/api/feedback")
def list_feedback(limit: int = 200):
    out = db.rows("""SELECT f.*, s.name AS source_name FROM feedback f LEFT JOIN sources s ON s.id=f.source_id
                     ORDER BY f.id DESC LIMIT ?""", (limit,))
    for f in out:
        f["snapshot"] = db.jload(f["snapshot"], {})
    return out


@app.get("/api/feedback/export.jsonl")
def export_feedback():
    """Eval set: one line per labeled example (false positives, false negatives, and confirmed tasks)."""
    lines = []
    for f in list_feedback(limit=100000):
        lines.append({"label": "false_positive" if f["kind"] == "irrelevant" else "false_negative",
                      "source": f["source_name"], "category": f["category_key"], "note": f["note"], **f["snapshot"]})
    for t in db.rows("SELECT id FROM tasks WHERE status='done' AND origin='model'"):
        t = _task_out(_get_task(t["id"]))
        lines.append({"label": "true_positive", "source": t["source_name"], "category": t["category_key"],
                      "title": t["title"], "messages": [m["body"] for m in t["messages"]]})
    return PlainTextResponse("\n".join(json.dumps(l, ensure_ascii=False) for l in lines),
                             media_type="application/x-ndjson",
                             headers={"content-disposition": "attachment; filename=radar-feedback.jsonl"})


@app.get("/api/stats")
def stats():
    per_cat = db.rows("""SELECT c.key, c.name, c.color,
          (SELECT count(*) FROM tasks t WHERE t.category_key=c.key AND t.origin='model') AS created,
          (SELECT count(*) FROM tasks t WHERE t.category_key=c.key AND t.origin='model' AND t.status='done') AS done,
          (SELECT count(*) FROM tasks t WHERE t.category_key=c.key AND t.origin='model' AND t.status='irrelevant') AS irrelevant,
          (SELECT count(*) FROM feedback f WHERE f.category_key=c.key AND f.kind='missed') AS missed
        FROM categories c ORDER BY c.sort, c.id""")
    totals = db.row("""SELECT (SELECT count(*) FROM messages) AS messages,
                              (SELECT count(*) FROM messages WHERE analyzed_at IS NULL) AS pending,
                              (SELECT count(*) FROM tasks WHERE status='open') AS open_tasks,
                              (SELECT coalesce(sum(input_tokens),0) FROM batches) AS input_tokens,
                              (SELECT coalesce(sum(output_tokens),0) FROM batches) AS output_tokens,
                              (SELECT count(*) FROM batches WHERE status='error') AS failed_batches""")
    return {"categories": per_cat, "totals": totals}


@app.get("/api/batches")
def list_batches(limit: int = 30, source_id: int | None = None):
    sql = "SELECT b.*, s.name AS source_name FROM batches b LEFT JOIN sources s ON s.id=b.source_id"
    params = []
    if source_id:
        sql += " WHERE b.source_id=?"; params.append(source_id)
    return db.rows(sql + " ORDER BY b.id DESC LIMIT ?", (*params, limit))


# ---------------------------------------------------------------- sources & chats
@app.get("/api/sources")
def list_sources():
    return db.rows("""SELECT s.*,
        (SELECT count(*) FROM messages m WHERE m.source_id=s.id) AS message_count,
        (SELECT count(*) FROM messages m WHERE m.source_id=s.id AND m.analyzed_at IS NULL) AS pending_count,
        (SELECT max(sent_at) FROM messages m WHERE m.source_id=s.id) AS last_message_at,
        (SELECT count(*) FROM tasks t WHERE t.source_id=s.id AND t.status='open') AS open_tasks,
        (SELECT count(*) FROM messages m WHERE m.source_id=s.id AND m.batch_id IS NULL AND m.analyzed_at IS NOT NULL
            AND m.sent_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')) AS history_count
        FROM sources s ORDER BY s.name""")


def _source_fields(p: dict) -> dict:
    name = (p.get("name") or "").strip()
    pattern = (p.get("match_pattern") or "").strip()
    if not name or not pattern:
        raise HTTPException(400, "name and match_pattern are required")
    return {"name": name, "match_pattern": pattern, "channel": (p.get("channel") or "whatsapp").strip(),
            "context": (p.get("context") or "").strip(), "enabled": 1 if p.get("enabled", True) else 0,
            "is_direct": 1 if p.get("is_direct") else 0,
            "history_days": max(0, min(30, int(p.get("history_days") or 0)))}


@app.post("/api/sources")
def create_source(payload: dict = Body(...)):
    f = _source_fields(payload)
    with db.tx() as conn:
        sid = conn.execute("""INSERT INTO sources (name, channel, match_pattern, context, enabled, is_direct, history_days,
                                                   created_at)
                              VALUES (?,?,?,?,?,?,?,?)""",
                           (f["name"], f["channel"], f["match_pattern"], f["context"], f["enabled"], f["is_direct"],
                            f["history_days"], db.now_iso())).lastrowid
    return db.row("SELECT * FROM sources WHERE id=?", (sid,))


@app.put("/api/sources/{sid}")
def update_source(sid: int, payload: dict = Body(...)):
    f = _source_fields(payload)
    with db.tx() as conn:
        old = conn.execute("SELECT context FROM sources WHERE id=?", (sid,)).fetchone()
        conn.execute("UPDATE sources SET name=?, channel=?, match_pattern=?, context=?, enabled=?, is_direct=? WHERE id=?",
                     (f["name"], f["channel"], f["match_pattern"], f["context"], f["enabled"], f["is_direct"], sid))
    if old and old["context"] != f["context"]:
        instructions.record_user_edit(f"context of {f['name']} edited by user")
    return db.row("SELECT * FROM sources WHERE id=?", (sid,))


@app.delete("/api/sources/{sid}")
def delete_source(sid: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (sid,))
    return {"ok": True}


@app.post("/api/sources/{sid}/analyze")
def analyze_now(sid: int):
    return analyzer.analyze_source(sid)


@app.post("/api/sources/{sid}/analyze-history")
def analyze_history(sid: int, payload: dict = Body(default={})):
    """Look for tasks in the last `days` (default 7) of a source's stored history, in the background."""
    if not db.row("SELECT id FROM sources WHERE id=?", (sid,)):
        raise HTTPException(404, "no such source")
    days = max(1, min(30, int(payload.get("days") or 7)))
    n = analyzer.requeue_history(sid, days)
    started = analyzer.start_manual_run()["started"] if n else False
    return {"requeued": n, "started": started}


@app.get("/api/chats")
def list_chats():
    chats = db.rows("SELECT * FROM chats ORDER BY last_seen DESC")
    sources = db.rows("SELECT id, name, match_pattern, channel FROM sources WHERE enabled=1")
    for c in chats:
        c["seen_by"] = db.jload(c["seen_by"], [])
        matches = [s for s in sources if s["channel"] == c["channel"] and s["match_pattern"].lower() in c["name"].lower()]
        c["source"] = max(matches, key=lambda s: len(s["match_pattern"])) if matches else None
    return chats


# ---------------------------------------------------------------- categories
@app.get("/api/categories")
def list_categories():
    return db.rows("SELECT * FROM categories ORDER BY sort, id")


def _cat_fields(p: dict) -> dict:
    name, desc = (p.get("name") or "").strip(), (p.get("description") or "").strip()
    if not name or not desc:
        raise HTTPException(400, "name and description are required")
    return {"name": name, "description": desc, "color": p.get("color") or "#6b7280",
            "enabled": 1 if p.get("enabled", True) else 0, "sort": int(p.get("sort") or 100)}


@app.post("/api/categories")
def create_category(payload: dict = Body(...)):
    f = _cat_fields(payload)
    key = (payload.get("key") or "").strip().lower().replace(" ", "_") or \
        "".join(ch if ch.isalnum() else "_" for ch in f["name"].lower()).strip("_") or "category"
    if db.row("SELECT 1 FROM categories WHERE key=?", (key,)):
        key = f"{key}_{db.row('SELECT count(*) AS n FROM categories')['n'] + 1}"
    with db.tx() as conn:
        conn.execute("INSERT INTO categories (key, name, description, color, enabled, sort) VALUES (?,?,?,?,?,?)",
                     (key, f["name"], f["description"], f["color"], f["enabled"], f["sort"]))
    return db.row("SELECT * FROM categories WHERE key=?", (key,))


@app.put("/api/categories/{cid}")
def update_category(cid: int, payload: dict = Body(...)):
    f = _cat_fields(payload)
    with db.tx() as conn:
        old = conn.execute("SELECT description, enabled FROM categories WHERE id=?", (cid,)).fetchone()
        conn.execute("UPDATE categories SET name=?, description=?, color=?, enabled=?, sort=? WHERE id=?",
                     (f["name"], f["description"], f["color"], f["enabled"], f["sort"], cid))
    if old and (old["description"] != f["description"] or old["enabled"] != f["enabled"]):
        instructions.record_user_edit(f"category {f['name']} edited by user")
    return db.row("SELECT * FROM categories WHERE id=?", (cid,))


# ---------------------------------------------------------------- agent chat
@app.get("/api/conversations")
def list_conversations():
    return db.rows("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 50")


@app.post("/api/conversations")
def create_conversation(payload: dict = Body(default={})):
    now = db.now_iso()
    with db.tx() as conn:
        cid = conn.execute("INSERT INTO conversations (title, created_at, updated_at) VALUES (?,?,?)",
                           ((payload.get("title") or None), now, now)).lastrowid
    return db.row("SELECT * FROM conversations WHERE id=?", (cid,))


@app.get("/api/conversations/{cid}")
def get_conversation(cid: int):
    c = db.row("SELECT * FROM conversations WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "not found")
    return {**c, "items": agent.render(cid)}


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: int):
    with db.tx() as conn:
        conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/conversations/{cid}/messages")
def send_chat(cid: int, payload: dict = Body(...)):
    if not db.row("SELECT 1 FROM conversations WHERE id=?", (cid,)):
        raise HTTPException(404, "not found")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    try:
        out = agent.respond(cid, text)
    except Exception as e:  # surface API errors in the chat instead of a blank failure
        log.exception("agent failed")
        agent._store(cid, "assistant", [{"type": "text", "text": f"⚠️ Sorry — something went wrong: {str(e)[:300]}"}])
        out = {"changed": False}
    return {**out, "items": agent.render(cid)}


# ---------------------------------------------------------------- instructions & proposals
def _proposal_out(p: dict) -> dict:
    ops = json.loads(p["changes"])
    report = db.jload(p["replay"], {}) or {}
    return {**{k: p[k] for k in ("id", "created_at", "conversation_id", "status", "rationale", "decided_at", "decided_note")},
            "changes": ops, "lines": instructions.describe_ops(ops, instructions.current()),
            "replay_summary": report.get("summary"), "replay": report}


@app.get("/api/proposals")
def list_proposals(status: str | None = None, limit: int = 50):
    sql, params = "SELECT * FROM proposals", []
    if status:
        sql += " WHERE status=?"; params.append(status)
    return [_proposal_out(p) for p in db.rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]


@app.get("/api/proposals/{pid}")
def get_proposal(pid: int):
    p = db.row("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "not found")
    return _proposal_out(p)


@app.post("/api/proposals/{pid}/approve")
def approve_proposal(pid: int):
    p = db.row("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p or p["status"] != "pending":
        raise HTTPException(409, "proposal is not pending")
    try:
        vid = instructions.apply_to_db(json.loads(p["changes"]), pid, f"proposal #{pid}")
    except instructions.OpError as e:
        raise HTTPException(409, f"can't apply anymore: {e}")
    with db.tx() as conn:
        conn.execute("UPDATE proposals SET status='approved', decided_at=? WHERE id=?", (db.now_iso(), pid))
    if p["conversation_id"]:
        agent.add_event(p["conversation_id"], f"The user APPROVED proposal #{pid}; it is now live (instructions version {vid}).")
    return get_proposal(pid)


@app.post("/api/proposals/{pid}/reject")
def reject_proposal(pid: int, payload: dict = Body(default={})):
    p = db.row("SELECT * FROM proposals WHERE id=?", (pid,))
    if not p or p["status"] != "pending":
        raise HTTPException(409, "proposal is not pending")
    note = (payload.get("note") or "").strip() or None
    with db.tx() as conn:
        conn.execute("UPDATE proposals SET status='rejected', decided_at=?, decided_note=? WHERE id=?",
                     (db.now_iso(), note, pid))
    if p["conversation_id"]:
        agent.add_event(p["conversation_id"], f"The user REJECTED proposal #{pid}" + (f" — their reason: {note}" if note else "") + ".")
    return get_proposal(pid)


@app.get("/api/instructions")
def get_instructions():
    cur = instructions.current()
    names = {k: v["name"] for k, v in cur["sources"].items()}
    return {"rules": [{**r, "source_name": names.get(str(r["source_id"])) if r["source_id"] else None} for r in cur["rules"]],
            "max_rules": instructions.MAX_RULES,
            "versions": db.rows("SELECT id, created_at, proposal_id, note FROM instruction_versions ORDER BY id DESC LIMIT 30"),
            "pending": db.row("SELECT count(*) AS n FROM proposals WHERE status='pending'")["n"],
            "new_feedback": db.row("""SELECT count(*) AS n FROM feedback WHERE created_at >
                                      coalesce((SELECT max(created_at) FROM proposals), '')""")["n"]}


@app.post("/api/rules")
def add_rule(payload: dict = Body(...)):
    try:
        instructions.apply_to_db([{"op": "add_rule", "source_id": payload.get("source_id"), "text": payload.get("text")}],
                                 None, "rule added by user")
    except instructions.OpError as e:
        raise HTTPException(400, str(e))
    return get_instructions()


@app.put("/api/rules/{rid}")
def edit_rule(rid: int, payload: dict = Body(...)):
    try:
        instructions.apply_to_db([{"op": "edit_rule", "rule_id": rid, "text": payload.get("text")}], None, "rule edited by user")
    except instructions.OpError as e:
        raise HTTPException(400, str(e))
    return get_instructions()


@app.delete("/api/rules/{rid}")
def delete_rule(rid: int):
    try:
        instructions.apply_to_db([{"op": "remove_rule", "rule_id": rid}], None, "rule removed by user")
    except instructions.OpError as e:
        raise HTTPException(400, str(e))
    return get_instructions()


@app.post("/api/instructions/versions/{vid}/restore")
def restore_version(vid: int):
    try:
        instructions.restore_version(vid)
    except instructions.OpError as e:
        raise HTTPException(404, str(e))
    return get_instructions()


@app.post("/api/replay")
def run_replay():
    """Evaluate the CURRENT instructions against all labels (costs one model call per labeled batch)."""
    report = replay.run(None)
    report["summary"] = replay.summarize(report)
    return report


# ---------------------------------------------------------------- first-run setup
@app.post("/api/collector/status")
def collector_status(request: Request, payload: dict = Body(...)):
    """A collector reports how its last run went, so the setup screen can say what to fix."""
    _check_token(request)
    name = str(payload.get("collector") or "mac")[:60]
    db.kv_set(f"collector_status:{name}", json.dumps({
        "collector": name, "ok": bool(payload.get("ok")), "error": str(payload.get("error") or "")[:40],
        "message": str(payload.get("message") or "")[:300], "python": str(payload.get("python") or "")[:300],
        "at": db.now_iso()}, ensure_ascii=False))
    return {"ok": True}


def _server_python() -> str:
    """The interpreter that needs Full Disk Access (the collector runs with the same one)."""
    return os.path.realpath(sys.executable)


@app.get("/api/setup")
def setup_status():
    collectors = [json.loads(r["value"]) for r in db.rows("SELECT value FROM kv WHERE key LIKE 'collector_status:%'")]
    done = db.kv_get("setup_done") == "1"
    return {"local": LOCAL_MODE, "fake_llm": os.environ.get("RADAR_FAKE_LLM") == "1",
            "required": LOCAL_MODE and not done, "done": done, "python": _server_python(),
            "can_open_settings": LOCAL_MODE and sys.platform == "darwin",
            "llm_problem": llm.current_problem(),
            "api_key": bool(llm.api_key()), "api_key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "collectors": collectors, "sources": db.rows("SELECT count(*) n FROM sources")[0]["n"]}


@app.post("/api/analyze")
def analyze_all():
    """"Analyze now": everything waiting, in the background. Poll GET /api/analyze for progress."""
    return analyzer.start_manual_run()


@app.get("/api/analyze")
def analyze_status():
    return {"running": analyzer.manual_running(), "pending": len(db.rows("SELECT id FROM messages WHERE analyzed_at IS NULL"))}


@app.post("/api/llm/retry")
def llm_retry():
    """After topping up credit or replacing the key: forget the warning and analyze what is waiting, right now."""
    llm.clear_problem()
    return {"ok": True, "sources": analyzer.start_manual_run()["sources"]}


@app.post("/api/setup/finish")
def setup_finish():
    st = setup_status()
    if not (st["api_key"] or st["fake_llm"]):
        raise HTTPException(400, "Add your API key first.")
    if not st["sources"]:
        raise HTTPException(400, "Choose at least one chat to watch first.")
    db.kv_set("setup_done", "1")
    return {"ok": True}


@app.post("/api/setup/open-settings")
def setup_open_settings(payload: dict = Body(...)):
    """Local mode only: the server runs on the same Mac as the browser, so it can open System Settings for you."""
    if not (LOCAL_MODE and sys.platform == "darwin"):
        raise HTTPException(404, "only available in the one-command Mac install")
    what = payload.get("what")
    if what == "fda":
        cmd = ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"]
    elif what == "file":
        cmd = ["open", "-R", _server_python()]
    else:
        raise HTTPException(400, "what must be 'fda' or 'file'")
    subprocess.run(cmd, check=False, timeout=10)
    return {"ok": True}


@app.post("/api/setup/check-now")
def setup_check_now():
    """Local mode only: run the collector right away instead of waiting for its 2-minute timer."""
    label = os.environ.get("RADAR_COLLECTOR_LABEL")
    if not (LOCAL_MODE and label and sys.platform == "darwin"):
        raise HTTPException(404, "only available in the one-command Mac install")
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=10)
    return {"ok": r.returncode == 0}


@app.post("/api/setup/api-key")
def setup_api_key(payload: dict = Body(...)):
    """Check the key against Anthropic's (free) models endpoint, then store it."""
    key = str(payload.get("key") or "").strip()
    if not key.startswith("sk-ant-") or len(key) < 20:
        raise HTTPException(400, "That doesn't look like an Anthropic API key (it starts with sk-ant-).")
    try:
        r = httpx.get(llm.API_URL.rsplit("/messages", 1)[0] + "/models", timeout=15,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except httpx.HTTPError:
        raise HTTPException(502, "Couldn't reach Anthropic to check the key. Check your internet connection.")
    if r.status_code in (401, 403):
        raise HTTPException(400, "Anthropic rejected that key. Copy it again from console.anthropic.com.")
    if r.status_code >= 400:
        raise HTTPException(502, f"Anthropic returned {r.status_code} while checking the key.")
    db.kv_set("anthropic_api_key", key)
    return {"ok": True}
