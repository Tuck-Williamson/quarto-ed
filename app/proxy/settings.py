"""Global (settings-repo) settings, per-repo settings, and AI key config."""
import asyncio
import json
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..database import encrypt_token, get_db_session
from ..models import Session, UserAIConfig
from ..schemas import AIKeySaveRequest, RepoSettingsSaveRequest, SettingsSaveRequest
from . import _core
from ._core import (
    _SETTINGS_DEFAULTS,
    _get_user_and_token,
    _load_repo_settings,
    _repo_settings_path,
    _settings_repo_exists_on_github,
    _settings_repo_local_path,
    require_session,
    require_workspace,
)

# _ANTHROPIC_KEY, _ensure_settings_cloned and _push_settings are referenced via
# the _core module (not imported by value) so tests that monkeypatch them on
# _core take effect here.

router = APIRouter()


@router.get("/api/settings/repo/status")
async def settings_repo_status(sess: Session = Depends(require_session)):
    user, token = await _get_user_and_token(sess)
    exists = await _settings_repo_exists_on_github(user.username, token)
    return {"exists": exists}


@router.post("/api/settings/repo/create")
async def create_settings_repo(sess: Session = Depends(require_session)):
    user, token = await _get_user_and_token(sess)
    repo_name = f"{user.username}-quarto-ed-settings"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.github.com/user/repos",
            json={"name": repo_name, "private": True, "auto_init": True},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
    if resp.status_code not in (201, 422):
        raise HTTPException(status_code=502, detail="Failed to create GitHub repo")

    await asyncio.sleep(2)

    local_path = _settings_repo_local_path(user.username)
    if not os.path.isdir(os.path.join(local_path, ".git")):
        cloned = await _core._ensure_settings_cloned(user.id, user.username, token)
        if not cloned:
            raise HTTPException(status_code=502, detail="Failed to clone settings repo")

    for cmd in [
        ["git", "-C", local_path, "config", "user.email",
         f"{user.username}@users.noreply.github.com"],
        ["git", "-C", local_path, "config", "user.name", user.username],
    ]:
        p = await asyncio.create_subprocess_exec(*cmd)
        await p.wait()

    settings_file = os.path.join(local_path, "settings.json")
    snippets_file = os.path.join(local_path, "snippets.json")
    if not os.path.exists(settings_file):
        with open(settings_file, "w") as f:
            json.dump(_SETTINGS_DEFAULTS, f, indent=2)
    if not os.path.exists(snippets_file):
        with open(snippets_file, "w") as f:
            json.dump([], f)

    await _core._push_settings(local_path, "Initialize quarto-ed settings")
    return {"status": "created"}


@router.get("/api/settings")
async def get_settings(sess: Session = Depends(require_session)):
    user, token = await _get_user_and_token(sess)
    local_path = _settings_repo_local_path(user.username)

    if not os.path.isdir(os.path.join(local_path, ".git")):
        cloned = await _core._ensure_settings_cloned(user.id, user.username, token)
        if not cloned:
            repo_settings = _load_repo_settings(sess.workspace_path)
            return {
                "settings": {**_SETTINGS_DEFAULTS, **repo_settings},
                "global_settings": _SETTINGS_DEFAULTS.copy(),
                "repo_settings": repo_settings,
                "snippets": [],
                "repo_exists": False,
                "workspace_loaded": bool(sess.workspace_path),
            }

    global_settings = _SETTINGS_DEFAULTS.copy()
    snippets: list = []
    settings_file = os.path.join(local_path, "settings.json")
    snippets_file = os.path.join(local_path, "snippets.json")
    if os.path.exists(settings_file):
        try:
            with open(settings_file) as f:
                global_settings = {**_SETTINGS_DEFAULTS, **json.load(f)}
        except (json.JSONDecodeError, OSError):
            pass
    if os.path.exists(snippets_file):
        try:
            with open(snippets_file) as f:
                snippets = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    repo_settings = _load_repo_settings(sess.workspace_path)
    effective = {**global_settings, **repo_settings}
    return {
        "settings": effective,
        "global_settings": global_settings,
        "repo_settings": repo_settings,
        "snippets": snippets,
        "repo_exists": True,
        "workspace_loaded": bool(sess.workspace_path),
    }


@router.post("/api/settings")
async def save_settings(body: SettingsSaveRequest, sess: Session = Depends(require_session)):
    user, token = await _get_user_and_token(sess)
    local_path = _settings_repo_local_path(user.username)

    if not os.path.isdir(os.path.join(local_path, ".git")):
        cloned = await _core._ensure_settings_cloned(user.id, user.username, token)
        if not cloned:
            return {"status": "no_repo"}

    settings_file = os.path.join(local_path, "settings.json")
    snippets_file = os.path.join(local_path, "snippets.json")
    with open(settings_file, "w") as f:
        json.dump(body.settings, f, indent=2)
    if body.snippets is not None:
        with open(snippets_file, "w") as f:
            json.dump(body.snippets, f, indent=2)

    await _core._push_settings(local_path)
    return {"status": "ok"}


@router.post("/api/settings/repo")
async def save_repo_settings(body: RepoSettingsSaveRequest, sess: Session = Depends(require_workspace)):
    repo_settings_file = _repo_settings_path(sess.workspace_path)
    clean = {k: v for k, v in body.settings.items() if k in _SETTINGS_DEFAULTS}
    try:
        if clean:
            with open(repo_settings_file, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2)
        elif os.path.exists(repo_settings_file):
            os.remove(repo_settings_file)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save repo settings: {exc}")
    return {"status": "ok"}


@router.get("/api/ai/config")
async def get_ai_config(sess: Session = Depends(require_session)):
    async with get_db_session() as db:
        result = await db.execute(
            select(UserAIConfig).where(UserAIConfig.user_id == sess.user_id)
        )
        cfg = result.scalar_one_or_none()
    has_key = bool(cfg and cfg.api_key_encrypted)
    return {
        "has_key": has_key,
        "server_key_active": bool(_core._ANTHROPIC_KEY) and not has_key,
    }


@router.post("/api/ai/config")
async def save_ai_config(body: AIKeySaveRequest, sess: Session = Depends(require_session)):
    """Save (or clear) the user's Anthropic API key.

    Provider, model, and Ollama endpoint are now user/repo-level settings stored
    in the settings repo — only the secret key is managed here.
    Sending provider='ollama' with no api_key clears any stored Claude key.
    """
    if body.provider not in ("claude", "ollama"):
        raise HTTPException(status_code=400, detail="provider must be 'claude' or 'ollama'")

    encrypted_key: bytes | None = None
    if body.provider == "claude" and body.api_key:
        encrypted_key = encrypt_token(body.api_key)

    async with get_db_session() as db:
        result = await db.execute(
            select(UserAIConfig).where(UserAIConfig.user_id == sess.user_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg:
            if encrypted_key is not None:
                cfg.api_key_encrypted = encrypted_key
            elif body.provider == "ollama":
                cfg.api_key_encrypted = None
        else:
            db.add(UserAIConfig(
                user_id=sess.user_id,
                provider=body.provider,
                api_key_encrypted=encrypted_key,
            ))
        await db.commit()
    return {"status": "ok"}


@router.delete("/api/ai/config")
async def clear_ai_key(sess: Session = Depends(require_session)):
    async with get_db_session() as db:
        result = await db.execute(
            select(UserAIConfig).where(UserAIConfig.user_id == sess.user_id)
        )
        cfg = result.scalar_one_or_none()
        if cfg:
            cfg.api_key_encrypted = None
        await db.commit()
    return {"status": "ok"}
