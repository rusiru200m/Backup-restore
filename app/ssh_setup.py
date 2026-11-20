"""Controller-managed encrypted SSH keys and explicit server enrollment.

Only public metadata leaves this module. An existing VPS password is used once,
after host verification, to append the selected public key without replacing keys.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import posixpath
import socket
import stat
import subprocess
import threading
import uuid
from pathlib import Path

import paramiko
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .config import state_path
from .models import SSHServer

_lock = threading.RLock()


def state_directory() -> Path:
    root = state_path("SSH_STATE_DIR", "state/managed-ssh")
    with _lock:
        if not root.exists():
            root.mkdir(parents=True, mode=0o700)
            if os.name == "nt":
                # Windows ignores POSIX mode bits. Remove inherited ACL entries and
                # grant this controller account access to the directory and children.
                account = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
                try:
                    subprocess.run(["icacls", str(root), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"],
                                   check=True, capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError) as exc:
                    # Never write secrets when the directory couldn't be secured.
                    root.rmdir()
                    raise ValueError("Could not restrict SSH storage permissions for the controller account") from exc
            else:
                root.chmod(0o700)
    return root


def write_private(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def cipher(root: Path) -> Fernet:
    key_path = root / "master.key"
    with _lock:
        if not key_path.exists():
            # Do not silently replace a lost encryption key for existing credentials.
            if any(root.glob("*.json")):
                raise ValueError("Managed SSH encryption key is missing. Restore the complete SSH storage backup.")
            write_private(key_path, Fernet.generate_key())
        return Fernet(key_path.read_bytes())


def credential_path(credential_id: str) -> Path:
    if len(credential_id) != 32 or any(c not in "0123456789abcdef" for c in credential_id):
        raise ValueError("Invalid managed SSH key ID")
    return state_directory() / f"{credential_id}.json"


def public_metadata(record: dict) -> dict:
    return {key: record[key] for key in ("id", "name", "public_key", "fingerprint")}


def list_credentials() -> list[dict]:
    with _lock:
        return [public_metadata(json.loads(path.read_text(encoding="utf-8")))
                for path in sorted(state_directory().glob("*.json"))]


def credential_record(credential_id: str) -> dict:
    path = credential_path(credential_id)
    if not path.is_file():
        raise ValueError("Managed SSH key not found. Load keys and select an existing key.")
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def create_credential(name: str) -> dict:
    root = state_directory()
    credential_id = uuid.uuid4().hex
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    parsed = paramiko.Ed25519Key.from_private_key(io.StringIO(private.decode()))
    with _lock:
        record = {"id": credential_id, "name": name,
                  "public_key": f"{public} backup-restore-{credential_id}",
                  "fingerprint": fingerprint(parsed),
                  "encrypted_private_key": cipher(root).encrypt(private).decode()}
        write_private(root / f"{credential_id}.json", json.dumps(record).encode())
    return public_metadata(record)


def load_private_key(credential_id: str) -> paramiko.Ed25519Key:
    with _lock:
        record = credential_record(credential_id)
        try:
            private = cipher(state_directory()).decrypt(record["encrypted_private_key"].encode())
            return paramiko.Ed25519Key.from_private_key(io.StringIO(private.decode()))
        except Exception as exc:
            raise ValueError("Cannot unlock managed SSH key. Restore the correct controller encryption key and credential files.") from exc


def known_hosts_path() -> str:
    return str(state_directory() / "known_hosts")


def scan_key(server: SSHServer) -> paramiko.PKey:
    # This handshake discovers an identity; it does not authenticate it or send a password.
    with socket.create_connection((server.host, server.port), timeout=12) as sock:
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=15)
            if not transport.is_active():
                raise ValueError("SSH handshake did not complete")
            return transport.get_remote_server_key()
        finally:
            transport.close()


def scan_host(server: SSHServer) -> dict:
    key = scan_key(server)
    return {"host": server.host, "port": server.port, "key_type": key.get_name(),
            "fingerprint": fingerprint(key), "verified": False}


def host_entry(server: SSHServer) -> str:
    return server.host if server.port == 22 else f"[{server.host}]:{server.port}"


def trust_host(server: SSHServer, expected_fingerprint: str) -> dict:
    key = scan_key(server)
    if fingerprint(key) != expected_fingerprint:
        raise ValueError("Server fingerprint does not match. No host key was saved.")
    with _lock:
        path = Path(known_hosts_path())
        hosts = paramiko.HostKeys()
        if path.exists():
            hosts.load(str(path))
        existing = hosts.lookup(host_entry(server))
        if existing and not hosts.check(host_entry(server), key):
            raise ValueError("This server already has a different trusted identity. Investigate the change before updating known_hosts.")
        hosts.add(host_entry(server), key.get_name(), key)
        # Publish atomically so a concurrent connection never sees a partial file.
        temp = path.with_name(f"known_hosts-{uuid.uuid4().hex}.tmp")
        try:
            write_private(temp, b"")
            hosts.save(str(temp))
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return {"ok": True, "host": server.host, "port": server.port, "fingerprint": fingerprint(key)}


def authorize_key(server: SSHServer, password: str) -> dict:
    record = credential_record(server.credential_id)
    client = paramiko.SSHClient()
    try:
        if not Path(known_hosts_path()).exists():
            raise ValueError("Verify and trust the server fingerprint first")
        client.load_host_keys(known_hosts_path())
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(hostname=server.host, port=server.port, username=server.username,
                           password=password, look_for_keys=False, allow_agent=False,
                           timeout=12, auth_timeout=15, banner_timeout=15)
        except Exception as exc:
            raise ValueError("SSH enrollment login failed. Check the verified host identity, username and current VPS password. The server must allow SSH password login; otherwise use the public-key console instructions.") from exc
        with _lock, client.open_sftp() as sftp:
            sftp.get_channel().settimeout(30)
            home = sftp.normalize(".")
            directory = posixpath.join(home, ".ssh")
            authorized = posixpath.join(directory, "authorized_keys")
            try:
                attrs = sftp.lstat(directory)
            except FileNotFoundError:
                sftp.mkdir(directory, mode=0o700)
                attrs = sftp.lstat(directory)
            if not stat.S_ISDIR(attrs.st_mode or 0):
                raise ValueError("Account .ssh path is not a regular directory; use the VPS console")
            sftp.chmod(directory, 0o700)
            existing = b""
            try:
                attrs = sftp.lstat(authorized)
            except FileNotFoundError:
                attrs = None
            if attrs is not None:
                if not stat.S_ISREG(attrs.st_mode or 0) or attrs.st_size > 1024 * 1024:
                    raise ValueError("Account authorized_keys is not a regular small file; use the VPS console")
                with sftp.open(authorized, "rb") as file:
                    existing = file.read(1024 * 1024 + 1)
                if len(existing) > 1024 * 1024:
                    raise ValueError("authorized_keys is too large to update")
            public = record["public_key"]
            # Exact-line check avoids treating a restricted key as an unrestricted login.
            if public not in existing.decode("utf-8", errors="replace").splitlines():
                if attrs is None:
                    # Create with exclusive mode, then restrict before writing public data.
                    with sftp.open(authorized, "wx"):
                        pass
                sftp.chmod(authorized, 0o600)
                with sftp.open(authorized, "a") as file:
                    file.write(("\n" if existing and not existing.endswith(b"\n") else "") + public + "\n")
            sftp.chmod(authorized, 0o600)
    finally:
        client.close()
    return {"ok": True, "message": "Public key authorized for the selected SSH account. Test SSH access next."}
