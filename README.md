# quarto-ed

A SaaS service for editing [Quarto](https://quarto.org) documents in a browser-based VSCode environment (code-server), authenticated via GitHub OAuth2.

## Architecture

- **FastAPI** — auth, workspace API, HTTP + WebSocket proxy to code-server
- **code-server** — VSCode in the browser with the Quarto extension pre-installed
- **GitHub OAuth2** — user authentication; repos loaded using the user's access token
- **PostgreSQL or MySQL** — session and user storage (configured via `DATABASE_URL`)
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
```

**3. Run**

```bash
docker compose up --build
```

Visit `http://localhost:8000`.

## Deploying to Heroku

**One-time setup:**

```bash
# Create the app and set container stack
heroku create <app-name>
heroku stack:set container --app <app-name>

# Option A: Heroku-managed PostgreSQL
heroku addons:create heroku-postgresql:essential-0 --app <app-name>

# Option B: External database (Supabase, Neon, RDS, etc.)
heroku config:set DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db --app <app-name>

# Required secrets
heroku config:set \
  GITHUB_CLIENT_ID=<id> \
  GITHUB_CLIENT_SECRET=<secret> \
  SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  TOKEN_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
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
| `DATABASE_URL` | Yes | SQLAlchemy connection URL (postgres or mysql) |
| `GITHUB_CLIENT_ID` | Yes | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | Yes | GitHub OAuth App client secret |
| `SECRET_KEY` | Yes | 32-byte hex string for session cookie signing |
| `TOKEN_ENCRYPTION_KEY` | Yes | Fernet key for encrypting GitHub tokens at rest |
| `CODE_SERVER_PORT_MIN` | No (default 8100) | Start of internal port range for code-server |
| `CODE_SERVER_PORT_MAX` | No (default 8200) | End of internal port range (max 100 concurrent users) |
| `WORKSPACE_BASE` | No (default `/workspace`) | Base path for user workspaces |

## Known limitations

- **Ephemeral filesystem**: workspace files are lost on dyno restart. Use **Sync to GitHub** before closing.
- **Single worker**: max ~100 concurrent users per dyno (one code-server per user, ports 8100–8200).
