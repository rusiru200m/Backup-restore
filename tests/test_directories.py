import posixpath
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import workflow
from app.main import app


@pytest.fixture
def browser_api(monkeypatch):
    monkeypatch.setenv("APP_TOKEN", "test-admin-token-with-at-least-24-characters")
    remote = MagicMock()
    remote.__enter__.return_value = remote
    sftp = remote.sftp.return_value.__enter__.return_value
    sftp.normalize.side_effect = lambda path: "/root" if path == "." else posixpath.normpath(path)
    sftp.stat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR)
    sftp.listdir_iter.side_effect = lambda *args, **kwargs: iter([])
    connection = MagicMock(return_value=remote)
    monkeypatch.setattr(workflow, "connect_server", connection)
    api = TestClient(app, headers={"X-Admin-Token": "test-admin-token-with-at-least-24-characters"})
    return api, sftp, connection, remote


def entry(name, mode=stat.S_IFDIR, size=0):
    return SimpleNamespace(filename=name, st_mode=mode, st_size=size)


def test_browse_uses_selected_unsaved_server_and_lists_folders_first(browser_api):
    api, sftp, connection, remote = browser_api
    sftp.listdir_iter.side_effect = lambda *args, **kwargs: iter([
        entry("app.dump", stat.S_IFREG, 123), entry("Zebra"), entry("back ups"),
        entry(".hidden"), entry("notes.txt", stat.S_IFREG), entry("archive-link", stat.S_IFLNK),
        entry("."), entry(".."), entry("bad/name"), entry("bad\x00name"),
    ])
    server = {"host": "unsaved.example.com", "port": 2222, "username": "backup",
              "credential_id": "a" * 32}
    response = api.post("/api/ssh/directories", json={"server": server, "path": "/srv/backups"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["path"] == "/srv/backups"
    assert result["home"] == "/root"
    assert result["parent"] == "/srv"
    assert [item["name"] for item in result["items"]] == [".hidden", "back ups", "Zebra", "app.dump"]
    assert result["items"][1]["path"] == "/srv/backups/back ups"
    assert result["items"][-1]["bytes"] == 123
    selected, role = connection.call_args.args
    assert selected.host == server["host"]
    assert selected.credential_id == server["credential_id"]
    assert role == "SOURCE"
    assert response.headers["cache-control"] == "no-store"
    remote.run.assert_not_called()
    sftp.mkdir.assert_not_called()
    sftp.open.assert_not_called()


def test_browse_defaults_to_home_and_supports_root_and_destination_keys(browser_api):
    api, sftp, connection, remote = browser_api
    server = {"host": "target.example.com"}
    response = api.post("/api/ssh/directories", json={"server": server, "role": "DEST"})
    assert response.status_code == 200
    assert response.json()["path"] == "/root"
    assert connection.call_args.args[1] == "DEST"
    response = api.post("/api/ssh/directories", json={"server": server, "path": "/"})
    assert response.status_code == 200
    assert response.json()["parent"] is None
    assert response.json()["items"] == []


@pytest.mark.parametrize("path", ["relative/folder", "/srv/../etc", "/srv/\x00folder", "a" * 1025])
def test_invalid_browse_paths_fail_before_ssh(browser_api, path):
    api, sftp, connection, remote = browser_api
    response = api.post("/api/ssh/directories", json={"server": {"host": "example.com"}, "path": path})
    assert response.status_code == 422
    connection.assert_not_called()


def test_browse_requires_admin_token(browser_api):
    api, sftp, connection, remote = browser_api
    response = TestClient(app).post("/api/ssh/directories", json={"server": {"host": "example.com"}})
    assert response.status_code == 401
    connection.assert_not_called()


def test_browse_permission_error_returns_clear_failure(browser_api):
    api, sftp, connection, remote = browser_api
    sftp.listdir_iter.side_effect = PermissionError("Folder permission denied")
    response = api.post("/api/ssh/directories", json={"server": {"host": "example.com"}, "path": "/private"})
    assert response.status_code == 400
    assert "permission denied" in response.json()["detail"]


def test_browse_cannot_select_a_file_as_folder(browser_api):
    api, sftp, connection, remote = browser_api
    sftp.stat.return_value = SimpleNamespace(st_mode=stat.S_IFREG)
    response = api.post("/api/ssh/directories", json={"server": {"host": "example.com"}, "path": "/archive.dump"})
    assert response.status_code == 400
    sftp.listdir_iter.assert_not_called()


def test_large_folder_listing_is_sorted_and_bounded(browser_api):
    api, sftp, connection, remote = browser_api
    sftp.listdir_iter.side_effect = lambda *args, **kwargs: (entry(f"folder-{index:04d}") for index in range(700, -1, -1))
    response = api.post("/api/ssh/directories", json={"server": {"host": "example.com"}})
    assert response.status_code == 200
    result = response.json()
    assert result["truncated"] is True
    assert len(result["items"]) == 500
    assert result["items"][0]["name"] == "folder-0000"
    assert result["items"][-1]["name"] == "folder-0499"
