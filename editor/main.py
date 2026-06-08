import asyncio
import os

from fastapi import FastAPI
from sqlalchemy import update
from starlette.middleware.sessions import SessionMiddleware

from app.database import engine, get_db_session
from app.models import Base, Session
from .proxy import cleanup_idle_processes, router

app = FastAPI(title="quarto-ed-editor")
app.add_middleware(SessionMiddleware, secret_key=os.environ["SECRET_KEY"])
app.include_router(router)


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_db_session() as db:
        await db.execute(update(Session).values(cs_port=None, cs_pid=None))
        await db.commit()
    asyncio.create_task(cleanup_idle_processes())
