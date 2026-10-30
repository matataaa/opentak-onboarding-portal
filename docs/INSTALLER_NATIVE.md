# Native Installer (No Docker)

This script installs the OpenTAK Onboarding Portal directly on Ubuntu without Docker. It installs Python 3.11, Node.js 20, builds the frontend, and configures a systemd service.

## Ubuntu 22.04 / 24.04

```bash
apt-get install sudo curl -y
curl -s https://raw.githubusercontent.com/mattronix/opentak-onboarding-portal/refs/heads/main/scripts/installers/ubuntu-native-installer.sh | sudo bash
```

## What Gets Installed

- Python 3.11 (from deadsnakes PPA)
- Node.js 20 (from NodeSource)
- Python virtual environment with all dependencies
- Frontend build (React)
- SQLite database
- Systemd service (`opentak-portal`)

## Configuration

After installation, edit the environment file:

```bash
nano /opt/opentak-onboarding-portal/.env
```

Required settings:

| Variable | Description |
|----------|-------------|
| `OTS_URL` | Your OpenTAK Server URL (e.g., `https://tak.example.com`) |
| `OTS_USERNAME` | Admin username for OTS |
| `OTS_PASSWORD` | Admin password for OTS |

## Service Management

Start the service:
```bash
systemctl start opentak-portal
```

Stop the service:
```bash
systemctl stop opentak-portal
```

Restart the service:
```bash
systemctl restart opentak-portal
```

Check status:
```bash
systemctl status opentak-portal
```

View logs:
```bash
journalctl -u opentak-portal -f
```

## File Locations

| Path | Description |
|------|-------------|
| `/opt/opentak-onboarding-portal/` | Installation directory |
| `/opt/opentak-onboarding-portal/.env` | Configuration file |
| `/opt/opentak-onboarding-portal/instance/db.sqlite` | Database |
| `/opt/opentak-onboarding-portal/venv/` | Python virtual environment |
| `/etc/systemd/system/opentak-portal.service` | Systemd service file |

## Updating

To update to the latest version:

```bash
curl -s https://raw.githubusercontent.com/mattronix/opentak-onboarding-portal/refs/heads/main/scripts/installers/ubuntu-native-update.sh | sudo bash
```

Or manually:

```bash
cd /opt/opentak-onboarding-portal
sudo systemctl stop opentak-portal
sudo git pull
sudo venv/bin/pip install -r requirements.txt
cd frontend && sudo npm install && sudo npm run build && cd ..
sudo venv/bin/flask db upgrade
sudo systemctl start opentak-portal
```

### Backups and failed migrations

The update script copies a SQLite database to `instance/db.sqlite.pre-update-<UTC timestamp>`
before migrating; when updating manually, copy `instance/db.sqlite` yourself first. For
PostgreSQL, take a dump before updating:
`pg_dump -Fc -h <host> -U <user> <dbname> > portal-backup.dump`.

If the migration fails, the update script stops and leaves the service stopped, printing the
restore steps. To roll back: restore the backup (`cp` the SQLite copy back over
`instance/db.sqlite`, or `pg_restore --clean --if-exists -d <dbname> portal-backup.dump`),
`sudo git checkout <previous-commit>`, reinstall requirements, rebuild the frontend,
`sudo chown -R opentak-portal:opentak-portal /opt/opentak-onboarding-portal` and start the
service (run `sudo git checkout main` before the next update). `flask db stamp` only changes the recorded revision without migrating; use it
only if you know exactly which schema the database has.

## Uninstalling

```bash
sudo systemctl stop opentak-portal
sudo systemctl disable opentak-portal
sudo rm /etc/systemd/system/opentak-portal.service
sudo systemctl daemon-reload
sudo rm -rf /opt/opentak-onboarding-portal
sudo userdel opentak-portal
```

## Troubleshooting

### Check if service is running
```bash
systemctl status opentak-portal
```

### View application logs
```bash
journalctl -u opentak-portal -f
```

### Test manually
```bash
cd /opt/opentak-onboarding-portal
source venv/bin/activate
flask run --debug
```

### Database issues
Back up the database first (see [Backups and failed migrations](#backups-and-failed-migrations)), then:
```bash
cd /opt/opentak-onboarding-portal
source venv/bin/activate
flask db current
flask db upgrade
```
