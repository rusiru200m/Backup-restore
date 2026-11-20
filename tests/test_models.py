import pytest
from pydantic import ValidationError

from app.models import RestoreRequest, SourceSelection, SSHServer


def good_payload():
    return {
        "source": {"host": "167.172.77.230", "port": 22, "username": "root"},
        "backup_directory": "/srv/backups/postgresql/daily",
        "destination": {"host": "165.232.165.85", "port": 2222, "username": "root"},
        "container": "intern",
        "database": "200m-intern",
        "database_user": "intern_db",
        "backup_filename": "200m-intern_2026-11-20_02-00-01.dump",
        "confirmation": "200m-intern",
        "writes_paused": True,
        "agree_replace": True,
    }


def test_valid_restore():
    request = RestoreRequest.model_validate(good_payload())
    assert request.database == "200m-intern"
    assert request.destination.port == 2222


@pytest.mark.parametrize("change", [
    {"confirmation": "200m-inter"},
    {"writes_paused": False},
    {"agree_replace": False},
    {"backup_filename": "../../etc/passwd"},
    {"backup_filename": "evil.dump;touch BAD"},
    {"container": "intern; rm -rf /"},
    {"database": "db name"},
    {"backup_directory": "/srv/backups/../secrets"},
    {"backup_directory": "relative/daily"},
])
def test_rejects_unsafe_restore(change):
    data = good_payload()
    data.update(change)
    with pytest.raises(ValidationError):
        RestoreRequest.model_validate(data)


def test_rejects_invalid_ssh_host():
    with pytest.raises(ValidationError):
        SSHServer(host="ssh://example.com", port=22, username="root")


def test_accepts_directory_with_spaces_without_shell_execution():
    result = SourceSelection(source=SSHServer(host="167.172.77.230"), backup_directory="/srv/our backups/daily")
    assert result.backup_directory.endswith("/daily")
