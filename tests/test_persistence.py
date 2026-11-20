import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config, profiles, ssh_setup
from app.models import SSHServer, ServerProfile

SOURCE_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("custom_paths", [False, True])
def test_profiles_and_managed_keys_survive_cwd_change_and_process_restart(monkeypatch, tmp_path, custom_paths):
    project_root = tmp_path / "controller"
    project_root.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    if custom_paths:
        monkeypatch.setenv("PROFILE_DB_PATH", "saved/profiles.sqlite3")
        monkeypatch.setenv("SSH_STATE_DIR", "saved/keys")
    else:
        monkeypatch.delenv("PROFILE_DB_PATH", raising=False)
        monkeypatch.delenv("SSH_STATE_DIR", raising=False)
    db_path = project_root / ("saved/profiles.sqlite3" if custom_paths else "state/profiles.sqlite3")
    key_root = project_root / ("saved/keys" if custom_paths else "state/managed-ssh")
    # Storage permissions are covered by test_ssh_setup; use an existing temporary
    # directory here to avoid changing Windows ACLs in this path-resolution test.
    key_root.mkdir(parents=True)
    first_launch = tmp_path / "first-launch"
    second_launch = tmp_path / "second-launch"
    first_launch.mkdir()
    second_launch.mkdir()
    monkeypatch.chdir(first_launch)
    credential = ssh_setup.create_credential("Saved storage login")
    profile = ServerProfile(name="Saved storage", ssh=SSHServer(host="storage.example.com", credential_id=credential["id"]),
                            backup_directory="/srv/backups")
    profile_id = profiles.save_profile(profile)
    assert db_path.is_file()

    monkeypatch.chdir(second_launch)
    # Each profile read opens a fresh SQLite connection.
    assert profiles.get_profile(profile_id) == profile
    assert [item["id"] for item in profiles.list_profiles()] == [profile_id]
    assert ssh_setup.credential_path(credential["id"]) == key_root / f"{credential['id']}.json"
    assert ssh_setup.fingerprint(ssh_setup.load_private_key(credential["id"])) == credential["fingerprint"]
    assert Path(ssh_setup.known_hosts_path()) == key_root / "known_hosts"

    script = """
import json
import sys
from pathlib import Path
from app import config, profiles, ssh_setup
config.PROJECT_ROOT = Path(sys.argv[1])
profile = profiles.get_profile(sys.argv[2])
key = ssh_setup.load_private_key(profile.ssh.credential_id)
print(json.dumps({"name": profile.name, "host": profile.ssh.host,
                  "fingerprint": ssh_setup.fingerprint(key),
                  "credentials": [item["id"] for item in ssh_setup.list_credentials()]}))
"""
    environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
    completed = subprocess.run([sys.executable, "-c", script, str(project_root), profile_id],
                               cwd=second_launch, env=environment, capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == {"name": profile.name, "host": profile.ssh.host,
                                          "fingerprint": credential["fingerprint"], "credentials": [credential["id"]]}
    assert list(first_launch.iterdir()) == []
    assert list(second_launch.iterdir()) == []


@pytest.mark.parametrize("name,default", [("PROFILE_DB_PATH", "state/profiles.sqlite3"),
                                         ("SSH_STATE_DIR", "state/managed-ssh")])
def test_absolute_state_paths_are_preserved(monkeypatch, tmp_path, name, default):
    configured = tmp_path / "external-storage"
    monkeypatch.setenv(name, str(configured))
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "different-project")
    assert config.state_path(name, default) == configured


def test_state_path_expands_home_before_resolving(monkeypatch, tmp_path):
    monkeypatch.setenv("PROFILE_DB_PATH", "~/controller/profiles.sqlite3")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "project")
    assert config.state_path("PROFILE_DB_PATH", "unused") == Path.home() / "controller/profiles.sqlite3"


@pytest.mark.parametrize("name", ["PROFILE_DB_PATH", "SSH_STATE_DIR"])
@pytest.mark.parametrize("value", ["", "   "])
def test_blank_state_paths_are_rejected(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=f"{name} must be a nonempty"):
        config.state_path(name, "unused")


@pytest.mark.parametrize("process_override", [False, True])
def test_startup_loads_project_dotenv_from_another_cwd_without_overriding_environment(tmp_path, process_override):
    project_root = tmp_path / "controller"
    launch_directory = tmp_path / "launch"
    project_root.mkdir()
    launch_directory.mkdir()
    (project_root / ".env").write_text("APP_TOKEN=project-token\nPROFILE_DB_PATH=saved/profiles.sqlite3\nSSH_STATE_DIR=saved/keys\n")
    (launch_directory / ".env").write_text("APP_TOKEN=wrong-directory-token\nPROFILE_DB_PATH=wrong.sqlite3\n")
    environment = {**os.environ, "PYTHONPATH": str(SOURCE_ROOT)}
    for name in ("APP_TOKEN", "PROFILE_DB_PATH", "SSH_STATE_DIR"):
        environment.pop(name, None)
    if process_override:
        environment["APP_TOKEN"] = "process-token"
    script = """
import json
import os
import sys
from pathlib import Path
from app import config
config.PROJECT_ROOT = Path(sys.argv[1])
from app import main
print(json.dumps({name: os.environ.get(name) for name in ("APP_TOKEN", "PROFILE_DB_PATH", "SSH_STATE_DIR")}))
"""
    completed = subprocess.run([sys.executable, "-c", script, str(project_root)], cwd=launch_directory,
                               env=environment, capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == {"APP_TOKEN": "process-token" if process_override else "project-token",
                                          "PROFILE_DB_PATH": "saved/profiles.sqlite3", "SSH_STATE_DIR": "saved/keys"}
