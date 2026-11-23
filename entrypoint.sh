#!/bin/bash
set -e

# Map microservice hostnames to localhost so either 127.0.0.1 or service names resolve internally
echo "127.0.0.1 mongodb qdrant redis backend" >> /etc/hosts 2>/dev/null || true

# Ensure all runtime and data directories exist on the persistent volume
mkdir -p /data/db /data/redis /data/qdrant /data/docs /var/log/supervisor /var/run
chmod -R 777 /data /qdrant /var/log/supervisor /var/run 2>/dev/null || true

# Symlink Qdrant storage into persistent /data volume so vector embeddings survive redeploys
if [ ! -L /qdrant/storage ]; then
    rm -rf /qdrant/storage 2>/dev/null || true
    ln -s /data/qdrant /qdrant/storage
fi

# Start supervisord to launch and monitor all services if no command passed
if [ $# -gt 0 ]; then
    exec "$@"
fi

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
