"""SQLite storage. One file, WAL mode, plain sqlite3 (no ORM)."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("RADAR_DB", "/data/radar.db")
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
  id          INTEGER PRIMARY KEY,
  key         TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  description TEXT NOT NULL,          -- instructions for the model: what counts / what doesn't
  color       TEXT NOT NULL DEFAULT '#6b7280',
  enabled     INTEGER NOT NULL DEFAULT 1,
  sort        INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS sources (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  channel       TEXT NOT NULL DEFAULT 'whatsapp',
  match_pattern TEXT NOT NULL,        -- case-insensitive substring of the chat name
  context       TEXT NOT NULL DEFAULT '',  -- free text for the model: who's who, what matters here
  is_direct     INTEGER NOT NULL DEFAULT 0, -- 1 = one-on-one chat (sender is the chat name; don't split "X: …")
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);

-- Every chat the collectors have seen (name + counters only; content is stored only for sources).
CREATE TABLE IF NOT EXISTS chats (
  id         INTEGER PRIMARY KEY,
  channel    TEXT NOT NULL,
  name       TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen  TEXT NOT NULL,
  msg_count  INTEGER NOT NULL DEFAULT 0,
  seen_by    TEXT NOT NULL DEFAULT '[]',
  UNIQUE (channel, name)
);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY,
  source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  channel     TEXT NOT NULL,
  chat_name   TEXT NOT NULL,
  sender      TEXT,
  body        TEXT NOT NULL,
  media_type  TEXT,
  sent_at     TEXT NOT NULL,          -- collector timestamp (UTC ISO)
  ingested_at TEXT NOT NULL,          -- server time (UTC ISO), drives batching
  collectors  TEXT NOT NULL DEFAULT '[]',
  dedup_key   TEXT NOT NULL UNIQUE,
  external_key TEXT UNIQUE,            -- "<collector>:<collector's message id>" when the collector has one
  batch_id    INTEGER REFERENCES batches(id),
  analyzed_at TEXT,
  urgent      INTEGER NOT NULL DEFAULT 0   -- matched the urgent pattern: analyze now instead of at the next scheduled run
);
CREATE INDEX IF NOT EXISTS messages_source_idx ON messages (source_id, id);
CREATE INDEX IF NOT EXISTS messages_pending_idx ON messages (analyzed_at, source_id);

CREATE TABLE IF NOT EXISTS batches (
  id             INTEGER PRIMARY KEY,
  source_id      INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  created_at     TEXT NOT NULL,
  first_msg_id   INTEGER,
  last_msg_id    INTEGER,
  model          TEXT,
  status         TEXT NOT NULL DEFAULT 'running',   -- running / ok / error
  error          TEXT,
  input_tokens   INTEGER,
  output_tokens  INTEGER,
  tasks_created  INTEGER DEFAULT 0,
  request_json   TEXT,
  response_json  TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id                 INTEGER PRIMARY KEY,
  source_id          INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  category_key       TEXT NOT NULL,
  title              TEXT NOT NULL,
  details            TEXT,
  due_date           TEXT,            -- YYYY-MM-DD (local)
  start_time         TEXT,            -- HH:MM (local)
  end_time           TEXT,
  location           TEXT,
  amount             REAL,
  currency           TEXT,
  payment_url        TEXT,
  confidence         REAL,
  status             TEXT NOT NULL DEFAULT 'open',  -- open / done / irrelevant / superseded / cancelled
  status_note        TEXT,
  origin             TEXT NOT NULL DEFAULT 'model', -- model / manual
  batch_id           INTEGER REFERENCES batches(id),
  supersedes_task_id INTEGER REFERENCES tasks(id),
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  calendar_sent_at   TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks (status, due_date);

CREATE TABLE IF NOT EXISTS task_messages (
  task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, message_id)
);

CREATE TABLE IF NOT EXISTS attachments (
  id          INTEGER PRIMARY KEY,
  message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,           -- image / document / voice / video / other
  mime        TEXT,
  filename    TEXT,
  path        TEXT NOT NULL,           -- relative to RADAR_FILES
  size        INTEGER,
  sha256      TEXT,
  transcript  TEXT,                    -- voice notes, when a transcription backend is configured
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attachments_msg_idx ON attachments (message_id);

-- Learned rules: the part of the extractor's instructions the agent maintains (with approval).
CREATE TABLE IF NOT EXISTS rules (
  id         INTEGER PRIMARY KEY,
  source_id  INTEGER REFERENCES sources(id) ON DELETE CASCADE,   -- NULL = applies to every source
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Every approved change produces a full snapshot of the instructions (rules + source contexts + categories).
CREATE TABLE IF NOT EXISTS instruction_versions (
  id          INTEGER PRIMARY KEY,
  created_at  TEXT NOT NULL,
  proposal_id INTEGER,
  note        TEXT,
  snapshot    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
  id              INTEGER PRIMARY KEY,
  created_at      TEXT NOT NULL,
  conversation_id INTEGER,
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected / superseded
  rationale       TEXT,
  changes         TEXT NOT NULL,                    -- JSON list of ops
  base_version_id INTEGER,
  replay          TEXT,                             -- JSON replay report
  decided_at      TEXT,
  decided_note    TEXT
);

CREATE TABLE IF NOT EXISTS conversations (
  id         INTEGER PRIMARY KEY,
  title      TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,      -- user / assistant (API content blocks, JSON)
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- Human feedback, used as few-shot examples in the prompt and exportable as an eval set.
CREATE TABLE IF NOT EXISTS feedback (
  id           INTEGER PRIMARY KEY,
  kind         TEXT NOT NULL,          -- irrelevant (false positive) / missed (false negative)
  task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  message_id   INTEGER REFERENCES messages(id) ON DELETE SET NULL,
  source_id    INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  category_key TEXT,
  note         TEXT,
  snapshot     TEXT NOT NULL,          -- JSON copy of the task/message text at feedback time
  created_at   TEXT NOT NULL
);
"""

DEFAULT_CATEGORIES = [
    ("action_required", "Requires action",
     "Something I personally must do or attend: an event/activity I (or my child) must participate in, "
     "something to bring or prepare, a form to sign, a reply the sender explicitly asked me for. "
     "Not: general announcements with nothing to do, other people's to-dos, social chatter.",
     "#2563eb", 10),
    ("meeting_changed", "Meeting time changed",
     "An existing meeting, class, appointment, session or pickup moved to a different date/time/place, "
     "or was cancelled. Include the old and new time when known.",
     "#d97706", 20),
    ("new_meeting", "New meeting",
     "A new appointment/meeting/session/event with a specific date (and usually time) that involves me or my child, "
     "e.g. a therapist offering the next session, a parents' meeting, a scheduled trip.",
     "#059669", 30),
    ("payment_request", "Payment request",
     "A request to pay: a bill or invoice (e.g. a therapist or tutor sending the monthly bill), a payment link, "
     "a money collection with an amount. Extract amount, currency, payee and link when present.",
     "#dc2626", 40),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != DB_PATH:
        conn = connect(DB_PATH)
        _local.conn, _local.path = conn, DB_PATH
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    if conn.execute("SELECT count(*) FROM categories").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO categories (key, name, description, color, sort) VALUES (?,?,?,?,?)", DEFAULT_CATEGORIES)
    conn.commit()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS doesn't add them to existing databases.
MIGRATIONS = [
    ("messages", "external_key", "TEXT"),
    ("messages", "urgent", "INTEGER NOT NULL DEFAULT 0"),
    ("sources", "is_direct", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(conn) -> None:
    for table, col, decl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_external_key_idx ON messages (external_key)")


def kv_get(key: str, default: str | None = None) -> str | None:
    r = get_conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def kv_set(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def rows(sql: str, params=()) -> list[dict]:
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def row(sql: str, params=()) -> dict | None:
    r = get_conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
