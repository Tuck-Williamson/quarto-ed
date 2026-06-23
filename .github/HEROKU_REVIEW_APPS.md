# Heroku Review Apps Setup

This document explains how to configure Heroku review apps for automatic deployment of pull requests.

## What are Heroku Review Apps?

Review apps are temporary Heroku deployments created automatically for each pull request. They allow you to test changes in a live environment before merging to production.

## Prerequisites

1. **Heroku Account** — Team or personal account with access to `quarto-ed` app
2. **Heroku Pipeline** — Already set up linking the main app to a pipeline
3. **GitHub Secrets** — Required secrets must be configured in the repository

## Required GitHub Secrets

Configure these secrets in **Settings > Secrets and variables > Actions**:

### Existing Secrets (already set up for production)
- `HEROKU_API_KEY` — Your Heroku API key
- `HEROKU_APP_NAME` — `quarto-ed` (production app name)

### New Secrets for Review Apps

**1. `HEROKU_REVIEW_APP_GITHUB_CLIENT_ID`**
- Create a new GitHub OAuth App at https://github.com/settings/applications/new with:
  - **Application name:** `quarto-ed (review apps)`
  - **Homepage URL:** `https://quarto-ed-pr-<NUMBER>.herokuapp.com` (template, will vary per PR)
  - **Authorization callback URL:** `https://quarto-ed-pr-<NUMBER>.herokuapp.com/auth/callback`
  - Note: Heroku review apps don't have predictable URLs until deployed, so use wildcard or template
- Copy the **Client ID** to this secret

**2. `HEROKU_REVIEW_APP_GITHUB_CLIENT_SECRET`**
- From the same GitHub OAuth App created above
- Copy the **Client Secret** to this secret

**3. `HEROKU_REVIEW_APP_TOKEN_ENCRYPTION_KEY`**
- Generate a Fernet key (same format as production):
  ```bash
  python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- Copy the output to this secret

**4. `HEROKU_REVIEW_APP_ALLOWED_USERS`** (optional)
- Comma-separated list of GitHub usernames allowed to log in to review apps
- Example: `Tuck-Williamson,octocat,contributor1`
- If not set, all users can attempt to log in (but will fail auth)

## Heroku Pipeline Configuration

### 1. Create the Pipeline (if not already done)

```bash
heroku pipelines:create quarto-ed --app quarto-ed
```

### 2. Add Staging (optional but recommended)

```bash
# Connect a staging app to the pipeline (if you have one)
heroku pipeline:connect quarto-ed quarto-ed-staging
```

### 3. Enable Review Apps

Via **Heroku Dashboard**:
1. Go to **Apps** → **Pipelines** → **quarto-ed**
2. Click **Enable Review Apps**
3. Check **Create new review apps for pull requests**
4. Check **Automatically destroy review apps when PRs are closed**
5. Select **Docker image** as the source (pre-selected since we're using containers)
6. Click **Enable**

Or via Heroku CLI:
```bash
heroku pipelines:setup quarto-ed --yes --team <your-team>
```

## GitHub OAuth App Configuration

The review app GitHub OAuth App needs its callback URL updated after the first review app is created:

1. Create the first PR to trigger review app creation
2. Wait for the review app to be deployed
3. Note the review app URL from the PR comment (e.g., `https://quarto-ed-pr-123.herokuapp.com`)
4. Update the GitHub OAuth App at https://github.com/settings/applications/:
   - **Authorization callback URL:** Change from template to actual review app URL
   - Or use a custom domain with a wildcard (if your GitHub app supports it)

## Workflow

### Creating a Review App

1. **Open a Pull Request** from a feature branch to `main`
2. **Automatic Workflow Triggers**:
   - `.github/workflows/docker-publish.yml` builds and pushes the Docker image with tag `pr-<NUMBER>`
   - `.github/workflows/heroku-review-apps.yml` creates/updates the review app
3. **Deployment** — Review app is deployed with the PR-tagged Docker image
4. **Notification** — A PR comment posts the review app URL

### Testing a Review App

1. Click the review app URL from the PR comment
2. Log in with your GitHub account (must be in `HEROKU_REVIEW_APP_ALLOWED_USERS` or unrestricted)
3. Test your changes in the live environment
4. All features work as in production: OAuth, file sync, preview, etc.

### Closing a Review App

1. **Merge or close the PR**
2. **Automatic Cleanup** — Heroku automatically destroys the review app (configured in pipeline settings)
3. All data is lost (intentional — review apps are ephemeral)

## Troubleshooting

### Review App Deployment Failed

Check the **Heroku Dashboard** → **Apps** → **quarto-ed-pr-<NUMBER>** → **Logs**:
- `Permission denied (publickey)` — SSH key issue, likely with git clones
- `Application error R10` — App crashed during startup; check `.env` vars
- `Build error` — Docker image issues; check GitHub Actions logs

### OAuth Login Fails

- Verify `HEROKU_REVIEW_APP_GITHUB_CLIENT_ID` and `HEROKU_REVIEW_APP_GITHUB_CLIENT_SECRET` are set
- Check that the GitHub OAuth App's callback URL matches the review app URL
- Verify your GitHub username is in `HEROKU_REVIEW_APP_ALLOWED_USERS` (if set)

### Review App Won't Start

- Database connection issues — PostgreSQL addon may not have initialized; retry
- Missing environment variables — Check `HEROKU_REVIEW_APP_TOKEN_ENCRYPTION_KEY` is set
- Check app logs: `heroku logs -a quarto-ed-pr-<NUMBER> --tail`

### Cleanup: Manual Review App Deletion

```bash
heroku apps:destroy quarto-ed-pr-<NUMBER> --confirm quarto-ed-pr-<NUMBER>
```

## Costs

Review apps incur Heroku costs:
- **Dyno hours** — Small dyno (0.5x) is included in most plans
- **PostgreSQL** — Essential-0 plan (included or low cost)
- **Total** — Usually $0–$7 per review app per month

Automatic cleanup on PR close keeps costs minimal.

## See Also

- [Heroku Review Apps Documentation](https://devcenter.heroku.com/articles/github-integration-review-apps)
- [Docker Deployments on Heroku](https://devcenter.heroku.com/articles/container-registry-and-runtime)
- [GitHub OAuth Apps Setup](https://docs.github.com/en/developers/apps/building-oauth-apps)
