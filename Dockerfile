# Auth service — lightweight Python-only image (no code-server/Quarto/R)
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip supervisor ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-auth.txt /tmp/requirements-auth.txt
RUN pip install --no-cache-dir --break-system-packages -r /tmp/requirements-auth.txt

COPY app /app/app
COPY docker /app/docker

RUN mkdir -p /var/log/supervisor \
    && chmod +x /app/docker/entrypoint.sh

WORKDIR /app

CMD ["/app/docker/entrypoint.sh"]
