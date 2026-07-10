"""File browser + read/write/create/delete, and .gitignore management."""
import asyncio
import mimetypes
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ..models import Session
from ..schemas import DeleteFileRequest, FileCreateRequest, FileWriteRequest, GitignoreAddRequest
from ._core import (
    _build_tree,
    _collect_paths,
    _mark_ignored,
    _safe_path,
    require_workspace,
)

router = APIRouter()


@router.get("/api/files")
async def list_files(sess: Session = Depends(require_workspace)):
    tree = _build_tree(sess.workspace_path)

    paths = []
    _collect_paths(tree, paths)
    if paths:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", sess.workspace_path, "check-ignore", "-z", "--stdin",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        input_data = ("\0".join(paths) + "\0").encode("utf-8")
        out, _ = await proc.communicate(input_data)
        ignored = {p for p in out.decode("utf-8").split("\0") if p}
        _mark_ignored(tree, ignored)

    return {"tree": tree}


@router.get("/api/file")
async def read_file(path: str = Query(...), sess: Session = Depends(require_workspace)):
    full_path = _safe_path(sess.workspace_path, path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        with open(full_path, encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Binary file not supported")
    return {"path": path, "content": content}


@router.get("/api/file/raw")
async def read_file_raw(path: str = Query(...), sess: Session = Depends(require_workspace)):
    full_path = _safe_path(sess.workspace_path, path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
    return FileResponse(full_path, media_type=media_type)


@router.post("/api/file")
async def write_file(body: FileWriteRequest, sess: Session = Depends(require_workspace)):
    full_path = _safe_path(sess.workspace_path, body.path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"status": "ok"}


@router.post("/api/file/create")
async def create_file(body: FileCreateRequest, sess: Session = Depends(require_workspace)):
    full_path = _safe_path(sess.workspace_path, body.path)
    if os.path.exists(full_path):
        raise HTTPException(status_code=409, detail="File already exists")
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write("")
    return {"status": "ok"}


@router.post("/api/workspace/gitignore")
async def add_to_gitignore(body: GitignoreAddRequest, sess: Session = Depends(require_workspace)):
    _safe_path(sess.workspace_path, body.path)  # validate, raises on traversal

    rel = body.path.strip("/")
    entry = f"/{rel}" + ("/" if body.is_dir else "")

    gitignore_path = os.path.join(sess.workspace_path, ".gitignore")
    content = ""
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, encoding="utf-8") as f:
            content = f.read()

    added = entry not in content.splitlines()
    if added:
        if content and not content.endswith("\n"):
            content += "\n"
        content += entry + "\n"
        with open(gitignore_path, "w", encoding="utf-8") as f:
            f.write(content)

    # If the path was already committed, untrack it so .gitignore takes effect.
    ls_proc = await asyncio.create_subprocess_exec(
        "git", "-C", sess.workspace_path, "ls-files", "-z", "--", rel,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await ls_proc.communicate()
    untracked = False
    if out.strip(b"\x00"):
        rm_cmd = ["git", "-C", sess.workspace_path, "rm", "--cached", "-q"]
        if body.is_dir:
            rm_cmd.append("-r")
        rm_cmd += ["--", rel]
        rm_proc = await asyncio.create_subprocess_exec(
            *rm_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await rm_proc.wait()
        untracked = rm_proc.returncode == 0

    return {"status": "ok", "added": added, "untracked": untracked}


@router.post("/api/workspace/file/delete")
async def delete_file(body: DeleteFileRequest, sess: Session = Depends(require_workspace)):
    full_path = _safe_path(sess.workspace_path, body.path)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="Not found")

    rel = body.path.strip("/")

    rm_cmd = ["git", "-C", sess.workspace_path, "rm", "-q", "-f"]
    if body.is_dir:
        rm_cmd.append("-r")
    rm_cmd += ["--", rel]
    rm_proc = await asyncio.create_subprocess_exec(
        *rm_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await rm_proc.wait()

    # git rm fails for untracked files; fall back to plain filesystem removal.
    if os.path.exists(full_path):
        if body.is_dir:
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)

    return {"status": "ok"}
