"""List backup archives, validate destination, and perform guarded in-place restore.

The controller talks to each VPS separately. VPS-to-VPS SSH is NOT required.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import PreflightRequest, RestoreRequest, SourceSelection
from .remote import RemoteError, SSHRemote, docker_args


class WorkflowError(RuntimeError):
    pass


def required_setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise WorkflowError(f"Missing environment setting: {name}")
    return value


def connect_server(server, role: str):
    passphrase_name = server.passphrase_env or f"{role}_SSH_KEY_PASSPHRASE"
    if server.passphrase_env and not os.environ.get(passphrase_name):
        raise WorkflowError(f"Missing passphrase environment variable: {passphrase_name}")
    return SSHRemote(
        server,
        server.key_path or required_setting(f"{role}_SSH_KEY_PATH"),
        server.known_hosts_path or required_setting("SSH_KNOWN_HOSTS_PATH", "~/.ssh/known_hosts"),
        os.environ.get(passphrase_name) or None,
    )


def connect_source(server):
    return connect_server(server, "SOURCE")


def connect_destination(server):
    return connect_server(server, "DEST")


def list_backups(request: SourceSelection) -> list[dict]:
    with connect_source(request.source) as remote:
        with remote.sftp() as sftp:
            entries = sftp.listdir_attr(request.backup_directory)
        choices = []
        for entry in entries:
            if not entry.filename.endswith(".dump") or not stat.S_ISREG(entry.st_mode or 0):
                continue
            # Only filenames accepted by RestoreRequest can be selected.
            from .models import FILE_RE
            if not FILE_RE.fullmatch(entry.filename):
                continue
            choices.append({
                "filename": entry.filename,
                "bytes": entry.st_size,
                "modified_utc": datetime.fromtimestamp(entry.st_mtime, tz=timezone.utc).isoformat(),
            })
        return sorted(choices, key=lambda item: item["modified_utc"], reverse=True)[:100]


def preflight(request: PreflightRequest) -> dict:
    with connect_destination(request.destination) as remote:
        version = remote.run(docker_args(request.container, "pg_restore", "--version"))
        # SELECT forces a valid DB connection; pg_isready alone does not validate access.
        actual = remote.run(docker_args(request.container, "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", request.database_user, "-d", request.database, "-c", "SELECT current_database()"))
        if actual.strip() != request.database:
            raise WorkflowError(f"Connected to unexpected database: {actual}")
        return {"ok": True, "database": actual.strip(), "pg_restore": version, "message": "SSH, Docker and PostgreSQL access verified"}


def restore(request: RestoreRequest) -> dict:
    """Safely back up target and restore source dump into an EXISTING DB.

    All restore DDL/data changes are in one PostgreSQL transaction; external
    application writes must already be paused. The restore does NOT create a DB.
    """
    max_bytes = int(required_setting("MAX_DUMP_MB", "2048")) * 1024 * 1024
    destination_dir = request.safety_backup_directory or required_setting("DEST_SAFETY_BACKUP_DIR", "/var/backups/backup-restore-starter")
    if not destination_dir.startswith("/") or ".." in destination_dir.split("/"):
        raise WorkflowError("DEST_SAFETY_BACKUP_DIR must be a safe absolute path")
    job_id = uuid.uuid4().hex[:12]
    archive_path = posixpath.join(request.backup_directory, request.backup_filename)
    staged_host = f"/tmp/backup-restore-stage-{job_id}.dump"
    staged_container = f"/tmp/backup-restore-stage-{job_id}.dump"
    safety_container = f"/tmp/backup-restore-before-{job_id}.dump"
    safety_name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{request.database}_before_{job_id}.dump"
    safety_host = posixpath.join(destination_dir, safety_name)
    local_sha256 = hashlib.sha256()
    source_bytes = 0
    safety_saved = False
    restore_succeeded = False
    logs = []

    # The controller needs free space only for the source dump; not both backups.
    with tempfile.TemporaryDirectory(prefix="backup-restore-starter-") as tmp:
        local_archive = Path(tmp) / "source.dump"
        try:
            with connect_source(request.source) as source:
                with source.sftp() as sftp:
                    attrs = sftp.lstat(archive_path)
                    if not stat.S_ISREG(attrs.st_mode or 0):
                        raise WorkflowError("Selected backup is not a regular file")
                    if attrs.st_size <= 0 or attrs.st_size > max_bytes:
                        raise WorkflowError(f"Dump size is zero or exceeds configured MAX_DUMP_MB ({max_bytes // 1024 // 1024})")
                    # Avoid SFTP symlink tricks and large local memory buffers.
                    file_descriptor = os.open(local_archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(file_descriptor, "wb") as target, sftp.open(archive_path, "rb") as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            source_bytes += len(chunk)
                            if source_bytes > max_bytes:
                                raise WorkflowError("Download exceeded allowed maximum")
                            target.write(chunk)
                            local_sha256.update(chunk)
                    if source_bytes != attrs.st_size:
                        raise WorkflowError(f"Incomplete download: expected {attrs.st_size} bytes, got {source_bytes}")
            logs.append(f"Downloaded {request.backup_filename} ({source_bytes:,} bytes) via source SFTP")

            with connect_destination(request.destination) as dest:
                sftp_dest = dest.sftp()
                try:
                    dest.run(["mkdir", "-p", destination_dir])
                    dest.run(["chmod", "700", destination_dir])
                    # Verify database and Docker access before touching existing data.
                    current_db = dest.run(docker_args(request.container, "psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", request.database_user, "-d", request.database, "-c", "SELECT current_database()"))
                    if current_db.strip() != request.database:
                        raise WorkflowError(f"Wrong target database: {current_db}")

                    sftp_dest.put(str(local_archive), staged_host)
                    sftp_dest.chmod(staged_host, 0o600)
                    dest.run(["docker", "cp", staged_host, f"{request.container}:{staged_container}"])
                    # pg_restore -l checks the archive TOC (not a full physical integrity test).
                    toc = dest.run(docker_args(request.container, "pg_restore", "-l", staged_container))
                    if "TOC Entries" not in toc and ";" not in toc:
                        raise WorkflowError("The uploaded file is not recognized as a PostgreSQL custom dump")
                    logs.append("Destination PostgreSQL tools recognized the dump format")

                    # Mandatory safety copy of current target database before replacement.
                    dest.run(docker_args(request.container, "pg_dump", "-U", request.database_user, "-d", request.database, "-Fc", "-f", safety_container), timeout=1800)
                    dest.run(docker_args(request.container, "pg_restore", "-l", safety_container), timeout=120)
                    dest.run(["docker", "cp", f"{request.container}:{safety_container}", safety_host], timeout=120)
                    dest.run(["chmod", "600", safety_host])
                    backup_stat = sftp_dest.stat(safety_host)
                    if backup_stat.st_size <= 0:
                        raise WorkflowError("Safety backup is empty; refusing to restore")
                    safety_saved = True
                    logs.append(f"Safety backup saved on destination: {safety_host} ({backup_stat.st_size:,} bytes)")

                    # Never pass --create. Replaces objects in the existing database.
                    restore_output = dest.run(docker_args(
                        request.container,
                        "pg_restore", "-U", request.database_user, "-d", request.database,
                        "--clean", "--if-exists", "--no-owner", "--no-acl",
                        "--single-transaction", "--exit-on-error", staged_container,
                    ), timeout=3600)
                    restore_succeeded = True
                    logs.append("pg_restore completed successfully in a single transaction")
                    if restore_output:
                        logs.append(restore_output[-1000:])
                finally:
                    # Remove staging files even if validation or restore fails.
                    for args in (
                        docker_args(request.container, "rm", "-f", staged_container, safety_container),
                        ["rm", "-f", staged_host],
                    ):
                        try:
                            dest.run(args, timeout=30)
                        except Exception:
                            pass
                    sftp_dest.close()
        except Exception as exc:
            recovery = f" Safety backup (if created): {safety_host}" if safety_saved else " No verified safety backup was saved."
            raise WorkflowError(f"Restore not completed. {type(exc).__name__}: {exc}.{recovery}") from exc
    return {
        "ok": restore_succeeded,
        "job_id": job_id,
        "database": request.database,
        "destination": request.destination.host,
        "backup_filename": request.backup_filename,
        "source_bytes": source_bytes,
        "sha256": local_sha256.hexdigest(),
        "safety_backup_on_destination": safety_host,
        "logs": logs,
        "note": "Verify application tables and row counts before resuming writes. Objects absent from the dump are not automatically deleted.",
    }
