# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `ALLOWED_GITHUB_USERS` env var to restrict login to a list of trusted
  GitHub usernames -- the access boundary on deployments that can't run as
  root (see SECURITY.md).

### Fixed

- `app/sandbox.py` now degrades gracefully on platforms that don't run the
  app as root (e.g. Heroku Common Runtime), instead of raising on every
  workspace request. Per-user OS sandboxing still applies in full when
  running as root.

## [0.1.0] - 2026-06-10

### Added

- Semantic versioning (`__version__`), `/api/version` endpoint, and
  app version + commit display in the editor UI.
- Issue templates (bug report, feature request, documentation) and a pull
  request template.
- `CONTRIBUTING.md` and `SECURITY.md`.
- CI: automatic git tag + GitHub Release + versioned Docker image
  (`vX.Y.Z`) on version bumps merged to `main`.
- `app.json` for a Heroku Review Apps pipeline.
