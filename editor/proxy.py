import asyncio
import os
import socket

import httpx
import websockets
import websockets.exceptions
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy import select, update

from app.database import get_db_session
from app.models import Session, User
from app.schemas import InternalReloadRequest, InternalWorkspaceLoadRequest, InternalWorkspaceSyncRequest

router = APIRouter()

INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]
_PORT_MIN = int(os.environ.get("CODE_SERVER_PORT_MIN", 8100))
_PORT_MAX = int(os.environ.get("CODE_SERVER_PORT_MAX", 8200))
_WORKSPACE_BASE = os.environ.get("WORKSPACE_BASE", "/workspace")

_processes: dict[int, asyncio.subprocess.Process] = {}
_ports: dict[int, int] = {}


def _check_internal(request: Request):
    if request.headers.get("X-Internal-Token") != INTERNAL_TOKEN:
        raise HTTPException(status_code=403)


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
        if proc.returncode is not None:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
            raise RuntimeError(
                f"code-server exited early (code {proc.returncode}): {stderr_bytes.decode()[:500]}"
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
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass


async def _get_or_spawn(user_id: int, session_id: int) -> int:
    proc = _processes.get(user_id)
    if proc and proc.returncode is None:
        return _ports[user_id]
    _processes.pop(user_id, None)
    _ports.pop(user_id, None)
    async with get_db_session() as db:
        result = await db.execute(select(Session).where(Session.id == session_id))
        sess = result.scalar_one_or_none()
    if not sess or not sess.workspace_path:
        raise HTTPException(status_code=400, detail="No workspace loaded. Load a repo first.")
    return await spawn_code_server(user_id, sess.workspace_path, session_id)


async def cleanup_idle_processes():
    while True:
        await asyncio.sleep(300)
        for uid in list(_processes.keys()):
            async with get_db_session() as db:
                result = await db.execute(select(Session).where(Session.user_id == uid))
                if not result.scalar_one_or_none():
                    await kill_code_server(uid)


# ── Internal API ─────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/internal/workspace/load")
async def internal_workspace_load(
    body: InternalWorkspaceLoadRequest, _: None = Depends(_check_internal)
):
    workspace_path = os.path.join(_WORKSPACE_BASE, body.repo_owner, body.repo_name)
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
            f"https://oauth2:{body.access_token}@github.com/"
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

        async with get_db_session() as db:
            result = await db.execute(select(User).where(User.id == body.user_id))
            user = result.scalar_one_or_none()
        username = user.username if user else "quarto-ed-user"

        for cmd in [
            ["git", "-C", workspace_path, "config", "user.email",
             f"{username}@users.noreply.github.com"],
            ["git", "-C", workspace_path, "config", "user.name", username],
        ]:
            p = await asyncio.create_subprocess_exec(*cmd)
            await p.wait()

    async with get_db_session() as db:
        await db.execute(
            update(Session)
            .where(Session.id == body.session_id)
            .values(
                workspace_path=workspace_path,
                repo_owner=body.repo_owner,
                repo_name=body.repo_name,
            )
        )
        await db.commit()

    if body.user_id in _processes:
        await kill_code_server(body.user_id)

    return {"workspace_path": workspace_path}


@router.post("/internal/workspace/sync")
async def internal_workspace_sync(
    body: InternalWorkspaceSyncRequest, _: None = Depends(_check_internal)
):
    async with get_db_session() as db:
        result = await db.execute(select(Session).where(Session.id == body.session_id))
        sess = result.scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
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


@router.post("/internal/reload")
async def internal_reload(
    body: InternalReloadRequest, _: None = Depends(_check_internal)
):
    await kill_code_server(body.user_id)
    return {"status": "ok"}


@router.delete("/internal/cs/{user_id}")
async def internal_kill(user_id: int, _: None = Depends(_check_internal)):
    await kill_code_server(user_id)
    return {"status": "ok"}


# ── HTTP proxy ────────────────────────────────────────────────────────────────

_STRIP_HEADERS = {
    "host", "connection", "transfer-encoding",
    "x-internal-token", "x-user-id", "x-session-id",
}


@router.api_route(
    "/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def http_proxy(request: Request, path: str):
    if request.headers.get("X-Internal-Token") != INTERNAL_TOKEN:
        raise HTTPException(status_code=403)

    user_id = int(request.headers.get("X-User-Id", 0))
    session_id = int(request.headers.get("X-Session-Id", 0))
    if not user_id or not session_id:
        raise HTTPException(status_code=400, detail="Missing user/session headers")

    port = await _get_or_spawn(user_id, session_id)
    url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_HEADERS}
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
    if websocket.headers.get("x-internal-token") != INTERNAL_TOKEN:
        await websocket.close(code=1008)
        return

    user_id = int(websocket.headers.get("x-user-id", 0))
    session_id = int(websocket.headers.get("x-session-id", 0))
    if not user_id or not session_id:
        await websocket.close(code=1008)
        return

    try:
        port = await _get_or_spawn(user_id, session_id)
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
