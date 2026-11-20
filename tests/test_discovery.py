import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import discovery
from app.main import app
from app.remote import RemoteError, SSHRemote
from app.models import SSHServer


@pytest.fixture
def discovery_api(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "test-admin-token-with-at-least-24-characters")
    remote = MagicMock()
    remote.__enter__.return_value = remote
    connection = MagicMock(return_value=remote)
    monkeypatch.setattr(discovery, "connect_destination", connection)
    api = TestClient(app, headers={"X-Admin-Token": "test-admin-token-with-at-least-24-characters"})
    return api, remote, connection


def containers(*names):
    return "\n".join(json.dumps({"Names": name, "Image": image}) for name, image in names)


def catalog(user, *databases):
    return json.dumps({"database_user": user, "databases": list(databases)})


def test_discovery_finds_custom_postgres_role_multiple_containers_and_excludes_non_postgres(discovery_api):
    api, remote, connection = discovery_api

    def run(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return containers(("web", "nginx"), ("intern", "postgres:16"), ("analytics", "custom-db:latest"))
        name = args[4] if args[4] != "-e" else args[6]
        if "pg_restore" in args:
            if name == "web":
                raise RemoteError("No such executable")
            return "pg_restore (PostgreSQL) 16.2"
        if "sh" in args:
            return "intern_db\n200m-intern" if name == "intern" else "analytics_user\nanalytics"
        if "psql" in args:
            user = args[args.index("-U") + 1]
            assert "-w" in args and "PGCONNECT_TIMEOUT=5" in args
            assert args[-1] == discovery.DATABASE_QUERY
            return catalog(user, "200m-intern", "postgres") if name == "intern" else catalog(user, "analytics")
        pytest.fail(f"Unexpected command: {args}")

    remote.run.side_effect = run
    response = api.post("/api/ssh/databases", json={"server": {"host": "selected.example.com", "credential_id": "a" * 32}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["scanned_containers"] == 3
    assert result["postgres_containers"] == 2
    assert result["warnings"] == []
    assert [(item["container"], item["database"], item["database_user"]) for item in result["items"]] == [
        ("analytics", "analytics", "analytics_user"), ("intern", "200m-intern", "intern_db"),
        ("intern", "postgres", "intern_db")]
    assert connection.call_args.args[0].host == "selected.example.com"
    assert connection.call_args.args[0].credential_id == "a" * 32
    assert response.headers["cache-control"] == "no-store"
    all_commands = "\n".join(str(call.args[0]) for call in remote.run.call_args_list)
    assert "POSTGRES_PASSWORD" not in all_commands
    assert "inspect" not in all_commands
    assert "{{json .}}" not in all_commands
    assert "pg_dump" not in all_commands
    assert "--clean" not in all_commands
    assert "DROP" not in discovery.DATABASE_QUERY
    assert "NOT datistemplate" in discovery.DATABASE_QUERY
    assert "has_database_privilege" in discovery.DATABASE_QUERY


def test_discovery_tries_known_role_and_falls_back_from_unavailable_default_database(discovery_api):
    api, remote, connection = discovery_api
    attempts = []

    def run(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return containers(("custom-db", "postgres:16"))
        if "pg_restore" in args:
            return "pg_restore (PostgreSQL) 16"
        if "sh" in args:
            raise RemoteError("No shell")
        user, database = args[args.index("-U") + 1], args[args.index("-d") + 1]
        attempts.append((user, database))
        if (user, database) == ("app_admin", "template1"):
            return catalog("app_admin", "app_db")
        raise RemoteError("Authentication or database access failed")

    remote.run.side_effect = run
    response = api.post("/api/ssh/databases", json={"server": {"host": "example.com"}, "database_user": "app_admin", "database": "removed_database"})
    assert response.status_code == 200
    assert response.json()["items"][0]["database_user"] == "app_admin"
    assert attempts == [("app_admin", "removed_database"), ("app_admin", "postgres"), ("app_admin", "template1")]


def test_discovery_reports_inaccessible_container_without_losing_other_databases(discovery_api):
    api, remote, connection = discovery_api

    def run(args, **kwargs):
        if args[:2] == ["docker", "ps"]:
            return containers(("locked", "postgres:16"), ("working", "postgres:16"))
        if "pg_restore" in args:
            return "pg_restore (PostgreSQL) 16"
        if "sh" in args:
            return "postgres\npostgres"
        if args[6] == "locked":
            raise RemoteError("Password authentication failed")
        return catalog("postgres", "app")

    remote.run.side_effect = run
    response = api.post("/api/ssh/databases", json={"server": {"host": "example.com"}})
    assert response.status_code == 200
    result = response.json()
    assert len(result["items"]) == 1
    assert result["items"][0]["container"] == "working"
    assert result["warnings"][0]["container"] == "locked"


@pytest.mark.parametrize("output", ["not-json", '["wrong structure"]', '{"database_user":"postgres","databases":"bad"}'])
def test_invalid_catalog_is_not_mistaken_for_a_database_list(discovery_api, output):
    api, remote, connection = discovery_api
    remote.run.side_effect = lambda args, **kwargs: containers(("db", "postgres")) if args[:2] == ["docker", "ps"] else (
        "pg_restore (PostgreSQL) 16" if "pg_restore" in args else ("postgres\npostgres" if "sh" in args else output))
    response = api.post("/api/ssh/databases", json={"server": {"host": "example.com"}})
    assert response.status_code == 200
    assert response.json()["items"] == []
    assert len(response.json()["warnings"]) == 1


def test_unsupported_database_names_are_skipped_and_reported(discovery_api):
    api, remote, connection = discovery_api
    remote.run.side_effect = lambda args, **kwargs: containers(("db", "postgres")) if args[:2] == ["docker", "ps"] else (
        "pg_restore (PostgreSQL) 16" if "pg_restore" in args else ("postgres\npostgres" if "sh" in args else catalog("postgres", "valid-db", "bad name", "x;drop", "valid-db")))
    response = api.post("/api/ssh/databases", json={"server": {"host": "example.com"}})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["database"] == "valid-db"
    assert "2 database name(s)" in response.json()["warnings"][0]["message"]


def test_empty_server_returns_empty_list(discovery_api):
    api, remote, connection = discovery_api
    remote.run.return_value = ""
    response = api.post("/api/ssh/databases", json={"server": {"host": "example.com"}})
    assert response.status_code == 200
    assert response.json()["scanned_containers"] == 0
    assert response.json()["items"] == []


def test_discovery_docker_access_failure_is_clear(discovery_api):
    api, remote, connection = discovery_api
    remote.run.side_effect = RemoteError("Docker access denied")
    response = api.post("/api/ssh/databases", json={"server": {"host": "example.com"}})
    assert response.status_code == 400
    assert "Docker access denied" in response.json()["detail"]


def test_discovery_requires_token_and_valid_hints(discovery_api):
    api, remote, connection = discovery_api
    assert TestClient(app).post("/api/ssh/databases", json={"server": {"host": "example.com"}}).status_code == 401
    assert api.post("/api/ssh/databases", json={"server": {"host": "example.com"}, "database_user": "user; bad"}).status_code == 422
    connection.assert_not_called()


@pytest.mark.parametrize("require_complete", [True, False])
def test_remote_capture_limit_never_silently_truncates_discovery(require_complete):
    remote = SSHRemote(SSHServer(host="example.com"), "key", "known-hosts")
    channel = MagicMock()
    channel.recv_ready.side_effect = [True, False]
    channel.recv.return_value = b"x" * 40000
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 0
    remote.client = MagicMock()
    remote.client.get_transport.return_value.open_session.return_value = channel
    if require_complete:
        with pytest.raises(RemoteError, match="incomplete result"):
            remote.run(["docker", "ps"], require_complete=True)
    else:
        assert len(remote.run(["pg_restore", "-l", "archive.dump"])) == 32768
    channel.close.assert_called_once()
