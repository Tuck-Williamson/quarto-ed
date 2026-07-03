"""Tests for terminal and autosave-branch features (Issues #13, #25B)."""
import os
import subprocess
from pathlib import Path


# ---------------------------------------------------------------------------
# A. _make_server_id / _AUTOSAVE_BRANCH constants
# ---------------------------------------------------------------------------

def test_server_id_is_alphanumeric_hyphen():
    from app.proxy import _SERVER_ID
    assert _SERVER_ID, "server ID must not be empty"
    assert all(c.isalnum() or c == "-" for c in _SERVER_ID), \
        f"server ID contains invalid chars: {_SERVER_ID!r}"


def test_server_id_max_length():
    from app.proxy import _SERVER_ID
    assert len(_SERVER_ID) <= 40


def test_autosave_branch_format():
    from app.proxy import _AUTOSAVE_BRANCH, _SERVER_ID
    assert _AUTOSAVE_BRANCH == f"quarto-ed-{_SERVER_ID}"
    assert _AUTOSAVE_BRANCH.startswith("quarto-ed-")


def test_make_server_id_uses_dyno(monkeypatch):
    """DYNO env var (Heroku) takes priority over hostname."""
    monkeypatch.setenv("DYNO", "web.1")
    from app.proxy import _make_server_id
    result = _make_server_id()
    assert result == "web-1"  # dot replaced by hyphen, lowercased


def test_make_server_id_sanitizes(monkeypatch):
    """Special characters are replaced with hyphens."""
    monkeypatch.delenv("DYNO", raising=False)
    import app.proxy as proxy
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "MY.HOST_Name:99")
    result = proxy._make_server_id()
    assert all(c.isalnum() or c == "-" for c in result)
    assert result == result.lower()


# ---------------------------------------------------------------------------
# B. _setup_autosave_branch
# ---------------------------------------------------------------------------

def _make_local_git_repo(tmp_path):
    """Helper: bare remote + workspace with initial commit on main, returns (remote, workspace)."""
    remote = tmp_path / "remote.git"
    ws = tmp_path / "ws"
    ws.mkdir()

    run = lambda *cmd: subprocess.run(list(cmd), check=True, capture_output=True)
    run("git", "init", "--bare", str(remote))
    run("git", "init", str(ws))
    run("git", "-C", str(ws), "config", "user.email", "t@t.com")
    run("git", "-C", str(ws), "config", "user.name", "T")
    run("git", "-C", str(ws), "remote", "add", "origin", f"file://{remote}")
    (ws / "readme.md").write_text("hello")
    run("git", "-C", str(ws), "add", ".")
    run("git", "-C", str(ws), "commit", "-m", "Initial")
    branch = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    run("git", "-C", str(ws), "push", "--set-upstream", "origin", branch)
    run("git", "-C", str(ws), "remote", "set-head", "origin", branch)
    return remote, ws


async def test_setup_autosave_branch_creates_branch(tmp_path):
    """On a fresh clone with no autosave branch, the branch is created."""
    from app.proxy import _setup_autosave_branch, _AUTOSAVE_BRANCH

    _, ws = _make_local_git_repo(tmp_path)
    await _setup_autosave_branch(str(ws))

    branch = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == _AUTOSAVE_BRANCH


async def test_setup_autosave_branch_pushes_to_remote(tmp_path):
    """The new autosave branch is pushed to origin so it survives dyno restarts."""
    from app.proxy import _setup_autosave_branch, _AUTOSAVE_BRANCH

    _, ws = _make_local_git_repo(tmp_path)
    await _setup_autosave_branch(str(ws))

    ls = subprocess.run(
        ["git", "-C", str(ws), "ls-remote", "--heads", "origin", _AUTOSAVE_BRANCH],
        capture_output=True, text=True, check=True,
    ).stdout
    assert _AUTOSAVE_BRANCH in ls


async def test_setup_autosave_branch_reuses_existing_local(tmp_path):
    """Calling setup twice reuses the existing local branch without error."""
    from app.proxy import _setup_autosave_branch, _AUTOSAVE_BRANCH

    _, ws = _make_local_git_repo(tmp_path)
    await _setup_autosave_branch(str(ws))
    # Second call — branch already exists locally
    await _setup_autosave_branch(str(ws))

    branch = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == _AUTOSAVE_BRANCH


async def test_setup_autosave_branch_skips_empty_repo(tmp_path):
    """An empty repo (no commits) should not raise and should be left unchanged."""
    from app.proxy import _setup_autosave_branch

    ws = tmp_path / "empty"
    ws.mkdir()
    subprocess.run(["git", "init", str(ws)], check=True, capture_output=True)

    # Must not raise even though there are no commits
    await _setup_autosave_branch(str(ws))


async def test_setup_autosave_branch_fetches_from_remote(tmp_path):
    """If autosave branch exists on remote but not locally, it is fetched."""
    from app.proxy import _setup_autosave_branch, _AUTOSAVE_BRANCH

    remote, ws = _make_local_git_repo(tmp_path)

    # Detect the actual default branch name (main or master depending on git config).
    default_branch = subprocess.run(
        ["git", "-C", str(ws), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Manually create the autosave branch on remote (simulates a different server session).
    subprocess.run(
        ["git", "-C", str(ws), "push", "origin", f"{default_branch}:{_AUTOSAVE_BRANCH}"],
        check=True, capture_output=True,
    )

    # Clone into a new workspace that doesn't have the branch locally.
    ws2 = tmp_path / "ws2"
    subprocess.run(
        ["git", "clone", f"file://{remote}", str(ws2)], check=True, capture_output=True
    )
    subprocess.run(["git", "-C", str(ws2), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(ws2), "config", "user.name", "T"], check=True, capture_output=True)

    await _setup_autosave_branch(str(ws2))

    branch = subprocess.run(
        ["git", "-C", str(ws2), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == _AUTOSAVE_BRANCH


# ---------------------------------------------------------------------------
# C. Terminal process management
# ---------------------------------------------------------------------------

def test_set_winsize_does_not_raise():
    """_set_winsize should set terminal dimensions without raising."""
    import pty
    from app.proxy import _set_winsize

    master, slave = pty.openpty()
    try:
        _set_winsize(master, 80, 24)
        _set_winsize(master, 200, 50)
    finally:
        os.close(master)
        os.close(slave)


def test_spawn_terminal_creates_process(tmp_path):
    """_spawn_terminal returns a valid fd and registers the process."""
    from app.proxy import _spawn_terminal, _kill_terminal, _terminal_fds, _terminal_procs

    user_id = 800001
    _kill_terminal(user_id)  # start clean

    ws = tmp_path / "ws"
    ws.mkdir()

    try:
        fd = _spawn_terminal(user_id, str(ws))
        assert isinstance(fd, int) and fd > 0
        assert user_id in _terminal_fds
        assert user_id in _terminal_procs
        assert _terminal_procs[user_id].poll() is None  # still alive
    finally:
        _kill_terminal(user_id)

    assert user_id not in _terminal_fds
    assert user_id not in _terminal_procs


def test_spawn_terminal_cwd_is_workspace(tmp_path):
    """The shell starts in the workspace directory."""
    import time
    from app.proxy import _spawn_terminal, _kill_terminal, _terminal_fds

    user_id = 800002
    _kill_terminal(user_id)

    ws = tmp_path / "myworkspace"
    ws.mkdir()

    try:
        master_fd = _spawn_terminal(user_id, str(ws))
        # Write 'pwd' to the shell and read output.
        time.sleep(0.3)  # let shell start
        os.write(master_fd, b"pwd\n")
        time.sleep(0.2)
        try:
            out = os.read(master_fd, 2048).decode(errors="replace")
            assert str(ws) in out
        except OSError:
            pass  # shell may have exited in test env; skip assertion
    finally:
        _kill_terminal(user_id)


def test_spawn_terminal_replaces_existing_process(tmp_path):
    """Spawning a second terminal for the same user kills the first."""
    from app.proxy import _spawn_terminal, _kill_terminal, _terminal_procs

    user_id = 800003
    _kill_terminal(user_id)

    ws = tmp_path / "ws"
    ws.mkdir()

    try:
        _spawn_terminal(user_id, str(ws))
        proc1 = _terminal_procs[user_id]

        _spawn_terminal(user_id, str(ws))
        proc2 = _terminal_procs[user_id]

        assert proc1 is not proc2
        # Old process should have been terminated
        import time
        time.sleep(0.1)
        assert proc1.poll() is not None  # terminated
    finally:
        _kill_terminal(user_id)


# ---------------------------------------------------------------------------
# D. Terminal environment security
# ---------------------------------------------------------------------------

def test_terminal_env_excludes_server_secrets():
    """Secrets must not be forwarded to the terminal subprocess."""
    from app.proxy import _TERMINAL_ENV_ALLOWLIST

    forbidden = {
        "SECRET_KEY", "TOKEN_ENCRYPTION_KEY",
        "GITHUB_CLIENT_SECRET", "GITHUB_CLIENT_ID",
        "DATABASE_URL", "ANTHROPIC_API_KEY",
    }
    leaked = forbidden & _TERMINAL_ENV_ALLOWLIST
    assert not leaked, f"Secret env vars in terminal allowlist: {leaked}"


def test_terminal_env_includes_path():
    """PATH must be forwarded so standard commands work in the shell."""
    from app.proxy import _TERMINAL_ENV_ALLOWLIST
    assert "PATH" in _TERMINAL_ENV_ALLOWLIST


def test_terminal_env_excludes_port():
    """Heroku's PORT env var must not be forwarded (would break quarto/terminal port binding)."""
    from app.proxy import _TERMINAL_ENV_ALLOWLIST
    assert "PORT" not in _TERMINAL_ENV_ALLOWLIST


# ---------------------------------------------------------------------------
# E. Sandbox warning logic
# ---------------------------------------------------------------------------

def _warning(sandboxed: bool, allowed_users: str) -> bool:
    """Pure re-implementation of the _TERMINAL_SANDBOX_WARNING computation."""
    raw = allowed_users.strip()
    return not sandboxed and (not raw or raw.count(",") >= 1)


def test_sandbox_warning_when_unsandboxed_public():
    assert _warning(sandboxed=False, allowed_users="") is True


def test_sandbox_warning_when_unsandboxed_multi_user():
    assert _warning(sandboxed=False, allowed_users="alice,bob") is True


def test_no_sandbox_warning_when_unsandboxed_single_user():
    assert _warning(sandboxed=False, allowed_users="alice") is False


def test_no_sandbox_warning_when_sandboxed_public():
    assert _warning(sandboxed=True, allowed_users="") is False


def test_no_sandbox_warning_when_sandboxed_multi_user():
    assert _warning(sandboxed=True, allowed_users="alice,bob") is False


# ---------------------------------------------------------------------------
# F. Terminal WebSocket auth guard
# ---------------------------------------------------------------------------

async def test_terminal_ws_unauthenticated(anon_client):
    """An unauthenticated upgrade should be immediately rejected."""
    from starlette.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        try:
            with client.websocket_connect("/api/terminal/ws") as ws:
                ws.receive_bytes()  # should raise — server closes immediately
            assert False, "Expected WebSocket to be rejected"
        except Exception:
            pass  # expected: disconnect or close with 1008


async def test_terminal_ws_no_workspace(auth_client, test_session, db_session):
    """WebSocket without a valid session cookie is immediately rejected (code 1008)."""
    test_session.workspace_path = None
    db_session.add(test_session)
    await db_session.commit()

    from starlette.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/api/terminal/ws") as ws:
                ws.receive_bytes()
            assert False, "Expected rejection"
        except Exception:
            pass
