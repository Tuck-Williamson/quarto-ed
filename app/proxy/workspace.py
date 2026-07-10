"""Workspace lifecycle: clone/pull (load), sync (commit/merge/push), download
uncommitted changes, and discard."""
import asyncio
import io
import os
import re
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select, update

from .. import sandbox
from ..database import decrypt_token, get_db_session
from ..models import Session, User
from ..schemas import SyncRequest, WorkspaceLoadRequest
from . import _core
from ._core import (
    _AUTOSAVE_BRANCH,
    _get_default_branch,
    _preview_processes,
    _redact,
    _setup_autosave_branch,
    _validate_github_name,
    kill_quarto_preview,
    require_session,
    require_workspace,
)

# _WORKSPACE_BASE is referenced via _core (not by value) so tests that
# monkeypatch it take effect here.

router = APIRouter()


@router.post("/api/workspace/load")
async def load_workspace(body: WorkspaceLoadRequest, sess: Session = Depends(require_session)):
    _validate_github_name(body.repo_owner, "repo_owner")
    _validate_github_name(body.repo_name, "repo_name")

    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()

    access_token = decrypt_token(user.access_token_encrypted)
    sandbox.ensure_workspace_owned(os.path.join(_core._WORKSPACE_BASE, user.username), user.id)
    workspace_path = os.path.join(_core._WORKSPACE_BASE, user.username, body.repo_name)
    os.makedirs(workspace_path, exist_ok=True)

    if os.path.isdir(os.path.join(workspace_path, ".git")):
        # Switch to main before pulling so we always update the primary branch.
        # If the checkout fails (dirty working tree on autosave branch between
        # syncs), skip the pull and continue on the current branch.
        chk = await asyncio.create_subprocess_exec(
            "git", "-C", workspace_path, "checkout", "main",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await chk.wait()

        if chk.returncode == 0:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", workspace_path, "pull",
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                stderr_str = _redact(stderr.decode(), access_token)
                if "would be overwritten" in stderr_str or "Please commit" in stderr_str:
                    stat = await asyncio.create_subprocess_exec(
                        "git", "-C", workspace_path, "status", "--porcelain",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    )
                    stat_out, _ = await stat.communicate()
                    files = [ln[3:] for ln in stat_out.decode().splitlines() if ln.strip()]
                    raise HTTPException(
                        status_code=409,
                        detail={"type": "local_changes", "files": files},
                    )
                raise HTTPException(status_code=400, detail=f"git pull failed: {stderr_str}")

        await _setup_autosave_branch(workspace_path)
    else:
        clone_url = (
            f"https://oauth2:{access_token}@github.com/"
            f"{body.repo_owner}/{body.repo_name}.git"
        )
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", clone_url, workspace_path,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(status_code=400, detail=f"git clone failed: {_redact(stderr.decode(), access_token)}")

        for cmd in [
            ["git", "-C", workspace_path, "config", "user.email",
             f"{user.username}@users.noreply.github.com"],
            ["git", "-C", workspace_path, "config", "user.name", user.username],
        ]:
            p = await asyncio.create_subprocess_exec(*cmd)
            await p.wait()

        await _setup_autosave_branch(workspace_path)

    async with get_db_session() as db:
        await db.execute(
            update(Session)
            .where(Session.id == sess.id)
            .values(
                workspace_path=workspace_path,
                repo_owner=body.repo_owner,
                repo_name=body.repo_name,
            )
        )
        await db.commit()

    if sess.user_id in _preview_processes:
        await kill_quarto_preview(sess.user_id)

    return {"workspace_path": workspace_path}


@router.post("/api/workspace/sync")
async def sync_workspace(body: SyncRequest, sess: Session = Depends(require_workspace)):
    path = sess.workspace_path
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    async def _git(*args: str, allow_nothing_to_commit: bool = False) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        combined = stdout + stderr
        if proc.returncode != 0:
            if allow_nothing_to_commit and b"nothing to commit" in combined:
                return combined
            msg = re.sub(
                r'https?://[^@\s]*@', 'https://***@',
                combined.decode(errors='replace'),
            )
            raise HTTPException(
                status_code=500,
                detail=f"git {args[0]} failed: {msg}",
            )
        return combined

    default_branch = await _get_default_branch(path)

    # 1. Stage and commit everything to the autosave branch.
    await _git("add", "-A")
    await _git("commit", "-m", body.message, allow_nothing_to_commit=True)

    # 2. Push the autosave branch so it survives dyno restarts.
    await _git("push", "origin", _AUTOSAVE_BRANCH)

    # 3. Fetch all remotes, bring local default branch up to date, then merge autosave → default.
    await _git("fetch", "origin")
    await _git("checkout", default_branch)
    # Fast-forward local default branch to match remote (no-op if already current).
    ff_proc = await asyncio.create_subprocess_exec(
        "git", "-C", path, "merge", "--ff-only", f"origin/{default_branch}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env,
    )
    await ff_proc.wait()  # non-zero means local/remote diverged; push will surface it

    merge_proc = await asyncio.create_subprocess_exec(
        "git", "-C", path, "merge", "--no-ff", _AUTOSAVE_BRANCH,
        "-m", f"Sync: {body.message}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    m_out, m_err = await merge_proc.communicate()
    if merge_proc.returncode != 0:
        abort = await asyncio.create_subprocess_exec(
            "git", "-C", path, "merge", "--abort",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env,
        )
        await abort.wait()
        await _git("checkout", _AUTOSAVE_BRANCH)
        raise HTTPException(
            status_code=409,
            detail={
                "type": "merge_conflict",
                "message": (m_out + m_err).decode(errors="replace"),
            },
        )

    # 4. Push default branch and return to the autosave branch.
    try:
        await _git("push", "origin", default_branch)
    finally:
        await _git("checkout", _AUTOSAVE_BRANCH)

    return {"status": "ok"}


@router.get("/api/workspace/changes.zip")
async def download_local_changes(sess: Session = Depends(require_workspace)):
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", sess.workspace_path, "status", "--porcelain",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    files = [ln[3:].strip() for ln in out.decode().splitlines() if ln.strip()]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            full = os.path.join(sess.workspace_path, rel)
            if os.path.isfile(full):
                zf.write(full, rel)
    buf.seek(0)

    repo = os.path.basename(sess.workspace_path)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{repo}-local-changes.zip"'},
    )


@router.post("/api/workspace/discard")
async def discard_local_changes(sess: Session = Depends(require_workspace)):
    path = sess.workspace_path
    branch_proc = await asyncio.create_subprocess_exec(
        "git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    branch_out, _ = await branch_proc.communicate()
    branch = branch_out.decode().strip() or "main"

    for cmd in [
        ["git", "-C", path, "fetch", "origin"],
        ["git", "-C", path, "reset", "--hard", f"origin/{branch}"],
        ["git", "-C", path, "clean", "-fd"],
    ]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    return {"status": "ok"}
