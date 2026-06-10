"""
Per-user OS-level sandboxing for the `quarto preview` subprocess.

`quarto preview` executes arbitrary user-authored code (Python via Jupyter,
R via knitr/rmarkdown, shell via bash chunks). Without privilege separation,
that code runs as the same UID as the FastAPI server -- root in the
container -- with full filesystem visibility, including other users'
workspaces and /proc/<pid>/environ of the server process itself.

This module gives each authenticated GitHub user a dedicated, deterministic
system account (`qe<user_id>`) and helpers to lock down their workspace
directory so only that account (and root) can access it. `spawn_quarto_preview`
in app/proxy.py runs the quarto subprocess as that account via
subprocess's `user=`/`group=` kwargs.

INVARIANT: root must never execute, import, or source code from inside
/workspace/<username>/... -- only read/write it directly (file API, git).
Workspace directories are made group-writable by the per-user account, so
executing user-supplied content as root would be a privilege-escalation
path back to root.
"""
import os
import pwd
import subprocess

_UID_GID_BASE = int(os.environ.get("SANDBOX_UID_BASE", "20000"))


def _account_name(user_id: int) -> str:
    return f"qe{user_id}"


def ensure_user_account(user_id: int) -> tuple[int, int]:
    """Idempotently create a system account `qe<user_id>` with a deterministic
    UID/GID (so ownership stays consistent across container restarts).

    Returns (uid, gid).
    """
    name = _account_name(user_id)
    try:
        pw = pwd.getpwnam(name)
        return pw.pw_uid, pw.pw_gid
    except KeyError:
        pass

    uid = _UID_GID_BASE + user_id
    subprocess.run(
        [
            "useradd",
            "--system",
            "--no-create-home",
            "--shell", "/usr/sbin/nologin",
            "--uid", str(uid),
            "--user-group",
            name,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    pw = pwd.getpwnam(name)
    return pw.pw_uid, pw.pw_gid


def ensure_dir_owned(path: str, uid: int, gid: int, mode: int = 0o700) -> None:
    """Create `path` if missing and ensure it is owned uid:gid with `mode`."""
    os.makedirs(path, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def _regroup_file(path: str, gid: int) -> None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return  # broken symlink etc.
    os.chown(path, -1, gid)
    owner_bits = st.st_mode & 0o700
    group_exec = 0o010 if owner_bits & 0o100 else 0
    os.chmod(path, owner_bits | group_exec | 0o060)  # owner unchanged, group rw(x)


def ensure_workspace_owned(base_path: str, user_id: int) -> None:
    """Lock down `base_path` (e.g. /workspace/<username>, the parent of all of
    a user's repos and their settings repo) so only root and the user's
    sandbox account can access it.

    Sets group ownership to the user's dedicated group with the setgid bit,
    so every file/directory created underneath -- by root (file API, git) or
    by the sandbox account (quarto preview, pip) -- is automatically group-
    owned by `qe<user_id>` and inaccessible to other users (mode *70).

    On first run for a given path, recursively re-groups any pre-existing
    content (relevant for local dev with a persistent workspace volume
    created before this sandboxing was added, or after a dyno restart wipes
    just the marker but leaves git data behind).
    """
    uid, gid = ensure_user_account(user_id)
    os.makedirs(base_path, exist_ok=True)

    marker = os.path.join(base_path, ".qe-sandbox-initialized")
    if not os.path.exists(marker):
        for root, _dirs, files in os.walk(base_path):
            os.chown(root, -1, gid)
            os.chmod(root, 0o2770)
            for f in files:
                _regroup_file(os.path.join(root, f), gid)
        with open(marker, "w"):
            pass

    os.chown(base_path, 0, gid)
    os.chmod(base_path, 0o2770)


def drop_privileges_kwargs(user_id: int) -> dict:
    """subprocess kwargs to run a child process as the user's sandbox account."""
    uid, gid = ensure_user_account(user_id)
    return {"user": uid, "group": gid}
