# Contributing

Radar is a small FastAPI app with a vanilla-JS UI (no build step) and a standard-library-only Mac collector.
Read [CLAUDE.md](CLAUDE.md) first: it has the architecture, the invariants that must not break (the assistant can
only *propose* instruction changes, chat content is data, replay must not leak labels, ...) and the roadmap.

## Running it from source

```bash
pip install -r requirements.txt pytest
RADAR_DB=/tmp/r.db RADAR_FILES=/tmp/f RADAR_PASSWORD=demo RADAR_INGEST_TOKEN=tok RADAR_FAKE_LLM=1 \
  uvicorn app.main:app --port 8080
python3 scripts/demo_seed.py http://localhost:8080 tok demo   # sample data, no API key needed
```

`RADAR_FAKE_LLM=1` swaps Claude for keyword stand-ins so the whole UI works offline.

## Running the tests

```bash
pip install -r requirements.txt pytest && pytest -q
```

The tests cover:

- **Ingest:** normalization, cross-collector dedupe, attachments.
- **Extractor and learning:** the analyzer (supersede, cancel, evidence), the feedback loop, instruction ops / versions / restore, and replay A/B, including that labels don't leak into the prompt.
- **Assistant:** the agent loop with a scripted model (proposal → approval → the model is told about it), and that the agent has no tool to apply or approve changes.
- **Connector and schedule:** the MCP endpoint over the real protocol (auth, tools, proposal-only behaviour), plus scheduled mode with the urgent fast path.
- **Local mode and setup:** the localhost guard (foreign Host/Origin refused), API-key check, collector status.
- **Mac collector:** end to end against a synthetic WhatsApp database: senders resolved from the address book, backfill, lazy media downloads.

Tests never call the real API. Keep them fast and offline; new features get a test in `tests/` in the same style.

## Testing the installer without touching your own setup

```bash
RADAR_HOME=/tmp/rh RADAR_LABEL=dev.radar.test RADAR_SRC=$PWD RADAR_NO_OPEN=1 ./install.sh
/tmp/rh/bin/radar uninstall --purge
```

## Making a release

1. Bump `__version__` in `app/__init__.py` and commit.
2. `git tag v0.2.0 && git push --tags` (the tag must match the version).
3. GitHub Actions runs the tests, then publishes the Docker image `ghcr.io/<owner>/<repo>` (tags `0.2.0`, `0.2`, `latest`, for amd64 and arm64) and a GitHub release with generated notes.

The Mac one-command install does not use the image: `radar update` downloads the source for the branch or tag set in `RADAR_REF` (default `main`).
