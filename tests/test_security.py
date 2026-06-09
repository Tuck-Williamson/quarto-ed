"""
Security regression tests.

Sections:
  A. _quarto_env() — secrets are filtered from the quarto subprocess env
  B. _redact()     — token redaction helper works correctly
  C. _validate_github_name() — unit tests + HTTP API integration
  D. Cross-user IDOR via path traversal
  E. Command injection resilience in subprocess calls
  F. Quarto render integration — Python chunk cannot read server secrets
     (covers the SQL-via-DATABASE_URL vector as well)
"""
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import HTTPException

QUARTO_DIR = Path(__file__).parent / "quarto"

quarto = pytest.mark.skipif(
    shutil.which("quarto") is None, reason="quarto not installed"
)


def _jupyter_available() -> bool:
    try:
        import ipykernel  # noqa: F401
        import nbclient   # noqa: F401
        import nbformat   # noqa: F401
        return True
    except ImportError:
        return False


jupyter_available = pytest.mark.skipif(
    not _jupyter_available(), reason="ipykernel/nbclient/nbformat not available"
)


# ---------------------------------------------------------------------------
# A. _quarto_env() — allowlist-based env filtering
# ---------------------------------------------------------------------------

def test_quarto_env_excludes_server_secrets():
    """No secret env var must reach the quarto subprocess."""
    from app.proxy import _quarto_env
    env = _quarto_env(user_id=1)
    for var in (
        "SECRET_KEY", "TOKEN_ENCRYPTION_KEY",
        "GITHUB_CLIENT_SECRET", "GITHUB_CLIENT_ID",
        "DATABASE_URL", "ANTHROPIC_API_KEY",
    ):
        assert var not in env, f"{var} must not be forwarded to the quarto subprocess"


def test_quarto_env_includes_path_and_sets_home():
    """PATH must be preserved; HOME must point to the per-user workspace dir."""
    from app.proxy import _quarto_env
    env = _quarto_env(user_id=42)
    assert "PATH" in env
    assert "42" in env["HOME"]


def test_quarto_env_does_not_contain_arbitrary_vars(monkeypatch):
    """A variable not in the allowlist must be excluded even if present."""
    monkeypatch.setenv("_CUSTOM_NOT_ALLOWED", "should_not_appear")
    from app.proxy import _quarto_env
    env = _quarto_env(user_id=1)
    assert "_CUSTOM_NOT_ALLOWED" not in env


# ---------------------------------------------------------------------------
# B. _redact() — token redaction helper
# ---------------------------------------------------------------------------

def test_redact_replaces_secret():
    from app.proxy import _redact
    result = _redact("some text with secret_value inside", "secret_value")
    assert result == "some text with *** inside"
    assert "secret_value" not in result


def test_redact_replaces_all_occurrences():
    from app.proxy import _redact
    result = _redact("token abc123 and again abc123 here", "abc123")
    assert result == "token *** and again *** here"


def test_redact_preserves_non_secret_content():
    from app.proxy import _redact
    result = _redact("fatal: repo not found", "my_token")
    assert result == "fatal: repo not found"


def test_redact_handles_empty_secret():
    from app.proxy import _redact
    result = _redact("some message", "")
    assert result == "some message"


def test_redact_handles_multiple_secrets():
    from app.proxy import _redact
    result = _redact("token=abc and key=xyz", "abc", "xyz")
    assert "abc" not in result
    assert "xyz" not in result
    assert result == "token=*** and key=***"


def test_redact_git_error_url_pattern():
    """Simulates the exact git stderr pattern where the access token leaks."""
    from app.proxy import _redact
    token = "ghp_abc123secrettoken"
    git_error = (
        f"fatal: repository 'https://oauth2:{token}@github.com/user/repo.git/' not found"
    )
    result = _redact(git_error, token)
    assert token not in result
    assert "***" in result
    assert "github.com/user/repo.git" in result


# ---------------------------------------------------------------------------
# C. _validate_github_name() — unit tests + HTTP API
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["valid-repo", "My.Repo_1", "a", "org-name"])
def test_validate_github_name_accepts_valid(name):
    from app.proxy import _validate_github_name
    _validate_github_name(name, "repo_name")  # must not raise


@pytest.mark.parametrize("name", [
    "../../etc",
    "../other",
    "has/slash",
    ".hidden",
    "a..b",
    "",
])
def test_validate_github_name_rejects_unsafe(name):
    from app.proxy import _validate_github_name
    with pytest.raises(HTTPException) as exc:
        _validate_github_name(name, "repo_name")
    assert exc.value.status_code == 400


async def test_load_workspace_rejects_dotdot_repo_name(auth_client):
    resp = await auth_client.post(
        "/api/workspace/load",
        json={"repo_owner": "testuser", "repo_name": "../../etc"},
    )
    assert resp.status_code == 400


async def test_load_workspace_rejects_dotdot_repo_owner(auth_client):
    resp = await auth_client.post(
        "/api/workspace/load",
        json={"repo_owner": "../other-user", "repo_name": "valid-repo"},
    )
    assert resp.status_code == 400


async def test_load_workspace_rejects_slash_in_name(auth_client):
    resp = await auth_client.post(
        "/api/workspace/load",
        json={"repo_owner": "testuser", "repo_name": "a/b"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# D. Cross-user IDOR via path traversal
# ---------------------------------------------------------------------------

async def test_cannot_read_into_sibling_workspace(auth_client, test_session, tmp_path):
    """Path traversal that would land in another user's workspace must be blocked."""
    victim_dir = tmp_path / "otheruser" / "their-repo"
    victim_dir.mkdir(parents=True)
    (victim_dir / "secret.qmd").write_text("TOP SECRET")

    rel = os.path.relpath(str(victim_dir / "secret.qmd"), test_session.workspace_path)
    resp = await auth_client.get("/api/file", params={"path": rel})
    assert resp.status_code == 403


async def test_cannot_write_into_sibling_workspace(auth_client, test_session, tmp_path):
    """Write traversal into another user's namespace must also be blocked."""
    victim_dir = tmp_path / "otheruser" / "their-repo"
    victim_dir.mkdir(parents=True)

    rel = os.path.relpath(str(victim_dir / "evil.qmd"), test_session.workspace_path)
    resp = await auth_client.post("/api/file", json={"path": rel, "content": "pwned"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# E. Command injection resilience
# ---------------------------------------------------------------------------

async def test_sync_shell_metacharacters_not_executed(git_auth_client, tmp_path):
    """Commit message containing shell metacharacters must not trigger shell execution.

    create_subprocess_exec (not shell=True) passes the message as a single
    argument to git, so the shell never interprets the metacharacters.
    """
    client, sess = git_auth_client
    workspace = Path(sess.workspace_path)
    (workspace / "doc.qmd").write_text("content")

    marker = tmp_path / "injection_marker.txt"
    payload = f"legit message; echo INJECTED > {marker}"

    resp = await client.post("/api/workspace/sync", json={"message": payload})
    assert resp.status_code in (200, 500)
    assert not marker.exists(), "Shell metacharacters in commit message triggered execution"


# ---------------------------------------------------------------------------
# F. Quarto render integration — env leakage + SQL-via-DATABASE_URL
# ---------------------------------------------------------------------------

@quarto
@jupyter_available
def test_quarto_python_chunk_cannot_read_server_secrets(tmp_path):
    """
    Renders a Python code chunk that tries to read SECRET_KEY and DATABASE_URL.
    Both are set by conftest to known test values; neither must appear in the
    rendered HTML because _quarto_env() excludes them.

    This also covers the SQL injection vector: without DATABASE_URL, a user
    cannot write Python code to connect directly to the production database.
    """
    from app.proxy import _quarto_env

    secret_key_val = os.environ["SECRET_KEY"]
    database_url_val = os.environ["DATABASE_URL"]

    qmd = tmp_path / "leak_test.qmd"
    qmd.write_text(textwrap.dedent("""\
        ---
        format: html
        ---
        ```{python}
        import os
        print(os.environ.get('SECRET_KEY',   'SECRET_KEY_NOT_PRESENT'))
        print(os.environ.get('DATABASE_URL', 'DATABASE_URL_NOT_PRESENT'))
        ```
    """))

    env = _quarto_env(user_id=1)
    env["QUARTO_PYTHON"] = sys.executable

    result = subprocess.run(
        ["quarto", "render", str(qmd), "--output-dir", str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    html = next(tmp_path.glob("*.html")).read_text()
    assert secret_key_val not in html, "SECRET_KEY leaked into rendered quarto output"
    assert database_url_val not in html, "DATABASE_URL leaked into rendered quarto output"
    assert "SECRET_KEY_NOT_PRESENT" in html
    assert "DATABASE_URL_NOT_PRESENT" in html
