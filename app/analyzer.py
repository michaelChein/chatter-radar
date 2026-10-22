"""Near-real-time analysis: per source, wait for the chat to go quiet, then send the new messages to Claude."""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from . import attachments, db, instructions, llm, prompts
from .store import TZ

log = logging.getLogger("radar.analyzer")

DEBOUNCE = int(os.environ.get("RADAR_DEBOUNCE_SECONDS", "120"))   # chat quiet for this long → analyze
MAX_WAIT = int(os.environ.get("RADAR_MAX_WAIT_SECONDS", "600"))   # …or oldest pending message is this old
POLL = int(os.environ.get("RADAR_POLL_SECONDS", "15"))
# Default: analyze four times a day (cheap), plus a fast path for urgent messages. Realtime mode analyzes after
# every burst of messages instead. Change it in the UI (Pipeline tab) or with RADAR_SCHEDULE ("" or "realtime" =
# realtime, or comma-separated local times). The UI choice, stored in the database, wins over the env var.
DEFAULT_SCHEDULE = "07:00,12:00,16:00,20:00"
URGENT_DEBOUNCE = int(os.environ.get("RADAR_URGENT_DEBOUNCE_SECONDS", "90"))  # collect follow-ups before an urgent run
MAX_BATCH = 80
CONTEXT_MSGS = 15
ERROR_BACKOFF = 300
FEEDBACK_EXAMPLES = 6

_lock = threading.Lock()
_stop = threading.Event()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _local(ts: str) -> str:
    return _parse(ts).astimezone(TZ).strftime("%a %d.%m.%Y %H:%M")


def _line(m: dict, atts: list[dict] | None = None, shown: set | None = None) -> str:
    extra = ""
    if atts:
        extra = " " + " ".join(attachments.placeholder(a, a["id"] in (shown or set())) for a in atts)
    elif m.get("media_type"):
        extra = f" [attachment: {m['media_type']} — not visible to you]"
    return f"#{m['id']} [{_local(m['sent_at'])}] {m.get('sender') or '?'}: {m['body']}{extra}"


def parse_schedule(value: str) -> list[str]:
    """"realtime"/"" -> []; "7:00, 12:00" -> ["07:00", "12:00"]. Raises ValueError on anything else."""
    value = (value or "").strip().lower()
    if value in ("", "realtime"):
        return []
    times = set()
    for part in value.split(","):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part.strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f"not a time of day (HH:MM): {part.strip()!r}")
        times.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    if len(times) > 12:
        raise ValueError("at most 12 times a day")
    return sorted(times)


def schedule() -> list[str]:
    """Local times of day when everything pending is analyzed; [] means realtime (after every burst)."""
    value = db.kv_get("schedule")
    if value is None:
        value = os.environ.get("RADAR_SCHEDULE", DEFAULT_SCHEDULE)
    try:
        return parse_schedule(value)
    except ValueError:
        return parse_schedule(DEFAULT_SCHEDULE)


def set_schedule(times: list[str] | None) -> list[str]:
    """Save the user's choice (None/[] = realtime). Switching to a schedule must not fire an earlier slot today."""
    parsed = parse_schedule(",".join(times) if times else "realtime")
    db.kv_set("schedule", ",".join(parsed) if parsed else "realtime")
    if parsed:
        mark_slot_done()
    return parsed


def _in_backoff(source_id: int, now: datetime) -> bool:
    last_err = db.row("SELECT created_at FROM batches WHERE source_id=? AND status='error' ORDER BY id DESC LIMIT 1",
                      (source_id,))
    last_ok = db.row("SELECT created_at FROM batches WHERE source_id=? AND status='ok' ORDER BY id DESC LIMIT 1",
                     (source_id,))
    return bool(last_err and (not last_ok or last_err["created_at"] > last_ok["created_at"])
                and now - _parse(last_err["created_at"]) < timedelta(seconds=ERROR_BACKOFF))


def current_slot(now: datetime) -> str | None:
    """The most recent scheduled run time that has passed, as 'YYYY-MM-DD HH:MM' (local), or None."""
    sched = schedule()
    if not sched:
        return None
    local = now.astimezone(TZ)
    best = None
    for day_offset in (0, -1):
        d = (local + timedelta(days=day_offset)).date()
        for t in sched:
            h, m = map(int, t.split(":"))
            slot = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
            if slot <= local and (best is None or slot > best):
                best = slot
    return best.strftime("%Y-%m-%d %H:%M") if best else None


def next_slot(now: datetime) -> str | None:
    sched = schedule()
    if not sched:
        return None
    local = now.astimezone(TZ)
    for day_offset in (0, 1):
        d = (local + timedelta(days=day_offset)).date()
        for t in sched:
            h, m = map(int, t.split(":"))
            slot = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
            if slot > local:
                return slot.strftime("%Y-%m-%d %H:%M")
    return None


def due_sources(now: datetime | None = None) -> list[int]:
    """Sources whose pending messages should be analyzed now.

    Realtime mode (schedule() is empty): when the chat has been quiet for DEBOUNCE, or the oldest pending message
    has waited MAX_WAIT. Scheduled mode: everything pending at each scheduled time, plus — between runs — sources
    with an urgent pending message once the chat has been quiet for URGENT_DEBOUNCE.
    """
    now = now or datetime.now(timezone.utc)
    pending = db.rows("""SELECT source_id, min(ingested_at) AS oldest, max(ingested_at) AS newest, max(urgent) AS urgent
                         FROM messages m JOIN sources s ON s.id = m.source_id
                         WHERE m.analyzed_at IS NULL AND s.enabled = 1 GROUP BY source_id""")
    out = []
    sched = schedule()
    slot = current_slot(now)
    slot_due = bool(slot) and db.kv_get("last_scheduled_slot") != slot
    for r in pending:
        if _in_backoff(r["source_id"], now):
            continue
        quiet_for = now - _parse(r["newest"])
        if sched:
            last = db.row("SELECT status FROM batches WHERE source_id=? ORDER BY id DESC LIMIT 1", (r["source_id"],))
            retry = bool(last and last["status"] == "error")  # backoff already passed (checked above)
            if slot_due or retry or (r["urgent"] and quiet_for >= timedelta(seconds=URGENT_DEBOUNCE)):
                out.append(r["source_id"])
        elif quiet_for >= timedelta(seconds=DEBOUNCE) or now - _parse(r["oldest"]) >= timedelta(seconds=MAX_WAIT):
            out.append(r["source_id"])
    return out


def mark_slot_done(now: datetime | None = None) -> None:
    slot = current_slot(now or datetime.now(timezone.utc))
    if slot:
        db.kv_set("last_scheduled_slot", slot)


def feedback_block(source_id: int, exclude_message_ids: set | None = None) -> str:
    """Recent labels as examples. Replay excludes the labels it is evaluating (no answer leakage)."""
    exclude = exclude_message_ids or set()
    fb = db.rows("""SELECT f.*, (SELECT group_concat(tm.message_id) FROM task_messages tm WHERE tm.task_id = f.task_id) AS task_msgs
                    FROM feedback f ORDER BY (f.source_id = ?) DESC, f.id DESC LIMIT 200""", (source_id,))

    def leaks(f):
        ids = {int(x) for x in (f.get("task_msgs") or "").split(",") if x} | ({f["message_id"]} if f.get("message_id") else set())
        return bool(ids & exclude)

    fb = [f for f in fb if not leaks(f)]
    irr = [f for f in fb if f["kind"] == "irrelevant"][:FEEDBACK_EXAMPLES]
    missed = [f for f in fb if f["kind"] == "missed"][:FEEDBACK_EXAMPLES]
    if not irr and not missed:
        return prompts.NO_FEEDBACK
    parts = []
    if irr:
        parts.append("Tasks the user marked IRRELEVANT — do not create tasks like these:")
        for f in irr:
            s = db.jload(f["snapshot"], {})
            msgs = " / ".join(s.get("messages", []))[:300]
            note = f" — user's note: {f['note']}" if f.get("note") else ""
            parts.append(f"- [{s.get('category_key')}] \"{s.get('title')}\" from message: \"{msgs}\"{note}")
    if missed:
        parts.append("Messages the user says SHOULD have produced a task (you missed them):")
        for f in missed:
            s = db.jload(f["snapshot"], {})
            note = f" — user's note: {f['note']}" if f.get("note") else ""
            parts.append(f"- \"{s.get('body', '')[:300]}\" → expected category {f.get('category_key') or '?'}{note}")
    return "\n".join(parts)


def rules_block(instr: dict, source_id: int) -> str:
    rules = [r for r in instr["rules"] if r["source_id"] is None or str(r["source_id"]) == str(source_id)]
    if not rules:
        return prompts.NO_RULES
    return "\n".join(f"- {r['text']}" + ("" if r["source_id"] is not None else " (all sources)") for r in rules)


def build_prompt(source: dict, new_msgs: list[dict], now: datetime, instr: dict | None = None,
                 open_tasks: list[dict] | None = None, exclude_feedback_for: set | None = None,
                 with_attachments: bool = True) -> tuple[str, list]:
    instr = instr or instructions.current()
    src_instr = instr["sources"].get(str(source["id"]), {})
    cats = [c for c in instr["categories"] if c["enabled"]]
    cat_text = "\n".join(f"- `{c['key']}` ({c['name']}): {c['description']}" for c in cats)
    system = prompts.SYSTEM.format(
        categories=cat_text,
        source_context=(src_instr.get("context", source.get("context")) or "").strip() or "(none)",
        rules=rules_block(instr, source["id"]),
        feedback=feedback_block(source["id"], exclude_feedback_for))

    ctx = db.rows("""SELECT * FROM messages WHERE source_id=? AND id < ? ORDER BY id DESC LIMIT ?""",
                  (source["id"], new_msgs[0]["id"], CONTEXT_MSGS))[::-1]
    if open_tasks is None:
        open_tasks = db.rows("""SELECT id, category_key AS category, title, details, due_date, start_time, end_time, location
                                FROM tasks WHERE source_id=? AND status='open' ORDER BY id""", (source["id"],))

    atts = attachments.for_messages([m["id"] for m in new_msgs]) if with_attachments else {}
    blocks, shown = [], set()
    for m in new_msgs:
        for a in atts.get(m["id"], []):
            if a["kind"] == "voice":
                attachments.transcribe(a)
            elif len(shown) < attachments.MAX_PER_BATCH and (b := attachments.block_for(a)):
                blocks.append((a, b))
                shown.add(a["id"])
    ctx_atts = attachments.for_messages([m["id"] for m in ctx]) if with_attachments else {}

    text = "\n".join([
        f"Now: {now.astimezone(TZ).strftime('%A %Y-%m-%d %H:%M')} (local time)",
        f"Source: {source['name']} (chat: {new_msgs[-1]['chat_name']})",
        "",
        f"## Open tasks from this source ({len(open_tasks)})",
        json.dumps(open_tasks, ensure_ascii=False, indent=1) if open_tasks else "(none)",
        "",
        f"## Earlier messages — context only ({len(ctx)})",
        "\n".join(_line(m, ctx_atts.get(m["id"])) for m in ctx) or "(none)",
        "",
        f"## NEW messages ({len(new_msgs)})",
        "\n".join(_line(m, atts.get(m["id"]), shown) for m in new_msgs),
    ])
    content = [{"type": "text", "text": text}]
    for a, b in blocks:
        content.append({"type": "text", "text": f"Attachment of message #{a['message_id']}"
                                                f"{' (' + a['filename'] + ')' if a.get('filename') else ''}:"})
        content.append(b)
    return system, content


def _norm_title(s: str) -> str:
    return re.sub(r"\W+", "", (s or "").lower())


def _redact(content: list) -> list:
    """Batch logs keep the prompt text but not base64 payloads."""
    return [b if b.get("type") == "text" else {"type": b["type"], "omitted": True} for b in content]


def analyze_source(source_id: int, now: datetime | None = None) -> dict:
    """Analyze pending messages of one source. Returns summary dict."""
    with _lock:
        now = now or datetime.now(timezone.utc)
        source = db.row("SELECT * FROM sources WHERE id=?", (source_id,))
        new_msgs = db.rows("SELECT * FROM messages WHERE source_id=? AND analyzed_at IS NULL ORDER BY id LIMIT ?",
                           (source_id, MAX_BATCH))
        if not source or not new_msgs:
            return {"batch_id": None, "tasks_created": 0}

        system, content = build_prompt(source, new_msgs, now)
        with db.tx() as conn:
            batch_id = conn.execute(
                "INSERT INTO batches (source_id, created_at, first_msg_id, last_msg_id, model, request_json) VALUES (?,?,?,?,?,?)",
                (source_id, db.now_iso(), new_msgs[0]["id"], new_msgs[-1]["id"], llm.model_name(),
                 json.dumps({"system": system, "user": content[0]["text"], "content": _redact(content)},
                            ensure_ascii=False))).lastrowid
        try:
            result, usage = llm.call_tool(system, content, prompts.TOOL)
        except Exception as e:  # keep messages pending; retry after backoff
            log.exception("analysis failed for source %s", source_id)
            with db.tx() as conn:
                conn.execute("UPDATE batches SET status='error', error=? WHERE id=?", (str(e)[:2000], batch_id))
            return {"batch_id": batch_id, "error": str(e)}

        created = apply_result(source, new_msgs, batch_id, result)
        with db.tx() as conn:
            conn.execute("""UPDATE batches SET status='ok', response_json=?, input_tokens=?, output_tokens=?, tasks_created=?
                            WHERE id=?""",
                         (json.dumps(result, ensure_ascii=False), usage.get("input_tokens"), usage.get("output_tokens"),
                          created, batch_id))
        return {"batch_id": batch_id, "tasks_created": created}


def apply_result(source: dict, new_msgs: list[dict], batch_id: int, result: dict) -> int:
    new_ids = {m["id"] for m in new_msgs}
    cats = {c["key"] for c in db.rows("SELECT key FROM categories WHERE enabled=1")}
    now = db.now_iso()
    created = 0
    with db.tx() as conn:
        open_tasks = {t["id"]: dict(t) for t in conn.execute(
            "SELECT * FROM tasks WHERE source_id=? AND status='open'", (source["id"],)).fetchall()}

        for c in result.get("cancelled") or []:
            tid = c.get("task_id")
            if tid in open_tasks:
                conn.execute("UPDATE tasks SET status='cancelled', status_note=?, updated_at=? WHERE id=?",
                             (c.get("reason"), now, tid))
                for mid in set(c.get("evidence_message_ids") or []) & new_ids:
                    conn.execute("INSERT OR IGNORE INTO task_messages VALUES (?,?)", (tid, mid))

        for t in result.get("tasks") or []:
            cat = t.get("category")
            if cat not in cats or not (t.get("title") or "").strip():
                continue
            ev = [i for i in (t.get("evidence_message_ids") or []) if i in new_ids] or [new_msgs[-1]["id"]]
            sup = t.get("supersedes_task_id") if t.get("supersedes_task_id") in open_tasks else None
            dup = any(o["category_key"] == cat and o["due_date"] == t.get("due_date")
                      and _norm_title(o["title"]) == _norm_title(t["title"]) for o in open_tasks.values())
            if dup and not sup:
                continue
            tid = conn.execute(
                """INSERT INTO tasks (source_id, category_key, title, details, due_date, start_time, end_time, location,
                                      amount, currency, payment_url, confidence, origin, batch_id, supersedes_task_id,
                                      created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'model',?,?,?,?)""",
                (source["id"], cat, t["title"].strip(), t.get("details"), _date(t.get("due_date")),
                 _time(t.get("start_time")), _time(t.get("end_time")), t.get("location"), _num(t.get("amount")),
                 t.get("currency"), t.get("payment_url"), _num(t.get("confidence")), batch_id, sup, now, now)).lastrowid
            for mid in ev:
                conn.execute("INSERT OR IGNORE INTO task_messages VALUES (?,?)", (tid, mid))
            if sup:
                conn.execute("UPDATE tasks SET status='superseded', status_note=?, updated_at=? WHERE id=?",
                             (f"superseded by #{tid}", now, sup))
                open_tasks.pop(sup, None)
            open_tasks[tid] = {"id": tid, "category_key": cat, "due_date": t.get("due_date"), "title": t["title"]}
            created += 1

        conn.execute(f"UPDATE messages SET analyzed_at=?, batch_id=? WHERE id IN ({','.join('?' * len(new_ids))})",
                     (now, batch_id, *new_ids))
    return created


def _date(s):
    return s if isinstance(s, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def _time(s):
    if isinstance(s, str) and (m := re.fullmatch(r"(\d{1,2}):(\d{2})", s.strip())):
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def requeue_history(source_id: int, days: int = 7) -> int:
    """Make the stored-but-never-analyzed history of a source (last `days` days) waiting again. Returns how many."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with db.tx() as conn:
        return conn.execute("""UPDATE messages SET analyzed_at = NULL
                               WHERE source_id = ? AND batch_id IS NULL AND analyzed_at IS NOT NULL AND sent_at >= ?""",
                            (source_id, cutoff)).rowcount


_manual = {"running": False}
_manual_lock = threading.Lock()


def pending_sources() -> list[int]:
    return [r["source_id"] for r in db.rows(
        """SELECT DISTINCT m.source_id FROM messages m JOIN sources s ON s.id = m.source_id
           WHERE m.analyzed_at IS NULL AND s.enabled = 1""")]


def manual_running() -> bool:
    return _manual["running"]


def start_manual_run() -> dict:
    """Analyze everything that is waiting, right now, in the background (ignores the schedule and the error backoff)."""
    ids = pending_sources()
    with _manual_lock:
        if _manual["running"]:
            return {"started": False, "running": True, "sources": len(ids)}
        if not ids:
            return {"started": False, "running": False, "sources": 0}
        _manual["running"] = True

    def run():
        try:
            for sid in ids:
                try:
                    analyze_source(sid)
                except Exception:
                    log.exception("manual analysis failed for source %s", sid)
        finally:
            _manual["running"] = False
    threading.Thread(target=run, daemon=True, name="manual-analysis").start()
    return {"started": True, "running": True, "sources": len(ids)}


def loop():
    sched = schedule()
    log.info("analyzer loop started (%s)", f"schedule={','.join(sched)}, urgent after {URGENT_DEBOUNCE}s"
             if sched else f"realtime: debounce={DEBOUNCE}s, max_wait={MAX_WAIT}s")
    if sched and db.kv_get("last_scheduled_slot") is None:
        mark_slot_done()  # first start: don't treat an earlier slot today as missed
    while not _stop.wait(POLL):
        try:
            now = datetime.now(timezone.utc)
            for sid in due_sources(now):
                analyze_source(sid, now)
            mark_slot_done(now)
        except Exception:
            log.exception("analyzer loop error")


def start() -> threading.Thread:
    _stop.clear()
    t = threading.Thread(target=loop, name="analyzer", daemon=True)
    t.start()
    return t


def stop():
    _stop.set()
