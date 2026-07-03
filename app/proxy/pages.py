"""Version/health endpoint and the editor SPA page."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from .. import __version__
from ..database import get_db_session
from ..models import Session, User, UserAIConfig
from ._core import (
    _ANTHROPIC_KEY,
    _GIT_SHA,
    _QUARTO_VERSION,
    _TERMINAL_SANDBOX_WARNING,
    current_session,
    templates,
)

router = APIRouter()


@router.get("/api/version")
async def get_version():
    return {
        "version": __version__,
        "git_sha": _GIT_SHA,
        "quarto_version": _QUARTO_VERSION,
    }


@router.get("/editor", response_class=HTMLResponse)
async def editor(request: Request, sess: Session | None = Depends(current_session)):
    if not sess:
        return RedirectResponse("/login")
    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()
        ai_cfg_result = await db.execute(
            select(UserAIConfig).where(UserAIConfig.user_id == sess.user_id)
        )
        ai_cfg = ai_cfg_result.scalar_one_or_none()
    user_has_key = bool(
        ai_cfg and (
            (ai_cfg.provider == "claude" and ai_cfg.api_key_encrypted) or
            ai_cfg.provider == "ollama"
        )
    )
    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "username": user.username if user else "",
            "has_workspace": bool(sess.workspace_path),
            "repo_owner": sess.repo_owner or "",
            "repo_name": sess.repo_name or "",
            "ai_enabled": bool(_ANTHROPIC_KEY or user_has_key),
            "quarto_version": _QUARTO_VERSION,
            "app_version": __version__,
            "git_sha": _GIT_SHA,
            "terminal_sandbox_warning": _TERMINAL_SANDBOX_WARNING,
        },
    )
