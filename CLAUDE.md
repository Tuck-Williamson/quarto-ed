# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Local development

Copy `.env.example` (or create `.env`) with the required vars, then:

```bash
docker compose up --build        # starts postgres + app on http://localhost:8000
docker compose up --build app    # rebuild only the app container
docker compose logs -f app       # tail app logs
```

There is no test suite yet. Manual verification is done by running the stack and walking through the OAuth login → load repo → edit → sync flow.

To generate the two secret keys locally:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"                          # SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEY
```

## Deployment

Pushes to `main` are blocked — open a PR from a feature/fix branch. The GitHub Actions workflow (`.github/workflows/docker-publish.yml`) runs on merge to `main` and:
1. Builds the Docker image with layer caching via GitHub Actions cache
2. Pushes `latest` + SHA-tagged image to `ghcr.io/tuck-williamson/quarto-ed`
3. Re-tags and pushes to `registry.heroku.com/quarto-ed/web`
4. Releases to Heroku via the formation PATCH API using the image ID from `docker inspect` (not the ghcr.io digest — Heroku requires the ID from its own registry)

Required GitHub repo secrets: `HEROKU_API_KEY`, `HEROKU_APP_NAME`.

Heroku config vars are set via the Heroku dashboard or API — never committed. The app reads `DATABASE_URL` from the environment and normalizes the scheme at startup (`postgres://` → `postgresql+asyncpg://`, `mysql://` → `mysql+aiomysql://`). Heroku's `PORT` env var is used by uvicorn; it must be stripped from the environment before spawning code-server subprocesses (code-server also reads `PORT` and would otherwise collide).

## Architecture

The entire service runs in a single Docker container managed by supervisord. Supervisord runs one program: uvicorn (single worker). code-server processes are children of uvicorn, spawned dynamically per user at runtime — not managed by supervisord.

```
Browser → Heroku router ($PORT)
            └── uvicorn (FastAPI, 1 worker)
                  ├── /login  /auth/callback        app/auth.py
                  ├── /editor  /api/*               app/proxy.py
                  └── /proxy/{path}  (WS + HTTP)    app/proxy.py
                        └── 127.0.0.1:8100-8200  (per-user code-server)
```

**Per-user code-server lifecycle** (`app/proxy.py`):  
Two module-level dicts (`_processes`, `_ports`) map `user_id → process/port`. `spawn_code_server()` allocates a free port (8100–8200), launches code-server as an asyncio subprocess with `--auth none --bind-addr 127.0.0.1:{port}`, polls until the port accepts connections (up to 60 s), then writes `cs_port`/`cs_pid` back to the `sessions` DB row. On dyno restart, the startup event nulls out all `cs_port`/`cs_pid` values since all processes are gone. Single worker is required because these dicts are in-process state.

**Proxy** (`app/proxy.py`):  
HTTP requests are forwarded via `httpx.AsyncClient`. WebSocket connections use `websockets.connect()` with bidirectional `asyncio.gather` tasks; both tasks catch `ConnectionClosed`/`WebSocketDisconnect` internally, and both done+pending tasks are awaited via `gather(return_exceptions=True)` after the first completes to prevent "Task exception never retrieved" noise. `Sec-WebSocket-Protocol` subprotocols must be forwarded — VS Code's JSON-RPC protocol negotiation depends on this.

**Auth** (`app/auth.py`):  
GitHub OAuth2 via `authlib`. The CSRF state is managed automatically by Starlette's `SessionMiddleware` (signed cookie, key = `SECRET_KEY`). On callback, the GitHub access token is encrypted with Fernet (`TOKEN_ENCRYPTION_KEY`) before being written to `users.access_token_encrypted`. It is decrypted only when needed (git clone URL construction, GitHub API calls). Rotating `TOKEN_ENCRYPTION_KEY` invalidates stored tokens; affected users re-authenticate on next login.

**Database** (`app/models.py`, `app/database.py`):  
Two tables: `users` (github_id unique index, encrypted token) and `sessions` (session_token unique index, FK to users, cs_port, cs_pid, workspace_path). Schema is created via `Base.metadata.create_all` on startup (idempotent). No Alembic. `pool_pre_ping=True` handles idle connection drops from Bluehost MySQL's `wait_timeout`; `pool_recycle=1800` proactively replaces connections every 30 minutes.

**Templates**:  
Starlette's Jinja2Templates. The new API (Starlette ≥ 0.36) requires `TemplateResponse(request, name, context)` — `request` is the first positional argument, not a key inside the context dict.

**Workspace flow**:  
`POST /api/workspace/load` clones the repo via `https://oauth2:{token}@github.com/...` into `/workspace/{username}/{repo}`, kills any running code-server for that user, and updates the session row. The editor iframe loads `/proxy/` which spawns code-server pointed at that directory. `POST /api/workspace/sync` runs `git add -A && git commit && git push`. Workspace is ephemeral (lost on dyno restart); the UI warns users and provides a Sync button.

## Key constraints

- **Single uvicorn worker** — multi-worker would require moving process state to a shared store (Redis or DB).
- **Heroku ephemeral filesystem** — `/workspace` is lost on every restart; users must sync to GitHub before closing.
- **Memory ceiling** — code-server uses ~400 MB. The current Heroku plan (512 MB) is tight; switching repos causes a brief spike. Upgrade to Standard-2X (1 GB) is recommended, or split into separate auth and editor dynos.
- **`PORT` stripping** — always strip `PORT` from the env dict passed to code-server subprocesses (`cs_env = {k: v for k, v in os.environ.items() if k != "PORT"}`).
