import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
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

# Serve the compiled Tailwind CSS (and any future static assets) at /static.
# Built into the image by the cssbuilder Docker stage; the directory always
# exists in source (app/static/src/), so the mount is valid in tests too.
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)

# ── Security response headers ────────────────────────────────────────────────
# The Content-Security-Policy is scoped to the app's own pages. Styles are now
# served from /static (self); the only remaining external origins are esm.sh
# (CodeMirror/xterm ES modules), Google Fonts, jsdelivr (xterm CSS), and a
# browser-direct Ollama endpoint. 'unsafe-inline' is still needed for the inline
# event handlers and for the inline styles CodeMirror/xterm inject at runtime;
# 'unsafe-eval' and the Tailwind Play CDN are gone now that CSS is precompiled.
#
# CSP is deliberately NOT applied to the quarto preview proxy (/api/preview/*)
# or raw file serving (/api/file/raw): that content is rendered by quarto and
# may reference its own CDN assets (bootstrap, fontawesome, mathjax). Our app
# policy would break it. Those responses are same-origin and still framed only
# by the same-origin editor (X-Frame-Options: SAMEORIGIN).
_CSP_CONNECT_EXTRA = os.environ.get("CSP_CONNECT_SRC_EXTRA", "").strip()
_CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'self'",
    "form-action 'self'",
    "img-src 'self' data: blob:",
    "font-src 'self' https://fonts.gstatic.com data:",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net",
    "script-src 'self' 'unsafe-inline' https://esm.sh",
    "worker-src 'self' blob:",
    "frame-src 'self'",
    " ".join(filter(None, [
        "connect-src 'self' https://esm.sh http://localhost:11434 http://127.0.0.1:11434",
        _CSP_CONNECT_EXTRA,
    ])),
])

_CSP_EXEMPT_PREFIXES = ("/api/preview", "/api/file/raw")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    path = request.url.path
    if not any(path.startswith(p) for p in _CSP_EXEMPT_PREFIXES):
        response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
    return response


app.include_router(auth_router)
app.include_router(proxy_router)


@app.on_event("startup")
async def startup():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with get_db_session() as db:
        await db.execute(update(Session).values(cs_port=None, cs_pid=None))
        await db.commit()
