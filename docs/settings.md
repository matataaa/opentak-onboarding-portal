# Settings
The Following Environment Vars control the application. Changes apply after `docker compose up -d` (Docker; `docker compose restart` keeps the old values) or `sudo systemctl restart opentak-portal` (native).


SECRET_KEY: Secret Key for secure sessions and event link tokens. If empty or weak, images built from this version or later generate it once and store it in instance/. Older images refuse logins only when both keys are empty, so set both
JWT_SECRET_KEY: Signs login tokens and TAK profile download tokens. If empty or weak, images built from this version or later generate it once and store it in instance/. Older images refuse logins only when both keys are empty, so set both
FRONTEND_URL: The portal's public URL. Must be set: at its localhost default, links in emails and event QR codes point at localhost and won't work for recipients
TZ: (Docker) Container timezone, defaults to UTC. See DOCKER.md before changing it on a running install

OTS_USERNAME: Dedicated username of the Administrative Open Tak Server User.
OTS_PASSWORD: Password for the Open Tak Server Username. Both must match the existing account in OTS: the portal doesn't set or sync it. They are read at start only, so changing that account's password (in OTS or through the portal) requires updating .env and applying it as above. Use a dedicated service account.
OTS_URL: Open Tak Server url must be in format of http(s)://tak.domain.tld

DEBUG: (True/False) Show more detailed errors

MAIL_ENABLED: (True/False) Disable mail in App
MAIL_SERVER: Hostname of the Mailserver
MAIL_PORT: Port to use for E-Mail
MAIL_USERNAME: Username for E-Mail
MAIL_PASSWORD: Password for E-Mail
MAIL_USE_TLS: Use TLS make sure SSL is set to False if this is True
MAIL_USE_SSL: Use SSL make sure TLS is set to False if this is True
MAIL_DEFAULT_SENDER: The from E-Mail address in format of user@email.tld

BRAND_NAME: Sets the name of the portal used on headers and emails.
LOGO_PATH: Sets a logo path, if you want to add your own upload it to /app/static/img/custom/logo.ext and set the path to "/static/img/custom/logo.ext" we advise PNG formated.

ENABLE_API: Enables API Endpoints (BETA)
API_KEY: Not used. Nothing reads this setting; API keys are managed in the admin panel (and no endpoint accepts them yet).