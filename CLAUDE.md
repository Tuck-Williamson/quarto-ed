# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Local development

Copy `.env.example` (or create `.env`) with the required vars, then:

```bash
docker compose up --build          # starts postgres + auth (port 8000) + editor (port 8001)
docker compose up --build auth     # rebuild only the auth container
docker compose up --build editor   # rebuild only the editor container
docker compose logs -f auth        # tail auth logs
docker compose logs -f editor      # tail editor logs
```

Required `.env` vars for local dev: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`. `INTERNAL_TOKEN` defaults to `dev-internal-token` in compose.

To generate secret keys:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"                          # SECRET_KEY / INTERNAL_TOKEN
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEY
```

There is no test suite. Manual verification: OAuth login → load repo → edit → sync flow.

## Deployment

Pushes to `main` are blocked — open a PR from a feature/fix branch. On merge to `main`, the GitHub Actions workflow (`.github/workflows/docker-publish.yml`) runs two parallel jobs:

- **Auth job**: builds `Dockerfile` → pushes to `ghcr.io/.../quarto-ed-auth:latest` → re-tags to `registry.heroku.com/quarto-ed/web` → releases via Heroku formation PATCH API
- **Editor job**: builds `Dockerfile.editor` → pushes to `ghcr.io/.../quarto-ed-editor:latest` → re-tags to `registry.heroku.com/quarto-ed-editor/web` → releases via Heroku formation PATCH API

Heroku release uses `docker inspect --format='{{.Id}}'` after pushing to Heroku's registry — the image ID from Heroku's registry, not the ghcr.io digest.

Required GitHub secrets: `HEROKU_API_KEY`, `HEROKU_APP_NAME` (= `quarto-ed`), `HEROKU_EDITOR_APP_NAME` (= `quarto-ed-editor`).

Auth app Heroku config vars: `DATABASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, `INTERNAL_TOKEN`, `EDITOR_SERVICE_URL`.  
Editor app Heroku config vars: `DATABASE_URL`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, `INTERNAL_TOKEN`.

## Architecture

Two separate Heroku apps sharing one MySQL database (Bluehost):

```
Browser
  │
  ▼
quarto-ed  (auth, Standard-1X, ~100 MB)
FastAPI — uvicorn 1 worker
  ├── /login  /auth/callback          app/auth.py
  ├── /editor  /api/repos             app/proxy.py
  ├── /api/workspace/load|sync  ──────▶ editor /internal/workspace/*
  └── /proxy/{path}  (HTTP + WS)  ────▶ editor /proxy/{path}
                                         │
                                         ▼
                             quarto-ed-editor  (Standard-2X, 1 GB)
                             FastAPI — uvicorn 1 worker
                               ├── /internal/workspace/load|sync
                               ├── /internal/cs/{user_id}  (kill)
                               ├── /internal/reload
                               ├── /health
                               └── /proxy/{path}  ──▶ 127.0.0.1:8100-8200
                                                        per-user code-server
```

### Auth service (`app/`)

`app/proxy.py` is a **forwarder** — it no longer spawns processes. It validates the user session (DB lookup via `get_current_session`), then:
- For `/api/workspace/load|sync`: calls `POST {EDITOR_SERVICE_URL}/internal/workspace/*` with `X-Internal-Token` header, forwarding the GitHub token for git operations.
- For `/proxy/{path}` HTTP: forwards to `{EDITOR_SERVICE_URL}/proxy/{path}` with `X-Internal-Token`, `X-User-Id`, `X-Session-Id` headers.
- For `/proxy/{path}` WS: proxies to `{EDITOR_WS_URL}/proxy/{path}` using `websockets.connect(additional_headers=[...])` with the same internal auth headers.

Logout (`DELETE /api/session`) deletes the session from DB, then fire-and-forgets `DELETE {EDITOR_SERVICE_URL}/internal/cs/{user_id}` to kill the code-server. If the call fails, the editor service's idle cleanup task reclaims the process within 5 minutes.

### Editor service (`editor/`)

`editor/proxy.py` owns all code-server lifecycle logic (moved from `app/proxy.py`):
- `spawn_code_server` / `kill_code_server` / `_get_or_spawn` — same logic as before.
- All routes require `X-Internal-Token` header matching `INTERNAL_TOKEN` env var.
- User identity comes from `X-User-Id` / `X-Session-Id` headers (auth service validated the cookie).
- `cleanup_idle_processes()` runs as an asyncio background task, killing code-server processes for users with no active session in the DB (runs every 5 minutes).

`editor/main.py` startup: `Base.metadata.create_all` + clear `cs_port`/`cs_pid` + launch cleanup task.

### Shared code

`app/database.py`, `app/models.py`, `app/schemas.py` are shared by both services. The editor service imports them as `from app.database import ...` — both containers mount the `app/` package at `/app/app`.

### Database

Two tables: `users` (github_id unique, encrypted access token) and `sessions` (session_token unique, FK to users, cs_port, cs_pid, workspace_path, repo_owner, repo_name). `pool_pre_ping=True` + `pool_recycle=1800` handle Bluehost MySQL's idle-connection drops.

### Auth flow

GitHub OAuth2 via `authlib`. GitHub access token encrypted with Fernet (`TOKEN_ENCRYPTION_KEY`) before DB write. On workspace load, auth service decrypts the token and passes it plaintext to the editor service over HTTPS. `TOKEN_ENCRYPTION_KEY` rotation invalidates stored tokens — users re-authenticate on next login.

## Key constraints

- **Single uvicorn worker** per service — process dicts (`_processes`, `_ports`) in editor service are in-process state.
- **`PORT` stripping** — strip `PORT` from env before spawning code-server: `{k: v for k, v in os.environ.items() if k != "PORT"}`. Heroku sets `PORT`; code-server reads it and would otherwise bind on the wrong port.
- **Heroku ephemeral filesystem** — `/workspace` on the editor dyno is lost on restart. Users must sync to GitHub before closing.
- **Separate Heroku apps for HTTP routing** — Heroku only routes HTTP traffic to the `web` process type. Two separate apps (not `heroku.yml` multi-process) is required without Private Spaces.
- **WS double-proxy** — browser WS → auth service → editor service → code-server. The overhead is pure async I/O; memory impact on the auth service is negligible.
