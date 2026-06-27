import asyncio
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import zipfile

import httpx
import websockets
import websockets.exceptions
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update

from . import __version__, sandbox
from .auth import get_current_session
from .database import decrypt_token, get_db_session
from .models import Session, User
from .schemas import (
    AIChatRequest,
    CommitRequest,
    DeleteFileRequest,
    FileCreateRequest,
    FileWriteRequest,
    GitignoreAddRequest,
    PreviewRestartRequest,
    RepoSettingsSaveRequest,
    SettingsSaveRequest,
    SyncRequest,
    WorkspaceLoadRequest,
)

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_PORT_MIN = int(os.environ.get("PREVIEW_PORT_MIN", 8100))
_PORT_MAX = int(os.environ.get("PREVIEW_PORT_MAX", 8200))
_WORKSPACE_BASE = os.environ.get("WORKSPACE_BASE", "/workspace")

# Only these env vars are forwarded to the quarto subprocess. Using an
# allowlist rather than a denylist ensures new secrets added to the server
# environment are never accidentally exposed to user code chunks.
_QUARTO_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP",
    "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
    "DENO_DIR", "QUARTO_DENO", "QUARTO_PYTHON",
    "R_HOME", "R_LIBS", "R_LIBS_USER",
    "USER", "USERNAME", "LOGNAME",
})

_GITHUB_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _get_quarto_version() -> str:
    try:
        result = subprocess.run(
            ["quarto", "--version"], capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


_QUARTO_VERSION = _get_quarto_version()
_GIT_SHA = os.environ.get("GIT_SHA", "unknown")

_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-4-6")

_AI_SYSTEM = (
    "You are a writing and coding assistant for Quarto documents. "
    "Quarto is a scientific and technical publishing system built on Pandoc. "
    "Help the user write, edit, structure, and improve their Quarto documents. "
    "When showing code, use Quarto's fenced code chunk syntax (```{r}, ```{python}, etc.). "
    "Be concise. Format your responses in Markdown compatible with Quarto."
)

_SETTINGS_DEFAULTS = {
    "theme": "dark",
    "fontSize": 14,
    "tabSize": 2,
    "wordWrap": True,
    "autoSave": True,
    "autoSaveDelay": 120000,
    "vimMode": False,
    "commitMessage": "User saved.",
    "previewFollowDebounce": 1000,
}

# ── Preview process state ────────────────────────────────────────────────────

_preview_processes: dict[int, asyncio.subprocess.Process] = {}
_preview_ports: dict[int, int] = {}
_preview_logs: dict[int, list[str]] = {}
_preview_targets: dict[int, str | None] = {}
_preview_paths: dict[int, str] = {}
_preview_locks: dict[int, asyncio.Lock] = {}
_LOG_MAX_LINES = 500


async def _pipe_reader(user_id: int, stream: asyncio.StreamReader, prefix: str = "") -> None:
    try:
        async for line in stream:
            text = line.decode(errors="replace").rstrip()
            buf = _preview_logs.setdefault(user_id, [])
            buf.append(f"{prefix}{text}")
            if len(buf) > _LOG_MAX_LINES:
                del buf[:-_LOG_MAX_LINES]
    except Exception:
        pass


def _quarto_env(user_id: int, python_bin: str | None = None) -> dict:
    """Build a minimal env dict for the quarto subprocess.

    Allowlist-based so new secrets added to the server env are never forwarded
    to user code chunks (e.g. a Python block doing print(os.environ)).

    If `python_bin` is given (the interpreter from a per-repo venv), point
    QUARTO_PYTHON at it and prepend its bin/ dir to PATH so `!pip install`
    inside a chunk also targets the venv.
    """
    env = {k: v for k, v in os.environ.items() if k in _QUARTO_ENV_ALLOWLIST}
    env["HOME"] = os.path.join(_WORKSPACE_BASE, str(user_id))
    if python_bin:
        env["QUARTO_PYTHON"] = python_bin
        venv_bin = os.path.dirname(python_bin)
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    return env


def _add_gitignore_entry(workspace_path: str, entry: str) -> None:
    """Append `entry` to .gitignore if not already present (best-effort)."""
    gitignore_path = os.path.join(workspace_path, ".gitignore")
    content = ""
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, encoding="utf-8") as f:
            content = f.read()
    if entry in content.splitlines():
        return
    if content and not content.endswith("\n"):
        content += "\n"
    content += entry + "\n"
    with open(gitignore_path, "w", encoding="utf-8") as f:
        f.write(content)


async def _ensure_quarto_venv(workspace_path: str, user_id: int, env: dict) -> str:
    """Create a per-repo Python venv (if missing) so quarto's jupyter engine
    can `pip install` extra packages without touching the system site-packages.

    Uses --system-site-packages so jupyter/ipykernel from the base image are
    already importable; pip-installed packages still land in the venv's own
    site-packages, isolated per repo. Returns the path to the venv's python.
    """
    venv_path = os.path.join(workspace_path, ".venv")
    python_bin = os.path.join(venv_path, "bin", "python3")

    if not os.path.exists(python_bin):
        proc = await asyncio.create_subprocess_exec(
            *sandbox.setpriv_args(user_id),
            "python3", "-m", "venv", "--system-site-packages", venv_path,
            env=env,
            cwd=workspace_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create quarto venv: {stderr.decode()}")

    _add_gitignore_entry(workspace_path, ".venv/")
    return python_bin


def _redact(text: str, *secrets: str) -> str:
    """Replace each secret in text with '***'. Safe to call with empty strings."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _validate_github_name(value: str, label: str) -> None:
    """Raise 400 if value is not a safe GitHub owner/repo name."""
    if not _GITHUB_NAME_RE.match(value) or ".." in value:
        raise HTTPException(status_code=400, detail=f"Invalid {label}")


def _allocate_port() -> int:
    used = set(_preview_ports.values())
    for port in range(_PORT_MIN, _PORT_MAX + 1):
        if port in used:
            continue
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No available preview ports")


async def _wait_for_port(port: int, proc: asyncio.subprocess.Process, timeout: float = 120.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if proc.returncode is not None:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            raise RuntimeError(
                f"quarto preview exited early (code {proc.returncode}): "
                f"{stderr_bytes.decode()[:500]}"
            )
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.5)
    stderr_bytes = b""
    if proc.stderr:
        try:
            stderr_bytes = await asyncio.wait_for(proc.stderr.read(2048), timeout=1)
        except asyncio.TimeoutError:
            pass
    raise TimeoutError(
        f"quarto preview did not start on port {port} within {timeout}s. "
        f"stderr: {stderr_bytes.decode()[:500]}"
    )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BROWSE_RE = re.compile(r"Browse at (https?://[^/\s]+)(/\S*)?")


async def _capture_browse_path(user_id: int, proc: asyncio.subprocess.Process,
                                timeout: float = 10.0) -> str:
    """Read stdout lines until quarto prints 'Browse at <url>', returning the
    path portion (e.g. '/' or '/sub/doc.html'). Falls back to '/' on timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    buf = _preview_logs.setdefault(user_id, [])
    while asyncio.get_event_loop().time() < deadline:
        try:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=1)
        except asyncio.TimeoutError:
            continue
        if not raw:
            break
        line = raw.decode(errors="replace").rstrip()
        buf.append(line)
        m = _BROWSE_RE.search(_ANSI_RE.sub("", line))
        if m:
            return m.group(2) or "/"
    return "/"


async def spawn_quarto_preview(user_id: int, workspace_path: str, session_id: int,
                                target: str | None = None) -> int:
    port = _allocate_port()

    uid, gid = sandbox.ensure_user_account(user_id)
    sandbox.ensure_workspace_owned(os.path.dirname(workspace_path), user_id)

    home_dir = os.path.join(_WORKSPACE_BASE, str(user_id))
    sandbox.ensure_dir_owned(home_dir, uid, gid, mode=0o700)

    base_env = _quarto_env(user_id)
    python_bin = await _ensure_quarto_venv(workspace_path, user_id, base_env)
    env = _quarto_env(user_id, python_bin)

    preview_arg = _safe_path(workspace_path, target) if target else workspace_path
    process = await asyncio.create_subprocess_exec(
        *sandbox.setpriv_args(user_id),
        "quarto", "preview", preview_arg,
        "--port", str(port),
        "--host", "127.0.0.1",
        "--no-browser",
        env=env,
        cwd=workspace_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _preview_processes[user_id] = process
    _preview_ports[user_id] = port

    await _wait_for_port(port, process)

    _preview_logs[user_id] = []
    _preview_paths[user_id] = await _capture_browse_path(user_id, process)
    asyncio.ensure_future(_pipe_reader(user_id, process.stdout))
    asyncio.ensure_future(_pipe_reader(user_id, process.stderr))

    _preview_targets[user_id] = target
    async with get_db_session() as db:
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(cs_port=port, cs_pid=process.pid)
        )
        await db.commit()

    return port


async def kill_quarto_preview(user_id: int):
    proc = _preview_processes.pop(user_id, None)
    _preview_ports.pop(user_id, None)
    _preview_logs.pop(user_id, None)
    _preview_paths.pop(user_id, None)
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass


async def _get_or_spawn_preview(user_id: int, sess: Session) -> int:
    proc = _preview_processes.get(user_id)
    if proc and proc.returncode is None:
        return _preview_ports[user_id]
    _preview_processes.pop(user_id, None)
    _preview_ports.pop(user_id, None)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded. Load a repo first.")
    async with _preview_locks.setdefault(user_id, asyncio.Lock()):
        return await spawn_quarto_preview(user_id, sess.workspace_path, sess.id,
                                            target=_preview_targets.get(user_id))


# ── Path safety ───────────────────────────────────────────────────────────────

def _safe_path(workspace: str, rel: str) -> str:
    """Resolve rel relative to workspace, raise 403 on traversal."""
    full = os.path.realpath(os.path.join(workspace, rel.lstrip("/")))
    root = os.path.realpath(workspace)
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    return full


# ── Settings helpers ──────────────────────────────────────────────────────────

def _settings_repo_local_path(username: str) -> str:
    return os.path.join(_WORKSPACE_BASE, username, f"{username}-quarto-ed-settings")


async def _ensure_settings_cloned(user_id: int, username: str, access_token: str) -> bool:
    """Clone settings repo if not present locally. Returns True if available."""
    sandbox.ensure_workspace_owned(os.path.join(_WORKSPACE_BASE, username), user_id)
    local_path = _settings_repo_local_path(username)
    if os.path.isdir(os.path.join(local_path, ".git")):
        return True
    clone_url = (
        f"https://oauth2:{access_token}@github.com/"
        f"{username}/{username}-quarto-ed-settings.git"
    )
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", clone_url, local_path,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def _settings_repo_exists_on_github(username: str, access_token: str) -> bool:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{username}/{username}-quarto-ed-settings",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
            },
        )
    return resp.status_code == 200


async def _push_settings(local_path: str, message: str = "Update settings"):
    for cmd in [
        ["git", "-C", local_path, "add", "-A"],
        ["git", "-C", local_path, "commit", "-m", message],
        ["git", "-C", local_path, "push"],
    ]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0 and cmd[2] == "commit":
            break  # nothing to commit is fine


def _repo_settings_path(workspace_path: str) -> str:
    return os.path.join(workspace_path, ".quarto-ed-settings")


def _load_repo_settings(workspace_path: str | None) -> dict:
    if not workspace_path:
        return {}
    try:
        with open(_repo_settings_path(workspace_path), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in _SETTINGS_DEFAULTS}
    except (OSError, json.JSONDecodeError):
        return {}


# ── Version / health ─────────────────────────────────────────────────────────

@router.get("/api/version")
async def get_version():
    return {
        "version": __version__,
        "git_sha": _GIT_SHA,
        "quarto_version": _QUARTO_VERSION,
    }


# ── Editor page ───────────────────────────────────────────────────────────────

@router.get("/editor", response_class=HTMLResponse)
async def editor(request: Request):
    sess = await get_current_session(request)
    if not sess:
        return RedirectResponse("/login")
    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()
    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "username": user.username if user else "",
            "has_workspace": bool(sess.workspace_path),
            "repo_owner": sess.repo_owner or "",
            "repo_name": sess.repo_name or "",
            "ai_enabled": bool(_ANTHROPIC_KEY),
            "quarto_version": _QUARTO_VERSION,
            "app_version": __version__,
            "git_sha": _GIT_SHA,
        },
    )


# ── Repos API ─────────────────────────────────────────────────────────────────

@router.get("/api/repos")
async def list_repos(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()
    access_token = decrypt_token(user.access_token_encrypted)
    repos = []
    page = 1
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                "https://api.github.com/user/repos",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
                params={"per_page": 100, "page": page, "sort": "updated"},
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            repos.extend(r["name"] for r in batch)
            if len(batch) < 100:
                break
            page += 1
    return {"repos": repos}


# ── Workspace API ─────────────────────────────────────────────────────────────

@router.post("/api/workspace/load")
async def load_workspace(request: Request, body: WorkspaceLoadRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)

    _validate_github_name(body.repo_owner, "repo_owner")
    _validate_github_name(body.repo_name, "repo_name")

    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()

    access_token = decrypt_token(user.access_token_encrypted)
    sandbox.ensure_workspace_owned(os.path.join(_WORKSPACE_BASE, user.username), user.id)
    workspace_path = os.path.join(_WORKSPACE_BASE, user.username, body.repo_name)
    os.makedirs(workspace_path, exist_ok=True)

    if os.path.isdir(os.path.join(workspace_path, ".git")):
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
async def sync_workspace(request: Request, body: SyncRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")

    path = sess.workspace_path
    for cmd in [
        ["git", "-C", path, "add", "-A"],
        ["git", "-C", path, "commit", "-m", body.message],
        ["git", "-C", path, "push"],
    ]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0 and b"nothing to commit" not in stdout + stderr:
            raise HTTPException(status_code=500, detail=f"Command failed: {stderr.decode()}")

    return {"status": "ok"}


@router.get("/api/workspace/changes.zip")
async def download_local_changes(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")

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
async def discard_local_changes(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")

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


# ── File API ──────────────────────────────────────────────────────────────────

def _build_tree(root: str, rel_base: str = "") -> list:
    entries = []
    try:
        items = sorted(os.listdir(root))
    except PermissionError:
        return entries
    for name in items:
        if name.startswith("."):
            continue
        full = os.path.join(root, name)
        rel = os.path.join(rel_base, name) if rel_base else name
        if os.path.isdir(full):
            entries.append({"name": name, "path": rel, "type": "dir",
                            "children": _build_tree(full, rel)})
        else:
            entries.append({"name": name, "path": rel, "type": "file"})
    return entries


def _collect_paths(nodes: list, out: list) -> None:
    for node in nodes:
        out.append(node["path"])
        if node["type"] == "dir":
            _collect_paths(node.get("children", []), out)


def _mark_ignored(nodes: list, ignored: set) -> None:
    for node in nodes:
        if node["path"] in ignored:
            node["ignored"] = True
        if node["type"] == "dir":
            _mark_ignored(node.get("children", []), ignored)


@router.get("/api/files")
async def list_files(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
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
async def read_file(request: Request, path: str = Query(...)):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
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
async def read_file_raw(request: Request, path: str = Query(...)):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
    full_path = _safe_path(sess.workspace_path, path)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    media_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
    return FileResponse(full_path, media_type=media_type)


@router.post("/api/file")
async def write_file(request: Request, body: FileWriteRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
    full_path = _safe_path(sess.workspace_path, body.path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"status": "ok"}


@router.post("/api/file/create")
async def create_file(request: Request, body: FileCreateRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
    full_path = _safe_path(sess.workspace_path, body.path)
    if os.path.exists(full_path):
        raise HTTPException(status_code=409, detail="File already exists")
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write("")
    return {"status": "ok"}


@router.post("/api/workspace/gitignore")
async def add_to_gitignore(request: Request, body: GitignoreAddRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
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
async def delete_file(request: Request, body: DeleteFileRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
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


# ── Settings API ──────────────────────────────────────────────────────────────

async def _get_user_and_token(sess: Session):
    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()
    return user, decrypt_token(user.access_token_encrypted)


@router.get("/api/settings/repo/status")
async def settings_repo_status(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    user, token = await _get_user_and_token(sess)
    exists = await _settings_repo_exists_on_github(user.username, token)
    return {"exists": exists}


@router.post("/api/settings/repo/create")
async def create_settings_repo(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
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
        cloned = await _ensure_settings_cloned(user.id, user.username, token)
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

    await _push_settings(local_path, "Initialize quarto-ed settings")
    return {"status": "created"}


@router.get("/api/settings")
async def get_settings(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    user, token = await _get_user_and_token(sess)
    local_path = _settings_repo_local_path(user.username)

    if not os.path.isdir(os.path.join(local_path, ".git")):
        cloned = await _ensure_settings_cloned(user.id, user.username, token)
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
async def save_settings(request: Request, body: SettingsSaveRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    user, token = await _get_user_and_token(sess)
    local_path = _settings_repo_local_path(user.username)

    if not os.path.isdir(os.path.join(local_path, ".git")):
        cloned = await _ensure_settings_cloned(user.id, user.username, token)
        if not cloned:
            return {"status": "no_repo"}

    settings_file = os.path.join(local_path, "settings.json")
    snippets_file = os.path.join(local_path, "snippets.json")
    with open(settings_file, "w") as f:
        json.dump(body.settings, f, indent=2)
    if body.snippets is not None:
        with open(snippets_file, "w") as f:
            json.dump(body.snippets, f, indent=2)

    await _push_settings(local_path)
    return {"status": "ok"}


@router.post("/api/settings/repo")
async def save_repo_settings(request: Request, body: RepoSettingsSaveRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")

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


# ── AI Chat ───────────────────────────────────────────────────────────────────

@router.post("/api/ai/chat")
async def ai_chat(request: Request, body: AIChatRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not _ANTHROPIC_KEY:
        raise HTTPException(status_code=503, detail="AI not configured")

    user_content = body.message
    if body.context:
        user_content = f"<document>\n{body.context}\n</document>\n\n{body.message}"

    async def generate():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": _ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": _AI_MODEL,
                    "max_tokens": 4096,
                    "stream": True,
                    "system": _AI_SYSTEM,
                    "messages": [{"role": "user", "content": user_content}],
                },
            ) as response:
                async for chunk in response.aiter_text():
                    yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Preview logs ─────────────────────────────────────────────────────────────

@router.get("/api/preview/logs")
async def get_preview_logs(request: Request):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    running = (
        sess.user_id in _preview_processes
        and _preview_processes[sess.user_id].returncode is None
    )
    return {"lines": _preview_logs.get(sess.user_id, []), "running": running}


@router.post("/api/preview/restart")
async def restart_preview(request: Request, body: PreviewRestartRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded. Load a repo first.")
    target = body.target
    if target:
        full = _safe_path(sess.workspace_path, target)
        if not target.endswith(".qmd") or not os.path.isfile(full):
            raise HTTPException(status_code=400, detail="Invalid preview target")
    try:
        async with _preview_locks.setdefault(sess.user_id, asyncio.Lock()):
            await kill_quarto_preview(sess.user_id)
            port = await spawn_quarto_preview(sess.user_id, sess.workspace_path, sess.id, target=target)
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok", "port": port, "path": _preview_paths.get(sess.user_id, "/")}


# ── Preview proxy (HTTP + WS) ─────────────────────────────────────────────────

_STRIP_REQ_HEADERS = {"host", "connection", "transfer-encoding", "accept-encoding"}
_STRIP_RESP_HEADERS = {"host", "connection", "transfer-encoding", "content-encoding", "content-length"}


@router.api_route(
    "/api/preview/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def preview_http(request: Request, path: str):
    sess = await get_current_session(request)
    if not sess:
        return RedirectResponse("/login")

    port = await _get_or_spawn_preview(sess.user_id, sess)
    url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQ_HEADERS}
    body = await request.body()

    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        upstream = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )

    resp_headers = {k: v for k, v in upstream.headers.items()
                    if k.lower() not in _STRIP_RESP_HEADERS}
    if "location" in resp_headers:
        loc = resp_headers["location"]
        # Strip absolute quarto-origin prefix (http://127.0.0.1:PORT/...) → relative path
        if loc.startswith("http://127.0.0.1:"):
            loc = loc.split("/", 3)[3:]
            loc = "/" + (loc[0] if loc else "")
        # Prefix relative paths so they stay inside the proxy
        if loc.startswith("/") and not loc.startswith("/api/preview"):
            loc = f"/api/preview{loc}"
        resp_headers["location"] = loc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


@router.websocket("/api/preview/{path:path}")
async def preview_ws(websocket: WebSocket, path: str):
    token = websocket.session.get("session_token")
    if not token:
        await websocket.close(code=1008)
        return

    async with get_db_session() as db:
        result = await db.execute(select(Session).where(Session.session_token == token))
        sess = result.scalar_one_or_none()

    if not sess:
        await websocket.close(code=1008)
        return

    try:
        port = await _get_or_spawn_preview(sess.user_id, sess)
    except HTTPException:
        await websocket.close(code=1011)
        return

    query = websocket.url.query
    upstream_url = f"ws://127.0.0.1:{port}/{path}"
    if query:
        upstream_url += f"?{query}"

    subprotocols_header = websocket.headers.get("sec-websocket-protocol", "")
    subprotocols = [s.strip() for s in subprotocols_header.split(",") if s.strip()]
    accept_subprotocol = subprotocols[0] if subprotocols else None
    await websocket.accept(subprotocol=accept_subprotocol)

    _closed = (WebSocketDisconnect, websockets.exceptions.ConnectionClosed)

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols or None,
            open_timeout=10,
        ) as upstream_ws:

            async def to_upstream():
                try:
                    async for msg in websocket.iter_bytes():
                        await upstream_ws.send(msg)
                except _closed:
                    pass

            async def to_client():
                try:
                    async for msg in upstream_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            # Rewrite quarto's "reload/path" messages so the path
                            # includes our /api/preview/ proxy prefix.  Without this
                            # the live-reload JS navigates to the raw quarto path
                            # (e.g. "/") which escapes the proxy entirely.
                            if isinstance(msg, str) and msg.startswith("reload"):
                                tail = msg[len("reload"):]
                                if tail and not tail.startswith("/api/preview"):
                                    tail = "/api/preview" + tail
                                msg = "reload" + tail
                            await websocket.send_text(msg)
                except _closed:
                    pass

            tasks = [
                asyncio.ensure_future(to_upstream()),
                asyncio.ensure_future(to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, *done, return_exceptions=True)

    except _closed:
        pass
