"""Replay: re-run the extractor on messages you've labeled, with current vs. proposed instructions.

Labels come from your actions:
  false positive  — a task you marked Irrelevant           → good if no task is produced from those messages
  false negative  — a message you flagged "Should be a task" → good if a task is produced from it
  true positive   — a model task you marked Done / sent to calendar → good if it's still produced
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import analyzer, db, instructions, llm, prompts

log = logging.getLogger("radar.replay")
MAX_UNITS = int(os.environ.get("RADAR_REPLAY_MAX_UNITS", "30"))
WORKERS = int(os.environ.get("RADAR_REPLAY_WORKERS", "4"))


def affected_sources(ops: list[dict]) -> set[int] | None:
    """None = every source."""
    out = set()
    for op in ops:
        if op["op"] in ("add_rule",) and op.get("source_id") is not None:
            out.add(int(op["source_id"]))
        elif op["op"] == "set_source_context":
            out.add(int(op["source_id"]))
        elif op["op"] in ("edit_rule", "remove_rule"):
            r = db.row("SELECT source_id FROM rules WHERE id=?", (op.get("rule_id"),))
            if not r or r["source_id"] is None:
                return None
            out.add(r["source_id"])
        else:
            return None
    return out


def collect_labels(source_ids: set[int] | None = None) -> list[dict]:
    labels = []
    for f in db.rows("SELECT * FROM feedback ORDER BY id DESC"):
        if f["kind"] == "irrelevant" and f["task_id"]:
            mids = [r["message_id"] for r in db.rows("SELECT message_id FROM task_messages WHERE task_id=?", (f["task_id"],))]
            if mids:
                snap = db.jload(f["snapshot"], {})
                labels.append({"kind": "fp", "message_ids": mids, "category": f["category_key"],
                               "title": snap.get("title"), "note": f["note"], "feedback_id": f["id"]})
        elif f["kind"] == "missed" and f["message_id"]:
            snap = db.jload(f["snapshot"], {})
            labels.append({"kind": "fn", "message_ids": [f["message_id"]], "category": f["category_key"],
                           "title": (snap.get("body") or "")[:80], "note": f["note"], "feedback_id": f["id"]})
    for t in db.rows("""SELECT * FROM tasks WHERE origin='model' AND (status='done' OR calendar_sent_at IS NOT NULL)
                        ORDER BY id DESC LIMIT 200"""):
        mids = [r["message_id"] for r in db.rows("SELECT message_id FROM task_messages WHERE task_id=?", (t["id"],))]
        if mids:
            labels.append({"kind": "tp", "message_ids": mids, "category": t["category_key"], "title": t["title"],
                           "task_id": t["id"]})
    # attach unit (the batch the messages were analyzed in, or the message itself if never analyzed)
    out = []
    for lab in labels:
        m = db.row(f"SELECT id, source_id, batch_id FROM messages WHERE id IN ({','.join('?' * len(lab['message_ids']))}) "
                   "ORDER BY id LIMIT 1", tuple(lab["message_ids"]))
        if not m or (source_ids is not None and m["source_id"] not in source_ids):
            continue
        lab["source_id"] = m["source_id"]
        lab["unit"] = f"b{m['batch_id']}" if m["batch_id"] else f"m{m['id']}"
        out.append(lab)
    return out


def _unit_messages(unit: str, source_id: int) -> list[dict]:
    if unit.startswith("b"):
        return db.rows("SELECT * FROM messages WHERE batch_id=? ORDER BY id", (int(unit[1:]),))
    return db.rows("SELECT * FROM messages WHERE id=?", (int(unit[1:]),))


def _run_unit(unit: str, source: dict, msgs: list[dict], instr: dict, exclude: set) -> dict:
    now = datetime.fromisoformat(msgs[-1]["ingested_at"])
    system, content = analyzer.build_prompt(source, msgs, now, instr=instr, open_tasks=[], exclude_feedback_for=exclude)
    result, usage = llm.call_tool(system, content, prompts.TOOL)
    new_ids = {m["id"] for m in msgs}
    cats = {c["key"] for c in instr["categories"] if c["enabled"]}
    tasks = []
    for t in result.get("tasks") or []:
        if t.get("category") not in cats:
            continue
        ev = [i for i in (t.get("evidence_message_ids") or []) if i in new_ids] or [msgs[-1]["id"]]
        tasks.append({"title": t.get("title"), "category": t.get("category"), "evidence": ev})
    return {"tasks": tasks, "usage": usage}


def _score(labels: list[dict], tasks: list[dict]) -> list[dict]:
    out = []
    for lab in labels:
        hits = [t for t in tasks if set(t["evidence"]) & set(lab["message_ids"])]
        same_cat = [t for t in hits if not lab.get("category") or t["category"] == lab["category"]]
        if lab["kind"] == "fp":
            ok = not hits
        elif lab["kind"] == "fn":
            ok = bool(same_cat) or (bool(hits) and not lab.get("category"))
        else:
            ok = bool(same_cat)
        out.append({"ok": ok, "produced": [f"[{t['category']}] {t['title']}" for t in hits]})
    return out


def run(candidate_ops: list[dict] | None, include_baseline: bool = True) -> dict:
    base = instructions.current()
    cand = instructions.apply_ops(base, candidate_ops) if candidate_ops else base
    scope = affected_sources(candidate_ops) if candidate_ops else None
    labels = collect_labels(scope)

    units: dict[str, list[dict]] = {}
    for lab in labels:
        units.setdefault(lab["unit"], []).append(lab)
    unit_keys = sorted(units, key=lambda u: -min(min(l["message_ids"]) for l in units[u]))[:MAX_UNITS]

    variants = [("candidate", cand)] + ([("baseline", base)] if include_baseline and candidate_ops else [])
    jobs = []
    for u in unit_keys:
        src = db.row("SELECT * FROM sources WHERE id=?", (units[u][0]["source_id"],))
        msgs = _unit_messages(u, src["id"]) if src else []
        if not src or not msgs:
            continue
        exclude = {mid for lab in units[u] for mid in lab["message_ids"]}
        for name, instr in variants:
            jobs.append((u, name, src, msgs, instr, exclude))

    results: dict[tuple, dict] = {}
    errors = []

    def work(job):
        u, name, src, msgs, instr, exclude = job
        try:
            return (u, name), _run_unit(u, src, msgs, instr, exclude)
        except Exception as e:  # noqa: BLE001
            log.warning("replay unit %s/%s failed: %s", u, name, e)
            return (u, name), {"error": str(e)}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for key, res in pool.map(work, jobs):
            results[key] = res
            if "error" in res:
                errors.append(f"{key[0]}: {res['error'][:200]}")

    report = {"units": len({j[0] for j in jobs}), "labels": 0, "variants": {}, "details": [], "errors": errors,
              "tokens": {"input": 0, "output": 0}, "scope": "all sources" if scope is None else sorted(scope)}
    for name, _ in variants:
        report["variants"][name] = {k: {"ok": 0, "total": 0} for k in ("fp", "fn", "tp")} | {"extra_tasks": 0}

    for u in unit_keys:
        for name, _ in variants:
            res = results.get((u, name))
            if not res or "error" in res:
                continue
            report["tokens"]["input"] += res["usage"].get("input_tokens") or 0
            report["tokens"]["output"] += res["usage"].get("output_tokens") or 0
            scores = _score(units[u], res["tasks"])
            v = report["variants"][name]
            for lab, sc in zip(units[u], scores):
                v[lab["kind"]]["total"] += 1
                v[lab["kind"]]["ok"] += int(sc["ok"])
            labeled = {mid for lab in units[u] for mid in lab["message_ids"]}
            extra = [t for t in res["tasks"] if not set(t["evidence"]) & labeled]
            v["extra_tasks"] += len(extra)
            if name == "candidate":
                base_scores = _score(units[u], results[(u, "baseline")]["tasks"]) \
                    if (u, "baseline") in results and "error" not in results[(u, "baseline")] else [None] * len(scores)
                for lab, sc, bsc in zip(units[u], scores, base_scores):
                    report["details"].append({
                        "kind": lab["kind"], "title": lab.get("title"), "category": lab.get("category"),
                        "message_ids": lab["message_ids"], "candidate_ok": sc["ok"], "candidate_produced": sc["produced"],
                        "baseline_ok": bsc["ok"] if bsc else None})
                for t in extra:
                    report["details"].append({"kind": "extra", "title": t["title"], "category": t["category"],
                                              "message_ids": t["evidence"]})
    report["labels"] = sum(v["total"] for k, v in report["variants"]["candidate"].items() if k != "extra_tasks")
    return report


def summarize(report: dict) -> str:
    """One paragraph for the agent and the approval card."""
    if not report["labels"]:
        return "No labeled examples in scope yet, so this change could not be tested against your history."
    names = {"fp": "false positives avoided", "fn": "missed messages caught", "tp": "confirmed tasks kept"}
    parts = []
    for k in ("fp", "fn", "tp"):
        c = report["variants"]["candidate"][k]
        if not c["total"]:
            continue
        b = report["variants"].get("baseline", {}).get(k)
        parts.append(f"{names[k]}: {c['ok']}/{c['total']}" + (f" (currently {b['ok']}/{b['total']})" if b else ""))
    ce = report["variants"]["candidate"]["extra_tasks"]
    be = report["variants"].get("baseline", {}).get("extra_tasks")
    parts.append(f"unlabeled extra tasks: {ce}" + (f" (currently {be})" if be is not None else ""))
    s = f"Replayed {report['units']} batches / {report['labels']} labels — " + "; ".join(parts) + "."
    if report["errors"]:
        s += f" {len(report['errors'])} replay calls failed."
    return s
