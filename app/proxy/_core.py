import asyncio
import fcntl
import io
import json
import logging
import mimetypes
import os
import pty
import re
import shutil
import socket
import struct
import subprocess
import termios
import zipfile

_log = logging.getLogger(__name__)

import httpx
import websockets
import websockets.exceptions
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update

from .. import __version__, sandbox
from ..auth import get_current_session
from ..database import decrypt_token, encrypt_token, get_db_session
from ..models import Session, User, UserAIConfig

# Templates live at app/templates; __file__ here is app/proxy/_core.py.
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
)

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

# ── Per-server autosave branch ───────────────────────────────────────────────

def _make_server_id() -> str:
    raw = os.environ.get("DYNO") or socket.gethostname()
    return re.sub(r"[^a-zA-Z0-9]", "-", raw).lower()[:40]

_SERVER_ID = _make_server_id()
_AUTOSAVE_BRANCH = f"quarto-ed-{_SERVER_ID}"

# ── Terminal sandbox warning ─────────────────────────────────────────────────

_ALLOWED_USERS_RAW = os.environ.get("ALLOWED_GITHUB_USERS", "").strip()
# Warn when not sandboxed AND multiple users are allowed (empty = everyone, or 2+ listed)
_TERMINAL_SANDBOX_WARNING = (
    not sandbox.sandboxing_available() and
    (not _ALLOWED_USERS_RAW or _ALLOWED_USERS_RAW.count(",") >= 1)
)

# Env vars forwarded to terminal shells — same allowlist principle as quarto preview.
_TERMINAL_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "USER", "USERNAME", "LOGNAME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
})

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
    "aiProvider": "claude",
    "aiClaudeModel": "",
    "aiOllamaModel": "",
    "aiOllamaEndpoint": "http://localhost:11434",
    "inlineSystemPrompt": (
        "You are an inline editing assistant for Quarto documents. "
        "Make targeted, precise edits. Return only the replacement text with no explanation."
    ),
    "chatSystemPrompt": (
        "You are a writing and coding assistant for Quarto documents. "
        "Quarto is a scientific and technical publishing system built on Pandoc. "
        "Help the user write, edit, structure, and improve their Quarto documents. "
        "When showing code, use Quarto's fenced code chunk syntax (```{r}, ```{python}, etc.). "
        "Be concise. Format your responses in Markdown compatible with Quarto."
    ),
}

# ── Preview process state ────────────────────────────────────────────────────

_preview_processes: dict[int, asyncio.subprocess.Process] = {}
_preview_ports: dict[int, int] = {}
_preview_logs: dict[int, list[str]] = {}
_preview_targets: dict[int, str | None] = {}
_preview_paths: dict[int, str] = {}
_preview_locks: dict[int, asyncio.Lock] = {}
_LOG_MAX_LINES = 500

# ── Terminal process state (one shell per user) ──────────────────────────────

_terminal_fds: dict[int, int] = {}             # user_id → pty master fd
_terminal_procs: dict[int, subprocess.Popen] = {}  # user_id → bash process
_terminal_workspace: dict[int, str] = {}       # user_id → workspace path at spawn time
_terminal_gen: dict[int, int] = {}             # user_id → connection generation (race guard)


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


_TIOCSCTTY = getattr(termios, "TIOCSCTTY", 0x540E)  # 0x540E = Linux x86/x86_64


def _setup_terminal_child() -> None:
    """preexec_fn: new session + acquire slave pty as controlling terminal.

    Without setsid() the child inherits the parent's session (uvicorn's),
    which has no controlling terminal. The terminal driver can only deliver
    SIGINT/SIGTSTP to the foreground process group of the session whose
    controlling terminal the pty is — so without this setup, Ctrl-C and
    Ctrl-Z never reach the running process.
    """
    os.setsid()
    fcntl.ioctl(0, _TIOCSCTTY, 0)  # fd 0 = stdin = slave pty after Popen dup2s


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Set PTY window size (cols × rows)."""
    size = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        pass


def _kill_terminal(user_id: int) -> None:
    proc = _terminal_procs.pop(user_id, None)
    fd = _terminal_fds.pop(user_id, None)
    _terminal_workspace.pop(user_id, None)
    _terminal_gen.pop(user_id, None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.kill()  # belt-and-suspenders; non-blocking
        except OSError:
            pass
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _spawn_terminal(user_id: int, workspace_path: str, cols: int = 80, rows: int = 24) -> int:
    """Spawn a bash shell inside workspace_path, return the pty master fd."""
    _kill_terminal(user_id)
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, cols, rows)
    env = {k: v for k, v in os.environ.items() if k in _TERMINAL_ENV_ALLOWLIST}
    env["TERM"] = "xterm-256color"
    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-i"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workspace_path,
            env=env,
            close_fds=True,
            preexec_fn=_setup_terminal_child,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    _terminal_fds[user_id] = master_fd
    _terminal_procs[user_id] = proc
    _terminal_workspace[user_id] = workspace_path
    return master_fd


async def _setup_autosave_branch(workspace_path: str) -> None:
    """After clone/pull, ensure the per-server autosave branch is checked out."""
    env_no_prompt = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def git(*args, **kw):
        return asyncio.create_subprocess_exec(
            "git", "-C", workspace_path, *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **kw,
        )

    # Skip for empty repos (no commits yet)
    chk = await asyncio.create_subprocess_exec(
        "git", "-C", workspace_path, "rev-parse", "HEAD",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await chk.wait()
    if chk.returncode != 0:
        return

    # Check if local branch already exists
    local_chk = await asyncio.create_subprocess_exec(
        "git", "-C", workspace_path, "rev-parse", "--verify", _AUTOSAVE_BRANCH,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await local_chk.wait()

    if local_chk.returncode == 0:
        p = await git("checkout", _AUTOSAVE_BRANCH)
        await p.wait()
        return

    # Check if remote branch exists
    ls = await asyncio.create_subprocess_exec(
        "git", "-C", workspace_path, "ls-remote", "--heads", "origin", _AUTOSAVE_BRANCH,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env_no_prompt,
    )
    out, _ = await ls.communicate()

    if out.strip():
        p = await git("fetch", "origin", _AUTOSAVE_BRANCH, env=env_no_prompt)
        await p.wait()
        p = await git("checkout", "-b", _AUTOSAVE_BRANCH, f"origin/{_AUTOSAVE_BRANCH}")
        await p.wait()
    else:
        p = await git("checkout", "-b", _AUTOSAVE_BRANCH)
        await p.wait()
        p = await git("push", "-u", "origin", _AUTOSAVE_BRANCH, env=env_no_prompt)
        await p.wait()
        if p.returncode != 0:
            _log.warning(
                "_setup_autosave_branch: push failed (rc=%d); branch has no remote tracking ref",
                p.returncode,
            )


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


# ── AI config helper ─────────────────────────────────────────────────────────

async def _get_user_api_key(user_id: int) -> str:
    """Return the Anthropic API key for the user, falling back to the server env var.

    Provider, model, and endpoint are now user/repo settings — only the secret
    key is stored in the DB.
    """
    async with get_db_session() as db:
        result = await db.execute(
            select(UserAIConfig).where(UserAIConfig.user_id == user_id)
        )
        cfg = result.scalar_one_or_none()

    if cfg and cfg.api_key_encrypted:
        try:
            return decrypt_token(cfg.api_key_encrypted)
        except Exception:
            return _ANTHROPIC_KEY

    return _ANTHROPIC_KEY


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



# ── Editor page ───────────────────────────────────────────────────────────────



# ── Repos API ─────────────────────────────────────────────────────────────────



# ── Workspace API ─────────────────────────────────────────────────────────────



async def _get_default_branch(workspace_path: str) -> str:
    """Return the remote default branch name without a network call."""
    p = await asyncio.create_subprocess_exec(
        "git", "-C", workspace_path, "rev-parse", "--abbrev-ref", "origin/HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await p.communicate()
    branch = stdout.decode().strip()
    if branch.startswith("origin/"):
        return branch[len("origin/"):]
    # Fallback: check which conventional branch name exists on the remote.
    for candidate in ("main", "master"):
        p = await asyncio.create_subprocess_exec(
            "git", "-C", workspace_path, "rev-parse", "--verify", f"origin/{candidate}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await p.wait()
        if p.returncode == 0:
            return candidate
    return "main"








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
















# ── Settings API ──────────────────────────────────────────────────────────────

async def _get_user_and_token(sess: Session):
    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()
    return user, decrypt_token(user.access_token_encrypted)












# ── AI config endpoints ───────────────────────────────────────────────────────







# ── AI Chat ───────────────────────────────────────────────────────────────────





# ── Preview logs ─────────────────────────────────────────────────────────────





# ── Preview proxy (HTTP + WS) ─────────────────────────────────────────────────

_STRIP_REQ_HEADERS = {"host", "connection", "transfer-encoding", "accept-encoding"}
_STRIP_RESP_HEADERS = {"host", "connection", "transfer-encoding", "content-encoding", "content-length"}






# ── Terminal WebSocket ────────────────────────────────────────────────────────



# ── Auth dependencies ─────────────────────────────────────────────────────────
# Shared FastAPI dependencies that replace the per-handler session boilerplate
# (`sess = await get_current_session(...); if not sess: 401; if not
# sess.workspace_path: 400`). Tests override these via app.dependency_overrides
# (see tests/conftest.py) instead of monkeypatching get_current_session.

async def current_session(request: Request) -> Session | None:
    """Optional session — for routes that redirect anonymous users rather than
    returning 401 (e.g. the editor page and the preview proxy)."""
    return await get_current_session(request)


async def require_session(request: Request) -> Session:
    """Session or 401. Use for authenticated JSON endpoints."""
    sess = await get_current_session(request)
    if sess is None:
        raise HTTPException(status_code=401)
    return sess


async def require_workspace(sess: Session = Depends(require_session)) -> Session:
    """Session that also has a workspace loaded, or 400."""
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded")
    return sess
