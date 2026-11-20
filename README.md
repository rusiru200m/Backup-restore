# Backup Restore Fleet

A local FastAPI dashboard for managing PostgreSQL restore configurations across multiple Linux VPS servers. Add, edit, and delete saved server profiles in the UI. Select any configured backup storage profile and any configured destination profile for each restore. There are no preconfigured VPS addresses or two-server limit in the UI.

The supported restore target is an **existing PostgreSQL database inside a running Docker container**. Backup storage must contain PostgreSQL custom-format `.dump` archives. The app selects existing backups; it does not configure scheduled backup generation.

## Process

1. Configure the controller's admin token, then start the app.
2. Enter the token in **Access settings** and click **Connect & load saved servers**. The access settings collapse after your profiles load.
3. Choose **Manage servers** in the workspace navigation and create a **backup storage** profile: name, host, SSH port, SSH user, and remote backup folder. Expand **SSH access** to use **Generate SSH key** and the built-in SSH setup steps below.
4. Create a **destination** profile: enter its name, host and SSH settings, tick the destination checkbox, and click **Find databases on this VPS**. Choose the destination database from the dropdown; the container, database and role are filled automatically. Configure its safety backup folder and save the profile.
5. Repeat for all your VPS servers. A profile can have both roles. Use separate profiles for different databases or folders on the same server.
6. Return to **Restore database**. In the left **Backup storage & archive** card, select a backup storage profile, click **Load backups**, and choose a `.dump` archive.
7. In the right **Restore destination** card, select a destination profile and click **Test destination connection**.
8. Click **Review restore** below the two cards. Pause applications and scheduled jobs that write to the destination, check both confirmations, type the exact database name, and start the restore.
9. Verify the restored database and application before resuming writes.

The restore workspace keeps registration in the separate **Manage servers** view. Switching views retains your unsaved profile draft and current restore selections. The source and destination cards sit side by side on desktop and stack on smaller screens. Activity details are collapsed until opened. Changing the source, archive or destination clears restore confirmations and closes the review step; the confirmation fields and final replacement action remain required.

The interface uses a charcoal and lime-green theme. Desktop navigation includes a sidebar with actual saved source/destination counts and restore-selection progress. On smaller screens, navigation moves above the workspace. Database replacement actions remain red, and warnings remain amber.

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

### Built-in setup (recommended)

You can generate SSH credentials and install public keys from this dashboard without finding key paths or creating passphrase environment variables.

1. Enter the profile name, VPS host/IP, SSH port, and username.
2. Click **Generate SSH key**. The app creates an Ed25519 key, encrypts its private half on the controller, and selects it. Existing generated keys are available in the key dropdown.
3. Click **Trust this server** once. The app reads its SSH identity and saves it automatically without requiring a fingerprint comparison or console command. It checks the identity again before saving and refuses unexpected changes to an already saved identity. A matching saved server needs no new trust setup on later runs.
4. Enter the account's **current VPS SSH password** and click **Install public key using password**. The app uses that password once over SSH checked against the saved server identity to append the public key to that account's `~/.ssh/authorized_keys`, preserving existing entries. It never saves this password. Repeat installation of the exact same public key is idempotent.
5. Click **Test SSH key access**, configure the storage/destination details, and **Save profile**. Future operations use the generated key and saved server identity automatically.

The app uses **trust on first connection**: the identity received on that first connection is accepted, rather than independently proving it belongs to the intended VPS. Every later connection must match the saved identity; changed identities are blocked and are never silently replaced. This follows the first-use trust approach supported by [OpenSSH](https://man.openbsd.org/ssh_config#StrictHostKeyChecking). No identity is saved merely by opening a profile or running a read-only scan; use **Trust this server** to accept it.

The **server fingerprint** identifies the VPS, whereas the fingerprint in the **SSH key** dropdown identifies your generated login key. They are different keys. For optional independent verification, expand the server fingerprint details, click **Read server fingerprint**, then compare the value through your VPS provider's console or an already trusted SSH session. On that console, list public host-key fingerprints with:

```bash
for key in /etc/ssh/ssh_host_*_key.pub; do
  ssh-keygen -lf "$key" -E sha256
done
```

Match the displayed SHA256 value and key type with one of these entries, then click **Trust this server**. This comparison is optional. The command only reads public host keys; if the SSH service uses custom host-key locations, use those public-key files instead.

For backup and safety-backup folder fields, click **Browse VPS folders** after SSH setup. The picker browses the VPS entered in the current profile, including unsaved profiles. Open folders with a click, use **Server root /**, **Home folder**, **Up one level**, or the folder breadcrumbs to navigate, then click **Select this folder**. The selected remote path fills the field; click **Save profile** to keep it. `.dump` files are shown as a preview; archive selection still happens under **Backup storage & archive**. Browsing only reads directory metadata and does not upload files or modify the VPS. The account needs permission to read each opened folder. Symbolic links are omitted; a maximum of 500 sorted folders/archive entries is shown with a notice if more exist. A new safety folder can still be entered manually and will be created during the restore workflow.

If SSH password login is disabled, open **Public key for VPS console setup** and append the displayed public key through your existing VPS console/SSH session. Then return to test access and save the profile. The app does not enable password login or change SSH daemon configuration. It cannot obtain initial access without an existing authorized login or console access. A host address, username, port and saved server identity are still required.

### Discover destination databases

After SSH setup, tick **This server has a restore destination database** and click **Find databases on this VPS**. The app checks running Docker containers for `pg_restore`, reads only the non-secret `POSTGRES_USER` and `POSTGRES_DB` startup hints, and queries PostgreSQL's database catalog using local container authentication. It never reads or returns `POSTGRES_PASSWORD` or the full Docker environment, changes database permissions, creates databases, or runs a restore during discovery.

The dropdown lists each container/database pair with the actual PostgreSQL login role used for discovery. It excludes templates, databases not accepting connections, and databases for which that role lacks CONNECT privilege. Names unsupported by the current restore validator are skipped with a notice. Select one entry to fill connection settings automatically; save the profile and test the destination before restoring. Discovery does not prove that the login has sufficient dump/drop/create privileges or that every listed database will accept a later connection.

Discovery uses the current, possibly unsaved VPS settings and works with generated keys or existing-key profiles. Changing SSH settings clears old results and connection fields. Saved profiles display their saved destination until you refresh discovery. If a container cannot authenticate or lacks tools, notices explain the limitation while accessible containers still appear. For a custom login, expand **Connection details / manual setup (advanced)**, enter a known role/database and retry, or complete the connection fields manually. Existing credentials must work inside the container. Host-installed PostgreSQL, stopped containers and external database services are outside this Docker restore workflow.

### Existing SSH keys (advanced)

Select the blank key option and expand **Use an existing SSH key (advanced)**. These profiles retain the file-path/environment-variable workflow. Their known_hosts file is separate from the generated-key setup's file.

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

### Reuse servers after restarting

Configure a server once and click **Save profile** after choosing its backup folder or destination database. Testing SSH access, generating a key, browsing a folder, and discovering a database do not save the server profile by themselves.

On later runs, enter the same app token and click **Connect & load saved servers** (or press Enter in the token field). The editor opens your last selected saved profile, or the first saved profile if no previous selection is available. Previously selected backup storage and destination profiles are restored when they still exist. Use **New profile** only to add another server. Backups must be loaded again, and restore confirmations must be entered each time.

Server settings use the existing local SQLite database; no additional database service is needed. The browser remembers only selected profile IDs. The app token stays in page memory, and the one-time VPS login password is never saved. To change a saved server, select it under **Edit a saved profile**, edit its settings, and click **Save profile** again.

- Profiles are stored in `state/profiles.sqlite3`, configurable with `PROFILE_DB_PATH`. Relative profile and SSH storage paths are resolved from the project folder, so changing the launch directory does not create a different set of saved servers. Absolute paths can point to a permanent location outside the project. The app loads `.env` from the project folder; existing operating-system environment variables take precedence.
- Keep the profile database and the complete SSH storage directory when restarting or moving the controller. A fresh clone does not include these private files because `state/` is ignored by Git. Back them up together to preserve server settings and generated SSH access.
- Generated credentials and verified host identities are stored in `state/managed-ssh`, configurable with `SSH_STATE_DIR`. Private keys use authenticated Fernet encryption with an automatically generated controller `master.key`; that file is the local encryption secret. The app restricts the storage directory to the controller account on Windows with `icacls` and uses owner-only directory/file modes on POSIX. The controller account can decrypt its keys; this is local protection, not an external secrets service. Keep the entire directory private and back it up securely together with the profile database. Losing `master.key` makes saved generated keys unusable. Anyone obtaining both it and the credential files can decrypt them.
- The API returns only generated key names, IDs, public keys and fingerprints. Generated-key profiles save a credential ID, with no manually entered key paths or passphrase variables. Deleting a profile leaves its key available for other profiles and does not revoke it from any VPS.
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
| `GET /api/ssh/credentials` | List public metadata of generated keys |
| `POST /api/ssh/credentials` | Generate and encrypt an Ed25519 key using a supplied name |
| `POST /api/ssh/scan` | Read the SSH server fingerprint without sending credentials |
| `POST /api/ssh/trust` | Recheck a read fingerprint and save a new server identity; reject changed saved identities |
| `POST /api/ssh/authorize` | Install the selected public key using the current account password once |
| `POST /api/ssh/test` | Test key authentication and SFTP with current form settings |
| `POST /api/ssh/directories` | Browse a remote directory using current SSH settings; returns folders and .dump filename previews |
| `POST /api/ssh/databases` | Discover accessible PostgreSQL container/database pairs with detected roles; accepts optional known role/database hints |

Legacy `/api/backups`, `/api/preflight`, and `/api/restore` routes remain available for callers supplying full configuration directly.

### Finding and fixing errors

Failed actions show an error summary with the operation, the problem, and a suggested next step. Field errors appear beside their inputs; select a field in the summary to focus it. Correct the highlighted settings and repeat the action. The app validates server addresses, ports, paths, database names, server roles and restore confirmations before remote work begins, and repeats validation on the API.

| Problem | What to check |
|---|---|
| Admin token rejected or not configured | Use the `APP_TOKEN` from the controller's `.env`. Configure a real token of at least 24 characters and restart the server after changing it. |
| SSH connection refused, timed out, or hostname not found | Check the VPS address, separate SSH port, DNS, firewall and SSH service. |
| SSH authentication failed | Check the SSH username, selected key and public-key authorization. During initial enrollment, verify the account password and whether password login is enabled. |
| SSH identity missing or changed | Verify the fingerprint through a trusted VPS console. A changed identity requires investigation before updating trust. |
| Key, folder or saved-profile storage unavailable | Check controller paths and permissions, or the selected VPS folder. Keep the complete managed-key directory and its encryption key together. |
| Docker or PostgreSQL command failed | Check Docker access, the selected running container, PostgreSQL tools, database name, login role and database authentication. |

API failures keep the existing `detail` field and also return an `error` object with a stable `code`, readable `message`, corrective `hint`, field errors and `request_id`. The response's `X-Request-ID` matches the reference shown in the page and the diagnostic entry in the controller's terminal. Use that reference when investigating an unexpected failure. Errors do not echo submitted tokens, passwords or private keys.

Restore failures identify their stage and report the verified safety archive when one exists. A connection failure does not prove that a running restore stopped. Inspect the destination and the reported safety archive before retrying. If restore completed but cleanup failed, verify the database before deciding what to do next.

Development checks:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
node --check app/static/app.js
```

This remains a trusted, local administrative tool with a single admin token. It has no public hosting hardening, role-based permissions, background job queue, durable operation history, backup scheduling, retention management, row merging, or PITR. Multiple saved VPS profiles do not remove those operational limits.
