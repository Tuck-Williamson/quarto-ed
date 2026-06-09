"""Tests for the workspace load and sync API."""
from pathlib import Path


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

async def test_load_unauthenticated(anon_client):
    resp = await anon_client.post(
        "/api/workspace/load",
        json={"repo_owner": "user", "repo_name": "repo"},
    )
    assert resp.status_code == 401


async def test_sync_unauthenticated(anon_client):
    resp = await anon_client.post(
        "/api/workspace/sync", json={"message": "test"}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Sync requires a loaded workspace
# ---------------------------------------------------------------------------

async def test_sync_no_workspace(auth_client, test_session, db_session):
    """Sync returns 400 when no workspace is loaded on the session."""
    test_session.workspace_path = None
    db_session.add(test_session)
    await db_session.commit()

    resp = await auth_client.post(
        "/api/workspace/sync", json={"message": "save"}
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Sync with a real local git repo
# ---------------------------------------------------------------------------

async def test_sync_commits_new_file(git_auth_client):
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)

    # Add a new file to the workspace
    (workspace / "new_document.qmd").write_text("# New\n")

    resp = await client.post(
        "/api/workspace/sync", json={"message": "add new document"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Verify the file was committed
    import subprocess
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    )
    assert "add new document" in log.stdout


async def test_sync_idempotent_when_nothing_to_commit(git_auth_client):
    """Syncing with no changes should succeed without error."""
    client, sess = git_auth_client

    resp = await client.post(
        "/api/workspace/sync", json={"message": "no changes"}
    )
    assert resp.status_code == 200


async def test_sync_custom_commit_message(git_auth_client):
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)
    (workspace / "doc.qmd").write_text("content")

    resp = await client.post(
        "/api/workspace/sync", json={"message": "feat: add doc"}
    )
    assert resp.status_code == 200

    import subprocess
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    )
    assert "feat: add doc" in log.stdout
