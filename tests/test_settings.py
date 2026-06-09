"""Tests for the settings API."""
import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

async def test_get_settings_unauthenticated(anon_client):
    resp = await anon_client.get("/api/settings")
    assert resp.status_code == 401


async def test_save_settings_unauthenticated(anon_client):
    resp = await anon_client.post("/api/settings", json={"settings": {}})
    assert resp.status_code == 401


async def test_settings_repo_status_unauthenticated(anon_client):
    resp = await anon_client.get("/api/settings/repo/status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Defaults when no settings repo exists
# ---------------------------------------------------------------------------

async def test_get_settings_returns_defaults_when_no_repo(auth_client, monkeypatch):
    """When clone fails (no GitHub access), defaults are returned."""
    from app import proxy

    async def _mock_ensure_cloned(_username, _token):
        return False

    monkeypatch.setattr(proxy, "_ensure_settings_cloned", _mock_ensure_cloned)

    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repo_exists"] is False
    # Should include all expected default keys
    settings = data["settings"]
    assert "theme" in settings
    assert "fontSize" in settings
    assert "autoSave" in settings
    assert "commitMessage" in settings


async def test_save_settings_returns_no_repo_when_not_cloned(auth_client, monkeypatch):
    from app import proxy

    async def _mock_ensure_cloned(_username, _token):
        return False

    monkeypatch.setattr(proxy, "_ensure_settings_cloned", _mock_ensure_cloned)

    resp = await auth_client.post(
        "/api/settings",
        json={"settings": {"theme": "light"}, "snippets": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_repo"


# ---------------------------------------------------------------------------
# With a local settings repo
# ---------------------------------------------------------------------------

async def test_get_settings_reads_from_local_repo(auth_client, test_user, monkeypatch, tmp_path):
    """If a local .git dir exists, settings are read from files on disk."""
    from app import proxy

    # Build a fake local settings repo path
    settings_dir = tmp_path / "testuser" / "testuser-quarto-ed-settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / ".git").mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps({"theme": "light", "fontSize": 16})
    )
    (settings_dir / "snippets.json").write_text(json.dumps([{"label": "custom"}]))

    monkeypatch.setattr(proxy, "_WORKSPACE_BASE", str(tmp_path))

    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["repo_exists"] is True
    assert data["settings"]["theme"] == "light"
    assert data["settings"]["fontSize"] == 16
    # Default values are merged in for keys not in the file
    assert "autoSave" in data["settings"]
    assert data["snippets"] == [{"label": "custom"}]


async def test_save_settings_writes_to_local_repo(auth_client, monkeypatch, tmp_path):
    """POST /api/settings writes files when local repo exists."""
    from app import proxy

    settings_dir = tmp_path / "testuser" / "testuser-quarto-ed-settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / ".git").mkdir()

    monkeypatch.setattr(proxy, "_WORKSPACE_BASE", str(tmp_path))

    # Stub out the git push so we don't need a real remote
    async def _noop_push(_path, _msg=""):
        pass

    monkeypatch.setattr(proxy, "_push_settings", _noop_push)

    resp = await auth_client.post(
        "/api/settings",
        json={"settings": {"theme": "dark", "fontSize": 14}, "snippets": [{"label": "mysnip"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    saved = json.loads((settings_dir / "settings.json").read_text())
    assert saved["theme"] == "dark"
    snips = json.loads((settings_dir / "snippets.json").read_text())
    assert snips == [{"label": "mysnip"}]
