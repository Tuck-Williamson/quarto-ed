import os
import secrets

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update

from .database import decrypt_token, encrypt_token, get_db_session
from .models import Session, User

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

oauth = OAuth()
oauth.register(
    name="github",
    client_id=os.environ["GITHUB_CLIENT_ID"],
    client_secret=os.environ["GITHUB_CLIENT_SECRET"],
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "read:user user:email repo"},
)

# Comma-separated GitHub usernames allowed to log in. Empty/unset = anyone
# with a GitHub account may log in. See SECURITY.md: deployments that can't
# run as root (e.g. Heroku) have no per-user OS sandbox (app/sandbox.py), so
# this is the only access boundary between users on those deployments.
_ALLOWED_USERS = {
    u.strip().lower()
    for u in os.environ.get("ALLOWED_GITHUB_USERS", "").split(",")
    if u.strip()
}


def _is_user_allowed(username: str) -> bool:
    return not _ALLOWED_USERS or username.lower() in _ALLOWED_USERS


async def get_current_session(request: Request) -> Session | None:
    token = request.session.get("session_token")
    if not token:
        return None
    async with get_db_session() as db:
        result = await db.execute(
            select(Session).where(Session.session_token == token)
        )
        return result.scalar_one_or_none()


@router.get("/", response_class=RedirectResponse)
async def root(request: Request):
    sess = await get_current_session(request)
    return RedirectResponse("/editor" if sess else "/login")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    sess = await get_current_session(request)
    if sess:
        return RedirectResponse("/editor")
    return templates.TemplateResponse(request, "login.html")


@router.get("/auth/github")
async def github_login(request: Request):
    redirect_uri = str(request.url_for("github_callback"))
    return await oauth.github.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="github_callback")
async def github_callback(request: Request):
    token = await oauth.github.authorize_access_token(request)
    resp = await oauth.github.get("user", token=token)
    profile = resp.json()

    github_id = profile["id"]
    username = profile["login"]

    if not _is_user_allowed(username):
        return RedirectResponse("/login?error=access_denied")

    encrypted = encrypt_token(token["access_token"])

    async with get_db_session() as db:
        result = await db.execute(select(User).where(User.github_id == github_id))
        user = result.scalar_one_or_none()
        if user:
            user.username = username
            user.access_token_encrypted = encrypted
        else:
            user = User(
                github_id=github_id,
                username=username,
                access_token_encrypted=encrypted,
            )
            db.add(user)
        await db.flush()

        session_token = secrets.token_hex(32)
        db.add(Session(session_token=session_token, user_id=user.id))
        await db.commit()

    request.session["session_token"] = session_token
    return RedirectResponse("/editor")


@router.delete("/api/session")
async def logout(request: Request):
    from .proxy import kill_quarto_preview

    sess = await get_current_session(request)
    if sess:
        await kill_quarto_preview(sess.user_id)
        async with get_db_session() as db:
            result = await db.execute(select(Session).where(Session.id == sess.id))
            s = result.scalar_one_or_none()
            if s:
                await db.delete(s)
            await db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
