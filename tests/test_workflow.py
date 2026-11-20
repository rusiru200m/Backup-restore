import io
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import workflow
from app.models import RestoreRequest


@pytest.fixture
def restore_setup(monkeypatch):
    monkeypatch.setenv("MAX_DUMP_MB", "2048")
    monkeypatch.setenv("DEST_SAFETY_BACKUP_DIR", "/srv/safety")
    request = RestoreRequest(
        source={"host": "archive.example.com"}, backup_directory="/archives",
        destination={"host": "app.example.com"}, container="postgres", database="app_db",
        database_user="app_user", backup_filename="app.dump", confirmation="app_db",
        writes_paused=True, agree_replace=True,
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
        if "SELECT current_database()" in args:
            return "app_db"
        if "pg_restore" in args and "-l" in args:
            return "; TOC Entries: 12"
        return ""

    destination.run.side_effect = run
    source_connection = MagicMock(return_value=source)
    destination_connection = MagicMock(return_value=destination)
    monkeypatch.setattr(workflow, "connect_source", source_connection)
    monkeypatch.setattr(workflow, "connect_destination", destination_connection)
    return SimpleNamespace(
        request=request, source=source, source_sftp=source_sftp,
        destination=destination, commands=commands, run=run,
        source_connection=source_connection, destination_connection=destination_connection,
    )


def test_workflow_error_metadata_is_optional():
    error = workflow.WorkflowError("Existing message")
    assert str(error) == "Existing message"
    assert error.stage is None
    assert error.safety_backup_on_destination is None
    assert error.restore_completed is False


@pytest.mark.parametrize("value", ["", "0", "-1", "many", "1.5"])
def test_restore_rejects_invalid_dump_limit_before_ssh(restore_setup, monkeypatch, value):
    monkeypatch.setenv("MAX_DUMP_MB", value)
    with pytest.raises(workflow.WorkflowError, match="MAX_DUMP_MB") as caught:
        workflow.restore(restore_setup.request)
    assert caught.value.stage == "configuration"
    assert caught.value.restore_completed is False
    restore_setup.source_connection.assert_not_called()
    restore_setup.destination_connection.assert_not_called()


@pytest.mark.parametrize("value", ["", "relative", "/srv/../unsafe", "/srv/unsafe\n"])
def test_restore_rejects_invalid_safety_directory_before_ssh(restore_setup, monkeypatch, value):
    monkeypatch.setenv("DEST_SAFETY_BACKUP_DIR", value)
    with pytest.raises(workflow.WorkflowError, match="DEST_SAFETY_BACKUP_DIR") as caught:
        workflow.restore(restore_setup.request)
    assert caught.value.stage == "configuration"
    restore_setup.source_connection.assert_not_called()
    restore_setup.destination_connection.assert_not_called()


@pytest.mark.parametrize("stage", [
    "source_download", "destination_connection", "destination_check", "archive_upload",
    "archive_validation", "safety_backup_creation", "safety_backup_verification", "restoring",
])
def test_restore_reports_original_failure_stage_and_verified_recovery_copy(restore_setup, stage):
    setup = restore_setup
    original = workflow.RemoteError("Remote diagnostics containing password=do-not-display")

    def fail_at_stage(args, **kwargs):
        fails = (
            (stage == "destination_check" and "SELECT current_database()" in args)
            or (stage == "archive_validation" and "pg_restore" in args and "-l" in args and "stage" in args[-1])
            or (stage == "safety_backup_creation" and "pg_dump" in args)
            or (stage == "safety_backup_verification" and "pg_restore" in args and "-l" in args and "before" in args[-1])
            or (stage == "restoring" and "--clean" in args)
        )
        if fails:
            raise original
        return setup.run(args, **kwargs)

    setup.destination.run.side_effect = fail_at_stage
    if stage == "source_download":
        setup.source_sftp.lstat.side_effect = original
    elif stage == "destination_connection":
        setup.destination.__enter__.side_effect = original
    elif stage == "archive_upload":
        setup.destination.sftp.return_value.put.side_effect = original

    with pytest.raises(workflow.WorkflowError, match="Restore not completed") as caught:
        workflow.restore(setup.request)
    error = caught.value
    assert error.__cause__ is original
    assert error.stage == stage
    assert error.restore_completed is False
    assert "password" not in str(error)
    assert "do-not-display" not in str(error)
    if stage == "restoring":
        assert error.safety_backup_on_destination.startswith("/srv/safety/")
        assert "Verified safety backup" in str(error)
    else:
        assert error.safety_backup_on_destination is None
        assert "No verified safety backup" in str(error)
        assert not any("--clean" in args for args in setup.commands)


def test_empty_safety_copy_blocks_restore_and_is_not_advertised_for_recovery(restore_setup):
    setup = restore_setup
    setup.destination.sftp.return_value.stat.return_value.st_size = 0
    with pytest.raises(workflow.WorkflowError) as caught:
        workflow.restore(setup.request)
    assert caught.value.stage == "safety_backup_verification"
    assert caught.value.safety_backup_on_destination is None
    assert not any("--clean" in args for args in setup.commands)


@pytest.mark.parametrize("failure", ["restore_channel_close", "staging_cleanup", "sftp_close", "ssh_close", "local_cleanup"])
def test_successful_restore_is_reported_as_completed_when_cleanup_fails(restore_setup, monkeypatch, failure):
    setup = restore_setup
    original = OSError("Connection closed during cleanup")
    if failure == "restore_channel_close":
        original = workflow.RemoteError("Remote command completed, but channel cleanup failed.", command_completed=True)

        def run(args, **kwargs):
            result = setup.run(args, **kwargs)
            if "--clean" in args:
                raise original
            return result

        setup.destination.run.side_effect = run
    elif failure == "staging_cleanup":
        def run(args, **kwargs):
            if args[:2] == ["rm", "-f"]:
                raise original
            return setup.run(args, **kwargs)
        setup.destination.run.side_effect = run
    elif failure == "sftp_close":
        setup.destination.sftp.return_value.close.side_effect = original
    elif failure == "ssh_close":
        setup.destination.__exit__.side_effect = original
    else:
        temporary_directory = workflow.tempfile.TemporaryDirectory

        class FailedLocalCleanup(temporary_directory):
            def __exit__(self, *args):
                super().__exit__(*args)
                raise original

        monkeypatch.setattr(workflow.tempfile, "TemporaryDirectory", FailedLocalCleanup)

    with pytest.raises(workflow.WorkflowError) as caught:
        workflow.restore(setup.request)
    error = caught.value
    assert error.__cause__ is original
    assert error.stage == "cleanup"
    assert error.restore_completed is True
    assert error.safety_backup_on_destination.startswith("/srv/safety/")
    assert "Restore completed successfully" in str(error)
    assert "Restore not completed" not in str(error)
    assert "Verify the destination database" in str(error)
    assert sum("--clean" in args for args in setup.commands) == 1
    setup.destination.sftp.return_value.close.assert_called_once()


def test_cleanup_failure_preserves_original_restore_failure(restore_setup):
    setup = restore_setup
    original = workflow.RemoteError("pg_restore failed")

    def run(args, **kwargs):
        if "--clean" in args:
            raise original
        if "rm" in args:
            raise OSError("Cleanup failed too")
        return setup.run(args, **kwargs)

    setup.destination.run.side_effect = run
    setup.destination.sftp.return_value.close.side_effect = OSError("SFTP close also failed")
    with pytest.raises(workflow.WorkflowError) as caught:
        workflow.restore(setup.request)
    assert caught.value.__cause__ is original
    assert caught.value.stage == "restoring"
    assert caught.value.restore_completed is False
    assert caught.value.safety_backup_on_destination.startswith("/srv/safety/")
    setup.destination.sftp.return_value.close.assert_called_once()


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
