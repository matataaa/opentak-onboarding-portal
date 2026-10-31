# Docker Deployment Guide

## Quick Start

1. **Copy the example files:**
   ```bash
   cp env.dist .env
   chmod 600 .env
   cp docker-compose.yml.dist docker-compose.yml
   ```

   Create the data directories with the owner the container runs as (UID 1000):
   ```bash
   mkdir -p instance datapackages
   sudo chown -R 1000:1000 instance datapackages
   ```

2. **Edit `.env` file with your configuration:**
   ```bash
   nano .env
   ```

   At minimum, you must set:
   - `OTS_USERNAME` - Your OpenTAK Server admin username
   - `OTS_PASSWORD` - Your OpenTAK Server admin password
   - `OTS_URL` - Your OpenTAK Server URL (e.g., https://tak.example.com)
   - `FRONTEND_URL` - The portal's public URL (e.g., https://portal.example.com). At its localhost
     default, links in emails and event QR codes point at localhost and won't work for recipients
   - `SECRET_KEY` - Generate with: `openssl rand -hex 32`
   - `JWT_SECRET_KEY` - Generate with: `openssl rand -hex 32`

   The two keys may be left empty only with an image built from this version or later (see
   [Secret keys](#secret-keys)): older images, including the current Docker Hub image, refuse
   logins without keys.

   `OTS_USERNAME` and `OTS_PASSWORD` must match the existing account in OTS: the portal doesn't
   set or sync it. `.env` is read at start only, so changing that account's password (in OTS or
   through the portal) requires updating `.env` and running `docker compose up -d`
   (`docker compose restart` keeps the old `.env` values). Use a dedicated service account.

   Put values containing `$` in single quotes (`OTS_PASSWORD='pa$word'`), otherwise
   docker compose expands them.

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
   Without the `build` section, `docker compose build` only prints "No services to build":
   the image from Docker Hub keeps running.

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

- **`SECRET_KEY`** - Secret key for Flask session security and event link tokens
- **`JWT_SECRET_KEY`** - Secret key for JWT token signing
- **`FRONTEND_URL`** - Public URL of the portal, used for links in emails and event QR codes; must be set
- **`DEBUG=False`** - Should be False in production
- **`OTS_VERIFY_SSL=True`** - Should be True if your OTS has valid SSL
- **`MAIL_ENABLED=False`** - Enable email for password reset and notifications
- **`SQLALCHEMY_DATABASE_URI`** - Database connection string (defaults to SQLite)
- **`TZ`** - Container timezone, see [Timezone](#timezone)

### Secret keys

With an image built from this version or later, when `SECRET_KEY` or `JWT_SECRET_KEY` is empty, shorter than 16 characters or a known placeholder
(`dev`, `your-secret-key`, `your-jwt-secret`), the portal generates a value once, stores it in
`instance/secret_key` or `instance/jwt_secret_key` (mode 0600) and logs a warning naming the
variable. If `instance/` or the key file isn't writable or readable, it logs a louder warning and
uses a temporary value until the next restart; fix it with `sudo chown -R 1000:1000 ./instance`.

Without an `./instance` mount (for example with PostgreSQL) generated keys are lost when the
container is recreated, so set both keys in `.env`.

An existing install that still has such a value gets new keys on its first start with this
behaviour: everyone has to log in again.

### Timezone

The container defaults to UTC. The `datetime-local` inputs for event windows and scheduled
announcements are stored without a timezone and read in the container's timezone, so set `TZ`
in `.env` (`env_file` passes it to the container), for example `TZ=Europe/Amsterdam`. Do this
before those values are entered: changing `TZ` later reinterprets the ones already stored.
Apply a change with `docker compose up -d` (`docker compose restart` keeps the old `.env` values).
Native installs use the host's timezone.

## Data Persistence

The following directories are mounted for persistence:
- `./instance` - SQLite database, its `pre-*` backups and generated secret keys
- `./datapackages` - TAK data packages

Both must be writable by UID 1000; see [Read-Only Database Fix](#read-only-database-fix).

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

### Upgrade note: FRONTEND_URL

Links in emails are now always built from `FRONTEND_URL`, so installs that relied on
autodetection must set it to the portal's public URL and run `docker compose up -d`
(`docker compose restart` keeps the old `.env` values); the log warns at startup while it is
still local. Only OIDC and the kiosk QR code still follow the request host while the default
is in place. Setting `FRONTEND_URL` changes the OIDC `redirect_uri`, which must match what is
registered at the identity provider.

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
`docker compose logs web`, then recover depending on the database:

- **SQLite** is not transactional across a migration and can be left with partial tables, so
  fixing the cause and restarting doesn't work (it fails with errors such as
  `table event_teams already exists`). Always restore the backup in step 2 before retrying.
- **PostgreSQL** rolls back the whole upgrade, so you can fix the cause and restart. Otherwise
  roll back:

1. Stop the stack: `docker compose down`
2. Restore the database:
   - SQLite: `cp instance/db.sqlite.pre-<revision>-<timestamp> instance/db.sqlite`. On SQLite,
     migrations are committed one revision at a time, so a failed multi-step upgrade can leave
     several `pre-*` files; restore the **oldest** one from this update attempt (earliest
     timestamp, named after the revision the previous version ran at).
   - PostgreSQL: `pg_restore --clean --if-exists -h <host> -U <user> -d <dbname> portal-<timestamp>.dump`
3. To stay on the previous version, go back to its image (pin the image tag in `docker-compose.yml`, or
   `git checkout <previous-version>` and `docker compose build`), then `docker compose up -d`.
   To retry the same version instead, fix the cause and run `docker compose up -d`.

`flask db stamp` only changes the recorded revision without migrating anything; use it
only if you know exactly which schema the database has.

## Read-Only Database Fix

If the database goes into read-only mode, uploads fail or the log shows a `WARNING: ... is not
writable` line at startup, fix ownership (the container runs as UID 1000, appuser):
```bash
sudo chown -R 1000:1000 instance datapackages
```

On rootless Docker or a root-squashed NFS export, UID 1000 in the container maps to a different
host user: give that user write access instead.

## Production Recommendations

1. **Use a reverse proxy** (nginx, Caddy) with SSL. The portal runs one synchronous worker with
   a 300-second timeout, so without a proxy that buffers requests and responses, one slow client
   blocks the whole portal
2. **Set FRONTEND_URL** to the portal's public URL
3. **Set DEBUG=False**
4. **Use PostgreSQL instead of SQLite** for better performance:
   ```
   SQLALCHEMY_DATABASE_URI=postgresql://user:pass@host:5432/dbname
   ```
5. **Enable email** for password reset functionality
6. **Regular backups** of the `./instance` directory; they contain the generated secret keys, so
   store them as securely as the keys themselves
7. **Monitor logs** for errors and security issues
