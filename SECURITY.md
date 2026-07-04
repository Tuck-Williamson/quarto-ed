# Security Policy

## Multi-user isolation and deployment trust model

`quarto preview` executes arbitrary user-authored code (Python, R, and shell
chunks) on behalf of whoever is logged in. `app/sandbox.py` isolates each
authenticated user's `quarto preview` process under a dedicated, unprivileged
OS account (`qe<user_id>`) with a locked-down workspace directory -- but this
**requires the app to run as root**.

Most container platforms, including **Heroku's Common Runtime, never run the
app as root** -- every dyno already runs as its own fixed, unprivileged user
with setuid/setgid escalation disabled at the kernel level. On these
platforms, `app/sandbox.py` detects this (`sandboxing_available()` returns
`False`) and becomes a no-op: every user's `quarto preview` process runs as
the app's own uid, with **no filesystem isolation between users**. A
malicious or compromised account could read other users' workspace files (and
potentially the app's own environment, depending on the platform's `/proc`
restrictions).

**If you can't run as root** (Heroku, most PaaS deployments), set
`ALLOWED_GITHUB_USERS` to a comma-separated allowlist of trusted GitHub
usernames -- this is the access boundary between users on those deployments.
Leaving it unset allows *any* GitHub account to log in and run code in the
shared, unsandboxed environment.

**If you run as root** (local Docker, or a self-hosted deployment with a
privileged/root container), the per-user sandbox in `app/sandbox.py` applies
automatically and `ALLOWED_GITHUB_USERS` is optional.

## OAuth scope

Login requests the GitHub `repo` scope, which grants read/write access to
**all** of the user's repositories (public and private), not just the one being
edited. This is required to clone/pull/push arbitrary repos the user selects and
to create the per-user settings repo. The encrypted access token therefore has a
broad blast radius: keep `TOKEN_ENCRYPTION_KEY` secret, and prefer running the
app only for trusted users (`ALLOWED_GITHUB_USERS`).

## Browser hardening

App pages are served with a `Content-Security-Policy` plus `X-Frame-Options`,
`X-Content-Type-Options`, and `Referrer-Policy` headers (see `app/main.py`). All
JS and CSS are compiled locally (esbuild + Tailwind) and served from `'self'`, so
the CSP needs no script/style CDN origins — the only external origins are Google
Fonts and a browser-direct Ollama endpoint, and exfiltration to arbitrary hosts
is blocked. It is intentionally *not* applied to the quarto preview proxy
(`/api/preview/*`), whose rendered output ships its own CDN assets. A
browser-direct Ollama endpoint other than `localhost:11434` requires adding its
origin via the `CSP_CONNECT_SRC_EXTRA` env var.

## Supported Versions

quarto-ed is pre-1.0 and under active development. Only the latest released
version is supported with security updates.

| Version  | Supported          |
|----------|--------------------|
| latest   | :white_check_mark: |
| < latest | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not open a public
GitHub issue**.

Please use [GitHub's private vulnerability reporting](https://github.com/Tuck-Williamson/quarto-ed/security/advisories/new)
(Security tab → "Report a vulnerability") for this repository. This allows us
to discuss and fix the issue privately before public disclosure.

When reporting, please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a proof-of-concept if possible
- Any suggested mitigations

We'll do our best to acknowledge reports within a few days and keep you
updated as we work on a fix.

> **Note for maintainers**: private vulnerability reporting must be enabled
> under repo Settings → Security → "Private vulnerability reporting" for the
> link above to work.
