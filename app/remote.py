"""SSH and SFTP transport: strict host-key checking, no passwords or shell injection."""
from __future__ import annotations

import os
import shlex
import time
from pathlib import Path
from typing import Sequence

try:
    import paramiko
except ImportError:  # allows validation tests before dependencies are installed
    paramiko = None

from .models import SSHServer


class RemoteError(RuntimeError):
    pass


class SSHRemote:
    def __init__(self, server: SSHServer, key_path: str, known_hosts: str, passphrase: str | None = None):
        self.server = server
        self.key_path = str(Path(key_path).expanduser())
        self.known_hosts = str(Path(known_hosts).expanduser())
        self.passphrase = passphrase
        self.client: paramiko.SSHClient | None = None

    def __enter__(self) -> "SSHRemote":
        if paramiko is None:
            raise RemoteError("Paramiko is missing. Run: python -m pip install -r requirements.txt")
        if not Path(self.key_path).is_file():
            raise RemoteError(f"SSH key not found on controller: {self.key_path}")
        if not Path(self.known_hosts).is_file():
            raise RemoteError("Known hosts file missing. Verify the server SSH fingerprint and add it on the controller first.")
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.load_host_keys(self.known_hosts)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self.server.host,
                port=self.server.port,
                username=self.server.username,
                key_filename=self.key_path,
                passphrase=self.passphrase,
                look_for_keys=False,
                allow_agent=False,
                timeout=12,
                auth_timeout=15,
                banner_timeout=15,
            )
        except Exception as exc:
            client.close()
            raise RemoteError(
                f"Cannot SSH to {self.server.host}:{self.server.port} as {self.server.username}: "
                f"{type(exc).__name__}: {exc}. Check port, firewall, host key and SSH key authorization."
            ) from exc
        self.client = client
        return self

    def __exit__(self, *_):
        if self.client:
            self.client.close()
            self.client = None

    def sftp(self) -> paramiko.SFTPClient:
        if not self.client:
            raise RemoteError("Not connected")
        return self.client.open_sftp()

    def run(self, args: Sequence[str], timeout: int = 120) -> str:
        """Execute only an argv list (shell-quoted), with bounded output capture."""
        if not self.client:
            raise RemoteError("Not connected")
        cmd = shlex.join([str(a) for a in args])
        channel = self.client.get_transport().open_session()
        channel.exec_command(cmd)
        start = time.monotonic()
        stdout = bytearray()
        stderr = bytearray()
        max_capture = 32_768
        try:
            while True:
                if channel.recv_ready():
                    data = channel.recv(65536)
                    if data:
                        stdout.extend(data[: max(0, max_capture - len(stdout))])
                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(65536)
                    if data:
                        stderr.extend(data[: max(0, max_capture - len(stderr))])
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() - start > timeout:
                    raise RemoteError(f"Remote command exceeded {timeout}s timeout: {args[0]}")
                time.sleep(0.03)
            code = channel.recv_exit_status()
        finally:
            channel.close()
        output = (stdout + stderr).decode("utf-8", errors="replace").strip()
        if code != 0:
            raise RemoteError(f"Remote command failed (exit {code}): {cmd}\n{output[-4000:]}")
        return output


def docker_args(container: str, *command: str) -> list[str]:
    # -u 0 ensures root inside the container can read temporary files.
    # PostgreSQL still authenticates using the explicitly selected DB role.
    return ["docker", "exec", "-u", "0", container, *command]
