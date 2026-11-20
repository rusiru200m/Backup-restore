import json
import os
import socket
import threading
from pathlib import Path
from unittest.mock import MagicMock

import paramiko
import pytest
from fastapi.testclient import TestClient

from app import ssh_setup, workflow
from app.main import app
from app.models import SSHServer


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_TOKEN", "test-admin-token-with-at-least-24-characters")
    monkeypatch.setenv("SSH_STATE_DIR", str(tmp_path / "managed-ssh"))
    monkeypatch.setenv("PROFILE_DB_PATH", str(tmp_path / "profiles.sqlite3"))
    return TestClient(app, headers={"X-Admin-Token": "test-admin-token-with-at-least-24-characters"})


def generated(api):
    response = api.post("/api/ssh/credentials", json={"name": "Test server"})
    assert response.status_code == 201, response.text
    return response.json()


def test_generated_key_encrypted_reusable_and_public_only(api):
    public = generated(api)
    assert set(public) == {"id", "name", "public_key", "fingerprint"}
    assert public["public_key"].startswith("ssh-ed25519 ")
    assert public["fingerprint"].startswith("SHA256:")
    private = ssh_setup.load_private_key(public["id"])
    assert ssh_setup.fingerprint(private) == public["fingerprint"]
    signature = private.sign_ssh_data(b"authentication-test")
    public_key = paramiko.Ed25519Key(data=private.asbytes())
    assert public_key.verify_ssh_sig(b"authentication-test", paramiko.Message(signature.asbytes()))
    path = ssh_setup.credential_path(public["id"])
    record = json.loads(path.read_text())
    assert "PRIVATE KEY" not in path.read_text()
    assert record["encrypted_private_key"]
    response = api.get("/api/ssh/credentials")
    assert response.json() == {"items": [public]}
    assert "encrypted_private_key" not in response.text
    assert response.headers["cache-control"] == "no-store"
    # New reads load the encrypted file, not an in-memory credential cache.
    assert ssh_setup.load_private_key(public["id"]).get_base64() == private.get_base64()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_missing_master_key_is_not_silently_replaced(api):
    public = generated(api)
    master = ssh_setup.state_directory() / "master.key"
    master.unlink()
    with pytest.raises(ValueError, match="Cannot unlock"):
        ssh_setup.load_private_key(public["id"])
    response = api.post("/api/ssh/credentials", json={"name": "Another"})
    assert response.status_code == 400
    assert not master.exists()


def test_managed_key_is_used_without_legacy_env_settings(api, monkeypatch):
    public = generated(api)
    monkeypatch.delenv("SOURCE_SSH_KEY_PATH", raising=False)
    monkeypatch.delenv("DEST_SSH_KEY_PATH", raising=False)
    server = SSHServer(host="example.com", credential_id=public["id"])
    remote = workflow.connect_source(server)
    assert remote.key_path is None
    assert ssh_setup.fingerprint(remote.pkey) == public["fingerprint"]
    assert remote.known_hosts == ssh_setup.known_hosts_path()
    assert workflow.connect_destination(server).pkey.get_base64() == remote.pkey.get_base64()


def test_host_requires_matching_verified_identity_and_rejects_rotation(api, monkeypatch):
    public = generated(api)
    key = ssh_setup.load_private_key(public["id"])
    monkeypatch.setattr(ssh_setup, "scan_key", lambda _: key)
    server = {"host": "example.com", "port": 2222}
    scan = api.post("/api/ssh/scan", json=server)
    assert scan.json()["fingerprint"] == public["fingerprint"]
    assert scan.json()["verified"] is False
    assert not Path(ssh_setup.known_hosts_path()).exists()
    bad = api.post("/api/ssh/trust", json={"server": server, "fingerprint": "SHA256:" + "A" * 43})
    assert bad.status_code == 400
    assert not Path(ssh_setup.known_hosts_path()).exists()
    for _ in range(2):
        response = api.post("/api/ssh/trust", json={"server": server, "fingerprint": public["fingerprint"]})
        assert response.status_code == 200, response.text
    hosts = paramiko.HostKeys(ssh_setup.known_hosts_path())
    assert hosts.check("[example.com]:2222", key)
    saved_hosts = Path(ssh_setup.known_hosts_path()).read_bytes()
    for _ in range(2):
        scan = api.post("/api/ssh/scan", json=server)
        assert scan.status_code == 200, scan.text
        assert scan.json()["verified"] is True
        assert scan.json()["fingerprint"] == public["fingerprint"]
        assert Path(ssh_setup.known_hosts_path()).read_bytes() == saved_hosts
    changed = ssh_setup.load_private_key(generated(api)["id"])
    monkeypatch.setattr(ssh_setup, "scan_key", lambda _: changed)
    scan = api.post("/api/ssh/scan", json=server)
    assert scan.status_code == 400
    assert scan.json()["error"]["code"] == "SSH_HOST_KEY_MISMATCH"
    assert Path(ssh_setup.known_hosts_path()).read_bytes() == saved_hosts
    response = api.post("/api/ssh/trust", json={"server": server, "fingerprint": ssh_setup.fingerprint(changed)})
    assert response.status_code == 400
    assert hosts.check("[example.com]:2222", key)
    assert paramiko.HostKeys(ssh_setup.known_hosts_path()).check("[example.com]:2222", key)


@pytest.mark.parametrize("trusted_port", [22, 2222])
def test_scan_trust_is_specific_to_host_and_port(api, monkeypatch, trusted_port):
    public = generated(api)
    key = ssh_setup.load_private_key(public["id"])
    monkeypatch.setattr(ssh_setup, "scan_key", lambda _: key)
    trusted = SSHServer(host="example.com", port=trusted_port)
    ssh_setup.trust_host(trusted, public["fingerprint"])
    path = Path(ssh_setup.known_hosts_path())
    saved_hosts = path.read_bytes()
    for server in (
        SSHServer(host="other.example.com", port=trusted_port),
        SSHServer(host="example.com", port=2222 if trusted_port == 22 else 22),
    ):
        assert ssh_setup.scan_host(server)["verified"] is False
        assert path.read_bytes() == saved_hosts
    assert ssh_setup.scan_host(trusted)["verified"] is True


def test_scan_different_key_type_does_not_replace_trusted_identity(api, monkeypatch):
    public = generated(api)
    original = ssh_setup.load_private_key(public["id"])
    monkeypatch.setattr(ssh_setup, "scan_key", lambda _: original)
    server = SSHServer(host="example.com")
    ssh_setup.trust_host(server, public["fingerprint"])
    path = Path(ssh_setup.known_hosts_path())
    saved_hosts = path.read_bytes()
    changed = paramiko.RSAKey.generate(2048)
    monkeypatch.setattr(ssh_setup, "scan_key", lambda _: changed)
    with pytest.raises(ValueError, match="already has a different trusted identity"):
        ssh_setup.scan_host(server)
    assert path.read_bytes() == saved_hosts
    assert paramiko.HostKeys(str(path)).check("example.com", original)
    assert not paramiko.HostKeys(str(path)).check("example.com", changed)


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/ssh/credentials"), ("POST", "/api/ssh/credentials"),
    ("POST", "/api/ssh/scan"), ("POST", "/api/ssh/trust"),
    ("POST", "/api/ssh/authorize"), ("POST", "/api/ssh/test"),
])
def test_setup_requires_admin_token(api, method, path):
    assert TestClient(app).request(method, path, json={}).status_code == 401


def test_password_validation_does_not_echo_secrets(api):
    secret = "one-time-password-not-for-errors"
    response = api.post("/api/ssh/authorize", json={"server": {"host": "invalid host"}, "password": secret})
    assert response.status_code == 422
    assert secret not in response.text
    response = api.post("/api/ssh/authorize", json={"server": {"host": "example.com"}, "password": secret})
    assert response.status_code == 422
    assert secret not in response.text


def test_invalid_credential_ids_cannot_escape_storage(api):
    with pytest.raises(ValueError, match="Invalid managed"):
        ssh_setup.load_private_key("../outside")
    assert api.post("/api/ssh/test", json={"host": "example.com", "credential_id": "../outside"}).status_code == 422


def test_managed_key_and_advanced_paths_cannot_mix(api):
    response = api.post("/api/profiles", json={"name": "Mixed", "backup_directory": "/backups",
        "ssh": {"host": "example.com", "credential_id": "a" * 32, "key_path": "other-key"}})
    assert response.status_code == 422


class FakeSftp:
    def __init__(self, root):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def normalize(self, path):
        return "/home/test"

    def get_channel(self):
        return MagicMock()

    def local(self, path):
        return self.root / Path(path).relative_to("/home/test")

    def lstat(self, path):
        return self.local(path).lstat()

    def mkdir(self, path, mode):
        self.local(path).mkdir(mode=mode)

    def chmod(self, path, mode):
        self.local(path).chmod(mode)

    def open(self, path, mode):
        return self.local(path).open("x" if mode == "wx" else mode)


@pytest.mark.parametrize("has_existing_key", [True, False])
def test_enrollment_preserves_existing_keys_is_idempotent_and_does_not_save_password(api, monkeypatch, tmp_path, has_existing_key):
    public = generated(api)
    key = ssh_setup.load_private_key(public["id"])
    server = SSHServer(host="example.com", credential_id=public["id"])
    monkeypatch.setattr(ssh_setup, "scan_key", lambda _: key)
    ssh_setup.trust_host(server, public["fingerprint"])
    home = tmp_path / "remote-account"
    home.mkdir()
    authorized = home / ".ssh" / "authorized_keys"
    existing = 'from="10.0.0.1" ssh-ed25519 EXISTING existing-key' if has_existing_key else ""
    if has_existing_key:
        (home / ".ssh").mkdir()
        authorized.write_text(existing)
    client = MagicMock()
    client.open_sftp.return_value = FakeSftp(home)
    monkeypatch.setattr(ssh_setup.paramiko, "SSHClient", lambda: client)
    secret = "temporary-vps-password"
    body = {"server": server.model_dump(), "password": secret}
    for _ in range(2):
        response = api.post("/api/ssh/authorize", json=body)
        assert response.status_code == 200, response.text
        assert secret not in response.text
    assert authorized.read_text() == (existing + "\n" if existing else "") + public["public_key"] + "\n"
    assert client.connect.call_args.kwargs["password"] == secret
    assert client.connect.call_args.kwargs["look_for_keys"] is False
    client.set_missing_host_key_policy.assert_called()
    assert isinstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
    assert all(secret.encode() not in path.read_bytes() for path in ssh_setup.state_directory().iterdir())
    assert client.close.call_count == 2


def test_untrusted_host_never_receives_bootstrap_password(api, monkeypatch):
    public = generated(api)
    client = MagicMock()
    monkeypatch.setattr(ssh_setup.paramiko, "SSHClient", lambda: client)
    response = api.post("/api/ssh/authorize", json={"server": {"host": "example.com", "credential_id": public["id"]}, "password": "one-time-secret"})
    assert response.status_code == 400
    client.connect.assert_not_called()


def test_actual_ssh_handshake_and_key_login_with_pinned_host(api):
    public = generated(api)
    login_key = ssh_setup.load_private_key(public["id"])
    host_key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    errors = []

    class Server(paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return "publickey"

        def check_auth_publickey(self, username, key):
            return paramiko.AUTH_SUCCESSFUL if username == "test" and key == login_key else paramiko.AUTH_FAILED

    def serve():
        # One scan, one trust verification, then one key-authenticated connection.
        for _ in range(3):
            connection, _ = listener.accept()
            transport = paramiko.Transport(connection)
            transport.add_server_key(host_key)
            try:
                transport.start_server(server=Server())
                transport.join(timeout=5)
            except (EOFError, ConnectionResetError):
                pass  # unauthenticated scanning closes after the handshake
            except Exception as exc:
                errors.append(exc)
            finally:
                transport.close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    server = SSHServer(host="127.0.0.1", port=port, username="test", credential_id=public["id"])
    try:
        scanned = ssh_setup.scan_host(server)
        assert scanned["fingerprint"] == ssh_setup.fingerprint(host_key)
        ssh_setup.trust_host(server, scanned["fingerprint"])
        with workflow.connect_source(server) as connection:
            assert connection.client.get_transport().is_authenticated()
    finally:
        listener.close()
        worker.join(timeout=6)
    assert not errors
