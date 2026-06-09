# quarto-ed

A SaaS service for editing [Quarto](https://quarto.org) documents in a browser-based web editor, authenticated via GitHub OAuth2.

## Architecture

- **FastAPI** — auth, workspace API, file API, settings API, AI chat (SSE), preview proxy
- **CodeMirror 6** — web editor with Markdown + code chunk syntax highlighting, CTRL+Space snippet engine
- **Quarto preview** — per-user `quarto preview` subprocess; browser preview streams live via HTTP+WS proxy
- **GitHub OAuth2** — user authentication; repos loaded using the user's access token
- **MySQL** — session and user storage (configured via `DATABASE_URL`)
- **Docker + Heroku** — single container, deployed via GitHub Actions → ghcr.io → Heroku

## Quick start (local)

**1. Create a GitHub OAuth App**

Go to [GitHub Developer Settings](https://github.com/settings/developers) → New OAuth App:
- Homepage URL: `http://localhost:8000`
- Callback URL: `http://localhost:8000/auth/callback`

**2. Create a `.env` file**

```bash
GITHUB_CLIENT_ID=your_client_id
GITHUB_CLIENT_SECRET=your_client_secret
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
TOKEN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
# Optional — enables AI writing assistant:
# ANTHROPIC_API_KEY=sk-ant-...
```

**3. Run**

```bash
docker compose up --build
```

Visit `http://localhost:8000`.

## Deploying to Heroku

**One-time setup:**

```bash
heroku create <app-name>
heroku stack:set container --app <app-name>
heroku config:set \
  DATABASE_URL=mysql+aiomysql://user:pass@host:3306/db \
  GITHUB_CLIENT_ID=<id> \
  GITHUB_CLIENT_SECRET=<secret> \
  SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  TOKEN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  ANTHROPIC_API_KEY=<key> \
  --app <app-name>
```

Set your GitHub OAuth App callback URL to `https://<app-name>.herokuapp.com/auth/callback`.

**Add GitHub Actions secrets** to your repo:
- `HEROKU_API_KEY` — from `heroku auth:token`
- `HEROKU_APP_NAME` — your Heroku app name

Push to `main` to trigger a build and deploy.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `PORT` | Set by Heroku | Port uvicorn listens on |
| `DATABASE_URL` | Yes | SQLAlchemy async URL (`mysql+aiomysql://` or `postgresql+asyncpg://`) |
| `GITHUB_CLIENT_ID` | Yes | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | Yes | GitHub OAuth App client secret |
| `SECRET_KEY` | Yes | 32-byte hex string for session cookie signing |
| `TOKEN_ENCRYPTION_KEY` | Yes | Fernet key for encrypting GitHub tokens at rest |
| `ANTHROPIC_API_KEY` | No | Enables AI writing assistant panel |
| `AI_MODEL` | No (default `claude-sonnet-4-6`) | Claude model ID for AI chat |
| `WORKSPACE_BASE` | No (default `/workspace`) | Base path for user workspaces |

## Known limitations

- **Ephemeral filesystem**: workspace files are lost on dyno restart. Use **Sync to GitHub** before closing.
- **Single worker**: process state (quarto preview port/pid) is in-memory; multiple uvicorn workers not supported.
