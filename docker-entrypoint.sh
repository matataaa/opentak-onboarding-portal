#!/bin/bash
set -euo pipefail

echo "Starting OpenTAK Onboarding Portal..."

# Bind-mounted directories keep their host ownership; this container runs as UID 1000
for dir in instance datapackages; do
    if [ ! -w "/app/$dir" ]; then
        echo "WARNING: /app/$dir is not writable by UID $(id -u). On the host, run: sudo chown -R 1000:1000 ./$dir" >&2
    fi
done

# A generated key this user can't read makes the portal fall back to a temporary one
for key in secret_key jwt_secret_key; do
    if [ -e "/app/instance/$key" ] && [ ! -r "/app/instance/$key" ]; then
        echo "WARNING: /app/instance/$key is not readable by UID $(id -u). On the host, run: sudo chown -R 1000:1000 ./instance" >&2
    fi
done

# Back up the SQLite database before applying pending migrations
python3 /app/docker-backup-sqlite.py

# Run database migrations; refuse to start on a half-migrated database
echo "Running database migrations..."
if ! flask db upgrade; then
    echo "ERROR: database migration failed; not starting. See docs/DOCKER.md (Recovering from a failed migration)." >&2
    flask db current || true
    exit 1
fi
echo "Migrations completed successfully"

# Start the application with gunicorn
echo "Starting gunicorn..."
exec gunicorn -w 1 -t 300 app:app -b 0.0.0.0:5000
