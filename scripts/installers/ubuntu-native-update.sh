#!/bin/bash

set -e

INSTALL_DIR="/opt/opentak-onboarding-portal"

echo "============================================"
echo "OpenTAK Onboarding Portal - Update Script"
echo "============================================"

# Check if installation exists
if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: Installation not found at $INSTALL_DIR"
    exit 1
fi

cd "$INSTALL_DIR"

# Stop service
echo "[1/6] Stopping service..."
sudo systemctl stop opentak-portal || true

# Pull latest code
echo "[2/6] Pulling latest code..."
OLD_COMMIT=$(sudo git rev-parse HEAD)
sudo git pull

# Update Python dependencies
echo "[3/6] Updating Python dependencies..."
sudo venv/bin/pip install --upgrade pip
sudo venv/bin/pip install -r requirements.txt

# Rebuild frontend
echo "[4/6] Rebuilding frontend..."
cd frontend
sudo npm install
sudo npm run build
cd ..

# Run database migrations
echo "[5/6] Running database migrations..."
# Back up the SQLite database first (PostgreSQL: take a pg_dump yourself)
DB_URI=$(grep -E '^SQLALCHEMY_DATABASE_URI=' .env 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d "\"'" || true)
DB_URI=${DB_URI:-sqlite:///db.sqlite}
DB_BACKUP=""
if [[ "$DB_URI" == sqlite:///* ]]; then
    DB_FILE=${DB_URI#sqlite:///}
    [[ "$DB_FILE" == /* ]] || DB_FILE="$INSTALL_DIR/instance/$DB_FILE"
    if [ -f "$DB_FILE" ]; then
        DB_BACKUP="$DB_FILE.pre-update-$(date -u +%Y%m%dT%H%M%SZ)"
        sudo cp -p "$DB_FILE" "$DB_BACKUP"
        echo "Database backed up to $DB_BACKUP"
    else
        echo "No SQLite database found at $DB_FILE; no backup taken"
    fi
fi
if ! sudo venv/bin/flask db upgrade; then
    echo "ERROR: database migration failed; the service has NOT been restarted." >&2
    if [ -n "$DB_BACKUP" ]; then
        echo "To roll back: restore the backup and check out the previous code:" >&2
        echo "  sudo cp -p \"$DB_BACKUP\" \"$DB_FILE\"" >&2
        echo "  cd $INSTALL_DIR && sudo git checkout $OLD_COMMIT" >&2
        echo "  then reinstall requirements, rebuild the frontend," >&2
        echo "  sudo chown -R opentak-portal:opentak-portal $INSTALL_DIR and start the service" >&2
        echo "  (run 'sudo git checkout main' before the next update; the checkout is detached)" >&2
    fi
    echo "See docs/INSTALLER_NATIVE.md (Backups and failed migrations)." >&2
    exit 1
fi

# Fix ownership
sudo chown -R opentak-portal:opentak-portal "$INSTALL_DIR"

# Restart service
echo "[6/6] Starting service..."
sudo systemctl start opentak-portal

echo ""
echo "============================================"
echo "Update Complete!"
echo "============================================"
echo ""
echo "Check status: sudo systemctl status opentak-portal"
echo "View logs:    sudo journalctl -u opentak-portal -f"
echo ""
