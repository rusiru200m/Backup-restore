"""Actionable API diagnostics without returning transport output or submitted secrets."""
from __future__ import annotations

import errno
import logging
import re
import socket
import sqlite3
import traceback
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

import paramiko
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException

from . import models
from .remote import RemoteError
from .workflow import WorkflowError

logger = logging.getLogger("backup_restore.api")


@dataclass(frozen=True)
class Diagnostic:
    status: int
    code: str
    message: str
    hint: str


class OperationError(HTTPException):
    """Retain a classified error when a synchronous operation handles its failure."""

    def __init__(self, exc: Exception):
        self.original = exc
        diagnostic = classify(exc)
        super().__init__(status_code=diagnostic.status, detail=diagnostic.message)


def exception_chain(exc: BaseException) -> list[BaseException]:
    chain = []
    seen = set()
    while exc is not None and id(exc) not in seen:
        chain.append(exc)
        seen.add(id(exc))
        if isinstance(exc, OperationError):
            exc = exc.original
        else:
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return chain


def _storage_error(exc: sqlite3.Error) -> Diagnostic:
    # Text is inspected only to accommodate sqlite versions without error codes.
    # Neither the SQLite text nor its SQL/payload is ever returned or logged.
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    text = str(exc).lower()
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or "locked" in text or "busy" in text:
        return Diagnostic(503, "PROFILE_STORAGE_LOCKED", "Saved server storage is busy.",
                          "Close any other process editing the profile database, then reload saved servers.")
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB) or "malformed" in text or "not a database" in text:
        return Diagnostic(500, "PROFILE_STORAGE_CORRUPT", "The saved server database cannot be read.",
                          "Keep a copy of the profile database and restore a known good backup. Do not delete it to retry.")
    if code == sqlite3.SQLITE_FULL or "disk is full" in text:
        return Diagnostic(503, "STORAGE_FULL", "The controller has no space to save settings.",
                          "Free disk space on the controller and reload saved servers.")
    if code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_PERM) or any(
            word in text for word in ("readonly", "read-only", "unable to open", "permission")):
        return Diagnostic(503, "PROFILE_STORAGE_UNWRITABLE", "Saved server storage is not writable.",
                          "Check PROFILE_DB_PATH in .env and grant the controller account read/write access to its folder and file.")
    return Diagnostic(500, "PROFILE_STORAGE_ERROR", "Saved server storage failed.",
                      "Check the profile database location, permissions and available disk space. Keep the database when investigating.")


def _application_error(exc: BaseException) -> Diagnostic | None:
    """Match fixed application messages; never publish arbitrary exception text."""
    text = str(exc).lower()
    if isinstance(exc, RemoteError) and text.startswith("docker access denied"):
        return Diagnostic(400, "DOCKER_PERMISSION_DENIED", "Docker access denied for the selected SSH account.",
                          "Grant the selected SSH account Docker access using your server's administration policy, reconnect, then test the destination.")
    settings = ("MAX_DUMP_MB", "PROFILE_DB_PATH", "SSH_STATE_DIR", "DEST_SAFETY_BACKUP_DIR",
                "SOURCE_SSH_KEY_PATH", "DEST_SSH_KEY_PATH", "SSH_KNOWN_HOSTS_PATH")
    if isinstance(exc, (ValueError, WorkflowError)):
        for setting in settings:
            fixed_prefixes = (f"missing environment setting: {setting.lower()}", f"{setting.lower()} must ", f"{setting.lower()} is ")
            if text.startswith(fixed_prefixes):
                steps = "Enter a positive whole number of megabytes" if setting == "MAX_DUMP_MB" else "Check the configured value and the controller's file/folder access"
                if setting == "DEST_SAFETY_BACKUP_DIR":
                    steps = "Use an absolute Linux directory without .. or control characters and allow the destination SSH account to write there"
                return Diagnostic(400, "CONFIGURATION_ERROR", f"The {setting} setting is missing or invalid.",
                                  f"{steps} for {setting} in .env, then restart the controller.")
        if text.startswith("missing passphrase environment variable:"):
            return Diagnostic(400, "SSH_KEY_PASSPHRASE_REQUIRED", "The configured SSH key passphrase environment variable is missing.",
                              "Set the passphrase environment variable selected in this server's advanced settings, then restart and test SSH access.")
    if isinstance(exc, RemoteError) and "remote command failed" in text:
        commands = [
            (("no such container", "is not running"), "DOCKER_CONTAINER_UNAVAILABLE", "The selected Docker container is missing or stopped.",
             "Check the destination container name and start it if needed, then run the destination test."),
            (("cannot connect to the docker daemon", "is the docker daemon running", "docker: command not found", "docker: not found"), "DOCKER_UNAVAILABLE", "Docker is unavailable on the destination server.",
             "Install or start Docker on the destination and confirm that the SSH account can run docker ps."),
            (("permission denied while trying to connect to the docker", "permission denied while connecting to the docker"), "DOCKER_PERMISSION_DENIED", "The SSH account cannot access Docker.",
             "Grant the selected SSH account Docker access using your server's administration policy, reconnect, then test the destination."),
            (("password authentication failed", "peer authentication failed", "no pg_hba.conf entry"), "POSTGRES_AUTHENTICATION_FAILED", "PostgreSQL rejected the selected database role or authentication method.",
             "Check the PostgreSQL role, target database and local pg_hba.conf authentication inside the selected container."),
            (("does not exist",), "POSTGRES_TARGET_NOT_FOUND", "A requested PostgreSQL database, role or object does not exist.",
             "Check the selected database and role in the destination container. During restore, also check that required extensions and objects exist."),
            (("unsupported version", "unsupported version in file header"), "BACKUP_VERSION_UNSUPPORTED", "The PostgreSQL restore tool cannot read this backup version.",
             "Use a destination pg_restore version compatible with the pg_dump version that created the backup."),
            (("permission denied", "must be owner", "must be superuser"), "POSTGRES_PERMISSION_DENIED", "The database role lacks a required permission.",
             "Check the selected role's connection, dump, schema and object ownership privileges before restoring."),
        ]
        for patterns, code, message, hint in commands:
            if any(pattern in text for pattern in patterns):
                return Diagnostic(400, code, message, hint)
    matches = [
        (("ssh key not found", "managed ssh key not found"), "SSH_KEY_NOT_FOUND", "The selected SSH key was not found.",
         "Load and select an existing managed key, or check the advanced key path on this controller."),
        (("known hosts file missing", "verify and trust the server fingerprint", "not found in known_hosts"), "SSH_HOST_NOT_TRUSTED", "The server identity has not been trusted.",
         "Scan the server fingerprint, verify it using the VPS console, then trust it before connecting."),
        (("fingerprint does not match", "different trusted identity"), "SSH_HOST_KEY_MISMATCH", "The server identity does not match the trusted fingerprint.",
         "Verify the SSH host fingerprint in the VPS console and investigate the change before updating trusted hosts."),
        (("cannot unlock managed", "managed ssh encryption key is missing"), "SSH_KEY_STORAGE_ERROR", "The managed SSH key cannot be unlocked.",
         "Restore the matching controller encryption key and credential files from the same SSH storage backup."),
        (("restrict ssh storage permissions",), "SSH_STORAGE_PERMISSION_DENIED", "The SSH storage folder could not be secured.",
         "Check SSH_STATE_DIR and allow the controller account to manage that folder's permissions."),
        (("missing environment setting:", "max_dump_mb", "dest_safety_backup_dir", "must be a nonempty file or directory path", "must not contain control characters", "must be a positive integer"), "CONFIGURATION_ERROR", "A required controller setting is missing or invalid.",
         "Check .env for SOURCE_SSH_KEY_PATH, DEST_SSH_KEY_PATH, SSH_KNOWN_HOSTS_PATH, the configured passphrase variable, MAX_DUMP_MB and storage paths; restart after changing settings."),
        (("remote command exceeded",), "REMOTE_COMMAND_TIMEOUT", "A command on the server timed out.",
         "Check server load and Docker/PostgreSQL health. Check whether a restore already completed before trying it again."),
        (("remote discovery output exceeded",), "REMOTE_OUTPUT_TOO_LARGE", "The server returned more discovery data than can be safely listed.",
         "Narrow the server/container selection or configure the target manually and test it."),
        (("remote command failed",), "REMOTE_COMMAND_FAILED", "A command on the server failed.",
         "Check that Docker is running, the SSH account can use Docker, the selected container exists, and the PostgreSQL role can access the database. Run the destination test before restoring."),
        (("unexpected database", "wrong target database"), "DESTINATION_MISMATCH", "The connected database differs from the selected destination.",
         "Reload and select the intended destination, then run the destination test again before confirming a restore."),
        (("not recognized as a postgresql",), "INVALID_BACKUP_ARCHIVE", "The selected file is not a supported PostgreSQL custom dump.",
         "Select a complete .dump archive created with pg_dump -Fc and test the destination's pg_restore version."),
        (("dump size is zero", "download exceeded allowed maximum"), "BACKUP_SIZE_INVALID", "The backup is empty or exceeds the configured size limit.",
         "Check the backup file size and MAX_DUMP_MB in .env. Select a complete backup within that limit."),
        (("incomplete download",), "BACKUP_DOWNLOAD_INCOMPLETE", "The backup download did not complete.",
         "Check the source connection and select a backup that is no longer being written."),
        (("safety backup is empty",), "SAFETY_BACKUP_INVALID", "A verified destination safety backup could not be saved.",
         "Check destination disk space, safety backup folder permissions and PostgreSQL dump privileges before trying a restore."),
        (("not a regular file",), "BACKUP_FILE_INVALID", "The selected backup is not a regular file.",
         "Reload the backup list and select an existing .dump file rather than a link or directory."),
        (("select a directory, not a file",), "DIRECTORY_REQUIRED", "The selected path is not a directory.",
         "Browse to an existing backup directory on the selected server."),
    ]
    for patterns, code, message, hint in matches:
        if any(pattern in text for pattern in patterns):
            return Diagnostic(400, code, message, hint)
    return None


def classify(exc: BaseException, *, storage: bool = False) -> Diagnostic:
    chain = exception_chain(exc)
    for item in reversed(chain):
        if isinstance(item, sqlite3.Error):
            return _storage_error(item)
        if isinstance(item, paramiko.BadHostKeyException):
            return Diagnostic(400, "SSH_HOST_KEY_MISMATCH", "The server identity does not match its trusted SSH key.",
                              "Verify the SSH host fingerprint in the VPS console and investigate the change before updating trusted hosts.")
        if isinstance(item, paramiko.PasswordRequiredException):
            return Diagnostic(400, "SSH_KEY_PASSPHRASE_REQUIRED", "The SSH private key requires a passphrase.",
                              "Set the passphrase environment variable configured for this server, then restart and test SSH access.")
        if isinstance(item, paramiko.AuthenticationException):
            return Diagnostic(400, "SSH_AUTHENTICATION_FAILED", "The server rejected SSH authentication.",
                              "Check the SSH username and selected key. Authorize its public key for that account; for enrollment, check the current VPS password and allowed login method.")
        if isinstance(item, socket.gaierror):
            return Diagnostic(400, "SSH_HOST_NOT_FOUND", "The server hostname could not be resolved.",
                              "Check the hostname or IP address, the controller's network connection and DNS settings.")
        if isinstance(item, (TimeoutError, socket.timeout)):
            return Diagnostic(400, "SSH_TIMEOUT", "The connection to the server timed out.",
                              "Check the server address, SSH port, firewall and network connectivity, then test SSH access.")
        if isinstance(item, paramiko.ssh_exception.NoValidConnectionsError):
            errors = list(item.errors.values())
            if any(isinstance(error, (TimeoutError, socket.timeout)) for error in errors):
                return Diagnostic(400, "SSH_TIMEOUT", "The connection to the server timed out.",
                                  "Check the server address, SSH port, firewall and network connectivity, then test SSH access.")
            if errors and all(isinstance(error, ConnectionRefusedError) or getattr(error, "errno", None) == errno.ECONNREFUSED for error in errors):
                return Diagnostic(400, "SSH_CONNECTION_REFUSED", "The server refused the SSH connection.",
                                  "Check the SSH port and that the SSH service is running and permitted by the server firewall.")
            return Diagnostic(400, "SSH_CONNECTION_FAILED", "The server could not be reached over SSH.",
                              "Check the server address, SSH port, firewall and network route, then test SSH access.")
        if isinstance(item, ConnectionRefusedError):
            return Diagnostic(400, "SSH_CONNECTION_REFUSED", "The server refused the SSH connection.",
                              "Check the SSH port and that the SSH service is running and permitted by the server firewall.")
        if isinstance(item, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, EOFError)):
            return Diagnostic(400, "SSH_CONNECTION_LOST", "The connection to the server closed unexpectedly.",
                              "Check server availability and SSH logs. Check restore status before trying the operation again.")
        if isinstance(item, PermissionError):
            if storage:
                return Diagnostic(503, "PROFILE_STORAGE_UNWRITABLE", "Saved server storage is not writable.",
                                  "Check PROFILE_DB_PATH and grant the controller account access to its parent folder and database file.")
            return Diagnostic(400, "FILE_PERMISSION_DENIED", "File or directory permission denied.",
                              "Check read/write permissions on the selected remote folder and on controller key, known-hosts and state files.")
        if isinstance(item, FileNotFoundError):
            return Diagnostic(400, "FILE_NOT_FOUND", "A required file or directory was not found.",
                              "Check controller key/known-hosts paths and that the selected remote backup file or directory still exists.")
        if isinstance(item, OSError) and item.errno == errno.ENOSPC:
            return Diagnostic(503, "STORAGE_FULL", "There is not enough disk space to complete the operation.",
                              "Check free disk space on the controller and destination server before trying again.")
    if any(isinstance(item, ValidationError) for item in chain) and storage:
        return Diagnostic(500, "PROFILE_STORAGE_CORRUPT", "A saved server profile contains invalid settings.",
                          "Keep a copy of the profile database and restore valid settings from a known good backup.")
    # Outer workflow messages may merely wrap the useful inner error, so inspect
    # fixed application diagnostics from the innermost cause outward.
    for item in reversed(chain):
        if isinstance(item, (RemoteError, WorkflowError, ValueError, paramiko.SSHException)):
            diagnostic = _application_error(item)
            if diagnostic is not None:
                return diagnostic
    if any(isinstance(item, paramiko.SSHException) for item in chain):
        return Diagnostic(400, "SSH_CONNECTION_FAILED", "The SSH handshake or connection failed.",
                          "Check the SSH service, supported host keys and key format, then verify the server fingerprint and test SSH access.")
    if any(isinstance(item, WorkflowError) for item in chain):
        return Diagnostic(400, "WORKFLOW_ERROR", "The operation could not pass its safety checks.",
                          "Check the selected backup, destination and restore confirmations, then run the connection and destination tests.")
    if any(isinstance(item, RemoteError) for item in chain):
        return Diagnostic(400, "REMOTE_OPERATION_FAILED", "The operation on the server failed.",
                          "Test SSH access, check the selected remote paths, then run the destination test for Docker/PostgreSQL access.")
    if any(isinstance(item, ValueError) for item in chain):
        return Diagnostic(400, "CONFIGURATION_ERROR", "A controller or server setting is invalid.",
                          "Check the selected SSH key, server settings and .env storage paths, then test SSH access.")
    if any(isinstance(item, OSError) for item in chain):
        return Diagnostic(400, "FILE_OR_CONNECTION_ERROR", "A file or network operation failed.",
                          "Check the selected remote paths, local state/key permissions, network connectivity and available disk space.")
    return Diagnostic(500, "INTERNAL_ERROR", "The server could not complete this request.",
                      "Use the request ID to locate this failure in the controller log. Check operation status before trying a restore again.")


_SAFE_FIELDS = {name for value in vars(models).values() if isinstance(value, type)
                and issubclass(value, BaseModel) for name in value.model_fields}
_SAFE_FIELDS.update({"x-admin-token"})
_FIELD_MESSAGES = {
    "host": "Use an IP address or hostname without a scheme, spaces or a path.",
    "port": "Enter a whole SSH port number from 1 to 65535.",
    "username": "Enter a valid SSH account name.",
    "credential_id": "Select a valid managed SSH key.",
    "key_path": "Enter a valid controller private-key path or leave it unset.",
    "known_hosts_path": "Enter a valid controller known-hosts path or leave it unset.",
    "passphrase_env": "Enter an environment variable name, rather than the passphrase itself.",
    "fingerprint": "Use the SHA256 fingerprint returned by the server scan.",
    "backup_directory": "Use an absolute Linux directory without .. or control characters (maximum 1024 characters).",
    "safety_backup_directory": "Use an absolute Linux directory without .. or control characters (maximum 1024 characters).",
    "path": "Use an absolute Linux directory without .. or control characters (maximum 1024 characters).",
    "backup_filename": "Select one .dump file from the backup directory.",
    "confirmation": "Type the destination database name exactly to confirm.",
    "writes_paused": "Confirm that application writes are paused before restoring.",
    "agree_replace": "Confirm that replacement of existing database objects is intended.",
    "password": "Enter the current VPS SSH login password.",
    "name": "Enter a nonblank name of at most 120 characters.",
    "container": "Use letters, numbers, dots, underscores or hyphens for the container name.",
    "database": "Use letters, numbers, dots, underscores or hyphens for the database name.",
    "database_user": "Use letters, numbers, dots, underscores or hyphens for the PostgreSQL role.",
    "source_profile_id": "Select a saved backup-storage server profile.",
    "destination_profile_id": "Select a saved destination server profile.",
    "source_profile_revision": "Reload saved servers and reselect the backup-storage profile.",
    "destination_profile_revision": "Reload saved servers and reselect the destination profile.",
    "role": "Select a SOURCE or DEST server role.",
}
_SAFE_MODEL_MESSAGES = {
    "Choose a managed SSH key or advanced file settings, not both": ("credential_id",),
    "Select a generated SSH key first": ("server", "credential_id"),
    "Enter the current VPS SSH login password": ("password",),
    "Configure backup storage, a destination database, or both": ("backup_directory",),
    "Confirm that replacing existing backup objects is intended": ("agree_replace",),
    "Pause all application writes before restoring": ("writes_paused",),
    "Type the destination database name exactly to confirm": ("confirmation",),
}


def validation_details(errors: list[dict]) -> tuple[list[dict], list[dict]]:
    details, fields = [], []
    for error in errors:
        loc = [part for part in error.get("loc", ())
               if (isinstance(part, str) and part in _SAFE_FIELDS) or (isinstance(part, int) and 0 <= part <= 10000)]
        kind = error.get("type", "invalid")
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z_]+", kind):
            kind = "invalid"
        if kind == "missing":
            message = "This field is required."
        elif kind == "extra_forbidden":
            message = "Remove unsupported fields from this request."
        elif kind == "json_invalid":
            message = "Send a valid JSON request body."
        else:
            # Custom validators may include submitted values in msg/ctx. Only
            # fixed, known model-level instructions can be echoed verbatim.
            original = error.get("msg", "").removeprefix("Value error, ")
            if original in _SAFE_MODEL_MESSAGES:
                message = original
                loc.extend(_SAFE_MODEL_MESSAGES[original])
            else:
                message = _FIELD_MESSAGES.get(str(loc[-1]) if loc else "", "Check this field's value and required format.")
        field = ".".join(str(part) for part in loc) or "request"
        details.append({"loc": ["body", *loc], "msg": message, "type": kind})
        fields.append({"field": field, "message": message})
    return details, fields


_HTTP_DIAGNOSTICS = {
    "Invalid admin token": ("ADMIN_TOKEN_INVALID", "Enter the APP_TOKEN configured in .env, then connect again."),
    "Set a real APP_TOKEN (at least 24 characters) in .env": ("APP_TOKEN_NOT_CONFIGURED", "Set APP_TOKEN in .env to a random ASCII token of at least 24 characters, then restart the server."),
    "Another restore is already in progress": ("RESTORE_IN_PROGRESS", "Wait for the active restore to finish and verify its result before starting another restore."),
    "Server profile not found": ("PROFILE_NOT_FOUND", "Reload saved servers and select an existing profile."),
    "Server profile changed. Reload saved servers, reselect the backup and confirm the destination again.": ("PROFILE_CHANGED", "Reload saved servers, reselect the backup and confirm the destination again."),
    "Selected profile is not configured as backup storage": ("PROFILE_SOURCE_NOT_CONFIGURED", "Edit this profile to add a backup directory or select a backup-storage profile."),
    "Selected profile has no destination database": ("PROFILE_DESTINATION_NOT_CONFIGURED", "Edit this profile to add a destination database or select a destination profile."),
}
_STAGES = {"configuration": "configuration", "source_download": "the source download", "destination_connection": "the destination connection", "destination_check": "the destination check",
           "archive_upload": "the archive upload", "archive_validation": "archive validation", "safety_backup_creation": "safety backup creation",
           "safety_backup_verification": "safety backup verification", "restoring": "database restoration", "cleanup": "final cleanup"}


def _log_failure(request, exc: BaseException, diagnostic: Diagnostic, request_id: str) -> None:
    # Format frames ourselves: standard exc_info/format_exc would include raw
    # exception messages and source lines, either of which can contain secrets.
    chain = exception_chain(exc)
    frames = [f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
              for item in chain for frame in traceback.extract_tb(item.__traceback__)]
    route = getattr(request.scope.get("route"), "path", "/api/<unmatched>")
    logger.log(logging.ERROR if diagnostic.status >= 500 else logging.WARNING,
               "API failure request_id=%s method=%s path=%s code=%s types=%s frames=%s",
               request_id, request.method, route, diagnostic.code,
               ",".join(type(item).__name__ for item in chain), " > ".join(frames))


def error_response(request, exc: BaseException) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    fields, extra, detail = [], {}, None
    if isinstance(exc, RequestValidationError):
        detail, fields = validation_details(exc.errors())
        diagnostic = Diagnostic(422, "VALIDATION_ERROR", "Some request fields need attention.", "Correct the highlighted fields and submit again.")
    elif isinstance(exc, HTTPException) and not isinstance(exc, OperationError):
        known = _HTTP_DIAGNOSTICS.get(exc.detail) if isinstance(exc.detail, str) else None
        if known:
            diagnostic = Diagnostic(exc.status_code, known[0], exc.detail, known[1])
            detail = exc.detail
        elif isinstance(exc.detail, list):
            detail, fields = validation_details(exc.detail)
            diagnostic = Diagnostic(exc.status_code, "VALIDATION_ERROR", "Some request fields need attention.", "Correct the highlighted fields and submit again.")
        else:
            defaults = {404: ("NOT_FOUND", "The requested API endpoint was not found.", "Reload the app and check the requested API address."),
                        405: ("METHOD_NOT_ALLOWED", "This API endpoint does not support that method.", "Reload the app and use the expected request method.")}
            code, message, hint = defaults.get(exc.status_code, ("REQUEST_FAILED", "The API request could not be completed.", "Check the request and reload the app."))
            diagnostic = Diagnostic(exc.status_code, code, message, hint)
    else:
        storage = request.url.path.startswith(("/api/profiles", "/api/profile-"))
        # Permission errors in a profile-backed operation may come from remote
        # files. Identify profile storage by the stack, rather than route alone.
        profile_frames = any(frame.name in {"database", "list_profiles", "get_profile", "save_profile", "delete_profile"}
                             and Path(frame.filename).name == "profiles.py"
                             for item in exception_chain(exc) for frame in traceback.extract_tb(item.__traceback__))
        is_profile_crud = request.url.path == "/api/profiles" or request.url.path.startswith("/api/profiles/")
        diagnostic = classify(exc, storage=is_profile_crud or profile_frames or (storage and isinstance(exc, ValidationError)))
        workflow = next((item for item in exception_chain(exc) if isinstance(item, WorkflowError) and getattr(item, "stage", None) in _STAGES), None)
        if workflow is not None:
            stage = workflow.stage
            completed = getattr(workflow, "restore_completed", False) is True
            extra.update(stage=stage, restore_completed=completed)
            backup = getattr(workflow, "safety_backup_on_destination", None)
            if isinstance(backup, str) and backup.startswith("/") and len(backup) <= 2048 and not any(ord(char) < 32 or ord(char) == 127 for char in backup):
                extra["safety_backup_on_destination"] = backup
            if completed:
                diagnostic = Diagnostic(500, "RESTORE_FINALIZATION_FAILED", "The database restore completed, but final cleanup failed.",
                                        "Verify the restored database and inspect remaining staging files before retrying or resuming application writes.")
            else:
                diagnostic = replace(diagnostic, message=f"Restore stopped during {_STAGES[stage]}. {diagnostic.message}")
                recovery = " A verified safety backup is available on the destination." if "safety_backup_on_destination" in extra else " No verified safety backup was saved by this restore."
                diagnostic = replace(diagnostic, hint=diagnostic.hint + recovery)
    _log_failure(request, exc, diagnostic, request_id)
    payload = {"detail": detail if detail is not None else diagnostic.message,
               "error": {"code": diagnostic.code, "message": diagnostic.message,
                         "hint": diagnostic.hint, "fields": fields, "request_id": request_id, **extra}}
    return JSONResponse(status_code=diagnostic.status, content=payload,
                        headers={"X-Request-ID": request_id, "Cache-Control": "no-store"})
