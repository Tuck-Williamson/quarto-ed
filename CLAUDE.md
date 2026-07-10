# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Local development

Copy `.env.example` (or create `.env`) with the required vars, then:

```bash
docker compose up --build          # starts postgres + app (port 8000)
docker compose up --build app      # rebuild only the app container
docker compose logs -f app         # tail app logs
```

Required `.env` vars for local dev: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`. Optional: `ANTHROPIC_API_KEY` (enables AI chat), `AI_MODEL` (default `claude-sonnet-4-6`).

To generate secret keys:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"                          # SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEY
```

### Tests

The suite lives in `tests/` (pytest + pytest-asyncio, in-memory SQLite). Run it
against the built image with `scripts/run-tests.sh` (builds `app:ci` → `app:test`,
then runs the passes in parallel and combines coverage). The passes:

- **Pass 1 (root):** full fast suite; `requires_root` sandbox tests run.
- **Pass 2 (non-root):** `test_security.py` under an unprivileged user so
  `requires_non_root` tests run. Skipped in GitHub Actions (`CI` set) because
  `su` to another user isn't available there.
- **PDF suite (`-m pdf`):** slow TinyTeX PDF renders. Runs locally always, and in
  CI only on `main` (i.e. PR-merge builds); excluded from normal PR runs via
  `-m "not pdf"`. Enable elsewhere with `RUN_PDF_TESTS=1`.

Beyond automated tests, manually verify the OAuth login → load repo → edit →
sync flow for changes touching that path.

## Deployment

Pushes to `main` are blocked — open a PR from a feature/fix branch. On merge to `main`, the GitHub Actions workflow (`.github/workflows/docker-publish.yml`) runs a single job:

- Builds `Dockerfile` → pushes to `ghcr.io/.../quarto-ed:latest` → re-tags to `registry.heroku.com/quarto-ed/web` → releases via Heroku formation PATCH API

Heroku release uses `docker inspect --format='{{.Id}}'` after pushing to Heroku's registry — the image ID from Heroku's registry, not the ghcr.io digest.

Required GitHub secrets: `HEROKU_API_KEY`, `HEROKU_APP_NAME` (= `quarto-ed`).

Heroku config vars: `DATABASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, `ANTHROPIC_API_KEY` (optional), `AI_MODEL` (optional).

## Architecture

Single Heroku app (`quarto-ed`, Standard-2X, 1 GB), single uvicorn worker:

```
Browser
  │
  ▼
quarto-ed  (FastAPI — uvicorn 1 worker)
  ├── /login  /auth/callback              app/auth.py
  ├── /editor                             3-pane web editor SPA
  ├── /api/repos                          list GitHub repos
  ├── /api/workspace/load|sync            git clone/pull/push
  ├── /api/files  /api/file               file browser + read/write/create
  ├── /api/settings  /api/settings/repo   settings repo management
  ├── /api/ai/chat  (SSE)  ─────────────▶ Anthropic API
  └── /api/preview/{path}  (HTTP + WS)  ──▶ 127.0.0.1:8100-8200
                                             per-user quarto preview
```

### Auth service (`app/auth.py`)

GitHub OAuth2 via `authlib`. GitHub access token encrypted with Fernet (`TOKEN_ENCRYPTION_KEY`) before DB write. `TOKEN_ENCRYPTION_KEY` rotation invalidates stored tokens — users re-authenticate on next login.

### Proxy / editor (`app/proxy.py`)

Owns all quarto preview lifecycle:
- `spawn_quarto_preview(user_id, workspace_path, session_id)` — allocates port (8100–8200), runs `quarto preview {path} --port {port} --host 127.0.0.1 --no-browser`, strips `PORT` from env, polls until port accepts connections.
- `kill_quarto_preview(user_id)` — terminate → kill with timeouts.
- `_get_or_spawn_preview(user_id, sess)` — reuse live process or spawn fresh.
- Preview HTTP/WS proxy: same `httpx` + `websockets` bidirectional pattern.
- File API: `_safe_path()` prevents traversal; reads/writes UTF-8 text files.
- Settings API: reads/writes `settings.json` + `snippets.json` in a per-user GitHub repo (`{username}-quarto-ed-settings`), auto-cloned on access, pushed on every save.
- AI chat: streams Anthropic API via `httpx.AsyncClient.stream()` → `StreamingResponse(text/event-stream)`.

### Frontend build (Tailwind + esbuild)

Both CSS and JS are compiled by a real build step (the `assets` stage in the
`Dockerfile`, `node:20-slim`) and served from `/static` — no runtime CDN. Config
in `package.json` (`npm run build` = `build:css` + `build:js`); outputs are
build artifacts (git-ignored), regenerate locally with `npm run build`.

- **CSS** — `app/static/src/input.css` (theme in `tailwind.config.js`) →
  `tailwindcss --minify` → `app/static/app.css`. Components authored utility-first
  with `@apply`; palette in CSS custom properties so the light/dark toggle works.
- **JS** — `app/static/src/editor.js` (the editor SPA module) →
  `esbuild --bundle --minify --splitting` → `app/static/dist/` (entry
  `editor.js` + code-split language chunks + `editor.css` from the imported xterm
  stylesheet). Bundling gives esbuild a single deduped `@codemirror/state`
  instance (the CDN import map served three conflicting versions → "multiple
  instances" errors) and keeps CodeMirror/xterm/ansi_up local.

`StaticFiles` is mounted at `/static` in `app/main.py`. Because everything is
local, the CSP needs no script/style CDN origins (only Google Fonts + a
browser-direct Ollama endpoint remain) and drops `'unsafe-eval'`.

### Editor SPA (`app/templates/editor.html`)

Three-pane layout (CodeMirror 6 + xterm bundled locally via esbuild — see the
frontend build above; source lives in `app/static/src/editor.js`):
- **Left**: collapsible file browser (tree), "+ New file" button
- **Center**: CodeMirror 6 editor with tabs, dirty indicator, CTRL+S to save
- **Right**: switchable Preview (iframe → `/api/preview/`) / AI Chat (SSE) / Settings

CTRL+Space triggers the snippet engine (`autocompletion` with custom `completionSource`). Built-in snippets: R/Python/Bash code chunks, 5 callout types, panel tabset, column layout, figure with cross-ref, 4 YAML front matter templates. User-defined custom snippets are persisted to the settings repo.

Settings panel auto-pushes to the settings repo 1.5 s after any change. First-time users are prompted to create the settings repo via a modal.

### Shared code

`app/database.py`, `app/models.py`, `app/schemas.py`.

### Database

Two tables: `users` (github_id unique, encrypted access token) and `sessions` (session_token unique, FK to users, cs_port, cs_pid, workspace_path, repo_owner, repo_name). `cs_port`/`cs_pid` store the quarto preview port/pid. `pool_pre_ping=True` + `pool_recycle=1800` handle Bluehost MySQL's idle-connection drops.

### Auth flow

GitHub OAuth2 via `authlib`. On workspace load, auth service decrypts the token and uses it for git clone URLs and GitHub API calls (settings repo creation, repo listing).

## Key constraints

- **Single uvicorn worker** — process dicts (`_preview_processes`, `_preview_ports`) in `app/proxy.py` are in-process state; multiple workers would require a shared store.
- **`PORT` stripping** — strip `PORT` from env before spawning quarto preview: `{k: v for k, v in os.environ.items() if k != "PORT"}`. Heroku sets `PORT`; quarto preview reads it and would otherwise bind on the wrong port.
- **Heroku ephemeral filesystem** — `/workspace` on the dyno is lost on restart. Users must sync to GitHub before closing.
- **Settings repo re-clone** — on dyno restart the local settings repo dir is gone; `GET /api/settings` re-clones it automatically if the GitHub repo exists.
