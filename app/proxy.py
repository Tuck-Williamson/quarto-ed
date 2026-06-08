import asyncio
import os
import socket

import httpx
import websockets
import websockets.exceptions
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update

from .auth import get_current_session
from .database import decrypt_token, get_db_session
from .models import Session, User
from .schemas import SyncRequest, WorkspaceLoadRequest

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_PORT_MIN = int(os.environ.get("CODE_SERVER_PORT_MIN", 8100))
_PORT_MAX = int(os.environ.get("CODE_SERVER_PORT_MAX", 8200))
_WORKSPACE_BASE = os.environ.get("WORKSPACE_BASE", "/workspace")

_processes: dict[int, asyncio.subprocess.Process] = {}
_ports: dict[int, int] = {}


def _allocate_port() -> int:
    used = set(_ports.values())
    for port in range(_PORT_MIN, _PORT_MAX + 1):
        if port in used:
            continue
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No available ports for code-server")


async def _wait_for_port(port: int, proc: asyncio.subprocess.Process, timeout: float = 60.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        # If the process already exited, capture stderr and fail fast
        if proc.returncode is not None:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            raise RuntimeError(
                f"code-server exited early (code {proc.returncode}): {stderr_bytes.decode()[:500]}"
            )
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.5)
    # Timed out — read whatever stderr we have for diagnosis
    stderr_bytes = b""
    if proc.stderr:
        try:
            stderr_bytes = await asyncio.wait_for(proc.stderr.read(2048), timeout=1)
        except asyncio.TimeoutError:
            pass
    raise TimeoutError(
        f"code-server did not start on port {port} within {timeout}s. "
        f"stderr: {stderr_bytes.decode()[:500]}"
    )


async def spawn_code_server(user_id: int, workspace_path: str, session_id: int) -> int:
    port = _allocate_port()
    user_data_dir = os.path.join(_WORKSPACE_BASE, str(user_id), ".vscode")
    os.makedirs(user_data_dir, exist_ok=True)

    cs_env = {k: v for k, v in os.environ.items() if k != "PORT"}
    cs_env["HOME"] = os.path.join(_WORKSPACE_BASE, str(user_id))

    process = await asyncio.create_subprocess_exec(
        "/opt/code-server/bin/code-server",
        "--bind-addr", f"127.0.0.1:{port}",
        "--auth", "none",
        "--disable-workspace-trust",
        "--extensions-dir", "/opt/cs-extensions",
        "--user-data-dir", user_data_dir,
        workspace_path,
        env=cs_env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _processes[user_id] = process
    _ports[user_id] = port

    await _wait_for_port(port, process)

    async with get_db_session() as db:
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(cs_port=port, cs_pid=process.pid)
        )
        await db.commit()

    return port


async def kill_code_server(user_id: int):
    proc = _processes.pop(user_id, None)
    _ports.pop(user_id, None)
    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()


async def _get_or_spawn(user_id: int, sess: Session) -> int:
    proc = _processes.get(user_id)
    if proc and proc.returncode is None:
        return _ports[user_id]
    # Process is gone; clean up stale entry
    _processes.pop(user_id, None)
    _ports.pop(user_id, None)
    if not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded. Load a repo first.")
    return await spawn_code_server(user_id, sess.workspace_path, sess.id)


# ── Repos API ────────────────────────────────────────────────────────────────

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


# ── Editor page ──────────────────────────────────────────────────────────────

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
        },
    )


# ── Workspace API ─────────────────────────────────────────────────────────────

@router.post("/api/workspace/load")
async def load_workspace(request: Request, body: WorkspaceLoadRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)

    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.id == sess.user_id))
        user = result.scalar_one_or_none()

    access_token = decrypt_token(user.access_token_encrypted)
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
            raise HTTPException(status_code=400, detail=f"git pull failed: {stderr.decode()}")
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
            raise HTTPException(status_code=400, detail=f"git clone failed: {stderr.decode()}")

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

    if sess.user_id in _processes:
        await kill_code_server(sess.user_id)

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
        # "nothing to commit" is exit code 1 from git commit — not a real error
        if proc.returncode != 0 and b"nothing to commit" not in stdout + stderr:
            raise HTTPException(status_code=500, detail=f"Command failed: {stderr.decode()}")

    return {"status": "ok"}


# ── HTTP proxy ────────────────────────────────────────────────────────────────

_STRIP_HEADERS = {"host", "connection", "transfer-encoding"}


@router.api_route(
    "/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def http_proxy(request: Request, path: str):
    sess = await get_current_session(request)
    if not sess:
        return RedirectResponse("/login")

    port = await _get_or_spawn(sess.user_id, sess)
    url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_HEADERS
    }
    body = await request.body()

    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        upstream = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body,
        )

    resp_headers = dict(upstream.headers)
    if "location" in resp_headers:
        loc = resp_headers["location"]
        if loc.startswith("/") and not loc.startswith("/proxy"):
            resp_headers["location"] = f"/proxy{loc}"

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )


# ── WebSocket proxy ───────────────────────────────────────────────────────────

@router.websocket("/proxy/{path:path}")
async def ws_proxy(websocket: WebSocket, path: str):
    token = websocket.session.get("session_token")
    if not token:
        await websocket.close(code=1008)
        return

    async with get_db_session() as db:
        from sqlalchemy import select
        result = await db.execute(select(Session).where(Session.session_token == token))
        sess = result.scalar_one_or_none()

    if not sess:
        await websocket.close(code=1008)
        return

    try:
        port = await _get_or_spawn(sess.user_id, sess)
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

    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols or None,
            open_timeout=10,
        ) as upstream_ws:

            async def to_upstream():
                async for msg in websocket.iter_bytes():
                    await upstream_ws.send(msg)

            async def to_client():
                async for msg in upstream_ws:
                    if isinstance(msg, bytes):
                        await websocket.send_bytes(msg)
                    else:
                        await websocket.send_text(msg)

            done, pending = await asyncio.wait(
                [asyncio.ensure_future(to_upstream()),
                 asyncio.ensure_future(to_client())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        pass
