# Backup Restore Fleet

A local FastAPI dashboard for managing PostgreSQL restore configurations across multiple Linux VPS servers. Add, edit, and delete saved server profiles in the UI. Select any configured backup storage profile and any configured destination profile for each restore. There are no preconfigured VPS addresses or two-server limit in the UI.

The supported restore target is an **existing PostgreSQL database inside a running Docker container**. Backup storage must contain PostgreSQL custom-format `.dump` archives. The app selects existing backups; it does not configure scheduled backup generation.

## Process

1. Configure the controller's admin token and SSH access, then start the app.
2. Enter the token and click **Connect & load saved servers**.
3. Create a **backup storage** profile: name, host, SSH port, SSH user, controller SSH key path, and remote backup folder.
4. Create a **destination** profile: name, host, SSH settings, Docker container, existing database, PostgreSQL role, and safety backup folder on that VPS.
5. Repeat for all your VPS servers. A profile can have both roles. Use separate profiles for different databases or folders on the same server.
6. Select a backup storage profile, click **Load backups**, and choose a `.dump` archive.
7. Select a destination profile and click **Test destination connection**.
8. Pause applications and scheduled jobs that write to the destination. Check both confirmations, type the exact database name, and start the restore.
9. Verify the restored database and application before resuming writes.

Example: one storage VPS can serve backups to staging, testing, and production VPS profiles. Alternatively, give every VPS its own storage and destination profiles. Each operation restores one selected archive into one selected database; there is no bulk destructive restore.

```text
Backup storage A ──SSH/SFTP──> Controller ──SSH/SFTP──> Destination X
Backup storage B ──SSH/SFTP──>            ──SSH/SFTP──> Destination Y
Backup storage C ──SSH/SFTP──>            ──SSH/SFTP──> Destination Z
```

The controller connects independently to each selected server. No server-to-server SSH connection is required. The same VPS can be used for both roles.

## Install and run

Requirements: Python 3.11+, Linux VPS servers reachable over SSH from the controller, and PostgreSQL tools (`psql`, `pg_dump`, `pg_restore`) inside each destination container. Use a compatible `pg_restore` version for the archives.

Windows PowerShell, from this project folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
notepad .env
```

Set `APP_TOKEN` to the generated token, then run:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
# Edit .env and set APP_TOKEN.
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. Keep one app worker. For a remote controller, keep the app bound to localhost and access it through an SSH tunnel.

## SSH setup for each VPS

The UI saves connection configuration; it does not install SSH keys or automatically trust unknown hosts.

1. Create or choose a private SSH key on the controller. Install only its public key in the VPS account's `~/.ssh/authorized_keys` through your trusted console or existing SSH client.
2. Verify the VPS host-key fingerprint through a trusted console, then connect from the controller with the same host and port you will enter in the profile. Accept the key only after comparing fingerprints. Nonstandard ports use `[host]:port` known_hosts entries.
3. Enter that private key's **controller file path** in the profile, for example `~/.ssh/staging_ed25519` or `C:\Users\you\.ssh\staging_ed25519`.
4. For an encrypted key, add its passphrase to a controller environment variable or `.env`, e.g. `STAGING_SSH_PASSPHRASE=...`, and enter only `STAGING_SSH_PASSPHRASE` in the profile's passphrase-variable field. Restart the app after changing `.env`.

Example trusted-console fingerprint command:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Example controller connection (replace the host, account, key and port):

```bash
ssh -p 2222 -i ~/.ssh/staging_ed25519 backup-admin@staging.example.com
```

Each destination SSH account needs permission to run Docker and write its safety backup folder. Each backup storage account needs read access to its archive folder and files. Check firewalls and the actual SSH port if a connection times out. Docker access grants substantial control of the host; use trusted administrative accounts.

The PostgreSQL role must be able to dump and replace the target objects. Configure PostgreSQL authentication inside the container, such as an appropriate password file. No database password is collected in the UI. Host database ports are not used: PostgreSQL commands run through `docker exec` over SSH.

## Saved settings

- Profiles are stored in `state/profiles.sqlite3`, configurable with `PROFILE_DB_PATH`. Back up this database to preserve the fleet configuration.
- Profiles store names, connection settings, controller key paths, passphrase **variable names**, backup paths, and database/container settings. They do not store private key contents or passphrases.
- Per-profile key and known_hosts paths override `SOURCE_SSH_KEY_PATH`, `DEST_SSH_KEY_PATH`, and `SSH_KNOWN_HOSTS_PATH`. Blank paths use those environment defaults.
- A blank passphrase-variable field uses the role's legacy `SOURCE_SSH_KEY_PASSPHRASE` or `DEST_SSH_KEY_PASSPHRASE` default. A named but missing variable causes a connection error.
- A destination's safety folder overrides `DEST_SAFETY_BACKUP_DIR`; a blank field uses that default.
- Keep `.env`, profiles, keys and archives private. On Windows, protect their containing folders with your account's filesystem permissions.

## Restore behavior

1. Resolve the selected saved profiles and check that they have the requested roles. Refuse a restore if either profile has changed since the browser loaded it.
2. Download the selected regular `.dump` file to a restricted controller temporary file, enforcing `MAX_DUMP_MB` and calculating SHA-256.
3. Connect to the destination, verify the database, upload the archive, and inspect its table of contents with `pg_restore -l`.
4. Export the current destination database with `pg_dump -Fc`, inspect its table of contents, and copy it to the destination's configured safety folder. Refuse replacement if that step fails.
5. Run `pg_restore --clean --if-exists --no-owner --no-acl --single-transaction --exit-on-error` against the existing database. The app does not pass `--create` or drop the database.
6. Remove temporary files, retain the safety archive, and return its destination path and the source SHA-256 digest.

**Restores replace the current objects and data represented in the archive.** Objects absent from the archive may remain, and external dependencies can prevent replacement. A table-of-contents check is not a full data-integrity test. Arrange downtime and test recovery on an isolated database before production use.

Changing the selected source clears the archive selection. Changing the destination or archive clears confirmations. Reloading or editing saved profiles requires loading backups again. The API repeats destructive-confirmation validation and shares the existing one-operation restore lock.

If the browser disconnects, inspect the destination and safety archive before retrying. The restore is synchronous; closing a browser does not reliably cancel it. A controller crash can leave remote staging files in `/tmp/backup-restore-stage-*` or `/tmp/backup-restore-before-*`.

## Recovery

The result includes `safety_backup_on_destination`. Use that exact archive path on the destination host after checking it and pausing writes. Replace all example values below with your profile's settings:

```bash
docker cp /YOUR_SAFETY_FOLDER/YOUR_SAFETY_BACKUP.dump YOUR_CONTAINER:/tmp/recovery.dump
docker exec -u 0 YOUR_CONTAINER pg_restore -l /tmp/recovery.dump
# Only when replacement is intended and application writes are paused:
docker exec -u 0 YOUR_CONTAINER pg_restore -U YOUR_DB_ROLE -d YOUR_DB --clean --if-exists --no-owner --no-acl --single-transaction --exit-on-error /tmp/recovery.dump
docker exec -u 0 YOUR_CONTAINER rm -f /tmp/recovery.dump
```

## API and validation

All API routes require `X-Admin-Token`:

| Route | Purpose |
|---|---|
| `GET /api/profiles` | List saved profiles and configuration revisions |
| `POST /api/profiles` | Create a profile |
| `PUT /api/profiles/{id}` | Replace a profile's configuration |
| `DELETE /api/profiles/{id}` | Delete configuration only, without touching a VPS |
| `POST /api/profile-backups` | List archives using `source_profile_id` |
| `POST /api/profile-preflight` | Test using `destination_profile_id` |
| `POST /api/profile-restore` | Restore using both IDs, both profile revisions, filename and explicit confirmations |

Legacy `/api/backups`, `/api/preflight`, and `/api/restore` routes remain available for callers supplying full configuration directly.

Development checks:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
node --check app/static/app.js
```

This remains a trusted, local administrative tool with a single admin token. It has no public hosting hardening, role-based permissions, background job queue, durable operation history, backup scheduling, retention management, row merging, or PITR. Multiple saved VPS profiles do not remove those operational limits.
