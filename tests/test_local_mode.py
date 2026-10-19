"""One-command Mac install: Radar listens on 127.0.0.1 without a password, so it must refuse cross-site tricks."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RADAR_PASSWORD", "")
    monkeypatch.setenv("RADAR_LOCAL", "1")
    monkeypatch.setenv("RADAR_INGEST_TOKEN", "tok")
    monkeypatch.setenv("RADAR_DISABLE_ANALYZER", "1")
    from app import db
    importlib.reload(db)
    import app.store, app.analyzer, app.main
    for m in (app.store, app.analyzer, app.main):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(app.main.app, base_url="http://127.0.0.1:8080") as c:
        yield c


def test_local_mode_needs_no_password(local):
    assert local.get("/api/stats").status_code == 200
    assert local.get("/api/setup").json()["local"] is True


def test_foreign_host_is_refused(local):
    """DNS rebinding: an attacker's domain resolving to 127.0.0.1 sends its own Host header."""
    assert local.get("/api/tasks", headers={"Host": "evil.example"}).status_code == 403
    assert local.get("/api/tasks", headers={"Host": "localhost:8080"}).status_code == 200


def test_cross_site_write_is_refused(local):
    r = local.post("/api/setup/api-key", json={"key": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = local.post("/api/sources", json={"name": "A", "match_pattern": "a"}, headers={"Origin": "http://127.0.0.1:8080"})
    assert r.status_code == 200


def test_setup_wizard_gates_until_finished(local, monkeypatch):
    import httpx
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    st = local.get("/api/setup").json()
    assert st["required"] is True and st["done"] is False and st["python"]

    # can't finish without a key, then without a chat
    assert local.post("/api/setup/finish").status_code == 400
    monkeypatch.setattr(httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200})())
    assert local.post("/api/setup/api-key", json={"key": "sk-ant-" + "x" * 30}).status_code == 200
    assert local.post("/api/setup/finish").status_code == 400
    local.post("/api/sources", json={"name": "Class", "match_pattern": "Class"})
    assert local.post("/api/setup/finish").status_code == 200
    st = local.get("/api/setup").json()
    assert st["required"] is False and st["done"] is True


def test_open_settings_and_check_now(local, monkeypatch):
    import subprocess
    import app.main as main
    calls = []
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(main.sys, "platform", "darwin")

    assert local.post("/api/setup/open-settings", json={"what": "fda"}).status_code == 200
    assert calls[-1][0] == "open" and "Privacy_AllFiles" in calls[-1][1]
    assert local.post("/api/setup/open-settings", json={"what": "file"}).status_code == 200
    assert calls[-1][:2] == ["open", "-R"]
    assert local.post("/api/setup/open-settings", json={"what": "rm -rf /"}).status_code == 400  # only fixed actions
    assert local.post("/api/setup/check-now").status_code == 404                                  # no label configured

    monkeypatch.setenv("RADAR_COLLECTOR_LABEL", "com.radar.collector")
    assert local.post("/api/setup/check-now").status_code == 200
    assert calls[-1][:2] == ["launchctl", "kickstart"] and calls[-1][-1].endswith("/com.radar.collector")
    # and a foreign page can't trigger it
    assert local.post("/api/setup/check-now", headers={"Origin": "https://evil.example"}).status_code == 403


def test_open_settings_unavailable_outside_local_mode(tmp_path, monkeypatch):
    # the normal (password) server must not expose these
    monkeypatch.setenv("RADAR_LOCAL", "0")
    monkeypatch.setenv("RADAR_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("RADAR_PASSWORD", "pw")
    monkeypatch.setenv("RADAR_DISABLE_ANALYZER", "1")
    from app import db
    importlib.reload(db)
    import app.store, app.analyzer, app.main
    for m in (app.store, app.analyzer, app.main):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(app.main.app) as c:
        c.post("/api/login", json={"password": "pw"})
        assert c.get("/api/setup").json()["required"] is False
        assert c.post("/api/setup/open-settings", json={"what": "fda"}).status_code == 404
