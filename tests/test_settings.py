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

    async def _mock_ensure_cloned(_user_id, _username, _token):
        return False

    monkeypatch.setattr(proxy._core, "_ensure_settings_cloned", _mock_ensure_cloned)

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

    async def _mock_ensure_cloned(_user_id, _username, _token):
        return False

    monkeypatch.setattr(proxy._core, "_ensure_settings_cloned", _mock_ensure_cloned)

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

    monkeypatch.setattr(proxy._core, "_WORKSPACE_BASE", str(tmp_path))

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

    monkeypatch.setattr(proxy._core, "_WORKSPACE_BASE", str(tmp_path))

    # Stub out the git push so we don't need a real remote
    async def _noop_push(_path, _msg=""):
        pass

    monkeypatch.setattr(proxy._core, "_push_settings", _noop_push)

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


# ---------------------------------------------------------------------------
# Repo-level settings (POST /api/settings/repo, GET /api/settings)
# ---------------------------------------------------------------------------

async def test_save_repo_settings_no_workspace(auth_client, db_session, test_user):
    """POST /api/settings/repo returns 400 when no workspace is loaded."""
    from app.models import Session as AppSession
    from app.main import app
    from app.proxy import require_session
    import secrets

    # Create a session with no workspace_path
    token = secrets.token_hex(32)
    sess_no_ws = AppSession(
        session_token=token,
        user_id=test_user.id,
        workspace_path=None,
    )
    db_session.add(sess_no_ws)
    await db_session.commit()
    await db_session.refresh(sess_no_ws)

    # save_repo_settings depends on require_workspace, which derives from
    # require_session; overriding the latter with a workspace-less session makes
    # the workspace check fire.
    app.dependency_overrides[require_session] = lambda: sess_no_ws

    resp = await auth_client.post("/api/settings/repo", json={"settings": {"theme": "light"}})
    assert resp.status_code == 400



async def test_save_repo_settings_writes_and_get_reads(git_auth_client, monkeypatch, tmp_path):
    """Full round-trip: write repo settings then read them back via GET."""
    from app import proxy

    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)

    # Write repo override
    resp = await client.post(
        "/api/settings/repo",
        json={"settings": {"theme": "light", "tabSize": 4}},
    )
    assert resp.status_code == 200

    repo_file = workspace / ".quarto-ed-settings"
    assert repo_file.exists()
    saved = json.loads(repo_file.read_text())
    assert saved == {"theme": "light", "tabSize": 4}

    # Read back: need a settings repo too — stub clone to fail so we hit early-return
    async def _mock_ensure_cloned(_user_id, _username, _token):
        return False

    monkeypatch.setattr(proxy._core, "_ensure_settings_cloned", _mock_ensure_cloned)

    resp2 = await client.get("/api/settings")
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["repo_settings"] == {"theme": "light", "tabSize": 4}
    assert data["settings"]["theme"] == "light"
    assert data["settings"]["tabSize"] == 4
    assert data["workspace_loaded"] is True


async def test_save_repo_settings_deletes_file_when_empty(git_auth_client):
    """POST /api/settings/repo with empty dict removes the file."""
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)

    # Create the file first
    repo_file = workspace / ".quarto-ed-settings"
    repo_file.write_text(json.dumps({"theme": "light"}))

    resp = await client.post("/api/settings/repo", json={"settings": {}})
    assert resp.status_code == 200
    assert not repo_file.exists()


async def test_load_repo_settings_ignores_non_dict_json(git_auth_client, monkeypatch):
    """Non-dict .quarto-ed-settings (e.g. a JSON array) must not crash GET /api/settings."""
    from app import proxy

    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)
    (workspace / ".quarto-ed-settings").write_text("[1, 2, 3]")

    async def _mock_ensure_cloned(_user_id, _username, _token):
        return False

    monkeypatch.setattr(proxy._core, "_ensure_settings_cloned", _mock_ensure_cloned)

    resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["repo_settings"] == {}


async def test_get_settings_includes_new_fields(auth_client, test_user, monkeypatch, tmp_path):
    """GET /api/settings response now includes global_settings, repo_settings, workspace_loaded."""
    from app import proxy

    settings_dir = tmp_path / "testuser" / "testuser-quarto-ed-settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / ".git").mkdir()
    (settings_dir / "settings.json").write_text(json.dumps({"theme": "light"}))

    monkeypatch.setattr(proxy._core, "_WORKSPACE_BASE", str(tmp_path))

    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "global_settings" in data
    assert "repo_settings" in data
    assert "workspace_loaded" in data
    assert data["global_settings"]["theme"] == "light"
    assert data["repo_settings"] == {}


async def test_save_repo_settings_strips_unknown_keys(git_auth_client):
    """Unknown keys in the payload are silently dropped."""
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)

    resp = await client.post(
        "/api/settings/repo",
        json={"settings": {"theme": "light", "__proto__": "bad", "evil": True}},
    )
    assert resp.status_code == 200
    saved = json.loads((workspace / ".quarto-ed-settings").read_text())
    assert "evil" not in saved
    assert "__proto__" not in saved
    assert saved == {"theme": "light"}
