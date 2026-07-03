"""Interactive terminal: a single bash PTY per user, bridged over a WebSocket."""
import asyncio
import json
import os

from fastapi import APIRouter, WebSocket
from sqlalchemy import select

from ..database import get_db_session
from ..models import Session
from ._core import (
    _set_winsize,
    _spawn_terminal,
    _terminal_fds,
    _terminal_gen,
    _terminal_procs,
    _terminal_workspace,
)

router = APIRouter()


@router.websocket("/api/terminal/ws")
async def terminal_ws(websocket: WebSocket):
    token = websocket.session.get("session_token")
    if not token:
        await websocket.close(code=1008)
        return

    async with get_db_session() as db:
        result = await db.execute(select(Session).where(Session.session_token == token))
        sess = result.scalar_one_or_none()

    if not sess or not sess.workspace_path:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    user_id = sess.user_id

    # Reuse an already-running terminal only when it's alive AND in the same workspace.
    existing_alive = (
        user_id in _terminal_fds
        and _terminal_procs.get(user_id) is not None
        and _terminal_procs[user_id].poll() is None
        and _terminal_workspace.get(user_id) == sess.workspace_path
    )
    if not existing_alive:
        try:
            master_fd = _spawn_terminal(user_id, sess.workspace_path)
        except Exception as exc:
            await websocket.send_text(f"\r\n\x1b[31m[terminal error: {exc}]\x1b[0m\r\n")
            await websocket.close(code=1011)
            return
    else:
        master_fd = _terminal_fds[user_id]

    # Bump generation so an older connection's finally-remove_reader won't evict our reader.
    gen = (_terminal_gen.get(user_id, 0) + 1)
    _terminal_gen[user_id] = gen

    loop = asyncio.get_running_loop()
    output_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _on_pty_readable():
        try:
            data = os.read(master_fd, 4096)
            output_queue.put_nowait(data)
        except OSError:
            output_queue.put_nowait(None)
            loop.remove_reader(master_fd)

    loop.add_reader(master_fd, _on_pty_readable)

    async def pty_to_ws():
        try:
            while True:
                chunk = await output_queue.get()
                if chunk is None:
                    break
                await websocket.send_bytes(chunk)
        except Exception:
            pass

    async def ws_to_pty():
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                raw: bytes = msg.get("bytes") or (msg.get("text") or "").encode()
                if not raw:
                    continue
                # Text frames carry JSON control messages (resize); binary frames carry keystrokes.
                if msg.get("text"):
                    try:
                        obj = json.loads(raw)
                        if obj.get("type") == "resize":
                            _set_winsize(
                                master_fd,
                                int(obj.get("cols", 80)),
                                int(obj.get("rows", 24)),
                            )
                        continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                try:
                    os.write(master_fd, raw)
                except OSError:
                    break
        except Exception:
            pass

    try:
        tasks = [
            asyncio.ensure_future(pty_to_ws()),
            asyncio.ensure_future(ws_to_pty()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, *done, return_exceptions=True)
    finally:
        # Only remove the reader if we're still the current connection for this user.
        # A reconnect that arrived during the await above bumps _terminal_gen, so this
        # guard prevents us from evicting the new connection's reader.
        if _terminal_gen.get(user_id) == gen:
            loop.remove_reader(master_fd)
