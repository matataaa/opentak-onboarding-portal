#!/bin/bash
set -euo pipefail

echo "Starting OpenTAK Onboarding Portal..."

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
exec gunicorn -w 1 -t 50 app:app -b 0.0.0.0:5000
