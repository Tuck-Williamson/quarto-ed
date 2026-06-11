# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Nothing yet.

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
