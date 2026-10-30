# Docker Deployment Guide

## Quick Start

1. **Copy the example files:**
   ```bash
   cp env.dist .env
   cp docker-compose.yml.dist docker-compose.yml
   ```

2. **Edit `.env` file with your configuration:**
   ```bash
   nano .env
   ```

   At minimum, you must set:
   - `SECRET_KEY` - Generate with: `python3 -c "import secrets; print(secrets.token_hex(32))"`
   - `JWT_SECRET_KEY` - Generate with: `python3 -c "import secrets; print(secrets.token_hex(32))"`
   - `OTS_USERNAME` - Your OpenTAK Server admin username
   - `OTS_PASSWORD` - Your OpenTAK Server admin password
   - `OTS_URL` - Your OpenTAK Server URL (e.g., https://tak.example.com)

3. **Start the container:**
   ```bash
   docker compose up -d
   ```

   The default configuration pulls the pre-built image from Docker Hub (`mattronix/atak-onboarding-portal`).

   To build locally instead, edit `docker-compose.yml` and uncomment the `build` section:
   ```yaml
   build:
       context: .
       dockerfile: Dockerfile
       args:
           GIT_COMMIT: ${GIT_COMMIT:-dev}
           GIT_DATE: ${GIT_DATE:-unknown}
   ```
   Then run:
   ```bash
   docker compose build
   docker compose up -d
   ```

4. **Check logs:**
   ```bash
   docker compose logs -f
   ```

5. **Access the portal:**
   Open http://localhost:5000 in your browser

Database migrations run automatically on container startup via the entrypoint script. If a
migration fails, the container refuses to start instead of running on a half-migrated database.

## Configuration Options

See `env.dist` for all available configuration options.

### Important Settings

- **`SECRET_KEY`** - Secret key for Flask session security
- **`JWT_SECRET_KEY`** - Secret key for JWT token signing
- **`DEBUG=False`** - Should be False in production
- **`OTS_VERIFY_SSL=True`** - Should be True if your OTS has valid SSL
- **`MAIL_ENABLED=False`** - Enable email for password reset and notifications
- **`SQLALCHEMY_DATABASE_URI`** - Database connection string (defaults to SQLite)

## Data Persistence

The following directories are mounted for persistence:
- `./instance` - SQLite database
- `./datapackages` - TAK data packages

## Troubleshooting

### 404 Error on Login

The portal runs with the React SPA frontend bundled in the container. The API serves the frontend from `/frontend/dist`.

### Can't Access OTS

- Check `OTS_URL` is correct
- Check `OTS_USERNAME` and `OTS_PASSWORD` are correct
- If using self-signed SSL, set `OTS_VERIFY_SSL=False`

### Database Errors

Migrations run automatically on startup; if one fails the container refuses to start
(see [Recovering from a failed migration](#recovering-from-a-failed-migration)).
Because a crash-looping container can't be `exec`'d into, run one-off commands in a fresh container:
```bash
docker compose run --rm web flask db current
docker compose run --rm web flask db upgrade
```

### Check Application Logs

```bash
docker compose logs -f web
```

## Updating

To update to a new version:

```bash
# Pull latest image
docker compose pull

# Restart services (migrations run automatically)
docker compose up -d
```

If building locally:
```bash
# Pull latest code
git pull

# Rebuild container
docker compose build

# Restart services
docker compose up -d
```

### Backups

Back up before every update. On startup, when migrations are pending, the container
copies a SQLite database to `instance/db.sqlite.pre-<revision>-<UTC timestamp>` before
migrating (one copy per starting revision while the database is unchanged). If that copy can't be made, the container
does not migrate and does not start.

For PostgreSQL there is no automatic backup; take a dump yourself before updating:
```bash
pg_dump -Fc -h <host> -U <user> <dbname> > portal-$(date -u +%Y%m%dT%H%M%SZ).dump
```

### Recovering from a failed migration

If `flask db upgrade` fails, the container logs
`ERROR: database migration failed; not starting.`, prints the current revision and exits;
with `restart: always` it keeps restarting without serving traffic. Check the logs with
`docker compose logs web`, then either fix the cause and restart, or roll back:

1. Stop the stack: `docker compose down`
2. Restore the database:
   - SQLite: `cp instance/db.sqlite.pre-<revision>-<timestamp> instance/db.sqlite`. Migrations
     are committed one revision at a time, so a failed multi-step upgrade can leave several
     `pre-*` files; restore the **oldest** one from this update attempt (earliest timestamp,
     named after the revision the previous version ran at).
   - PostgreSQL: `pg_restore --clean --if-exists -h <host> -U <user> -d <dbname> portal-<timestamp>.dump`
3. Go back to the previous image/version (pin the image tag in `docker-compose.yml`, or
   `git checkout <previous-version>` and `docker compose build`), then `docker compose up -d`.

`flask db stamp` only changes the recorded revision without migrating anything; use it
only if you know exactly which schema the database has.

## Read-Only Database Fix

If the database goes into read-only mode, fix ownership (the container uses UID 1000 for appuser):
```bash
sudo chown -R 1000:1000 instance/
```

## Production Recommendations

1. **Use a reverse proxy** (nginx, Caddy) with SSL
2. **Set strong SECRET_KEY and JWT_SECRET_KEY**
3. **Set DEBUG=False**
4. **Use PostgreSQL instead of SQLite** for better performance:
   ```
   SQLALCHEMY_DATABASE_URI=postgresql://user:pass@host:5432/dbname
   ```
5. **Enable email** for password reset functionality
6. **Regular backups** of the `./instance` directory
7. **Monitor logs** for errors and security issues
