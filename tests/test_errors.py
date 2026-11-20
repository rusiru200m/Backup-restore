"""API diagnostics must help recovery without publishing secrets or command output."""
import errno
import logging
import re
import socket
import sqlite3

import paramiko
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main, profiles, ssh_setup
from app.remote import RemoteError
from app.workflow import WorkflowError

TOKEN = "test-admin-token-with-at-least-24-characters"
SECRET = "do-not-publish-password-private-key-or-token"
SOURCE = {"source": {"host": "source.example.com"}, "backup_directory": "/backups"}
DESTINATION = {"destination": {"host": "destination.example.com"}, "container": "postgres",
               "database": "app", "database_user": "app_user"}
RESTORE = {**SOURCE, **DESTINATION, "backup_filename": "app.dump", "confirmation": "app",
           "writes_paused": True, "agree_replace": True}
PROFILE = {"name": "Storage", "ssh": SOURCE["source"], "backup_directory": "/backups"}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_TOKEN", TOKEN)
    monkeypatch.setenv("PROFILE_DB_PATH", str(tmp_path / "profiles.sqlite3"))
    return TestClient(main.app, headers={"X-Admin-Token": TOKEN})


def failure(exc):
    def raise_error(*args, **kwargs):
        raise exc
    return raise_error


def diagnostic(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert isinstance(body["detail"], (str, list))
    error = body["error"]
    assert error["code"] == code
    assert error["message"]
    assert error["hint"]
    assert isinstance(error["fields"], list)
    assert re.fullmatch("[a-f0-9]{32}", error["request_id"])
    assert error["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text
    return error


@pytest.mark.parametrize("attribute,path,payload", [
    ("list_backups", "/api/backups", SOURCE),
    ("preflight", "/api/preflight", DESTINATION),
    ("restore", "/api/restore", RESTORE),
])
def test_all_workflow_routes_handle_raw_ssh_errors(api, monkeypatch, attribute, path, payload):
    monkeypatch.setattr(main, attribute, failure(paramiko.AuthenticationException(SECRET)))
    error = diagnostic(api.post(path, json=payload), 400, "SSH_AUTHENTICATION_FAILED")
    assert "username" in error["hint"]


@pytest.mark.parametrize("exc,code", [
    (paramiko.PasswordRequiredException(SECRET), "SSH_KEY_PASSPHRASE_REQUIRED"),
    (paramiko.SSHException("Server 'example.com' not found in known_hosts " + SECRET), "SSH_HOST_NOT_TRUSTED"),
    (ConnectionRefusedError(errno.ECONNREFUSED, SECRET), "SSH_CONNECTION_REFUSED"),
    (paramiko.ssh_exception.NoValidConnectionsError({("example.com", 22): ConnectionRefusedError(errno.ECONNREFUSED, SECRET)}), "SSH_CONNECTION_REFUSED"),
    (paramiko.ssh_exception.NoValidConnectionsError({("example.com", 22): TimeoutError(SECRET)}), "SSH_TIMEOUT"),
    (socket.gaierror(-2, SECRET), "SSH_HOST_NOT_FOUND"),
    (TimeoutError(SECRET), "SSH_TIMEOUT"),
    (FileNotFoundError(SECRET), "FILE_NOT_FOUND"),
    (PermissionError(SECRET), "FILE_PERMISSION_DENIED"),
    (OSError(errno.ENOSPC, SECRET), "STORAGE_FULL"),
])
def test_connection_and_file_failures_are_actionable(api, monkeypatch, exc, code):
    monkeypatch.setattr(main, "preflight", failure(exc))
    diagnostic(api.post("/api/preflight", json=DESTINATION), 503 if code == "STORAGE_FULL" else 400, code)


def test_host_key_mismatch_does_not_echo_fingerprints_or_exception_text(api, monkeypatch):
    # BadHostKeyException.__str__ includes host/key details, so its type alone is sufficient.
    exc = paramiko.BadHostKeyException(SECRET, object(), object())
    monkeypatch.setattr(main, "preflight", failure(exc))
    diagnostic(api.post("/api/preflight", json=DESTINATION), 400, "SSH_HOST_KEY_MISMATCH")


def test_chained_restore_failure_preserves_underlying_authentication_reason(api, monkeypatch):
    def run(_):
        try:
            raise paramiko.AuthenticationException(SECRET)
        except paramiko.AuthenticationException as cause:
            raise WorkflowError("Restore not completed " + SECRET) from cause
    monkeypatch.setattr(main, "restore", run)
    diagnostic(api.post("/api/restore", json=RESTORE), 400, "SSH_AUTHENTICATION_FAILED")


@pytest.mark.parametrize("message,code", [
    ("Cannot connect to the Docker daemon", "DOCKER_UNAVAILABLE"),
    ("permission denied while trying to connect to the Docker daemon socket", "DOCKER_PERMISSION_DENIED"),
    ("No such container: hidden", "DOCKER_CONTAINER_UNAVAILABLE"),
    ("Container hidden is not running", "DOCKER_CONTAINER_UNAVAILABLE"),
    ("FATAL: password authentication failed for user hidden", "POSTGRES_AUTHENTICATION_FAILED"),
    ('FATAL: database "hidden" does not exist', "POSTGRES_TARGET_NOT_FOUND"),
    ("ERROR: must be owner of table hidden", "POSTGRES_PERMISSION_DENIED"),
    ("pg_restore: unsupported version (1.16) in file header", "BACKUP_VERSION_UNSUPPORTED"),
    ("unclassified command output", "REMOTE_COMMAND_FAILED"),
])
def test_remote_command_output_is_classified_without_being_exposed(api, monkeypatch, message, code):
    exc = RemoteError(f"Remote command failed (exit 1): docker exec psql {SECRET}\n{message} {SECRET}")
    monkeypatch.setattr(main, "preflight", failure(exc))
    diagnostic(api.post("/api/preflight", json=DESTINATION), 400, code)


@pytest.mark.parametrize("method,path,attribute,body", [
    ("GET", "/api/profiles", "list_profiles", None),
    ("POST", "/api/profiles", "save_profile", PROFILE),
    ("PUT", "/api/profiles/existing", "save_profile", PROFILE),
    ("DELETE", "/api/profiles/existing", "delete_profile", None),
])
def test_profile_crud_database_errors_are_json(api, monkeypatch, method, path, attribute, body):
    monkeypatch.setattr(profiles, attribute, failure(sqlite3.OperationalError("database is locked " + SECRET)))
    diagnostic(api.request(method, path, json=body), 503, "PROFILE_STORAGE_LOCKED")


@pytest.mark.parametrize("exc,status,code", [
    (sqlite3.DatabaseError("database disk image is malformed " + SECRET), 500, "PROFILE_STORAGE_CORRUPT"),
    (sqlite3.DatabaseError("file is not a database " + SECRET), 500, "PROFILE_STORAGE_CORRUPT"),
    (sqlite3.OperationalError("attempt to write a readonly database " + SECRET), 503, "PROFILE_STORAGE_UNWRITABLE"),
    (PermissionError(SECRET), 503, "PROFILE_STORAGE_UNWRITABLE"),
])
def test_profile_storage_failures_have_recovery_instructions(api, monkeypatch, exc, status, code):
    monkeypatch.setattr(profiles, "list_profiles", failure(exc))
    diagnostic(api.get("/api/profiles"), status, code)


def test_profile_backed_route_also_handles_database_errors(api, monkeypatch):
    monkeypatch.setattr(profiles, "get_profile", failure(sqlite3.OperationalError("unable to open database file " + SECRET)))
    diagnostic(api.post("/api/profile-backups", json={"source_profile_id": "a" * 32}), 503, "PROFILE_STORAGE_UNWRITABLE")


@pytest.mark.parametrize("path,attribute,body", [
    ("/api/ssh/scan", "scan_host", {"host": "example.com"}),
    ("/api/ssh/trust", "trust_host", {"server": {"host": "example.com"}, "fingerprint": "SHA256:" + "A" * 43}),
    ("/api/ssh/authorize", "authorize_key", {"server": {"host": "example.com", "credential_id": "a" * 32}, "password": SECRET}),
])
def test_ssh_setup_routes_handle_raw_ssh_errors(api, monkeypatch, path, attribute, body):
    monkeypatch.setattr(ssh_setup, attribute, failure(paramiko.AuthenticationException(SECRET)))
    diagnostic(api.post(path, json=body), 400, "SSH_AUTHENTICATION_FAILED")


def test_validation_reports_fields_without_secret_input_or_context(api):
    response = api.post("/api/ssh/authorize", json={"server": {"host": SECRET + " bad host", "port": 99999}, "password": SECRET})
    error = diagnostic(response, 422, "VALIDATION_ERROR")
    assert {item["field"] for item in error["fields"]} == {"server.host", "server.port"}
    assert all(set(item) == {"loc", "msg", "type"} for item in response.json()["detail"])
    assert "body." not in str(error["fields"])
    assert "input" not in response.text
    assert '"ctx"' not in response.text


def test_validation_does_not_echo_arbitrary_custom_validator_message(api, monkeypatch):
    from fastapi.exceptions import RequestValidationError
    exc = RequestValidationError([{"loc": ("body", "password"), "type": "value_error", "msg": "Value error, " + SECRET,
                                   "input": SECRET, "ctx": {"error": ValueError(SECRET)}}])
    monkeypatch.setattr(main, "list_backups", failure(exc))
    diagnostic(api.post("/api/backups", json=SOURCE), 422, "VALIDATION_ERROR")


@pytest.mark.parametrize("change,field", [({"agree_replace": False}, "agree_replace"),
                                         ({"writes_paused": False}, "writes_paused"),
                                         ({"confirmation": "other"}, "confirmation")])
def test_restore_model_checks_identify_the_correct_field(api, change, field):
    error = diagnostic(api.post("/api/restore", json={**RESTORE, **change}), 422, "VALIDATION_ERROR")
    assert error["fields"][0]["field"] == field


def test_invalid_saved_profile_is_a_storage_error_without_leaking_payload(api):
    with profiles.database() as connection:
        connection.execute("INSERT INTO profiles VALUES (?, ?)", ("existing", '{"name":"' + SECRET + '"}'))
    error = diagnostic(api.get("/api/profiles"), 500, "PROFILE_STORAGE_CORRUPT")
    assert "known good backup" in error["hint"]


@pytest.mark.parametrize("setting", ["MAX_DUMP_MB", "PROFILE_DB_PATH", "SSH_STATE_DIR", "DEST_SAFETY_BACKUP_DIR"])
def test_configuration_failure_names_setting_without_its_value(api, monkeypatch, setting):
    monkeypatch.setattr(main, "preflight", failure(ValueError(f"{setting} must be valid: {SECRET}")))
    error = diagnostic(api.post("/api/preflight", json=DESTINATION), 400, "CONFIGURATION_ERROR")
    assert setting in error["message"]
    assert setting in error["hint"]


@pytest.mark.parametrize("token", ["", "short", "REPLACE_" + "A" * 32, "é" * 32, " " * 32])
def test_invalid_configured_admin_token_is_actionable(api, monkeypatch, token):
    monkeypatch.setenv("APP_TOKEN", token)
    diagnostic(api.get("/api/profiles"), 503, "APP_TOKEN_NOT_CONFIGURED")


def test_auth_error_keeps_existing_detail_and_response_contract(api):
    response = api.get("/api/profiles", headers={"X-Admin-Token": SECRET})
    diagnostic(response, 401, "ADMIN_TOKEN_INVALID")
    assert response.json()["detail"] == "Invalid admin token"


def test_request_ids_are_generated_and_cannot_be_spoofed(api, monkeypatch):
    monkeypatch.setattr(main, "preflight", failure(RuntimeError(SECRET)))
    responses = [api.post("/api/preflight", json=DESTINATION, headers={"X-Request-ID": SECRET}) for _ in range(2)]
    ids = [diagnostic(response, 500, "INTERNAL_ERROR")["request_id"] for response in responses]
    assert len(set(ids)) == 2


def test_unexpected_errors_log_safe_frames_and_request_id(api, monkeypatch, caplog):
    monkeypatch.setattr(main, "list_backups", failure(RuntimeError(SECRET)))
    with caplog.at_level(logging.WARNING, logger="backup_restore.api"):
        response = api.post("/api/backups", json={**SOURCE, "backup_directory": "/" + SECRET})
    error = diagnostic(response, 500, "INTERNAL_ERROR")
    assert SECRET not in caplog.text
    assert TOKEN not in caplog.text
    assert error["request_id"] in caplog.text
    assert "method=POST path=/api/backups code=INTERNAL_ERROR" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "test_errors.py:" in caplog.text


def test_unknown_http_exception_detail_is_not_exposed(api, monkeypatch):
    monkeypatch.setattr(main, "list_backups", failure(HTTPException(400, SECRET)))
    diagnostic(api.post("/api/backups", json=SOURCE), 400, "REQUEST_FAILED")


@pytest.mark.parametrize("method,path,code,status", [("GET", "/api/missing", "NOT_FOUND", 404),
                                                     ("PATCH", "/api/profiles", "METHOD_NOT_ALLOWED", 405)])
def test_framework_http_errors_follow_api_contract(api, method, path, code, status):
    diagnostic(api.request(method, path), status, code)


def test_completed_restore_finalization_error_requires_verification(api, monkeypatch):
    exc = WorkflowError(SECRET)
    exc.stage = "cleanup"
    exc.restore_completed = True
    exc.safety_backup_on_destination = "/safety/app.dump"
    monkeypatch.setattr(main, "restore", failure(exc))
    error = diagnostic(api.post("/api/restore", json=RESTORE), 500, "RESTORE_FINALIZATION_FAILED")
    assert error["restore_completed"] is True
    assert error["stage"] == "cleanup"
    assert error["safety_backup_on_destination"] == "/safety/app.dump"
    assert "Verify the restored database" in error["hint"]


def test_incomplete_restore_exposes_stage_and_verified_recovery_backup(api, monkeypatch):
    exc = WorkflowError(SECRET)
    exc.stage = "restoring"
    exc.restore_completed = False
    exc.safety_backup_on_destination = "/safety/app.dump"
    monkeypatch.setattr(main, "restore", failure(exc))
    error = diagnostic(api.post("/api/restore", json=RESTORE), 400, "WORKFLOW_ERROR")
    assert error["stage"] == "restoring"
    assert error["restore_completed"] is False
    assert "verified safety backup is available" in error["hint"]
