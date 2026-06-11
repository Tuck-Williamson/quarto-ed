"""Tests for auth routes: root redirect, login page, editor access."""
import pytest

from app import __version__
from app.auth import _is_user_allowed


async def test_root_unauthenticated_redirects_to_login(anon_client):
    resp = await anon_client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"] == "/login"


def test_is_user_allowed_empty_allowlist_allows_everyone(monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "_ALLOWED_USERS", set())
    assert _is_user_allowed("anyone") is True


def test_is_user_allowed_checks_allowlist_case_insensitively(monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "_ALLOWED_USERS", {"tuck-williamson"})
    assert _is_user_allowed("Tuck-Williamson") is True
    assert _is_user_allowed("tuck-williamson") is True
    assert _is_user_allowed("someone-else") is False


async def test_api_version(anon_client):
    resp = await anon_client.get("/api/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == __version__
    assert "git_sha" in body
    assert "quarto_version" in body


async def test_root_authenticated_redirects_to_editor(auth_client):
    resp = await auth_client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"] == "/editor"


async def test_login_page_returns_html(anon_client):
    resp = await anon_client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_login_page_when_authenticated_redirects_to_editor(auth_client):
    resp = await auth_client.get("/login", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"] == "/editor"


async def test_editor_unauthenticated_redirects_to_login(anon_client):
    resp = await anon_client.get("/editor", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/login" in resp.headers["location"]


async def test_editor_authenticated_returns_html(auth_client):
    resp = await auth_client.get("/editor")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "quarto" in resp.text.lower()


async def test_logout_clears_session(auth_client):
    resp = await auth_client.delete("/api/session", follow_redirects=False)
    assert resp.status_code in (301, 302, 303, 307, 308)
