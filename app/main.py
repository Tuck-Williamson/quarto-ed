import os

from fastapi import FastAPI
from sqlalchemy import update
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .auth import router as auth_router
from .database import engine, get_db_session
from .models import Base, Session, UserAIConfig  # noqa: F401 — ensures table is registered
from .proxy import router as proxy_router

# Group-writable by default: /workspace dirs are setgid to a per-user sandbox
# group (see app/sandbox.py), so files this process creates there must be
# group-writable for the sandboxed quarto preview process to use them.
os.umask(0o002)

app = FastAPI(title="quarto-ed", version=__version__)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SECRET_KEY"],
    https_only=os.environ.get("ENVIRONMENT") != "development",
    same_site="lax",
)

_ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")
if _ALLOWED_ORIGIN:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[_ALLOWED_ORIGIN],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )

app.include_router(auth_router)
app.include_router(proxy_router)


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_db_session() as db:
        await db.execute(update(Session).values(cs_port=None, cs_pid=None))
        await db.commit()
