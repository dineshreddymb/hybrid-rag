#!/bin/bash
set -e

# Support selecting service via CLI arg or SERVICE environment variable
TARGET_SERVICE="${1:-$SERVICE}"

case "$TARGET_SERVICE" in
    mongodb|mongo|mongod)
        if [ "$1" = "$TARGET_SERVICE" ]; then shift; fi
        mkdir -p /data/db /var/log/mongodb
        chmod -R 777 /data/db /var/log/mongodb 2>/dev/null || true
        export LD_PRELOAD="/usr/local/lib/libfake_uname.so"
        export GLIBC_TUNABLES="glibc.pthread.rseq=0"
        echo "[entrypoint] Starting MongoDB with fake_uname and indexBuildMinAvailableDiskSpaceMB=0..."
        exec /usr/bin/mongod --dbpath /data/db --bind_ip_all --setParameter indexBuildMinAvailableDiskSpaceMB=0 "$@"
        ;;

    redis|redis-server)
        if [ "$1" = "$TARGET_SERVICE" ]; then shift; fi
        mkdir -p /data/redis
        chmod -R 777 /data/redis 2>/dev/null || true
        echo "[entrypoint] Starting Redis on 0.0.0.0:6379..."
        exec /usr/bin/redis-server --bind 0.0.0.0 --port 6379 --protected-mode no --dir /data/redis "$@"
        ;;

    qdrant)
        if [ "$1" = "$TARGET_SERVICE" ]; then shift; fi
        mkdir -p /data/qdrant /qdrant/storage
        chmod -R 777 /data/qdrant /qdrant 2>/dev/null || true
        if [ ! -L /qdrant/storage ]; then
            rm -rf /qdrant/storage 2>/dev/null || true
            ln -s /data/qdrant /qdrant/storage
        fi
        export QDRANT__SERVICE__HOST="0.0.0.0"
        export QDRANT__STORAGE__STORAGE_PATH="/data/qdrant"
        echo "[entrypoint] Starting Qdrant on 0.0.0.0:6333..."
        exec /usr/local/bin/qdrant "$@"
        ;;

    backend|api)
        if [ "$1" = "$TARGET_SERVICE" ]; then shift; fi
        mkdir -p /data/docs
        chmod -R 777 /data/docs 2>/dev/null || true
        cd /srv/backend
        export PYTHONPATH="/srv/backend:${PYTHONPATH:-}"
        PORT="${PORT:-8000}"
        echo "[entrypoint] Starting Backend API on port ${PORT}..."
        exec /opt/venv/bin/uvicorn backend_app.main:app --host 0.0.0.0 --port "$PORT" "$@"
        ;;

    frontend|ui)
        if [ "$1" = "$TARGET_SERVICE" ]; then shift; fi
        cd /srv/frontend
        export PYTHONPATH="/srv/frontend:${PYTHONPATH:-}"
        PORT="${PORT:-8050}"
        echo "[entrypoint] Starting Frontend UI on port ${PORT}..."
        exec /opt/venv/bin/gunicorn -b "0.0.0.0:${PORT}" --workers 2 --threads 4 --timeout 180 frontend_app.main:server "$@"
        ;;

    mcp|mcp-server)
        if [ "$1" = "$TARGET_SERVICE" ]; then shift; fi
        cd /srv/mcp-server
        export PYTHONPATH="/srv/mcp-server:${PYTHONPATH:-}"
        export MCP_HOST="${MCP_HOST:-0.0.0.0}"
        export MCP_PORT="${MCP_PORT:-${PORT:-8100}}"
        export MCP_PATH="${MCP_PATH:-/mcp}"
        echo "[entrypoint] Starting MCP server on ${MCP_HOST}:${MCP_PORT}${MCP_PATH}..."
        exec /opt/venv/bin/python -m mcp_server --transport http "$@"
        ;;

    all|all-in-one|supervisord|"")
        echo "127.0.0.1 mongodb qdrant redis backend" >> /etc/hosts 2>/dev/null || true
        mkdir -p /data/db /data/redis /data/qdrant /data/docs /var/log/supervisor /var/run
        chmod -R 777 /data /qdrant /var/log/supervisor /var/run 2>/dev/null || true
        if [ ! -L /qdrant/storage ]; then
            rm -rf /qdrant/storage 2>/dev/null || true
            ln -s /data/qdrant /qdrant/storage
        fi
        echo "[entrypoint] Starting all services via supervisord..."
        exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
        ;;

    *)
        exec "$@"
        ;;
esac
