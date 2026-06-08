import asyncio
import os

import httpx
import websockets
import websockets.exceptions
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .auth import get_current_session
from .database import decrypt_token, get_db_session
from .models import Session, User
from .schemas import (
    InternalWorkspaceLoadRequest,
    InternalWorkspaceSyncRequest,
    SyncRequest,
    WorkspaceLoadRequest,
)

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

EDITOR_SERVICE_URL = os.environ["EDITOR_SERVICE_URL"]
INTERNAL_TOKEN = os.environ["INTERNAL_TOKEN"]
_EDITOR_WS_URL = EDITOR_SERVICE_URL.replace("https://", "wss://").replace("http://", "ws://")

_STRIP_HEADERS = {"host", "connection", "transfer-encoding"}


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

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{EDITOR_SERVICE_URL}/internal/workspace/load",
            json=InternalWorkspaceLoadRequest(
                user_id=sess.user_id,
                session_id=sess.id,
                access_token=access_token,
                repo_owner=body.repo_owner,
                repo_name=body.repo_name,
            ).model_dump(),
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=120,
        )
    if not resp.is_success:
        detail = resp.json().get("detail", "Load failed") if resp.headers.get("content-type", "").startswith("application/json") else "Load failed"
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


@router.post("/api/workspace/sync")
async def sync_workspace(request: Request, body: SyncRequest):
    sess = await get_current_session(request)
    if not sess:
        raise HTTPException(status_code=401)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{EDITOR_SERVICE_URL}/internal/workspace/sync",
            json=InternalWorkspaceSyncRequest(
                session_id=sess.id,
                message=body.message,
            ).model_dump(),
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=60,
        )
    if not resp.is_success:
        detail = resp.json().get("detail", "Sync failed") if resp.headers.get("content-type", "").startswith("application/json") else "Sync failed"
        raise HTTPException(status_code=resp.status_code, detail=detail)
    return resp.json()


# ── HTTP proxy ────────────────────────────────────────────────────────────────

@router.api_route(
    "/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def http_proxy(request: Request, path: str):
    sess = await get_current_session(request)
    if not sess:
        return RedirectResponse("/login")

    url = f"{EDITOR_SERVICE_URL}/proxy/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    headers = {
        **{k: v for k, v in request.headers.items() if k.lower() not in _STRIP_HEADERS},
        "X-Internal-Token": INTERNAL_TOKEN,
        "X-User-Id": str(sess.user_id),
        "X-Session-Id": str(sess.id),
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
        result = await db.execute(select(Session).where(Session.session_token == token))
        sess = result.scalar_one_or_none()

    if not sess:
        await websocket.close(code=1008)
        return

    query = websocket.url.query
    upstream_url = f"{_EDITOR_WS_URL}/proxy/{path}"
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
            additional_headers=[
                ("X-Internal-Token", INTERNAL_TOKEN),
                ("X-User-Id", str(sess.user_id)),
                ("X-Session-Id", str(sess.id)),
            ],
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
