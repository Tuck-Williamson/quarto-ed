# ── Stage 1: builder ─────────────────────────────────────────────────────────[...]
FROM debian:bookworm-slim AS builder

ARG QUARTO_VERSION=1.9.38

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget curl ca-certificates dpkg python3.11 python3-pip libgnutls30 \
    && rm -rf /var/lib/apt/lists/*

# Quarto (installs to /opt/quarto; binary at /opt/quarto/bin/quarto)
RUN wget -q "https://github.com/quarto-dev/quarto-cli/releases/download/v${QUARTO_VERSION}/quarto-${QUARTO_VERSION}-linux-amd64.deb" \
        -O /tmp/quarto.deb \
    && dpkg -i /tmp/quarto.deb \
    && rm /tmp/quarto.deb

# R (needed for Quarto knitr engine)
# r-base-dev: C/C++ toolchain for source compilation fallbacks.
# libuv1-dev: required by the 'fs' R package (rmarkdown dep chain).
RUN apt-get update && apt-get install -y --no-install-recommends r-base r-base-dev libuv1-dev default-jre \
    && rm -rf /var/lib/apt/lists/*

# R packages for knitr/rmarkdown code-chunk execution.
# RSPM supplies pre-built binaries where available; r-base-dev handles source fallbacks.
# The second Rscript call verifies installation — if either package is missing the build fails.
RUN Rscript -e "install.packages(c('knitr', 'rmarkdown'), \
      repos='https://packagemanager.posit.co/cran/__linux__/bookworm/latest')" \
    && Rscript -e "stopifnot(all(c('knitr','rmarkdown') %in% installed.packages()[,'Package']))"

# Addint Quarto tools (installs to /root/.local/share/quarto/...)
RUN /opt/quarto/bin/quarto install verapdf --no-prompt 
RUN /opt/quarto/bin/quarto install chrome-headless-shell --no-prompt 

# TinyTeX (installs to /root/.TinyTeX; quarto finds it automatically)
# Beyond tagpdf/luamml, pre-install the LaTeX packages Quarto needs to render
# callout blocks to PDF (tcolorbox + its deps, fontawesome5 icons, luatexbase).
# Without these, `quarto render --to pdf` on a callout doc triggers an on-demand
# tlmgr install that fails in an offline container.
RUN /opt/quarto/bin/quarto install tinytex --no-prompt \
    && /root/.TinyTeX/bin/x86_64-linux/tlmgr update --self \
    && /root/.TinyTeX/bin/x86_64-linux/tlmgr install \
        tagpdf luamml \
        tcolorbox pgf environ trimspaces fontawesome5 luatexbase etoolbox

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# ── Stage: frontend assets build (Tailwind CSS + esbuild JS) ──────────────────
# build:css → app/static/app.css (minified, purged against the templates),
#             replacing the runtime cdn.tailwindcss.com Play CDN.
# build:js  → app/static/dist/ (esbuild bundle of app/static/src/editor.js:
#             CodeMirror/xterm/ansi_up, deduped to single instances, code-split
#             so language modes load on demand). Replaces the esm.sh/jsdelivr
#             ESM CDN + import map, so all scripts are served from 'self'.
FROM node:20-slim AS assets
WORKDIR /build
COPY package.json ./
RUN npm install --no-audit --no-fund --loglevel=error
COPY tailwind.config.js ./
COPY app/static/src ./app/static/src
COPY app/templates ./app/templates
RUN npm run build:css && npm run build:js

# ── Stage 2: runtime ──────────────────────────────────────────────────────────[...]
FROM debian:bookworm-slim

ARG GIT_SHA=unknown

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/quarto/bin:/root/.TinyTeX/bin/x86_64-linux:/usr/local/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GIT_SHA=$GIT_SHA

RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip git openssh-client ca-certificates \
        supervisor r-base libgnutls30 default-jre \
    && rm -rf /var/lib/apt/lists/*

# Artifacts from builder
COPY --from=builder /opt/quarto /opt/quarto
COPY --from=builder /root/.TinyTeX /root/.TinyTeX
COPY --from=builder /usr/local/lib/python3.11/dist-packages \
                    /usr/local/lib/python3.11/dist-packages
COPY --from=builder /usr/lib/R/library /usr/lib/R/library
COPY --from=builder /usr/local/lib/R/site-library /usr/local/lib/R/site-library

COPY --from=builder /root/.local/share/ /root/.local/share/

COPY app /app/app
COPY docker /app/docker

# Compiled frontend assets from the assets stage: Tailwind CSS (/static/app.css)
# and the esbuild bundle + code-split chunks (/static/dist/…).
COPY --from=assets /build/app/static/app.css /app/app/static/app.css
COPY --from=assets /build/app/static/dist /app/app/static/dist

RUN mkdir -p /workspace /var/log/supervisor \
    && chmod +x /app/docker/entrypoint.sh

# Pre-generate the luaotfload font cache as root so that sandboxed users
# (qe<user_id>, which have no home dir and no write access to /root/.TinyTeX)
# can render PDFs without hitting "no writeable cache path" at runtime.
# The dummy lualatex run writes the cache to $TEXMFVAR (inside /root/.TinyTeX);
# the chmod below then makes it world-readable.
RUN echo '\documentclass{article}\begin{document}x\end{document}' \
      > /tmp/cache_warmup.tex \
    && lualatex --interaction=batchmode --output-directory=/tmp /tmp/cache_warmup.tex \
    || true \
    && rm -f /tmp/cache_warmup.*

# Sandbox hardening: `quarto preview` runs as a per-user unprivileged
# account (qe<user_id>, see app/sandbox.py) that needs read+execute access
# to the shared runtime (quarto, R, TinyTeX, Python packages) but must not
# be able to read the application source or other users' home directories.
# /root is 0700 by default, which would otherwise hide TinyTeX from that
# account entirely.
RUN chmod o+x /root \
    && chmod -R o+rX /root/.TinyTeX /opt/quarto \
        /usr/local/lib/python3.11/dist-packages \
        /usr/lib/R/library /usr/local/lib/R/site-library \
	/root/.local/share/
RUN chmod -R o-rwx /app

WORKDIR /app

CMD ["/app/docker/entrypoint.sh"]
