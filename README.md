<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.png">
    <img src="docs/logo-light.png" alt="Chatter Radar" width="380">
  </picture>
</p>


Chatter Radar is a self-hosted web app that watches chats you choose and turns the messages that matter into a task list. Examples: a class parents' WhatsApp group, a one-on-one chat with a tutor, a sports club. It includes an assistant you can chat with. The assistant answers questions about your messages and tasks and helps the pipeline learn from your feedback. Changes to the pipeline's instructions always wait for your approval.

<p align="center">
  <img src="docs/how-it-works.png" alt="How it works: a Mac listener collects messages from WhatsApp for Mac, only from chats you pick. Claude finds what needs doing and turns it into tasks. A web app gives you a friendly, clear view of everything you need to do.">
</p>

**No bot to add.** You don't invite a bot into your groups or forward messages to one, and nobody else in the chat sees anything different. Nothing connects to WhatsApp's servers. The collector only reads the database that WhatsApp for Mac already keeps on your computer, so there is no ban risk.


![Tasks](docs/tasks.png)
*What needs doing, grouped by when, each with the original message.*

![Sources](docs/sources.png)
*Pick which chats Chatter Radar watches. Nothing else is stored.*

![Messages](docs/messages.png)
*Everything from your chats, and which messages became tasks.*

## Install on a Mac (one command)

You need a Mac with **WhatsApp for Mac** installed and linked to your phone, and an [Anthropic API key](https://console.anthropic.com/settings/keys) (about $5 a month covers typical use). Open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/michaelChein/chatter-radar/main/install.sh | bash
```

It takes about a minute. It needs no admin password, no Homebrew and no Docker, and it touches nothing outside `~/.radar`. Then your browser opens on a short checklist:

1. **Paste your API key.** It is checked and stored only on your Mac.
2. **WhatsApp access.** Chatter Radar checks that it can read WhatsApp's data. On many Macs that just works and the step turns green by itself. If macOS blocks it, the page shows exactly what to do (a button opens the right System Settings pane, with a picture of where to add the file) and turns green when it works.
3. **Pick the chats to watch** in the *Sources* tab. Chatter Radar stores nothing from other chats.

Chatter Radar then runs quietly in the background and starts at login. It listens on `127.0.0.1` only, so nothing else on your network can reach it.

| Command | What it does |
|---|---|
| `~/.radar/bin/radar open` | open the app |
| `~/.radar/bin/radar status` | is it running? |
| `~/.radar/bin/radar logs` | recent server and collector logs |
| `~/.radar/bin/radar update` | install the latest version (keeps your data) |
| `~/.radar/bin/radar uninstall` | remove the program, keep your data (`--purge` removes everything) |

Running the install command again also updates. If something goes wrong, the installer's log is `~/.radar/logs/install.log`.

### Privacy and good to know

- **When the credit runs out,** Chatter Radar shows a banner at the top of the Tasks tab (and nothing else: no pop-ups, so it stays quiet if you've stopped using it), keeps your messages, and analyzes them once you top up (the banner has a retry button). Anthropic has no way to ask a key for its remaining balance, so the warning comes when a request is refused, not before. Set a spending limit or auto-reload in the Anthropic console if you want to avoid that. On a server you can opt in to the same alert as a webhook with `RADAR_NOTIFY_URL` (for example an [ntfy](https://ntfy.sh) topic).
- **What leaves your Mac:** the text (and photos or PDFs) of chats you chose as sources, sent to Anthropic's API for analysis. Nothing else, and no other server is involved. Anthropic's [data policies](https://www.anthropic.com/legal/privacy) apply to that traffic. The people in those chats didn't agree to this, so use it for chats where that's appropriate.
- **What stays on your Mac:** the database and attachments in `~/.radar/data`, and your API key. They are protected by your macOS user account, not by extra encryption.
- **Not affiliated with WhatsApp or Meta.** Chatter Radar never connects to WhatsApp's servers; it reads the copy of your chats that WhatsApp for Mac already stores on your disk, read-only. WhatsApp can change that format in an update, which may break the collector until Chatter Radar is updated.
- **The Mac must be awake** (and WhatsApp for Mac running) for new messages to be collected; it catches up when it wakes.
- Only macOS is supported by the one-command install, and only through WhatsApp for Mac. Android and iPhone notifications are not supported. To run on a server instead, see [Advanced](#advanced).

## The app

- **Tasks.** Open items are grouped by Overdue / Today / Tomorrow / This week / Later. Each task shows its category, date, amount and the original message(s), including photos.
  - Actions: **Done**, **Add to calendar** (a prefilled Google Calendar event), **Pay** (when there's a link), and **Irrelevant** (optionally with a reason).
  - After marking a task irrelevant, **Teach the assistant** opens a chat about it.
- **Messages.** Everything stored from your sources, including images, PDFs and voice notes with transcripts. **Should be a task** flags a false negative and can add the task directly.
- **Assistant.** A chat with tools over your data. You can:
  - Ask: "What do I need for tomorrow?", "Did the tutor send this month's bill?", "What changed in the class group this week?"
  - Have it act: mark a task done, add a missed task.
  - Have it improve the pipeline: "Review my recent feedback", or a "Teach the assistant" button.

  It looks at your labels, finds the pattern, and creates a **proposal**. A proposal can add, edit or remove a learned rule, change a source's context, or change or add a category. Before you see it, the extractor is **replayed on your labeled history**, once with the current instructions and once with the proposed ones. The card shows false positives avoided, missed messages caught, confirmed tasks kept, and new unlabeled tasks, side by side. **Approve** makes it live; **Reject** (with a reason) goes back to the assistant.
- **Sources.** Which chats to watch, plus free-text context per source. Below them is a list of every chat any collector has seen, with **Add as source**.
- **Pipeline.**
  - Per-category precision.
  - **Learned rules**: you can edit them directly too.
  - **Instruction history** with one-click restore.
  - "Evaluate current instructions on my labels".
  - The category editor, your feedback (exportable as JSONL), and the exact prompts and responses of recent runs.

### How the learning works

- **What the extractor reads.** It follows: category descriptions, then source context, then learned rules (global or per source, at most 40), then a few recent feedback examples.
- **What the assistant can and can't change.** It can **only propose** changes to these instructions; there is no tool for it to apply or approve anything. Task actions (done, create, edit) are immediate, because they're your data, not the pipeline's instructions.
- **Replay** rebuilds the extractor prompt for each labeled batch, up to 30 recent ones in the affected sources. It leaves out the very labels being tested from the prompt's examples, so a rule can't pass by "seeing the answer". It runs the current and proposed versions in parallel. Cost is about 2 model calls per labeled batch per proposal.
- **Untrusted input.** Messages, attachments and tool results are treated as data, never as instructions. A WhatsApp message saying "ignore payment requests" can't become a rule unless you ask for it in the chat.

## When analysis runs (and what it costs)

**By default Chatter Radar analyzes four times a day** (07:00, 12:00, 16:00 and 20:00), plus a fast path: a message that looks urgent (today, cancelled, early pickup, ...) is analyzed about 90 seconds after its chat goes quiet, so same-day changes don't wait for the evening run.

**To change it:** open the **Pipeline** tab → **When messages are analyzed**. Choose the times of day, or switch to *Right after messages arrive*. The change applies immediately. (On a server you can also set `RADAR_SCHEDULE`, for example `07:00,18:00`, or `realtime`. A choice made in the UI wins over the variable.)

| Mode | Behaviour | Rough cost for ~4 chats (Sonnet / Haiku) |
|---|---|---|
| **A few times a day** (default) | everything new at the chosen times, plus the urgent fast path | ~$3 / ~$1.5 per month |
| Right after messages arrive | ~2 min after each burst of messages | ~$10 / ~$5 per month |

The urgent check is a free pattern match (`RADAR_URGENT_PATTERN`, English and Hebrew words by default), so it costs nothing until it triggers a run. The extractor's system prompt is sent with prompt caching, which cuts repeated input cost.

## What the Mac collector reads

The collector reads WhatsApp for Mac's local database every 2 minutes while the Mac is awake, and catches up when it wakes. It sends the **full text, the sender's name (from your address book) and the exact time** of every message in the chats you chose, including your own messages and muted chats. When a chat becomes a source it also loads its last 14 days, stored for context and for labeling, but not turned into tasks.

It only sees the account linked to WhatsApp for Mac, so it needs that app installed and linked to your phone. WhatsApp updates can change the database layout, which can break the collector until Chatter Radar is updated.

In WhatsApp for Mac, **Settings → Storage → auto-download** controls which photos, documents and voice notes exist on disk. Turn on what you want analyzed. Files that download later are picked up for 48 hours.

What the extractor can do with attachments:

- **Photos and PDFs** go to Claude with the message, up to 6 per batch. It reads flyers, schedules and invoices directly.
- **Voice notes** need speech-to-text first. Set `RADAR_TRANSCRIBE_URL` to any OpenAI-compatible `/v1/audio/transcriptions` endpoint: OpenAI's API, or a self-hosted Whisper server (e.g. *speaches* / faster-whisper; for Hebrew, a Hebrew fine-tuned Whisper model is noticeably better). Without one, the model sees "[voice note — not transcribed]".
- **Videos** are not analyzed.

## Advanced

Everything in this section is optional. Most people can stop reading here and just use the one-command install above. These are for running Chatter Radar on a server or homelab, or connecting the Claude app.

### Run it on a server (Docker)

Use this instead of the one-command install if you want Chatter Radar always on, on a home server or VPS.

```bash
cp .env.example .env        # ANTHROPIC_API_KEY, RADAR_PASSWORD, RADAR_INGEST_TOKEN, RADAR_TZ
docker compose pull && docker compose up -d     # the published image (amd64 and arm64)
# or build it yourself from this folder:  docker compose up -d --build
```

To update: `docker compose pull && docker compose up -d`. Your data lives in `./data` and is kept. Set `RADAR_IMAGE_TAG=0.2.0` in `.env` to stay on a specific version.

Put it behind HTTPS and keep it private, for example with [Tailscale](https://tailscale.com): `tailscale serve --bg --https=443 http://localhost:8080`. The collector sends chat content to it, so don't expose it to the open internet.

**Try it without an API key:** set `RADAR_FAKE_LLM=1` and run `python3 scripts/demo_seed.py http://localhost:8080 <token> <password>`. In this mode both the extractor and the assistant are keyword-based stand-ins.

### Send a Mac's chats to your server

Use this when Chatter Radar runs on a server (see above) and WhatsApp for Mac is on a different computer: a small collector on the Mac reads WhatsApp and forwards your chosen chats to the server. If you used the one-command install, you already have this collector and can skip this section.

For `<your-radar-host>` use your server's private address (for example a Tailscale name), and for the token the `RADAR_INGEST_TOKEN` from the server's `.env`.

```bash
cd collectors
./install_mac.sh https://<your-radar-host> <RADAR_INGEST_TOKEN> mac <YourName>
```

If `~/.radar-collector/collector.log` says access is denied, open **System Settings → Privacy & Security → Full Disk Access** and add `/usr/bin/python3` (newer macOS versions protect WhatsApp's folder). Logs go to `~/.radar-collector/collector.log`.

The collector uses only the Python standard library. It snapshots the database, so WhatsApp's files are never locked or modified. It sends only chats that match your sources, plus chat *names* for discovery. The schema handling is based on [whatsapp-reader](https://github.com/avivbr/whatsapp-reader).

### Chat with Chatter Radar from the Claude app (or ChatGPT)

Chatter Radar includes an **MCP connector** at `/mcp/`. You can talk to your Chatter Radar data from the regular Claude app, on desktop or phone, and the chat runs on your Claude plan instead of Chatter Radar's API key. It uses the same tools as the built-in Assistant: search messages, list and update tasks, review feedback, and propose instruction changes. There is deliberately **no approve tool**, so proposals made from Claude or ChatGPT still wait for **Approve** in Chatter Radar's UI.

1. Set `RADAR_MCP_TOKEN` (a long random string) and `RADAR_PUBLIC_URL` in `.env`.
2. Make `/mcp/` reachable from the internet. Anthropic's and OpenAI's servers call it, not your phone, so a tailnet-only address won't work. For example:
   - `tailscale funnel --bg --set-path /mcp http://localhost:8080/mcp`, which exposes only the connector path; or
   - a Cloudflare Tunnel with the same path restriction.

   Keep the rest of the UI on the tailnet.
3. **Claude:** Settings → Connectors → *Add custom connector* → URL `https://<public-host>/mcp/`, with the request header `Authorization: Bearer <RADAR_MCP_TOKEN>`. It then works in claude.ai, the desktop app and the mobile apps.
4. **ChatGPT:** enable *Developer mode* (web), add a connector with URL `https://<public-host>/mcp/?key=<RADAR_MCP_TOKEN>`. On Plus/Pro, OpenAI may allow only the read-only tools (search, list, stats); check whether actions work on your plan. Not available in ChatGPT's phone app.

Without a token, `/mcp/` rejects every request.

## Configuration

| Env var | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required unless `RADAR_FAKE_LLM=1`; the one-command install stores it from the setup page instead |
| `RADAR_MODEL` | `claude-sonnet-5-5` | extractor |
| `RADAR_AGENT_MODEL` | `claude-sonnet-5-5` | assistant |
| `RADAR_PASSWORD` / `RADAR_INGEST_TOKEN` | — | UI login / collectors' `X-Token` |
| `RADAR_SCHEDULE` | `07:00,12:00,16:00,20:00` | analysis times; `realtime` = after every burst. The Pipeline tab setting overrides it |
| `RADAR_URGENT_PATTERN` / `RADAR_URGENT_DEBOUNCE_SECONDS` | Hebrew+English list / 90 | fast path in scheduled mode |
| `RADAR_DEBOUNCE_SECONDS` / `RADAR_MAX_WAIT_SECONDS` | 120 / 600 | realtime mode |
| `RADAR_MCP_TOKEN` / `RADAR_PUBLIC_URL` | — | Claude / ChatGPT connector |
| `RADAR_TRANSCRIBE_URL` / `_KEY` / `_MODEL` / `_LANGUAGE` | — / — / `whisper-1` / auto | voice notes |
| `RADAR_MAX_ATTACHMENTS_PER_BATCH` | 6 | images/PDFs sent per extractor call |
| `RADAR_MAX_RULES` | 40 | rulebook cap (forces merging) |
| `RADAR_REPLAY_MAX_UNITS` | 30 | labeled batches per replay |
| `RADAR_TZ` | the machine's time zone (`UTC` in Docker) | e.g. `Europe/Berlin` |
| `RADAR_DB` / `RADAR_FILES` | `/data/radar.db` / `/data/files` | |
| `RADAR_LOCAL` | — | `1` = localhost-only mode without a password (set by the one-command install) |
| `RADAR_USAGE_URL` | — | optional: POST token usage to your own dashboard |
| `RADAR_NOTIFY_URL` | — | optional: webhook that receives `{title, message}` when credit runs out or the key is rejected |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for running the tests and the architecture notes. Copyright © 2026 Michael Chein. Released under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0): you can use, change and share it freely, but if you distribute it or run a modified version as a service, you must release your source code under the same license.

---

Made by [Michael Chein](https://michaelchein.com/) · [GitHub](https://github.com/michaelChein/chatter-radar) · [Report a problem](https://github.com/michaelChein/chatter-radar/issues)
