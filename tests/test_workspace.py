"""Tests for the workspace load and sync API."""
import subprocess
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

    # The autosave branch should have the direct commit message.
    from app.proxy import _AUTOSAVE_BRANCH
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", _AUTOSAVE_BRANCH, "--oneline", "-1"],
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

    from app.proxy import _AUTOSAVE_BRANCH
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", _AUTOSAVE_BRANCH, "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    )
    assert "feat: add doc" in log.stdout


# ---------------------------------------------------------------------------
# Autosave branch behavior
# ---------------------------------------------------------------------------

async def test_sync_creates_merge_commit_on_main(git_auth_client):
    """Sync must produce a merge commit on the default branch with the Sync: prefix."""
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)
    (workspace / "doc.qmd").write_text("# Hello\n")

    resp = await client.post(
        "/api/workspace/sync", json={"message": "my work"}
    )
    assert resp.status_code == 200

    default_branch = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "origin/HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().replace("origin/", "") or "main"
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", default_branch, "--oneline", "-1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert "Sync: my work" in log


async def test_sync_returns_to_autosave_branch(git_auth_client):
    """After sync the working tree must be back on the autosave branch."""
    from app.proxy import _AUTOSAVE_BRANCH
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)
    (workspace / "doc.qmd").write_text("content")

    await client.post("/api/workspace/sync", json={"message": "test"})

    branch = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == _AUTOSAVE_BRANCH


async def test_sync_multiple_times(git_auth_client):
    """Two consecutive syncs should both succeed."""
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)

    (workspace / "a.qmd").write_text("first")
    r1 = await client.post("/api/workspace/sync", json={"message": "first sync"})
    assert r1.status_code == 200

    (workspace / "b.qmd").write_text("second")
    r2 = await client.post("/api/workspace/sync", json={"message": "second sync"})
    assert r2.status_code == 200

    default_branch = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "origin/HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip().replace("origin/", "") or "main"
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", default_branch, "--oneline", "-2"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Sync: second sync" in log
    assert "Sync: first sync" in log
