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

### Review apps (PRs)

`app.json` configures a Heroku Pipeline "Review Apps" stage so each pull
request gets an ephemeral preview deployment with its own database. GitHub
OAuth login on a review app requires a dedicated review-app OAuth App whose
callback URL currently points at that review app — only one review app can
do OAuth login at a time, and the maintainer updates the callback URL as
needed when reviewing a specific PR.

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

## Versioning

quarto-ed follows [Semantic Versioning](https://semver.org). The running
app reports its version via:

- The `/api/version` endpoint — returns JSON with the app version, git
  commit SHA, and the bundled Quarto version.
- The Preview panel toolbar in the editor, e.g. `quarto-ed 0.1.0 (abc1234)`.

When reporting a bug, please include this version string — see
[CONTRIBUTING.md](CONTRIBUTING.md) and the bug report template.

## Known limitations

- **Ephemeral filesystem**: workspace files are lost on dyno restart. Use **Sync to GitHub** before closing.
- **Single worker**: process state (quarto preview port/pid) is in-memory; multiple uvicorn workers not supported.

## Addendum: per-user sandbox accounts on non-ephemeral deployments

`quarto preview` runs as a dedicated, deterministic system account (`qe<user_id>`,
UID `20000 + user_id`) per authenticated user, with `/workspace/<username>`
locked down to that account's group (see `app/sandbox.py`). This is created
lazily on first use and never removed.

On Heroku's standard **ephemeral filesystem**, this is a non-issue — the
account and workspace directory disappear on every dyno restart along with
the rest of `/workspace`.

If you run quarto-ed on a host with **persistent storage** (a custom Heroku
setup with a mounted volume, or any non-Heroku deployment with a durable
`/workspace`), the `qe<user_id>` account and its workspace directory will
persist indefinitely, even after a user revokes GitHub access or is removed
from the `users` table. This is **not** a cross-user security risk — the
directory remains owned by that user's dedicated group at mode `2770`, so it
stays inaccessible to other `qe<user_id>` accounts — but it does mean stale
accounts and disk usage accumulate for users who no longer use the service.

If that matters for your deployment, periodically reconcile against the
`users` table and, for any `user_id` no longer present:

```bash
userdel qe<user_id>
groupdel qe<user_id>   # if not removed automatically
rm -rf /workspace/<username>
```

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for
development setup, the PR workflow, CI checks, and our versioning policy.

By participating, you're expected to follow our
[Code of Conduct](CODE_OF_CONDUCT.md). Use the
[issue templates](.github/ISSUE_TEMPLATE/) to report bugs or request
features.
