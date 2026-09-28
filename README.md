# quarto-ed

A SaaS platform for editing [Quarto](https://quarto.org) documents in a browser-based web editor, authenticated via GitHub OAuth2.

This code was a project of mine over the summer of 2026. 
My goal was to better understand what my students would encounter in the job market, given the recent advancements in AI.
A secondary goal was to make my life easier so I wouldn't need to lug around my heavy, battery-hungry laptop to work. I prep much of my class materials in Quarto. 

The secondary goal went away when the department provided me with a work laptop, so this is a usable product, but it has quirks and issues outstanding that I am not ironing out.
I tried to use AI for most of the actual work.
I was near 100% successful (occasionally, I ran out of credits and hand-tweaked some things).
I did try to review every commit thoroughly, but that said, this wasn't my job, and I had a lot going on this summer.

## CS Student Reflection Based on Agentic Work

My overall takeaway was that I needed to emphasize systems-level thinking while ensuring that they understand the interplay between different systems. 

While I used several different AI platforms to develop this project, I ultimately settled on Claude Code. 
It was **by FAR** the best agentic code development platform at the time. 
Despite that, I knew there were deep security issues with this project, inherent in the design (supporting a CLI toolchain that needed to install its own components dynamically). 
I prompted security analyses multiple times before having to point out major issues. 
After I did, the AI picked up on the kind of security issues by looking at similar products (Jupyter Notebooks), but I really needed to understand how putting CLI access on a web SaaS platform exposed them. 
There are only a few products that have similar security concerns, so until I connected those dots, AI was only picking up more typical security issues.

This has driven much of my lecturing for my Operating Systems course this semester.

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
| `ALLOWED_GITHUB_USERS` | No | Comma-separated GitHub usernames allowed to log in. See [Security model](#security-model) |

## Versioning

quarto-ed follows [Semantic Versioning](https://semver.org). The running
app reports its version via:

- The `/api/version` endpoint — returns JSON with the app version, git
  commit SHA, and the bundled Quarto version.
- The Preview panel toolbar in the editor, e.g. `quarto-ed 0.1.0 (abc1234)`.

When reporting a bug, please include this version string — see
[CONTRIBUTING.md](CONTRIBUTING.md) and the bug report template.

## Security model

`quarto preview` executes arbitrary user-authored code (Python, R, shell
chunks). `app/sandbox.py` isolates each user's preview process under its own
OS account -- but only when the app runs **as root**. Heroku's Common Runtime
(and most PaaS platforms) never run the app as root, so on those deployments
sandboxing is a no-op and **all logged-in users share the same OS user with
no filesystem isolation between them**.

If you can't run as root, set `ALLOWED_GITHUB_USERS` to a comma-separated
list of trusted GitHub usernames -- this is the access boundary in that case.
See [SECURITY.md](SECURITY.md) for details.

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
