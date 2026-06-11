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


# ---------------------------------------------------------------------------
# G. Per-user OS sandbox — cross-user isolation via UID/GID separation
#
# quarto preview executes Python/R/bash chunks. Without privilege
# separation those chunks run as the same UID as the server (root) with
# full filesystem visibility. spawn_quarto_preview now drops to a dedicated
# `qe<user_id>` account (app/sandbox.py) and locks workspace dirs to mode
# 2770. These tests require root (useradd/setuid), so they only run inside
# the Docker-based CI image (Dockerfile.test), not arbitrary local `pytest`.
# ---------------------------------------------------------------------------

requires_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="sandbox tests require root (useradd/setuid)"
)


def _r_available() -> bool:
    return shutil.which("Rscript") is not None


r_available = pytest.mark.skipif(not _r_available(), reason="R (Rscript) not installed")


@requires_root
def test_ensure_user_account_idempotent():
    from app.sandbox import ensure_user_account
    uid1, gid1 = ensure_user_account(900001)
    uid2, gid2 = ensure_user_account(900001)
    assert (uid1, gid1) == (uid2, gid2)
    assert uid1 >= 20000


@requires_root
def test_ensure_workspace_owned_blocks_other_users():
    """A workspace locked down by ensure_workspace_owned() is readable by its
    own sandbox account but denied to a different user's account.

    Uses a directory under _WORKSPACE_BASE rather than tmp_path: pytest's
    tmp_path lives under /tmp/pytest-of-root/... which is mode 0700, so a
    dropped-privilege account can't even traverse into it -- that would mask
    the permission check this test is actually verifying.
    """
    from app.proxy import _WORKSPACE_BASE
    from app.sandbox import ensure_user_account, ensure_workspace_owned

    base_a = Path(_WORKSPACE_BASE) / "isolation_test_usera"
    try:
        base_a.mkdir(parents=True)
        (base_a / "secret.txt").write_text("only user A should see this")
        os.makedirs(_WORKSPACE_BASE, exist_ok=True)
        os.chmod(_WORKSPACE_BASE, 0o755)
        ensure_workspace_owned(str(base_a), 900002)

        uid_b, gid_b = ensure_user_account(900003)
        denied = subprocess.run(
            ["cat", str(base_a / "secret.txt")],
            user=uid_b, group=gid_b,
            capture_output=True, text=True,
        )
        assert denied.returncode != 0
        assert "Permission denied" in denied.stderr

        uid_a, gid_a = ensure_user_account(900002)
        allowed = subprocess.run(
            ["cat", str(base_a / "secret.txt")],
            user=uid_a, group=gid_a,
            capture_output=True, text=True,
        )
        assert allowed.returncode == 0
        assert "only user A" in allowed.stdout
    finally:
        shutil.rmtree(base_a, ignore_errors=True)


@requires_root
def test_dropped_privilege_cannot_read_parent_environ():
    """The sandboxed account must not be able to read the server process's
    /proc/<pid>/environ (DATABASE_URL, SECRET_KEY, etc.) -- this is the
    /proc-based bypass of the _quarto_env() allowlist that exists as long
    as the quarto subprocess runs as the same UID as the server."""
    from app.sandbox import ensure_user_account

    uid, gid = ensure_user_account(900004)
    result = subprocess.run(
        ["cat", f"/proc/{os.getpid()}/environ"],
        user=uid, group=gid,
        capture_output=True, text=True,
    )
    assert result.returncode != 0


def test_quarto_env_sets_python_bin_and_path():
    """When a per-repo venv interpreter is supplied, QUARTO_PYTHON points at
    it and its bin/ dir is prepended to PATH (so `!pip install` in a chunk
    also targets the venv)."""
    from app.proxy import _quarto_env
    env = _quarto_env(user_id=1, python_bin="/workspace/u/repo/.venv/bin/python3")
    assert env["QUARTO_PYTHON"] == "/workspace/u/repo/.venv/bin/python3"
    assert env["PATH"].startswith("/workspace/u/repo/.venv/bin")


@requires_root
@quarto
async def test_ensure_quarto_venv_creates_isolated_python():
    """_ensure_quarto_venv creates a --system-site-packages venv owned by the
    sandbox account, and adds .venv/ to .gitignore.

    Uses a directory under _WORKSPACE_BASE rather than tmp_path: pytest's
    tmp_path lives under /tmp/pytest-of-root/... which is mode 0700, so the
    dropped-privilege account can't even traverse into it to create the venv.
    """
    from app.proxy import _WORKSPACE_BASE, _ensure_quarto_venv, _quarto_env
    from app.sandbox import ensure_dir_owned, ensure_user_account, ensure_workspace_owned

    user_id = 900005
    base = Path(_WORKSPACE_BASE) / "venv_test_user"
    repo = base / "repo"
    try:
        repo.mkdir(parents=True)
        os.makedirs(_WORKSPACE_BASE, exist_ok=True)
        os.chmod(_WORKSPACE_BASE, 0o755)
        ensure_workspace_owned(str(base), user_id)
        uid, gid = ensure_user_account(user_id)

        env = _quarto_env(user_id)
        ensure_dir_owned(env["HOME"], uid, gid)

        python_bin = await _ensure_quarto_venv(str(repo), user_id, env)

        assert os.path.exists(python_bin)
        assert os.stat(repo / ".venv").st_uid == uid

        cfg = (repo / ".venv" / "pyvenv.cfg").read_text()
        assert "include-system-site-packages = true" in cfg

        gitignore = (repo / ".gitignore").read_text()
        assert ".venv/" in gitignore.splitlines()
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(Path(_WORKSPACE_BASE) / str(user_id), ignore_errors=True)


@requires_root
@quarto
@jupyter_available
@r_available
def test_cross_user_isolation_python_r_bash_chunks():
    """
    Reproduces the original report: Python/R/bash code chunks in user A's
    document try to read user B's workspace (e.g.
    `ls -lah --recursive / | grep A_file_we_should_not_see.txt`). With the
    per-user UID sandbox, none of the three engines should be able to see
    the other user's file -- only their own.

    Each language is rendered as its own document: a single .qmd mixing
    {python} and {r} chunks is executed by quarto's knitr+reticulate engine,
    which isn't installed in this image, and isn't representative of how
    quarto-ed documents normally use one engine per file anyway.
    """
    from app.proxy import _WORKSPACE_BASE, _quarto_env
    from app.sandbox import ensure_dir_owned, ensure_user_account, ensure_workspace_owned

    user_a, user_b = 900010, 900011
    base_a = Path(_WORKSPACE_BASE) / "usera900010"
    base_b = Path(_WORKSPACE_BASE) / "userb900011"

    try:
        repo_a = base_a / "repo"
        repo_b = base_b / "repo"
        repo_a.mkdir(parents=True)
        repo_b.mkdir(parents=True)

        secret = repo_b / "A_file_we_should_not_see.txt"
        secret.write_text("TOP SECRET - belongs to user B")
        (repo_a / "my_file.txt").write_text("hello from user A")

        os.makedirs(_WORKSPACE_BASE, exist_ok=True)
        os.chmod(_WORKSPACE_BASE, 0o755)
        ensure_workspace_owned(str(base_a), user_a)
        ensure_workspace_owned(str(base_b), user_b)
        uid_a, gid_a = ensure_user_account(user_a)

        wb = str(_WORKSPACE_BASE)

        # echo: false -- otherwise quarto echoes the chunk *source* into the
        # HTML, and the source literally contains the string
        # "A_file_we_should_not_see.txt" as a search target, which would
        # trip the leakage assertion below even though nothing was found.
        #
        # engine: explicit -- without it, quarto defaults every doc in this
        # render to the jupyter engine (since QUARTO_PYTHON points at a
        # jupyter-capable interpreter), and the jupyter engine doesn't know
        # how to execute {r}/{bash} cells -- it just echoes them as literal
        # code blocks regardless of `echo: false`, which would also trip the
        # leakage assertion.
        probes = {
            "probe_python.qmd": textwrap.dedent("""\
                ---
                format: html
                engine: jupyter
                ---

                ```{python}
                #| echo: false
                import os
                found = []
                for root, dirs, files in os.walk("__WORKSPACE_BASE__"):
                    if "A_file_we_should_not_see.txt" in files:
                        found.append(root)
                print("PYTHON_FOUND:", found or "NONE")

                with open("my_file.txt") as f:
                    print("PYTHON_OWN_FILE:", f.read().strip())
                ```
            """),
            "probe_r.qmd": textwrap.dedent("""\
                ---
                format: html
                engine: knitr
                ---

                ```{r}
                #| echo: false
                found <- list.files("__WORKSPACE_BASE__", pattern = "A_file_we_should_not_see.txt",
                                     recursive = TRUE, all.files = TRUE, full.names = TRUE)
                cat("R_FOUND:", if (length(found) == 0) "NONE" else paste(found, collapse=","), "\\n")
                ```
            """),
            "probe_bash.qmd": textwrap.dedent("""\
                ---
                format: html
                engine: knitr
                ---

                ```{bash}
                #| echo: false
                echo "BASH_FOUND: $(find __WORKSPACE_BASE__ -name A_file_we_should_not_see.txt 2>/dev/null || true)"
                ```
            """),
        }

        env = _quarto_env(user_a)
        ensure_dir_owned(env["HOME"], uid_a, gid_a)
        env["QUARTO_PYTHON"] = sys.executable

        rendered = {}
        for name, content in probes.items():
            qmd = repo_a / name
            qmd.write_text(content.replace("__WORKSPACE_BASE__", wb))

            result = subprocess.run(
                ["quarto", "render", str(qmd), "--output-dir", str(repo_a)],
                env=env,
                user=uid_a,
                group=gid_a,
                cwd=str(repo_a),
                capture_output=True, text=True, timeout=180,
            )
            assert result.returncode == 0, result.stderr
            rendered[name] = (repo_a / name.replace(".qmd", ".html")).read_text()

        for html in rendered.values():
            assert "A_file_we_should_not_see.txt" not in html
            assert "TOP SECRET" not in html

        assert "PYTHON_FOUND: NONE" in rendered["probe_python.qmd"]
        assert "PYTHON_OWN_FILE: hello from user A" in rendered["probe_python.qmd"]
        assert "R_FOUND: NONE" in rendered["probe_r.qmd"]
        assert "BASH_FOUND: " in rendered["probe_bash.qmd"]
    finally:
        shutil.rmtree(base_a, ignore_errors=True)
        shutil.rmtree(base_b, ignore_errors=True)
        shutil.rmtree(Path(_WORKSPACE_BASE) / str(user_a), ignore_errors=True)
