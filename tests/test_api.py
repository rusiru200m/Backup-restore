import os
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_home_ui_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "Backup Restore Starter" in response.text
    assert 'id="restore"' in response.text


def test_api_requires_token(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "this-is-a-secure-test-token-at-least-24-chars")
    body = {"destination": {"host": "165.232.165.85"}, "container": "intern", "database": "200m-intern", "database_user": "intern_db"}
    response = client.post("/api/preflight", json=body)
    assert response.status_code == 401


def test_restore_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "this-is-a-secure-test-token-at-least-24-chars")
    payload = {
        "source": {"host": "167.172.77.230"},
        "backup_directory": "/srv/backups/postgresql/daily",
        "destination": {"host": "165.232.165.85"},
        "container": "intern", "database": "200m-intern", "database_user": "intern_db",
        "backup_filename": "200m-intern_2026-11-20_02-00-01.dump",
        "confirmation": "wrong-name", "agree_replace": True, "writes_paused": True,
    }
    response = client.post("/api/restore", json=payload, headers={"X-Admin-Token": os.environ["APP_TOKEN"]})
    assert response.status_code == 422  # no SSH connection should be attempted
