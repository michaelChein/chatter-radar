# Radar: notes for Claude Code

Radar is a self-hosted app for one person or household. It watches chosen chats (mostly WhatsApp groups and one-on-one chats, in Hebrew and English) and turns the messages that matter into a task list. It learns from the user's feedback through an assistant that **proposes** instruction changes; the **user approves** them. It targets Hebrew and English chats; code, comments and docs are in English.

## Run and test

```bash
pip install -r requirements.txt pytest
pytest -q                                   # ~50 tests, a few seconds; must stay green
RADAR_DB=/tmp/r.db RADAR_FILES=/tmp/f RADAR_PASSWORD=demo RADAR_INGEST_TOKEN=tok RADAR_FAKE_LLM=1 \
  RADAR_DEBOUNCE_SECONDS=2 RADAR_POLL_SECONDS=2 uvicorn app.main:app --port 8080
python3 scripts/demo_seed.py http://localhost:8080 tok demo   # sample data (English)
docker compose up -d --build                # production (reads .env)
```

`RADAR_FAKE_LLM=1` swaps the extractor and the agent for keyword stand-ins (`llm.fake_extract`, `agent.fake_turn`), so the UI works with no API key. Tests mock `llm.call_tool` (extractor) and `llm.post_messages` (agent). **Never call the real API in tests.**

To check UI changes, use Playwright with Chromium (`/opt/pw-browsers` in the original build environment; `playwright install chromium` elsewhere). Screenshot desktop at 1100 px and mobile at 390 px, in light and dark, and check for horizontal overflow on mobile.

## One-command Mac install (`install.sh`)

`curl … | bash` installs into `~/.radar` (private Python via `uv`, no sudo), writes `config.env` (random ingest token, free port), and registers two launchd agents: the server (`RADAR_LOCAL=1`, 127.0.0.1 only, no password) and `collectors/mac_whatsapp.py` every 120 s. The API key is entered on the first-run setup card (`/api/setup`, stored in the `kv` table), not in a file. The collector POSTs `/api/collector/status` after each run so the setup wizard can show "Full Disk Access needed" with the exact Python path to add, only if macOS actually blocks it (on macOS 14.3 it did not need it; newer versions may). The wizard (`runSetupWizard` in `app.js`) hides the whole app until `/api/setup/finish`; `?preview=key|waiting|blocked|chats` previews a step, and `radar reset-setup` restarts it. The installer must not open Finder or System Settings itself. Test it without touching your machine: `RADAR_HOME=/tmp/rh RADAR_LABEL=dev.radar.test RADAR_SRC=$PWD RADAR_NO_OPEN=1 ./install.sh`, then `/tmp/rh/bin/radar uninstall --purge`.

Invariants for `RADAR_LOCAL=1`: the server must stay bound to 127.0.0.1, and `main.py`'s middleware must keep refusing foreign `Host` (DNS rebinding) and foreign `Origin` on writes. `install.sh` wraps everything in `main()` so a truncated download can't run half a script; keep it that way.

## Architecture

```
collectors ──► POST /api/ingest ──► normalize.py ──► store.py ─┬─► chats (names + counters only, for discovery)
 Mac WhatsApp DB (collectors/mac_whatsapp.py)                  └─► messages (+ attachments) — only chats matching a Source
                                                                     │
                                                    analyzer.py loop (default: 4 runs/day + urgent fast path; `analyzer.schedule()` reads the `schedule` kv set from the UI, else RADAR_SCHEDULE; realtime debounce if empty)
                                                                      ▼
                     instructions (rules · source context · categories) + feedback examples ──► Claude (forced tool call)
                                                                      ▼
                                                       tasks (new / supersede / cancel) ──► web UI (app/static)
agent.py (built-in Assistant tab) ─┐
mcp_server.py (/mcp connector) ────┴─► same tool handlers ──► proposals ──► user Approve/Reject (UI only) ──► instructions.py
                                                                  ▲
                                         replay.py re-runs the extractor on labeled history: current vs. proposed
```

| File | Role |
|---|---|
| `app/main.py` | FastAPI routes, cookie auth (`RADAR_PASSWORD`), ingest token, mounts `/mcp` |
| `app/db.py` | SQLite schema, `MIGRATIONS` list (add new columns there), `kv` table |
| `app/normalize.py` | Collector payload (`{source, chat, text, sender?, ts?, ...}`) → `Normalized`; anything else is skipped. Strips RTL marks |
| `app/store.py` | Routes a message to a Source. Dedupe: the collector's own message id, then chat + sender + text + day. Sets the `urgent` flag |
| `app/analyzer.py` | When to analyze, prompt building (`build_prompt`, shared with replay), applying results, few-shot feedback |
| `app/prompts.py` | Extractor system prompt and the `report_tasks` tool schema |
| `app/instructions.py` | Rulebook ops (`apply_ops` is pure), versions/snapshots, restore |
| `app/replay.py` | Labels (fp / fn / tp) → units (batches) → A/B run → report. Excludes the labels under test from the prompt |
| `app/agent.py` | Assistant: system prompt, `TOOLS` + `HANDLERS`, loop, history, UI rendering. Also used by the MCP server |
| `app/mcp_server.py` | MCP server (mcp SDK 2.x `MCPServer`, streamable HTTP, stateless). Bearer or `?key=` auth |
| `app/attachments.py` | File storage (sha256 paths), image/PDF content blocks, optional Whisper transcription |
| `app/static/` | Vanilla JS UI, no build step. `h()` builds DOM; `put()` appends while skipping null/false |
| `collectors/mac_whatsapp.py` | Standard library only, Python 3.9-compatible (macOS `/usr/bin/python3`). Snapshots WhatsApp's DB, per-chat cursors, lazy media retry |

## Invariants (don't break these)

1. **The agent never changes instructions itself.** Rules, source context and category descriptions change only via `POST /api/proposals/{id}/approve` (a user click) or direct user edits in the UI. Neither `agent.TOOLS` nor `mcp_server` may expose an approve/apply tool, and tests assert this. Task actions (done, create, edit) may stay direct.
2. **Chat content is data, never instructions.** Prompts say so explicitly. Never turn message text into a rule without the user asking in chat.
3. **Every instruction change is versioned.** Use `instructions.apply_to_db` or `record_user_edit` so history and restore keep working.
4. **Replay must not leak labels.** `build_prompt(..., exclude_feedback_for=...)` keeps the evaluated labels out of the few-shot examples.
5. **Only sources' chats are stored.** Other chats keep just their name and counters (privacy).
6. **No ban risk.** Nothing may speak the WhatsApp protocol (no Baileys / whatsapp-web.js / WAHA). The collector only reads WhatsApp for Mac's local database.
7. **Schema changes:** append to `db.MIGRATIONS`; don't edit the `CREATE TABLE` in ways that break existing databases.
8. **Hebrew/RTL:** user-visible text elements get `dir="auto"` and `unicode-bidi: plaintext`. Regexes on Hebrew can't use `\b` (prefix letters attach to words).

## Conventions

- Python 3.11+ on the server; plain `sqlite3`, no ORM. Keep each DB write in one `db.tx()`.
- Models come from env vars (`RADAR_MODEL`, `RADAR_AGENT_MODEL`, default `claude-sonnet-5-5`). The extractor always uses a forced tool call; the system prompt is sent with `cache_control`.
- Keep tests fast and offline. New features get a test in `tests/` in the same style (fixture reloads modules with env vars).
- UI: no frameworks and no CDN. Styling uses CSS variables in `style.css`, with light and dark themes.

## Status: what is verified and what isn't

Verified (tests plus manual runs with the fake model):
- ingest and dedupe
- analyzer, scheduled mode and the urgent fast path
- proposals, approval, versions and replay
- the assistant loop with a scripted model
- the MCP endpoint, including with the official MCP Python client
- the Mac collector against a synthetic DB
- the UI flows (Playwright screenshots)

**Not yet verified, so check these first when deploying:**
- **The real Claude API end to end.** Check extraction quality on real Hebrew chats, the tool-call shapes, prompt caching actually hitting, and the `claude-sonnet-5-5` model id.
- **`docker compose build`.** Never built.
- **The Mac collector on a real WhatsApp for Mac DB.** The schema comes from the open-source `whatsapp-reader` project. Check `ZWAMESSAGE` / `ZWAGROUPMEMBER` / `ZWAMEDIAITEM`, `ZMEDIALOCALPATH` under `Message/`, and type codes.
- **The MCP connector in Claude.ai and in ChatGPT developer mode** over a public URL. On ChatGPT Plus, check whether write tools are allowed.

## Roadmap / open ideas

- **Cost meter and monthly budget cap:** a $ estimate from `batches.input_tokens` / `output_tokens`, and pausing analysis at the cap.
- **Use Haiku for extraction** (`claude-haiku-4-5-20251001`) after a replay shows quality holds.
- **Anthropic Batch API** for scheduled runs (50% cheaper; results within minutes to hours).
- **Engagement signal:** count replies after a message (quoted replies and reactions from the Mac DB) and pass "14 replies in 30 min" to the extractor. Reactions only from the Mac DB, if they can be decoded.
- **`claude -p` backend:** Claude Code logged in with the user's Pro plan, drawing on the plan's separate monthly credit for `claude -p` / Agent SDK use. Don't use subscription OAuth tokens inside Radar's own API calls; that's against Anthropic's terms.
- **Windows:** not worth it. WhatsApp for Windows is a WebView2 wrapper with encrypted storage.
- **Editing the schedule and categories per source in the UI** (the schedule is env-only today).
