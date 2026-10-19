#!/usr/bin/env python3
"""Post a small, realistic set of sample messages to a running Radar instance (for trying the UI or screenshots).

    python3 scripts/demo_seed.py http://localhost:8080 <ingest-token> [<ui-password>]

Run Radar with RADAR_FAKE_LLM=1 to analyze them without an API key.
"""
import sys
import time

import httpx

base, token, password = (sys.argv + [None, None, None])[1:4]
base = base or "http://localhost:8080"
c = httpx.Client(base_url=base, timeout=30)
if password:
    c.post("/api/login", json={"password": password}).raise_for_status()

sources = [
    {"name": "Class 4B parents", "match_pattern": "Parents 4B",
     "context": "My child's class parents group. The teacher is Ms. Green and the class rep is Dana. Ignore thank-yous and birthday wishes."},
    {"name": "Maple Kindergarten", "match_pattern": "Maple Kindergarten",
     "context": "My child's kindergarten. The teacher is Rachel."},
    {"name": "Piano teacher", "match_pattern": "Mr. Adams", "is_direct": True,
     "context": "One-on-one chat with my child's piano teacher. Weekly lessons; he sends a monthly invoice."},
]
existing = {s["match_pattern"] for s in c.get("/api/sources").json()}
for s in sources:
    if s["match_pattern"] not in existing:
        c.post("/api/sources", json=s).raise_for_status()

H = {"X-Token": token or ""}
# Chats the collector has seen but that are not sources: only their names are kept.
c.post("/api/collector/chats", headers=H, json={"collector": "mac", "chats": [
    {"name": n, "channel": "whatsapp"} for n in ("Friends", "Work team", "Family", "Football club", "Book club")]})

notes = [  # (chat, sender, text)
    ("Parents 4B", "Ms. Green", "Hi all, tomorrow is the school opening ceremony. Please bring a white shirt for the kids, and arrive by 8:00."),
    ("Parents 4B", "Yael", "Thank you Ms. Green!! 🙏"),
    ("Parents 4B", "Dana", "Reminder: please transfer $50 for the class fund by Thursday: https://pay.example.com/class-fund"),
    ("Maple Kindergarten", "Rachel", "Dear parents, on Sunday pickup is moved to 12:00 instead of 14:00 because of staff training."),
    ("Maple Kindergarten", "Noah's mom", "Thanks Rachel, see you Sunday!"),
    ("Mr. Adams - Piano", "Mr. Adams", "Hi, the invoice for September is $120. You can pay here: https://pay.example.com/invoice-sep"),
    ("Mr. Adams - Piano", "Mr. Adams", "The next session is on Tuesday at 16:30, see you then."),
    ("Friends", "Sam", "Who is up for a beer on Thursday?"),
]
for chat, sender, text in notes:
    r = c.post("/api/ingest", headers=H, json={"source": "mac", "channel": "whatsapp", "chat": chat,
                                               "sender": sender, "text": text})
    r.raise_for_status()
    print(r.json()["results"][0], "←", chat)
    time.sleep(0.1)
print("Seeded. Analysis runs after the debounce; or press 'Analyze now' on the Sources tab.")
