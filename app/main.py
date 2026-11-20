"""Local-only FastAPI UI. Bind uvicorn to 127.0.0.1, NOT 0.0.0.0."""
from __future__ import annotations

import hmac
import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .models import (PreflightRequest, RestoreRequest, SourceSelection, ServerProfile,
                     ProfileSourceRequest, ProfileDestinationRequest, ProfileRestoreRequest)
from . import profiles
from .remote import RemoteError
from .workflow import WorkflowError, list_backups, preflight, restore

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Backup Restore Starter", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
_restore_lock = threading.Lock()


def require_token(x_admin_token: str | None = Header(default=None)) -> None:
    expected = os.environ.get("APP_TOKEN", "")
    if len(expected) < 24 or expected.startswith("REPLACE_"):
        raise HTTPException(status_code=503, detail="Set a real APP_TOKEN (at least 24 characters) in .env")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token")


def fail_safely(exc: Exception) -> HTTPException:
    # Exception text is returned only to token-authenticated local clients.
    return HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/api/backups", dependencies=[Depends(require_token)])
def backups(request: SourceSelection):
    try:
        return {"items": list_backups(request)}
    except (RemoteError, WorkflowError, OSError, ValueError) as exc:
        raise fail_safely(exc) from exc


@app.post("/api/preflight", dependencies=[Depends(require_token)])
def check_destination(request: PreflightRequest):
    try:
        return preflight(request)
    except (RemoteError, WorkflowError, OSError, ValueError) as exc:
        raise fail_safely(exc) from exc


@app.post("/api/restore", dependencies=[Depends(require_token)])
def run_restore(request: RestoreRequest):
    if not _restore_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another restore is already in progress")
    try:
        return restore(request)
    except (RemoteError, WorkflowError, OSError, ValueError) as exc:
        raise fail_safely(exc) from exc
    finally:
        _restore_lock.release()


def saved_profile(profile_id: str, expected_revision: str | None = None) -> ServerProfile:
    try:
        profile = profiles.get_profile(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Server profile not found") from None
    if expected_revision is not None and profiles.revision(profile) != expected_revision:
        raise HTTPException(status_code=409, detail="Server profile changed. Reload saved servers, reselect the backup and confirm the destination again.")
    return profile


def source_settings(profile_id: str, expected_revision: str | None = None) -> dict:
    profile = saved_profile(profile_id, expected_revision)
    if profile.backup_directory is None:
        raise HTTPException(status_code=422, detail="Selected profile is not configured as backup storage")
    return {"source": profile.ssh, "backup_directory": profile.backup_directory}


def destination_settings(profile_id: str, expected_revision: str | None = None) -> dict:
    profile = saved_profile(profile_id, expected_revision)
    if profile.destination is None:
        raise HTTPException(status_code=422, detail="Selected profile has no destination database")
    return {"destination": profile.ssh, **profile.destination.model_dump()}


@app.get("/api/profiles", dependencies=[Depends(require_token)])
def server_profiles():
    return {"items": profiles.list_profiles()}


@app.post("/api/profiles", dependencies=[Depends(require_token)], status_code=201)
def create_profile(request: ServerProfile):
    return {"id": profiles.save_profile(request)}


@app.put("/api/profiles/{profile_id}", dependencies=[Depends(require_token)])
def update_profile(profile_id: str, request: ServerProfile):
    try:
        return {"id": profiles.save_profile(request, profile_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Server profile not found") from None


@app.delete("/api/profiles/{profile_id}", dependencies=[Depends(require_token)])
def remove_profile(profile_id: str):
    try:
        profiles.delete_profile(profile_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Server profile not found") from None


@app.post("/api/profile-backups", dependencies=[Depends(require_token)])
def profile_backups(request: ProfileSourceRequest):
    return backups(SourceSelection(**source_settings(request.source_profile_id)))


@app.post("/api/profile-preflight", dependencies=[Depends(require_token)])
def profile_preflight(request: ProfileDestinationRequest):
    return check_destination(PreflightRequest(**destination_settings(request.destination_profile_id)))


@app.post("/api/profile-restore", dependencies=[Depends(require_token)])
def profile_restore(request: ProfileRestoreRequest):
    try:
        resolved = RestoreRequest(
            **source_settings(request.source_profile_id, request.source_profile_revision),
            **destination_settings(request.destination_profile_id, request.destination_profile_revision),
            **request.model_dump(exclude={"source_profile_id", "destination_profile_id", "source_profile_revision", "destination_profile_revision"}),
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_context=False, include_input=False)) from exc
    result = run_restore(resolved)
    return {**result, "source_profile_id": request.source_profile_id,
            "destination_profile_id": request.destination_profile_id}
