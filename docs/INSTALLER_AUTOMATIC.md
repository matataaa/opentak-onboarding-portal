# Installer 

The script will install Docker and clone the OpenTAK Onboarding Portal repository if it does not exist. It will then start the portal from the pre-built Docker Hub image (`mattronix/atak-onboarding-portal`), which runs the database migrations; nothing is built locally unless you enable the `build:` section in `docker-compose.yml`. Re-running `git pull` only updates the scripts and docs here, use `docker compose pull` to update the portal itself. 

## Debian 

```
apt-get install sudo curl -y
curl -s https://raw.githubusercontent.com/mattronix/opentak-onboarding-portal/refs/heads/main/scripts/installers/debian-installer.sh | sudo bash

```

## Ubuntu 

```
apt-get install sudo curl -y
curl -s https://raw.githubusercontent.com/mattronix/opentak-onboarding-portal/refs/heads/main/scripts/installers/ubuntu-installer.sh | sudo bash

```

## Config 
This section is needed to be done in order to get the portal to load. 

in the env make sure to change OTS_URL in most cases set it to https://host.docker.internal Make sure to set OTS_USERNAME and OTS_PASSWORD to an account with admin access inside of OTS. Set FRONTEND_URL to the portal's public URL: at its localhost default, links in emails and event QR codes point at localhost and won't work for recipients.
```
cd /opt/opentak-onboarding-portal
sudo nano .env
sudo docker compose up -d
```

## Error?

```
sudo docker compose logs -f
```