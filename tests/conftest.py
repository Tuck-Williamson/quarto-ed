"""
Shared pytest fixtures.  Env vars are set at module level so they are
in place before any app module is imported.
"""
import os
import secrets
import subprocess

from cryptography.fernet import Fernet

# Must be set before any app import so module-level reads in database.py /
# auth.py succeed with test values.
os.environ.setdefault("SECRET_KEY", secrets.token_hex(32))
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("GITHUB_CLIENT_ID", "test_client_id")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test_client_secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WORKSPACE_BASE", "/tmp/quarto_ed_test")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import encrypt_token
from app.models import Base
from app.models import Session as AppSession
from app.models import User


# ---------------------------------------------------------------------------
# Database fixture – per-test fresh SQLite in-memory instance
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def test_db(monkeypatch):
    """
    Replaces the production DB engine with a fresh in-memory SQLite for the
    duration of each test.  Yielded value is the session-maker so fixtures
    that need to seed data can create a session.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app import database
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "AsyncSessionLocal", maker)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_db):
    async with test_db() as sess:
        yield sess


# ---------------------------------------------------------------------------
# User / session fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def test_user(db_session):
    user = User(
        github_id=99999,
        username="testuser",
        access_token_encrypted=encrypt_token("fake_github_token"),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def workspace(tmp_path):
    wp = tmp_path / "workspace"
    wp.mkdir()
    return wp


@pytest_asyncio.fixture
async def test_session(db_session, test_user, workspace):
    token = secrets.token_hex(32)
    sess = AppSession(
        session_token=token,
        user_id=test_user.id,
        workspace_path=str(workspace),
        repo_owner="testuser",
        repo_name="test-repo",
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)
    return sess


# ---------------------------------------------------------------------------
# HTTP client fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def anon_client():
    """Client with no session cookie."""
    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def auth_client(test_session, monkeypatch):
    """Client whose every request is authenticated as test_session."""
    from app import auth, proxy

    sess = test_session

    async def _mock_get_current_session(_request):
        return sess

    monkeypatch.setattr(auth, "get_current_session", _mock_get_current_session)
    monkeypatch.setattr(proxy, "get_current_session", _mock_get_current_session)

    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# Git workspace fixture for sync / integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def git_workspace(tmp_path):
    """
    Returns a workspace directory that is a real git repo connected to a
    local bare remote.  Suitable for testing git commit/push operations.
    """
    remote = tmp_path / "remote.git"
    workspace = tmp_path / "ws"
    workspace.mkdir()

    _run = lambda *cmd: subprocess.run(list(cmd), check=True, capture_output=True)

    _run("git", "init", "--bare", str(remote))
    _run("git", "init", str(workspace))
    _run("git", "-C", str(workspace), "config", "user.email", "ci@test.example.com")
    _run("git", "-C", str(workspace), "config", "user.name", "CI Test")
    _run("git", "-C", str(workspace), "remote", "add", "origin", f"file://{remote}")

    (workspace / "readme.md").write_text("# Test Repo\n")
    _run("git", "-C", str(workspace), "add", ".")
    _run("git", "-C", str(workspace), "commit", "-m", "Initial commit")

    branch = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _run("git", "-C", str(workspace), "push", "--set-upstream", "origin", branch)

    return workspace


@pytest_asyncio.fixture
async def git_auth_client(db_session, test_user, git_workspace, monkeypatch):
    """Like auth_client but with a session whose workspace is a real git repo."""
    token = secrets.token_hex(32)
    sess = AppSession(
        session_token=token,
        user_id=test_user.id,
        workspace_path=str(git_workspace),
        repo_owner="testuser",
        repo_name="test-repo",
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)

    from app import auth, proxy

    async def _mock_get_current_session(_request):
        return sess

    monkeypatch.setattr(auth, "get_current_session", _mock_get_current_session)
    monkeypatch.setattr(proxy, "get_current_session", _mock_get_current_session)

    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, sess
