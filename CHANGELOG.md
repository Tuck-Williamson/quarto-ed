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
- Preview spawn failed under uvloop (`ValueError: unexpected kwargs: user,
  group`) when sandboxing was active -- privileges are now dropped via a
  `setpriv` command prefix instead of Popen's `user=`/`group=` kwargs,
  which uvloop does not support.
- Sandbox account GIDs are now deterministic (`20000 + user_id`, matching
  the UID). Previously `useradd --system --user-group` allocated GIDs in
  login order, which could let users inherit each other's group-owned
  workspace files across restarts on persistent-volume deployments.
- Editor layout no longer requires horizontal/vertical scrolling on tablet
  windows (e.g. iPad) -- the file browser and right panel become slide-in
  overlays below 1024px wide instead of fixed-pixel flex panes, and `body`
  uses `100dvh` so iOS Safari's dynamic toolbar doesn't hide the status
  bar. Both panels now also have touch-friendly edge-tab open buttons and
  in-panel close (✕) buttons, and the drag-resize handles support touch.

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
