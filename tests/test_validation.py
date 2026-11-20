"""Reject mistakes before connecting to a server or modifying a database."""
import pytest
from pydantic import ValidationError

from app import config
from app.models import (BrowseDirectoryRequest, CredentialCreate, DestinationSettings,
                        ProfileRestoreRequest, SSHServer, ServerProfile, SourceSelection)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "2001:db8::1", "localhost", "vps.example.com", "vps.example.com."])
def test_valid_ssh_addresses(host):
    assert SSHServer(host=host).host == host


@pytest.mark.parametrize("host", ["999.1.1.1", "192.168.001.1", "example.com:2222", "2001:::1", "vps..example.com",
                                 "vps-.example.com", "-vps.example.com", "a" * 64 + ".example.com",
                                 "https://vps.example.com", "vps.example.com/path", "vps example.com"])
def test_invalid_ssh_addresses_have_helpful_errors(host):
    with pytest.raises(ValidationError) as error:
        SSHServer(host=host)
    assert error.value.errors()[0]["loc"] == ("host",)


@pytest.mark.parametrize("port", [True, False, 0, 65536, 22.5, "abc"])
def test_invalid_ssh_ports(port):
    with pytest.raises(ValidationError) as error:
        SSHServer(host="example.com", port=port)
    assert error.value.errors()[0]["loc"] == ("port",)


@pytest.mark.parametrize("path", ["/backups\nwrong", "/backups\rwrong", "/backups\twrong", "/backups\x00wrong", "/backups\x7fwrong"])
def test_remote_directory_validation_is_consistent(path):
    for model, data in [
        (SourceSelection, {"source": {"host": "example.com"}, "backup_directory": path}),
        (ServerProfile, {"name": "Storage", "ssh": {"host": "example.com"}, "backup_directory": path}),
        (DestinationSettings, {"container": "postgres", "database": "app_db", "database_user": "app_user", "safety_backup_directory": path}),
        (BrowseDirectoryRequest, {"server": {"host": "example.com"}, "path": path}),
    ]:
        with pytest.raises(ValidationError, match="absolute Linux directory"):
            model.model_validate(data)


@pytest.mark.parametrize("field", ["key_path", "known_hosts_path"])
def test_local_ssh_paths_reject_control_characters(field):
    with pytest.raises(ValidationError) as error:
        SSHServer(host="example.com", **{field: "key\nwrong"})
    assert error.value.errors()[0]["loc"] == (field,)


def test_display_names_reject_control_characters():
    with pytest.raises(ValidationError, match="profile name without control"):
        ServerProfile(name="Storage\nwrong", ssh=SSHServer(host="example.com"), backup_directory="/backups")
    with pytest.raises(ValidationError, match="key name without control"):
        CredentialCreate(name="Key\nwrong")


def restore_selection(**changes):
    return {"source_profile_id": "source", "destination_profile_id": "target", "source_profile_revision": "revision",
            "destination_profile_revision": "revision", "backup_filename": "db.dump", "confirmation": "app_db",
            "writes_paused": True, "agree_replace": True, **changes}


@pytest.mark.parametrize("field", ["writes_paused", "agree_replace"])
@pytest.mark.parametrize("value", ["true", "yes", 1])
def test_restore_confirmations_require_json_booleans(field, value):
    with pytest.raises(ValidationError) as error:
        ProfileRestoreRequest.model_validate(restore_selection(**{field: value}))
    assert error.value.errors()[0]["loc"] == (field,)


def test_profile_restore_validates_filename_before_resolving_profiles():
    with pytest.raises(ValidationError) as error:
        ProfileRestoreRequest.model_validate(restore_selection(backup_filename="../../outside.dump"))
    assert error.value.errors()[0]["loc"] == ("backup_filename",)


@pytest.mark.parametrize("value", ["0", "-1", "", "wrong", "2.5"])
def test_invalid_dump_size_configuration_is_actionable(monkeypatch, value):
    monkeypatch.setenv("MAX_DUMP_MB", value)
    with pytest.raises(ValueError, match="MAX_DUMP_MB must be a positive whole number in .env"):
        config.positive_integer_setting("MAX_DUMP_MB", "2048")


def test_dump_size_configuration_uses_valid_default(monkeypatch):
    monkeypatch.delenv("MAX_DUMP_MB", raising=False)
    assert config.positive_integer_setting("MAX_DUMP_MB", "2048") == 2048


def test_state_path_rejects_control_characters_without_echoing_setting(monkeypatch):
    value = "private-folder\nwrong"
    monkeypatch.setenv("PROFILE_DB_PATH", value)
    with pytest.raises(ValueError, match="PROFILE_DB_PATH must not contain control characters") as error:
        config.state_path("PROFILE_DB_PATH", "state/profiles.sqlite3")
    assert value not in str(error.value)
