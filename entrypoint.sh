#!/bin/bash
set -e

# Map microservice hostnames to localhost so either 127.0.0.1 or service names resolve internally
echo "127.0.0.1 mongodb qdrant redis backend" >> /etc/hosts 2>/dev/null || true

# Ensure all runtime and data directories exist
mkdir -p /data/db /data/redis /qdrant/storage /data/docs /var/log/supervisor /var/run

# Start supervisord to launch and monitor all services if no command passed
if [ $# -gt 0 ]; then
    exec "$@"
fi

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
