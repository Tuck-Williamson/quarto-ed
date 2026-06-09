import os

from fastapi import FastAPI
from sqlalchemy import update
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .auth import router as auth_router
from .database import engine, get_db_session
from .models import Base, Session
from .proxy import router as proxy_router

app = FastAPI(title="quarto-ed")

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
