import pytest
from fastapi.testclient import TestClient

from app import main, profiles, workflow
from app.main import app
from app.models import SSHServer


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_TOKEN", "test-admin-token-with-at-least-24-characters")
    monkeypatch.setenv("PROFILE_DB_PATH", str(tmp_path / "profiles.sqlite3"))
    return TestClient(app, headers={"X-Admin-Token": "test-admin-token-with-at-least-24-characters"})


def storage(host="storage.example.com"):
    return {"name": "Archive storage", "ssh": {"host": host, "port": 2222,
            "username": "backup", "key_path": "~/.ssh/storage"}, "backup_directory": "/srv/backups"}


def target(host="target.example.com"):
    return {"name": "App destination", "ssh": {"host": host, "key_path": "~/.ssh/target"},
            "destination": {"container": "postgres", "database": "app_db", "database_user": "app_user",
                            "safety_backup_directory": "/srv/safety/app"}}


def create(api, payload):
    response = api.post("/api/profiles", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def restore_payload(api, source_id, target_id):
    saved = {p["id"]: p for p in api.get("/api/profiles").json()["items"]}
    return {"source_profile_id": source_id, "destination_profile_id": target_id,
            "source_profile_revision": saved[source_id]["revision"],
            "destination_profile_revision": saved[target_id]["revision"],
            "backup_filename": "app.dump", "confirmation": "app_db",
            "writes_paused": True, "agree_replace": True}


def test_multiple_profiles_persist_edit_and_delete(api):
    assert api.get("/api/profiles").json() == {"items": []}
    ids = [create(api, storage(f"storage{i}.example.com")) for i in range(4)]
    assert len(set(ids)) == 4
    # Read with a new client and fresh SQLite connections, as after an app restart.
    other = TestClient(app, headers=api.headers)
    assert len(other.get("/api/profiles").json()["items"]) == 4
    assert api.put(f"/api/profiles/{ids[1]}", json=target()).status_code == 200
    saved = profiles.get_profile(ids[1])
    assert saved.destination.database == "app_db"
    assert saved.backup_directory is None
    assert api.delete(f"/api/profiles/{ids[0]}").status_code == 200
    assert len(other.get("/api/profiles").json()["items"]) == 3
    assert api.delete(f"/api/profiles/{ids[0]}").status_code == 404
    assert api.put("/api/profiles/missing", json=target()).status_code == 404


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/profiles", None), ("POST", "/api/profiles", storage()),
    ("PUT", "/api/profiles/missing", storage()), ("DELETE", "/api/profiles/missing", None),
    ("POST", "/api/profile-backups", {"source_profile_id": "missing"}),
    ("POST", "/api/profile-preflight", {"destination_profile_id": "missing"}),
    ("POST", "/api/profile-restore", {}),
])
def test_profile_endpoints_require_token(api, method, path, body):
    response = TestClient(app).request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("payload", [
    {"name": "No role", "ssh": {"host": "example.com"}},
    {**storage(), "backup_directory": "/srv/../secret"},
    {**storage(), "name": "   "},
    {**target(), "destination": {**target()["destination"], "container": "db; whoami"}},
    {**target(), "destination": {**target()["destination"], "safety_backup_directory": "relative"}},
    {**storage(), "ssh": {"host": "example.com", "passphrase_env": "literal passphrase"}},
])
def test_reject_invalid_profiles(api, payload):
    assert api.post("/api/profiles", json=payload).status_code == 422
    assert api.get("/api/profiles").json()["items"] == []


def test_saved_profiles_route_selected_connections(api, monkeypatch):
    create(api, storage("unused.example.com"))
    source_id = create(api, storage("selected-storage.example.com"))
    target_id = create(api, target("selected-target.example.com"))
    received = {}

    def list_backups(request):
        received["source"] = request
        return [{"filename": "app.dump", "bytes": 42}]

    def preflight(request):
        received["target"] = request
        return {"ok": True}

    def restore(request):
        received["restore"] = request
        return {"ok": True}

    monkeypatch.setattr(main, "list_backups", list_backups)
    monkeypatch.setattr(main, "preflight", preflight)
    monkeypatch.setattr(main, "restore", restore)
    assert api.post("/api/profile-backups", json={"source_profile_id": source_id}).status_code == 200
    assert received["source"].source.host == "selected-storage.example.com"
    assert api.post("/api/profile-preflight", json={"destination_profile_id": target_id}).status_code == 200
    assert received["target"].destination.key_path == "~/.ssh/target"
    response = api.post("/api/profile-restore", json=restore_payload(api, source_id, target_id))
    assert response.status_code == 200, response.text
    resolved = received["restore"]
    assert resolved.source.host == "selected-storage.example.com"
    assert resolved.destination.host == "selected-target.example.com"
    assert resolved.safety_backup_directory == "/srv/safety/app"
    assert resolved.source.key_path == "~/.ssh/storage"
    assert response.json()["source_profile_id"] == source_id


def test_wrong_role_or_missing_profile_fails_before_ssh(api, monkeypatch):
    monkeypatch.setattr(main, "list_backups", lambda _: pytest.fail("SSH should not run"))
    monkeypatch.setattr(main, "preflight", lambda _: pytest.fail("SSH should not run"))
    source_id, target_id = create(api, storage()), create(api, target())
    assert api.post("/api/profile-backups", json={"source_profile_id": target_id}).status_code == 422
    assert api.post("/api/profile-preflight", json={"destination_profile_id": source_id}).status_code == 422
    assert api.post("/api/profile-backups", json={"source_profile_id": "missing"}).status_code == 404


@pytest.mark.parametrize("change", [
    {"confirmation": "wrong"}, {"writes_paused": False}, {"agree_replace": False},
    {"backup_filename": "../other.dump"},
])
def test_profile_restore_retains_confirmation_validation(api, monkeypatch, change):
    source_id, target_id = create(api, storage()), create(api, target())
    payload = restore_payload(api, source_id, target_id)
    payload.update(change)
    monkeypatch.setattr(main, "restore", lambda _: pytest.fail("Restore should not run"))
    assert api.post("/api/profile-restore", json=payload).status_code == 422


def test_changed_profile_requires_reload_before_restore(api, monkeypatch):
    source_id, target_id = create(api, storage()), create(api, target())
    payload = restore_payload(api, source_id, target_id)
    api.put(f"/api/profiles/{target_id}", json=target("different-host.example.com"))
    monkeypatch.setattr(main, "restore", lambda _: pytest.fail("Restore should not run"))
    assert api.post("/api/profile-restore", json=payload).status_code == 409


def test_profile_restore_uses_existing_operation_lock(api, monkeypatch):
    source_id, target_id = create(api, storage()), create(api, target())
    payload = restore_payload(api, source_id, target_id)
    monkeypatch.setattr(main, "restore", lambda _: pytest.fail("Concurrent restore should not run"))
    main._restore_lock.acquire()
    try:
        assert api.post("/api/profile-restore", json=payload).status_code == 409
    finally:
        main._restore_lock.release()


def test_profile_ssh_credentials_and_legacy_defaults(monkeypatch):
    monkeypatch.setenv("SOURCE_SSH_KEY_PATH", "default-source-key")
    monkeypatch.setenv("DEST_SSH_KEY_PATH", "default-target-key")
    monkeypatch.setenv("SSH_KNOWN_HOSTS_PATH", "default-known-hosts")
    monkeypatch.setenv("TEST_SERVER_PASSPHRASE", "secret")
    server = SSHServer(host="example.com", key_path="server-key", known_hosts_path="server-hosts",
                       passphrase_env="TEST_SERVER_PASSPHRASE")
    connection = workflow.connect_source(server)
    assert connection.key_path == "server-key"
    assert connection.known_hosts == "server-hosts"
    assert connection.passphrase == "secret"
    assert workflow.connect_destination(SSHServer(host="example.com")).key_path == "default-target-key"
    monkeypatch.delenv("TEST_SERVER_PASSPHRASE")
    with pytest.raises(workflow.WorkflowError, match="Missing passphrase environment variable"):
        workflow.connect_source(server)
