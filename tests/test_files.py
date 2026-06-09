"""Tests for the file browser and file CRUD API."""
from pathlib import Path


# ---------------------------------------------------------------------------
# Auth guards
# ---------------------------------------------------------------------------

async def test_list_files_unauthenticated(anon_client):
    resp = await anon_client.get("/api/files")
    assert resp.status_code == 401


async def test_read_file_unauthenticated(anon_client):
    resp = await anon_client.get("/api/file", params={"path": "test.qmd"})
    assert resp.status_code == 401


async def test_write_file_unauthenticated(anon_client):
    resp = await anon_client.post("/api/file", json={"path": "x.qmd", "content": "x"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

async def test_list_files_empty_workspace(auth_client):
    resp = await auth_client.get("/api/files")
    assert resp.status_code == 200
    assert resp.json()["tree"] == []


async def test_list_files_shows_files(auth_client, test_session):
    workspace = Path(test_session.workspace_path)
    (workspace / "index.qmd").write_text("# Hello")
    (workspace / "chapter1.qmd").write_text("# Chapter")

    resp = await auth_client.get("/api/files")
    assert resp.status_code == 200
    names = [e["name"] for e in resp.json()["tree"]]
    assert "index.qmd" in names
    assert "chapter1.qmd" in names


async def test_list_files_shows_subdirectories(auth_client, test_session):
    workspace = Path(test_session.workspace_path)
    subdir = workspace / "docs"
    subdir.mkdir()
    (subdir / "page.qmd").write_text("# Page")

    resp = await auth_client.get("/api/files")
    tree = resp.json()["tree"]
    dirs = [e for e in tree if e["type"] == "dir"]
    assert any(d["name"] == "docs" for d in dirs)


async def test_list_files_skips_hidden(auth_client, test_session):
    workspace = Path(test_session.workspace_path)
    (workspace / ".hidden").write_text("secret")
    (workspace / "visible.qmd").write_text("# Visible")

    resp = await auth_client.get("/api/files")
    names = [e["name"] for e in resp.json()["tree"]]
    assert ".hidden" not in names
    assert "visible.qmd" in names


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def test_read_file(auth_client, test_session):
    workspace = Path(test_session.workspace_path)
    (workspace / "hello.qmd").write_text("Hello world")

    resp = await auth_client.get("/api/file", params={"path": "hello.qmd"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "Hello world"
    assert resp.json()["path"] == "hello.qmd"


async def test_read_file_not_found(auth_client):
    resp = await auth_client.get("/api/file", params={"path": "nonexistent.qmd"})
    assert resp.status_code == 404


async def test_read_file_in_subdirectory(auth_client, test_session):
    workspace = Path(test_session.workspace_path)
    (workspace / "sub").mkdir()
    (workspace / "sub" / "page.qmd").write_text("Subpage content")

    resp = await auth_client.get("/api/file", params={"path": "sub/page.qmd"})
    assert resp.status_code == 200
    assert resp.json()["content"] == "Subpage content"


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def test_write_file_creates_file(auth_client, test_session):
    resp = await auth_client.post(
        "/api/file", json={"path": "new.qmd", "content": "# New"}
    )
    assert resp.status_code == 200
    assert Path(test_session.workspace_path, "new.qmd").read_text() == "# New"


async def test_write_file_overwrites_existing(auth_client, test_session):
    workspace = Path(test_session.workspace_path)
    (workspace / "edit.qmd").write_text("old content")

    resp = await auth_client.post(
        "/api/file", json={"path": "edit.qmd", "content": "new content"}
    )
    assert resp.status_code == 200
    assert (workspace / "edit.qmd").read_text() == "new content"


async def test_write_file_creates_parent_dirs(auth_client, test_session):
    resp = await auth_client.post(
        "/api/file", json={"path": "nested/deep/file.qmd", "content": "deep"}
    )
    assert resp.status_code == 200
    assert Path(test_session.workspace_path, "nested", "deep", "file.qmd").exists()


# ---------------------------------------------------------------------------
# Create (new empty file)
# ---------------------------------------------------------------------------

async def test_create_file(auth_client, test_session):
    resp = await auth_client.post("/api/file/create", json={"path": "brand_new.qmd"})
    assert resp.status_code == 200
    assert Path(test_session.workspace_path, "brand_new.qmd").exists()


async def test_create_file_conflict(auth_client, test_session):
    workspace = Path(test_session.workspace_path)
    (workspace / "exists.qmd").write_text("already here")

    resp = await auth_client.post("/api/file/create", json={"path": "exists.qmd"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Path traversal protection
# ---------------------------------------------------------------------------

async def test_read_traversal_blocked(auth_client):
    resp = await auth_client.get("/api/file", params={"path": "../../../etc/passwd"})
    assert resp.status_code == 403


async def test_write_traversal_blocked(auth_client):
    resp = await auth_client.post(
        "/api/file", json={"path": "../../etc/cron.d/evil", "content": "evil"}
    )
    assert resp.status_code == 403


async def test_create_traversal_blocked(auth_client):
    resp = await auth_client.post(
        "/api/file/create", json={"path": "../outside.txt"}
    )
    assert resp.status_code == 403
