# Contributing to quarto-ed

Thanks for your interest in contributing! This document covers how to get a
local dev environment running, the PR workflow, and our versioning policy.

## Development setup

See the [Quick start (local)](README.md#quick-start-local) section of the
README for full setup instructions: creating a GitHub OAuth App, configuring
your `.env` file, and running `docker compose up --build`.

## Testing locally

**Contributors are expected to run the test suite locally before opening a PR.**
CI will run the same suite, but catching failures locally is faster and cheaper
for everyone.

### Prerequisites

- Docker (Desktop or Engine) running locally
- No other setup — the test script builds the full production image before
  running tests, so the environment is identical to CI

### Running the tests

```bash
bash scripts/run-tests.sh
```

This performs three steps:

1. **Build** `app:ci` (production image) and `app:test` (test layer on top)
2. **Run tests in two passes** inside Docker:
   - *Pass 1 (root)* — full test suite; sandbox tests that require `root`
     (privilege-drop, user isolation) run here
   - *Pass 2 (non-root)* — `tests/test_security.py` re-run as an unprivileged
     user; tests that require a non-root environment (fallback sandbox paths)
     run here
3. **Print results**:
   - A `coverage combine` step merges both passes into a single coverage
     report and writes `scripts/test-results/coverage.xml`
   - A colour-coded per-test table (`scripts/summarize_test_results.py`)
     shows each test's pass / fail / skip status per pass:
     - **Green** — passed in at least one pass, no failures
     - **Yellow** — skipped in every pass it appeared in (expected for
       tests guarded by `requires_root` / `requires_non_root` that are
       inapplicable to the current user context)
     - **Red bold** — at least one failure
     - Red flashing — unexpected outcome (should not occur; indicates a
       bug in the test infrastructure)

Raw artifacts (JUnit XML, `.coverage.*`, `coverage.xml`) are written to
`scripts/test-results/` (gitignored) for post-run inspection.

### Build only (no test run)

```bash
bash scripts/run-tests.sh --no-run
```

Builds both images without running tests — useful for iterating on
`Dockerfile` changes.

### Writing tests

- All tests live in `tests/`. pytest is configured in `pytest.ini`.
- Tests that require root (sandbox privilege-drop) use the `@requires_root`
  marker defined at the top of `tests/test_security.py`; they are automatically
  skipped in pass 2.
- Tests that require a non-root process use `@requires_non_root`; they skip
  in pass 1 and run in pass 2.
- For everything else, plain `async def test_*` (no marker needed).

## Branch and PR workflow

1. Fork the repository and create a feature branch off `main`.
2. Make your changes, following the existing code style and patterns.
3. **Run `bash scripts/run-tests.sh` locally** and ensure all tests pass (see
   [Testing locally](#testing-locally) above).
4. Add or update tests in `tests/` for any behavioral changes.
5. Update `CHANGELOG.md` (see below).
6. Open a pull request against `main` using the PR template — fill out the
   description, link any related issues, and complete the checklist.

## CI checks

Every pull request runs the following checks (see
`.github/workflows/docker-publish.yml`):

- **`pip-audit`** — scans Python dependencies for known vulnerabilities
- **`bandit`** — static security analysis of `app/`
- **`trivy`** — container image vulnerability scan
- **`pytest`** (via `Dockerfile.test`) — runs the test suite inside the
  production image

All checks must pass before a PR can be merged.

## Versioning

quarto-ed follows [Semantic Versioning](https://semver.org). The current
version is defined in a single place: `__version__` in `app/__init__.py`.

- The running app reports its version (and the git commit it was built from)
  via the `/api/version` endpoint, and shows it in the editor's Preview panel
  (e.g. `quarto-ed 0.1.0 (abc1234)`).
- **When to bump the version**: if your change is user-facing (a fix, a new
  feature, a breaking change), bump `__version__` following semver
  (`MAJOR.MINOR.PATCH`). Internal refactors, CI/docs-only changes, etc. do
  not need a version bump.
- **What happens when you bump it**: when a PR that changes `__version__` is
  merged to `main`, CI automatically:
  1. Tags the resulting commit `vX.Y.Z`
  2. Pushes a matching `vX.Y.Z` Docker image tag to `ghcr.io`
  3. Creates a GitHub Release for `vX.Y.Z` with auto-generated notes

  You don't need to create tags or releases manually.

## Updating the changelog

Add an entry to the `[Unreleased]` section of `CHANGELOG.md` describing your
change, following the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
format (`Added`, `Changed`, `Fixed`, `Removed`, etc.). If your change doesn't
affect users (e.g. internal tooling, CI), you can skip this — the PR template
checklist gives you the option to note that.

## Reporting bugs and requesting features

Please use the issue templates:

- [Bug report](.github/ISSUE_TEMPLATE/bug_report.yml)
- [Feature request](.github/ISSUE_TEMPLATE/feature_request.yml)
- [Documentation issue](.github/ISSUE_TEMPLATE/documentation.yml)

For security vulnerabilities, please follow the process in
[SECURITY.md](SECURITY.md) instead of opening a public issue.

## Code of conduct

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md). By
participating, you are expected to uphold it.
