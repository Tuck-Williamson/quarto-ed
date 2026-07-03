"""
Integration tests that exercise multiple app components together.
These use a real git workspace (no GitHub required) and cover the main
user flows: editor page → file edit → save → sync.
"""
import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Editor page + file listing
# ---------------------------------------------------------------------------

async def test_editor_page_loads(auth_client):
    resp = await auth_client.get("/editor")
    assert resp.status_code == 200
    # CodeMirror and key UI landmarks must be present
    assert "codemirror" in resp.text.lower()
    assert "preview" in resp.text.lower()


async def test_repos_api_requires_auth(anon_client):
    resp = await anon_client.get("/api/repos")
    assert resp.status_code == 401


async def test_preview_logs_empty_when_no_preview(auth_client):
    resp = await auth_client.get("/api/preview/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert "lines" in data
    assert "running" in data
    assert data["running"] is False


# ---------------------------------------------------------------------------
# Full edit → save → sync flow
# ---------------------------------------------------------------------------

async def test_create_write_and_list_file(auth_client, test_session):
    """Create a file, write content, verify it appears in the file tree."""
    workspace = Path(test_session.workspace_path)

    # Create
    resp = await auth_client.post("/api/file/create", json={"path": "doc.qmd"})
    assert resp.status_code == 200

    # Write content
    content = "---\ntitle: Integration Test\nformat: html\n---\n\n# Hello\n"
    resp = await auth_client.post("/api/file", json={"path": "doc.qmd", "content": content})
    assert resp.status_code == 200

    # Read back
    resp = await auth_client.get("/api/file", params={"path": "doc.qmd"})
    assert resp.status_code == 200
    assert resp.json()["content"] == content

    # Appears in listing
    resp = await auth_client.get("/api/files")
    names = [e["name"] for e in resp.json()["tree"]]
    assert "doc.qmd" in names


async def test_write_then_sync(git_auth_client):
    """Write a file and then sync (commit + push) to the local git remote."""
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)

    # Write a quarto document
    qmd = "---\ntitle: Synced\nformat: html\n---\n\n# Synced doc\n"
    resp = await client.post("/api/file", json={"path": "synced.qmd", "content": qmd})
    assert resp.status_code == 200

    # Sync
    resp = await client.post(
        "/api/workspace/sync", json={"message": "integration: add synced.qmd"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # File committed in git history
    import subprocess
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    assert "integration: add synced.qmd" in log.stdout


async def test_multiple_file_edits_sync_together(git_auth_client):
    """Multiple file writes are bundled in a single sync commit."""
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)

    for name in ["a.qmd", "b.qmd", "c.qmd"]:
        await client.post("/api/file", json={"path": name, "content": f"# {name}"})

    resp = await client.post(
        "/api/workspace/sync", json={"message": "batch: add a b c"}
    )
    assert resp.status_code == 200

    import subprocess
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--short"],
        capture_output=True, text=True, check=True,
    )
    # After sync, working tree should be clean (everything committed + pushed)
    assert status.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Settings integration
# ---------------------------------------------------------------------------

async def test_settings_defaults_then_save_then_read(auth_client, monkeypatch, tmp_path):
    """Full settings round-trip: read defaults → save custom → read custom."""
    from app import proxy

    settings_dir = tmp_path / "testuser" / "testuser-quarto-ed-settings"
    settings_dir.mkdir(parents=True)
    (settings_dir / ".git").mkdir()

    monkeypatch.setattr(proxy._core, "_WORKSPACE_BASE", str(tmp_path))

    async def _noop_push(_path, _msg=""):
        pass

    monkeypatch.setattr(proxy._core, "_push_settings", _noop_push)

    # Read — should return defaults merged with empty file
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json()["settings"]["theme"] in ("dark", "light")

    # Save custom values
    custom = {"theme": "light", "fontSize": 18, "tabSize": 4}
    resp = await auth_client.post("/api/settings", json={"settings": custom, "snippets": []})
    assert resp.status_code == 200

    # Read back — custom values must be present
    resp = await auth_client.get("/api/settings")
    assert resp.status_code == 200
    s = resp.json()["settings"]
    assert s["theme"] == "light"
    assert s["fontSize"] == 18
    assert s["tabSize"] == 4
