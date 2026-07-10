"""Quarto preview: logs, restart, and the HTTP + WebSocket reverse proxy to the
per-user `quarto preview` process."""
import asyncio
import os

import httpx
import websockets
import websockets.exceptions
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from ..database import get_db_session
from ..models import Session
from ..schemas import PreviewRestartRequest
from ._core import (
    _STRIP_REQ_HEADERS,
    _STRIP_RESP_HEADERS,
    _get_or_spawn_preview,
    _preview_locks,
    _preview_logs,
    _preview_paths,
    _preview_processes,
    _safe_path,
    current_session,
    kill_quarto_preview,
    require_session,
    require_workspace,
    spawn_quarto_preview,
)

router = APIRouter()


@router.get("/api/preview/logs")
async def get_preview_logs(sess: Session = Depends(require_session)):
    running = (
        sess.user_id in _preview_processes
        and _preview_processes[sess.user_id].returncode is None
    )
    return {"lines": _preview_logs.get(sess.user_id, []), "running": running}


@router.post("/api/preview/restart")
async def restart_preview(body: PreviewRestartRequest, sess: Session = Depends(require_workspace)):
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


@router.api_route(
    "/api/preview/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def preview_http(request: Request, path: str, sess: Session | None = Depends(current_session)):
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
