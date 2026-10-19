"""Anthropic Messages API client (plain httpx) + a keyword-based fake for demos/tests."""
import os
import re
import threading
import time

import httpx

from . import db, notify

API_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com") + "/v1/messages"


def api_key() -> str:
    """The env var wins; otherwise the key saved from the setup screen."""
    return os.environ.get("ANTHROPIC_API_KEY") or db.kv_get("anthropic_api_key") or ""


def model_name() -> str:
    return os.environ.get("RADAR_MODEL", "claude-sonnet-5-5")


def call_tool(system: str, user, tool: dict, max_tokens: int = 16000) -> tuple[dict, dict]:
    """Get exactly one call of `tool`. `user` is a string or a list of content blocks. Returns (tool_input, usage).

    Current models (Sonnet 5.5, Opus 5.5) reject a forced tool_choice, so this uses "auto" plus an explicit
    instruction, and retries once if the model answers in text instead.
    """
    if os.environ.get("RADAR_FAKE_LLM") == "1":
        text = user if isinstance(user, str) else "\n".join(b.get("text", "") for b in user if b.get("type") == "text")
        return fake_extract(text), {"input_tokens": 0, "output_tokens": 0}
    blocks = [{"type": "text", "text": user}] if isinstance(user, str) else list(user)
    blocks.append({"type": "text", "text": f"Answer only by calling the {tool['name']} tool exactly once "
                                           "(with empty lists if nothing qualifies). Do not reply in text."})
    body = {
        "model": model_name(), "max_tokens": max_tokens,
        # The system prompt (categories, rules, examples) is identical across a source's runs: cache it.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "tools": [tool], "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
        "messages": [{"role": "user", "content": blocks}],
    }
    data = {}
    for _ in range(2):
        data = post_messages(body)
        for block in data.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
                return block["input"], data.get("usage", {})
    raise RuntimeError(f"No tool_use in response (stop_reason={data.get('stop_reason')}): {str(data)[:500]}")


def report_usage(model: str, usage: dict, feature: str) -> None:
    """Optionally POST token usage to RADAR_USAGE_URL. Fire-and-forget: never blocks or raises."""
    url = os.environ.get("RADAR_USAGE_URL", "")
    if not url or not usage:
        return

    def send():
        try:
            httpx.post(url, json={"app": "radar", "feature": feature, "model": model, "usage": usage}, timeout=5)
        except Exception:
            pass

    threading.Thread(target=send, daemon=True).start()


PROBLEMS = {
    "credit": ("Chatter Radar paused: your Anthropic credit has run out",
               "Add credit at console.anthropic.com/settings/billing. Messages are kept and analyzed once you do."),
    "key": ("Chatter Radar paused: your Anthropic API key was rejected",
            "Create a new key at console.anthropic.com/settings/keys and paste it into Chatter Radar."),
}


def classify_error(status: int, text: str) -> str | None:
    """Which problem the user has to fix, if this error is one: 'credit' (balance empty) or 'key' (rejected)."""
    if status == 400 and "credit balance" in text.lower():
        return "credit"
    if status in (401, 403):
        return "key"
    return None


def note_problem(kind: str) -> None:
    """Remember it for the web page and notify once per problem (not on every retry)."""
    import json
    current = db.kv_get("llm_problem")
    if current and json.loads(current).get("kind") == kind:
        return
    title, message = PROBLEMS[kind]
    db.kv_set("llm_problem", json.dumps({"kind": kind, "message": message, "at": db.now_iso()}))
    notify.send(title, message)


def clear_problem() -> None:
    if db.kv_get("llm_problem"):
        db.kv_set("llm_problem", "")


def current_problem() -> dict | None:
    import json
    v = db.kv_get("llm_problem")
    return json.loads(v) if v else None


def post_messages(body: dict, timeout: float = 180) -> dict:
    """Raw Messages API call with retries on overload/rate limits."""
    key = api_key()
    if not key:
        raise RuntimeError("No Anthropic API key: add one in Chatter Radar's setup (or set ANTHROPIC_API_KEY)")
    last = None
    for attempt in range(4):
        r = httpx.post(API_URL, json=body, timeout=timeout,
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
        if r.status_code in (429, 500, 502, 503, 529):
            last = r
            time.sleep(min(30, 2 ** attempt * 3))
            continue
        if r.status_code >= 400:
            kind = classify_error(r.status_code, r.text)
            if kind:
                note_problem(kind)
            raise RuntimeError(f"Anthropic API {r.status_code}: {r.text[:500]}")
        data = r.json()
        clear_problem()
        # The extractor (call_tool) always sets tool_choice; the Assistant chat never does.
        report_usage(data.get("model") or body["model"], data.get("usage") or {},
                     "extract" if "tool_choice" in body else "chat")
        return data
    raise RuntimeError(f"Anthropic API {last.status_code} after retries: {last.text[:300]}")


# ---------------------------------------------------------------- fake (RADAR_FAKE_LLM=1)
_NEW = re.compile(r"^#(\d+) \[[^\]]*\] ([^:]*): (.*)$")


def _fake_title(text: str) -> str:
    """First sentence without links, cut at a word boundary."""
    t = re.sub(r"https?://\S+", "", text).strip(" :")
    t = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0].rstrip(".")
    if len(t) > 70:
        t = t[:70].rsplit(" ", 1)[0] + "…"
    return t


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _fake_due(text: str):
    """Date for 'today' / 'tomorrow' / a weekday name, else None (the real model reads dates properly)."""
    from datetime import date, timedelta
    low, today = text.lower(), date.today()
    if re.search(r"\b(today)\b|היום", low):
        return today.isoformat()
    if re.search(r"\b(tomorrow)\b|מחר", low):
        return (today + timedelta(days=1)).isoformat()
    for i, name in enumerate(_WEEKDAYS):
        if name in low:
            return (today + timedelta(days=(i - today.weekday()) % 7 or 7)).isoformat()
    return None


def fake_extract(user_prompt: str) -> dict:
    """Very rough keyword rules — only so the UI can be tried without an API key."""
    tasks = []
    new_part = user_prompt.split("## NEW messages", 1)[-1]
    for line in new_part.splitlines():
        m = _NEW.match(line.strip())
        if not m:
            continue
        mid, text = int(m.group(1)), m.group(3)
        cat = None
        if re.search(r"(₪|ש\"ח|שח|לשלם|תשלום|חשבונית|invoice|pay)", text, re.I):
            cat = "payment_request"
        elif re.search(r"(במקום|נדחה|הוזז|שינוי|בוטל|moved|reschedul|cancel)", text, re.I):
            cat = "meeting_changed"
        elif re.search(r"(להביא|תביאו|לחתום|bring|sign)", text, re.I):
            cat = "action_required"
        elif re.search(r"(פגישה|אסיפ|מפגש|טיפול הבא|appointment|meeting|session)", text, re.I):
            cat = "new_meeting"
        if cat:
            amt = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(₪|ש\"ח|שח|nis|ils)", text, re.I)
            usd = re.search(r"([$€£])\s?(\d[\d,]*(?:\.\d+)?)", text)
            t = re.search(r"\b(\d{1,2}:\d{2})\b", text)
            url = re.search(r"https?://\S+", text)
            tasks.append({
                "category": cat, "title": _fake_title(text), "details": text, "due_date": _fake_due(text),
                "start_time": t.group(1) if t else None,
                "amount": (float(amt.group(1).replace(",", "")) if amt else
                           float(usd.group(2).replace(",", "")) if usd else None),
                "currency": "ILS" if amt else {"$": "USD", "€": "EUR", "£": "GBP"}[usd.group(1)] if usd else None,
                "payment_url": url.group(0) if url and cat == "payment_request" else None,
                "evidence_message_ids": [mid], "confidence": 0.5,
            })
    return {"tasks": tasks, "cancelled": []}
