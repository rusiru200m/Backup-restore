import io
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import workflow
from app.models import RestoreRequest


@pytest.mark.parametrize("safety_fails", [False, True])
def test_restore_saves_safety_to_selected_folder_before_replacing(monkeypatch, safety_fails):
    request = RestoreRequest(
        source={"host": "archive.example.com"}, backup_directory="/archives",
        destination={"host": "app.example.com"}, container="postgres", database="app_db",
        database_user="app_user", safety_backup_directory="/srv/selected-safety",
        backup_filename="app.dump", confirmation="app_db", writes_paused=True, agree_replace=True,
    )
    source = MagicMock()
    source.__enter__.return_value = source
    source_sftp = source.sftp.return_value.__enter__.return_value
    source_sftp.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFREG, st_size=7)
    source_sftp.open.return_value = io.BytesIO(b"PGDMPxx")
    destination = MagicMock()
    destination.__enter__.return_value = destination
    destination.sftp.return_value.stat.return_value = SimpleNamespace(st_size=32)
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if "pg_dump" in args and safety_fails:
            raise workflow.RemoteError("Safety dump failed")
        if "SELECT current_database()" in args:
            return "app_db"
        if "pg_restore" in args and "-l" in args:
            return "; TOC Entries: 12"
        return ""

    destination.run.side_effect = run
    monkeypatch.setattr(workflow, "connect_source", lambda _: source)
    monkeypatch.setattr(workflow, "connect_destination", lambda _: destination)
    if safety_fails:
        with pytest.raises(workflow.WorkflowError, match="No verified safety backup"):
            workflow.restore(request)
        assert not any("--clean" in args for args in commands)
    else:
        result = workflow.restore(request)
        assert result["ok"] is True
        assert result["safety_backup_on_destination"].startswith("/srv/selected-safety/")
        safety_copy = next(i for i, args in enumerate(commands) if args[:2] == ["docker", "cp"] and args[-1] == result["safety_backup_on_destination"])
        replacement = next(i for i, args in enumerate(commands) if "--clean" in args)
        assert safety_copy < replacement
        assert "--single-transaction" in commands[replacement]
    # Staging cleanup still runs in either case.
    assert any(args[:2] == ["rm", "-f"] for args in commands)
    assert ["mkdir", "-p", "-m", "700", "/srv/selected-safety"] in commands
    assert ["chmod", "700", "/srv/selected-safety"] not in commands
