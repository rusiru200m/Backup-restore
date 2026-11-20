"""Strict, minimal request validation. No shell strings are accepted."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from pydantic import BaseModel, Field, field_validator, model_validator

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.dump$")


def absolute_directory(value: str) -> str:
    if not value.startswith("/") or "\x00" in value or ".." in PurePosixPath(value).parts or len(value) > 1024:
        raise ValueError("Use an absolute Linux directory without .. (maximum 1024 characters)")
    return value


class SSHServer(BaseModel):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str = "root"
    key_path: str | None = Field(default=None, max_length=1024)
    known_hosts_path: str | None = Field(default=None, max_length=1024)
    passphrase_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @field_validator("key_path", "known_hosts_path")
    @classmethod
    def validate_local_path(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("Enter a controller file path or leave it unset")
        return value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not HOST_RE.fullmatch(value) or value.startswith("-"):
            raise ValueError("Use an IP address or hostname, without ssh:// or spaces")
        return value

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        if not USER_RE.fullmatch(value):
            raise ValueError("Invalid SSH username")
        return value


class SourceSelection(BaseModel):
    source: SSHServer
    backup_directory: str

    @field_validator("backup_directory")
    @classmethod
    def validate_directory(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("Backup directory must be an absolute Linux path")
        if ".." in PurePosixPath(value).parts:
            raise ValueError("Backup directory must not contain ..")
        if len(value) > 1024:
            raise ValueError("Backup directory path too long")
        return value


class DestinationSettings(BaseModel):
    container: str
    database: str
    database_user: str
    safety_backup_directory: str | None = None

    @field_validator("safety_backup_directory")
    @classmethod
    def validate_safety_directory(cls, value: str | None) -> str | None:
        return absolute_directory(value) if value is not None else None

    @field_validator("container", "database", "database_user")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not NAME_RE.fullmatch(value):
            raise ValueError("Names must contain only letters, numbers, dots, underscores or hyphens")
        return value


class PreflightRequest(DestinationSettings):
    destination: SSHServer


class ServerProfile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    ssh: SSHServer
    backup_directory: str | None = None
    destination: DestinationSettings | None = None

    @field_validator("name")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a profile name")
        return value.strip()

    @field_validator("backup_directory")
    @classmethod
    def validate_backup_directory(cls, value: str | None) -> str | None:
        return absolute_directory(value) if value is not None else None

    @model_validator(mode="after")
    def require_role(self) -> "ServerProfile":
        if self.backup_directory is None and self.destination is None:
            raise ValueError("Configure backup storage, a destination database, or both")
        return self


class ProfileSourceRequest(BaseModel):
    source_profile_id: str


class ProfileDestinationRequest(BaseModel):
    destination_profile_id: str


class ProfileRestoreRequest(ProfileSourceRequest, ProfileDestinationRequest):
    source_profile_revision: str
    destination_profile_revision: str
    backup_filename: str
    confirmation: str
    writes_paused: bool = False
    agree_replace: bool = False


class RestoreRequest(SourceSelection, PreflightRequest):
    backup_filename: str
    confirmation: str
    writes_paused: bool = False
    agree_replace: bool = False

    @field_validator("backup_filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        if not FILE_RE.fullmatch(value) or value in (".", ".."):
            raise ValueError("Select one .dump file from the backup directory")
        return value

    @model_validator(mode="after")
    def explicit_confirmation(self) -> "RestoreRequest":
        if not self.agree_replace:
            raise ValueError("Confirm that replacing existing backup objects is intended")
        if not self.writes_paused:
            raise ValueError("Pause all application writes before restoring")
        if self.confirmation != self.database:
            raise ValueError("Type the destination database name exactly to confirm")
        return self
