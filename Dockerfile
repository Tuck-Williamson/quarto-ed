# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS builder

ARG CODE_SERVER_VERSION=4.100.3
ARG QUARTO_VERSION=1.7.32

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget curl ca-certificates dpkg python3.11 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# code-server
RUN wget -q "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server-${CODE_SERVER_VERSION}-linux-amd64.tar.gz" \
        -O /tmp/code-server.tar.gz \
    && mkdir -p /opt/code-server \
    && tar -xzf /tmp/code-server.tar.gz -C /opt/code-server --strip-components=1 \
    && rm /tmp/code-server.tar.gz

# Quarto (installs to /opt/quarto; binary at /opt/quarto/bin/quarto)
RUN wget -q "https://github.com/quarto-dev/quarto-cli/releases/download/v${QUARTO_VERSION}/quarto-${QUARTO_VERSION}-linux-amd64.deb" \
        -O /tmp/quarto.deb \
    && dpkg -i /tmp/quarto.deb \
    && rm /tmp/quarto.deb

# Pre-install Quarto extension into a shared extensions dir
RUN mkdir -p /opt/cs-extensions \
    && /opt/code-server/bin/code-server \
        --extensions-dir /opt/cs-extensions \
        --install-extension quarto.quarto \
    || true

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/code-server/bin:/opt/quarto/bin:/usr/local/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip supervisor git openssh-client \
        libsecret-1-0 libx11-6 libxkbfile1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Artifacts from builder
COPY --from=builder /opt/code-server /opt/code-server
COPY --from=builder /opt/cs-extensions /opt/cs-extensions
COPY --from=builder /opt/quarto /opt/quarto
COPY --from=builder /usr/local/lib/python3.11/dist-packages \
                    /usr/local/lib/python3.11/dist-packages

COPY app /app/app
COPY docker /app/docker

RUN mkdir -p /workspace /var/log/supervisor \
    && chmod +x /app/docker/entrypoint.sh

WORKDIR /app

CMD ["/app/docker/entrypoint.sh"]
