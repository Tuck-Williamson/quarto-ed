#!/bin/bash
set -e

mkdir -p /workspace /var/log/supervisor

cat > /tmp/supervisord-editor.conf << EOF
[supervisord]
nodaemon=true
logfile=/var/log/supervisor/supervisord.log
pidfile=/tmp/supervisord.pid
loglevel=info

[program:uvicorn]
command=python3.11 -m uvicorn editor.main:app --host 0.0.0.0 --port %(ENV_PORT)s --workers 1 --proxy-headers --forwarded-allow-ips=*
directory=/app
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[unix_http_server]
file=/tmp/supervisor.sock

[supervisorctl]
serverurl=unix:///tmp/supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
EOF

exec supervisord -c /tmp/supervisord-editor.conf
