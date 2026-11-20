"""Strict, minimal request validation. No shell strings are accepted."""
from __future__ import annotations

import re
from ipaddress import ip_address
from pathlib import PurePosixPath
from pydantic import BaseModel, Field, SecretStr, StrictBool, field_validator, model_validator

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")
HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}\.dump$")


def absolute_directory(value: str) -> str:
    if (not value.startswith("/") or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or ".." in PurePosixPath(value).parts or len(value) > 1024):
        raise ValueError("Use an absolute Linux directory without .. or control characters (maximum 1024 characters)")
    return value


def validated_backup_filename(value: str) -> str:
    if not FILE_RE.fullmatch(value):
        raise ValueError("Select a .dump filename using letters, numbers, dots, underscores or hyphens; do not enter a path")
    return value


class SSHServer(BaseModel):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str = "root"
    credential_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    key_path: str | None = Field(default=None, max_length=1024)
    known_hosts_path: str | None = Field(default=None, max_length=1024)
    passphrase_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @model_validator(mode="after")
    def one_credential_mode(self) -> "SSHServer":
        if self.credential_id and (self.key_path or self.known_hosts_path or self.passphrase_env):
            raise ValueError("Choose a managed SSH key or advanced file settings, not both")
        return self

    @field_validator("key_path", "known_hosts_path")
    @classmethod
    def validate_local_path(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(character) < 32 or ord(character) == 127 for character in value)):
            raise ValueError("Enter a controller file path without control characters, or leave it unset")
        return value

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        try:
            ip_address(value)
            if "%" not in value:
                return value
        except ValueError:
            pass
        if not HOST_RE.fullmatch(value) or value.startswith("-"):
            raise ValueError("Use an IP address or hostname, without ssh:// or spaces")
        if ":" in value or ("." in value and all(character.isdigit() or character == "." for character in value)):
            raise ValueError("Enter a valid IPv4 or IPv6 address; enter the SSH port in its separate field")
        if any(not HOST_LABEL_RE.fullmatch(label) for label in value.removesuffix(".").split(".")):
            raise ValueError("Enter a valid hostname with labels of 1–63 letters, numbers or hyphens; labels cannot start or end with a hyphen")
        return value

    @field_validator("port", mode="before")
    @classmethod
    def validate_port(cls, value):
        if isinstance(value, bool):
            raise ValueError("Enter an SSH port number between 1 and 65535")
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
        return absolute_directory(value)


class CredentialCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a key name")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Enter a key name without control characters")
        return value.strip()


class BrowseDirectoryRequest(BaseModel):
    server: SSHServer
    path: str | None = None
    role: str = Field(default="SOURCE", pattern=r"^(SOURCE|DEST)$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return absolute_directory(value) if value is not None else None


class DiscoverDatabasesRequest(BaseModel):
    server: SSHServer
    database_user: str | None = None
    database: str | None = None

    @field_validator("database_user", "database")
    @classmethod
    def validate_hint(cls, value: str | None) -> str | None:
        if value is not None and not NAME_RE.fullmatch(value):
            raise ValueError("Use letters, numbers, dots, underscores or hyphens for discovery hints")
        return value


class HostTrustRequest(BaseModel):
    server: SSHServer
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")


class KeyAuthorizeRequest(BaseModel):
    server: SSHServer
    password: SecretStr

    @model_validator(mode="after")
    def require_managed_key(self) -> "KeyAuthorizeRequest":
        if not self.server.credential_id:
            raise ValueError("Select a generated SSH key first")
        if not self.password.get_secret_value():
            raise ValueError("Enter the current VPS SSH login password")
        return self


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
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Enter a profile name without control characters")
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
    source_profile_id: str = Field(min_length=1, max_length=128)


class ProfileDestinationRequest(BaseModel):
    destination_profile_id: str = Field(min_length=1, max_length=128)


class ProfileRestoreRequest(ProfileSourceRequest, ProfileDestinationRequest):
    source_profile_revision: str = Field(min_length=1, max_length=128)
    destination_profile_revision: str = Field(min_length=1, max_length=128)
    backup_filename: str
    confirmation: str = Field(max_length=128)
    writes_paused: StrictBool = False
    agree_replace: StrictBool = False

    @field_validator("backup_filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        return validated_backup_filename(value)


class RestoreRequest(SourceSelection, PreflightRequest):
    backup_filename: str
    confirmation: str = Field(max_length=128)
    writes_paused: StrictBool = False
    agree_replace: StrictBool = False

    @field_validator("backup_filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        return validated_backup_filename(value)

    @model_validator(mode="after")
    def explicit_confirmation(self) -> "RestoreRequest":
        if not self.agree_replace:
            raise ValueError("Confirm that replacing existing backup objects is intended")
        if not self.writes_paused:
            raise ValueError("Pause all application writes before restoring")
        if self.confirmation != self.database:
            raise ValueError("Type the destination database name exactly to confirm")
        return self
