"""Editor/preview backend, split into per-domain route modules.

Shared state, helpers, the quarto-preview lifecycle, and the auth dependencies
live in `_core`; each route module owns one domain's endpoints. The combined
`router` (imported by app.main) mounts them all.

Every public and underscore-prefixed name defined in `_core` is re-exported at
the package level below, so existing imports like `from app.proxy import
_redact` / `kill_quarto_preview` (used across the app and the test suite)
continue to resolve unchanged.
"""
from fastapi import APIRouter

from . import _core
from . import ai, files, pages, preview, repos, settings, terminal, workspace

# Re-export _core's names (helpers, state dicts, constants, dependencies) onto
# the package namespace for backward-compatible imports.
globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})

router = APIRouter()
for _module in (pages, repos, workspace, files, settings, ai, preview, terminal):
    router.include_router(_module.router)
