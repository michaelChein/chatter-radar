"""The extractor's editable instructions: learned rules, per-source context, category descriptions.

Only the user changes these directly (UI). The agent can only *propose* a list of ops; the user approves.
Every applied change is snapshotted in instruction_versions so it can be restored.
"""
import copy
import json

from . import db

MAX_RULES = int(__import__("os").environ.get("RADAR_MAX_RULES", "40"))
MAX_RULE_CHARS = 400

OPS = {
    "add_rule": "Add a learned rule. Fields: source_id (int, or null for all sources), text.",
    "edit_rule": "Rewrite an existing rule. Fields: rule_id, text.",
    "remove_rule": "Delete a rule (e.g. merged into another). Fields: rule_id.",
    "set_source_context": "Replace a source's context text. Fields: source_id, text.",
    "set_category_description": "Replace a category's description. Fields: category_key, description.",
    "add_category": "Add a new category. Fields: key, name, description, color (optional hex).",
}


class OpError(ValueError):
    pass


def current() -> dict:
    """Live instructions as a plain dict (what the extractor uses right now)."""
    return {
        "rules": db.rows("SELECT id, source_id, text FROM rules ORDER BY coalesce(source_id, 0), id"),
        "sources": {str(s["id"]): {"name": s["name"], "context": s["context"], "is_direct": s["is_direct"]}
                    for s in db.rows("SELECT id, name, context, is_direct FROM sources")},
        "categories": db.rows("SELECT key, name, description, color, enabled, sort FROM categories ORDER BY sort, id"),
    }


def apply_ops(instr: dict, ops: list[dict]) -> dict:
    """Pure function: return a new instructions dict with ops applied. Raises OpError on invalid ops."""
    out = copy.deepcopy(instr)
    next_tmp = -1
    if not isinstance(ops, list) or not ops:
        raise OpError("changes must be a non-empty list")
    for i, op in enumerate(ops):
        kind = op.get("op")
        where = f"change #{i + 1} ({kind})"
        if kind == "add_rule":
            text = (op.get("text") or "").strip()
            sid = op.get("source_id")
            if not text or len(text) > MAX_RULE_CHARS:
                raise OpError(f"{where}: text is required and must be under {MAX_RULE_CHARS} chars")
            if sid is not None and str(sid) not in out["sources"]:
                raise OpError(f"{where}: unknown source_id {sid}")
            out["rules"].append({"id": next_tmp, "source_id": sid, "text": text})
            next_tmp -= 1
        elif kind in ("edit_rule", "remove_rule"):
            rid = op.get("rule_id")
            idx = next((j for j, r in enumerate(out["rules"]) if r["id"] == rid), None)
            if idx is None:
                raise OpError(f"{where}: unknown rule_id {rid}")
            if kind == "remove_rule":
                out["rules"].pop(idx)
            else:
                text = (op.get("text") or "").strip()
                if not text or len(text) > MAX_RULE_CHARS:
                    raise OpError(f"{where}: text is required and must be under {MAX_RULE_CHARS} chars")
                out["rules"][idx]["text"] = text
        elif kind == "set_source_context":
            sid = str(op.get("source_id"))
            if sid not in out["sources"]:
                raise OpError(f"{where}: unknown source_id {op.get('source_id')}")
            out["sources"][sid]["context"] = (op.get("text") or "").strip()
        elif kind == "set_category_description":
            cat = next((c for c in out["categories"] if c["key"] == op.get("category_key")), None)
            if not cat:
                raise OpError(f"{where}: unknown category_key {op.get('category_key')}")
            desc = (op.get("description") or "").strip()
            if not desc:
                raise OpError(f"{where}: description is required")
            cat["description"] = desc
        elif kind == "add_category":
            key = (op.get("key") or "").strip().lower().replace(" ", "_")
            if not key or not (op.get("name") or "").strip() or not (op.get("description") or "").strip():
                raise OpError(f"{where}: key, name and description are required")
            if any(c["key"] == key for c in out["categories"]):
                raise OpError(f"{where}: category {key} already exists")
            out["categories"].append({"key": key, "name": op["name"].strip(), "description": op["description"].strip(),
                                      "color": op.get("color") or "#7c3aed", "enabled": 1, "sort": 100})
        else:
            raise OpError(f"{where}: unknown op; valid ops: {', '.join(OPS)}")
    if len(out["rules"]) > MAX_RULES:
        raise OpError(f"the rulebook would have {len(out['rules'])} rules (max {MAX_RULES}); merge or remove some")
    return out


def _snapshot(conn, note: str, proposal_id: int | None) -> int:
    snap = {
        "rules": [dict(r) for r in conn.execute("SELECT id, source_id, text FROM rules ORDER BY id").fetchall()],
        "sources": {str(r["id"]): r["context"] for r in conn.execute("SELECT id, context FROM sources").fetchall()},
        "categories": [dict(r) for r in conn.execute(
            "SELECT key, name, description, color, enabled, sort FROM categories ORDER BY sort, id").fetchall()],
    }
    return conn.execute("INSERT INTO instruction_versions (created_at, proposal_id, note, snapshot) VALUES (?,?,?,?)",
                        (db.now_iso(), proposal_id, note, json.dumps(snap, ensure_ascii=False))).lastrowid


def latest_version_id() -> int | None:
    r = db.row("SELECT max(id) AS id FROM instruction_versions")
    return r["id"] if r else None


def ensure_baseline() -> None:
    if latest_version_id() is None:
        with db.tx() as conn:
            _snapshot(conn, "initial", None)


def apply_to_db(ops: list[dict], proposal_id: int | None, note: str) -> int:
    """Validate against the live state, then apply ops for real. Returns the new version id."""
    apply_ops(current(), ops)  # validation only
    ensure_baseline()
    now = db.now_iso()
    with db.tx() as conn:
        for op in ops:
            k = op["op"]
            if k == "add_rule":
                conn.execute("INSERT INTO rules (source_id, text, created_at) VALUES (?,?,?)",
                             (op.get("source_id"), op["text"].strip(), now))
            elif k == "edit_rule":
                conn.execute("UPDATE rules SET text=? WHERE id=?", (op["text"].strip(), op["rule_id"]))
            elif k == "remove_rule":
                conn.execute("DELETE FROM rules WHERE id=?", (op["rule_id"],))
            elif k == "set_source_context":
                conn.execute("UPDATE sources SET context=? WHERE id=?", ((op.get("text") or "").strip(), op["source_id"]))
            elif k == "set_category_description":
                conn.execute("UPDATE categories SET description=? WHERE key=?", (op["description"].strip(), op["category_key"]))
            elif k == "add_category":
                conn.execute("INSERT INTO categories (key, name, description, color) VALUES (?,?,?,?)",
                             (op["key"].strip().lower().replace(" ", "_"), op["name"].strip(), op["description"].strip(),
                              op.get("color") or "#7c3aed"))
        return _snapshot(conn, note, proposal_id)


def restore_version(version_id: int) -> int:
    v = db.row("SELECT * FROM instruction_versions WHERE id=?", (version_id,))
    if not v:
        raise OpError("unknown version")
    snap = json.loads(v["snapshot"])
    with db.tx() as conn:
        conn.execute("DELETE FROM rules")
        live_sources = {str(r["id"]) for r in conn.execute("SELECT id FROM sources").fetchall()}
        for r in snap["rules"]:
            if r["source_id"] is None or str(r["source_id"]) in live_sources:
                conn.execute("INSERT INTO rules (id, source_id, text, created_at) VALUES (?,?,?,?)",
                             (r["id"], r["source_id"], r["text"], db.now_iso()))
        for sid, ctx in snap["sources"].items():
            conn.execute("UPDATE sources SET context=? WHERE id=?", (ctx, int(sid)))
        for c in snap["categories"]:
            conn.execute("""INSERT INTO categories (key, name, description, color, enabled, sort) VALUES (?,?,?,?,?,?)
                            ON CONFLICT(key) DO UPDATE SET name=excluded.name, description=excluded.description,
                            color=excluded.color, enabled=excluded.enabled, sort=excluded.sort""",
                         (c["key"], c["name"], c["description"], c["color"], c["enabled"], c["sort"]))
        return _snapshot(conn, f"restored version {version_id}", None)


def record_user_edit(note: str) -> None:
    """Called after the user edits instructions directly in the UI, so history stays complete."""
    ensure_baseline()
    with db.tx() as conn:
        _snapshot(conn, note, None)


def describe_ops(ops: list[dict], instr: dict) -> list[str]:
    """Human-readable lines for the approval card."""
    src_name = lambda sid: "all sources" if sid is None else instr["sources"].get(str(sid), {}).get("name", f"source {sid}")
    rule_text = {r["id"]: r["text"] for r in instr["rules"]}
    out = []
    for op in ops:
        k = op.get("op")
        if k == "add_rule":
            out.append(f"Add rule ({src_name(op.get('source_id'))}): {op.get('text')}")
        elif k == "edit_rule":
            out.append(f"Edit rule #{op.get('rule_id')}: “{rule_text.get(op.get('rule_id'), '?')}” → “{op.get('text')}”")
        elif k == "remove_rule":
            out.append(f"Remove rule #{op.get('rule_id')}: “{rule_text.get(op.get('rule_id'), '?')}”")
        elif k == "set_source_context":
            out.append(f"Set context of {src_name(op.get('source_id'))} to: {op.get('text')}")
        elif k == "set_category_description":
            out.append(f"Set description of category {op.get('category_key')} to: {op.get('description')}")
        elif k == "add_category":
            out.append(f"Add category {op.get('name')} ({op.get('key')}): {op.get('description')}")
        else:
            out.append(json.dumps(op, ensure_ascii=False))
    return out
