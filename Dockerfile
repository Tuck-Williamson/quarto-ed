# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM debian:bookworm-slim AS builder

ARG QUARTO_VERSION=1.7.32

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        wget curl ca-certificates dpkg python3.11 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Quarto (installs to /opt/quarto; binary at /opt/quarto/bin/quarto)
RUN wget -q "https://github.com/quarto-dev/quarto-cli/releases/download/v${QUARTO_VERSION}/quarto-${QUARTO_VERSION}-linux-amd64.deb" \
        -O /tmp/quarto.deb \
    && dpkg -i /tmp/quarto.deb \
    && rm /tmp/quarto.deb

# R (needed for Quarto knitr engine)
RUN apt-get update && apt-get install -y --no-install-recommends r-base \
    && rm -rf /var/lib/apt/lists/*

# TinyTeX (installs to /root/.TinyTeX; quarto finds it automatically)
RUN /opt/quarto/bin/quarto install tinytex --no-prompt

# Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/quarto/bin:/root/.TinyTeX/bin/x86_64-linux:/usr/local/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip git openssh-client ca-certificates \
        supervisor r-base \
    && rm -rf /var/lib/apt/lists/*

# Artifacts from builder
COPY --from=builder /opt/quarto /opt/quarto
COPY --from=builder /root/.TinyTeX /root/.TinyTeX
COPY --from=builder /usr/local/lib/python3.11/dist-packages \
                    /usr/local/lib/python3.11/dist-packages

COPY app /app/app
COPY docker /app/docker

RUN mkdir -p /workspace /var/log/supervisor \
    && chmod +x /app/docker/entrypoint.sh

WORKDIR /app

CMD ["/app/docker/entrypoint.sh"]
