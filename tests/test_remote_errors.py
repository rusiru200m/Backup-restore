from unittest.mock import MagicMock

import pytest

from app.models import SSHServer
from app import remote as remote_module
from app.remote import RemoteError, SSHRemote


def connected_remote():
    remote = SSHRemote(SSHServer(host="example.com"), "key", "known-hosts")
    remote.client = MagicMock()
    channel = remote.client.get_transport.return_value.open_session.return_value
    channel.recv_ready.return_value = False
    channel.recv_stderr_ready.return_value = False
    channel.exit_status_ready.return_value = True
    channel.recv_exit_status.return_value = 0
    return remote, channel


def test_command_failure_is_not_hidden_by_channel_close_failure():
    remote, channel = connected_remote()
    channel.recv_exit_status.return_value = 7
    channel.close.side_effect = OSError("Channel close failed")
    with pytest.raises(RemoteError, match="exit 7") as caught:
        remote.run(["docker", "ps"])
    assert caught.value.command_completed is False
    channel.close.assert_called_once()


def test_command_start_failure_is_preserved_and_channel_is_closed():
    remote, channel = connected_remote()
    original = OSError("Connection reset")
    channel.exec_command.side_effect = original
    channel.close.side_effect = OSError("Channel close failed")
    with pytest.raises(OSError) as caught:
        remote.run(["docker", "ps"])
    assert caught.value is original
    channel.close.assert_called_once()


def test_channel_close_failure_after_success_remains_visible():
    remote, channel = connected_remote()
    original = OSError("Channel close failed")
    channel.close.side_effect = original
    with pytest.raises(RemoteError, match="command completed") as caught:
        remote.run(["docker", "ps"])
    assert caught.value.command_completed is True
    assert caught.value.__cause__ is original


def test_ssh_close_does_not_hide_existing_workflow_failure():
    remote, _ = connected_remote()
    client = remote.client
    original = RemoteError("Database command failed")
    client.close.side_effect = OSError("SSH close failed")
    assert remote.__exit__(RemoteError, original, None) is None
    assert remote.client is None
    client.close.assert_called_once()


def test_ssh_close_failure_after_success_remains_visible():
    remote, _ = connected_remote()
    client = remote.client
    client.close.side_effect = OSError("SSH close failed")
    with pytest.raises(OSError, match="SSH close failed"):
        remote.__exit__(None, None, None)
    assert remote.client is None


def test_failed_authentication_is_not_hidden_by_ssh_close_failure(monkeypatch):
    monkeypatch.setattr(remote_module.Path, "is_file", lambda _: True)
    client = MagicMock()
    original = remote_module.paramiko.AuthenticationException("SSH key was rejected")
    client.connect.side_effect = original
    client.close.side_effect = OSError("SSH close failed")
    monkeypatch.setattr(remote_module.paramiko, "SSHClient", lambda: client)
    remote = SSHRemote(SSHServer(host="example.com"), "key", "known-hosts")
    with pytest.raises(RemoteError) as caught:
        remote.__enter__()
    assert caught.value.__cause__ is original
    assert remote.client is None
    client.close.assert_called_once()
