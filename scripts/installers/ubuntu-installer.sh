#!/bin/bash

# Update the package database
sudo apt-get update

# Install prerequisite packages
sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common git

# Check if Docker is installed
if ! command -v docker &> /dev/null
then
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    # Add the repository to Apt sources:
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update

    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # Start Docker service
    sudo systemctl start docker

    # Enable Docker to start on boot
    sudo systemctl enable docker

    # Verify Docker installation
    sudo docker --version

    echo "Docker installation completed successfully."
else
    echo "Docker is already installed."
fi

# Clone the OpenTAK Onboarding Portal repository if it does not exist
if [ ! -d "/opt/opentak-onboarding-portal" ]; then
    sudo git clone https://github.com/mattronix/opentak-onboarding-portal.git /opt/opentak-onboarding-portal
fi

# Change to the directory
cd /opt/opentak-onboarding-portal

if [ ! -f ".env" ]; then
    cp env.dist .env
fi

# Fill empty keys only: older images, including the current Docker Hub image, refuse logins without them
for key in SECRET_KEY JWT_SECRET_KEY; do
    sudo sed -i "s/^$key=\$/$key=$(openssl rand -hex 32)/" .env
done
# Holds the OTS admin password
sudo chmod 600 .env

if [ ! -f "docker-compose.yml" ]; then
    cp docker-compose.yml.dist docker-compose.yml
fi

git pull
# Only does something if the build: section in docker-compose.yml is enabled;
# by default the pre-built Docker Hub image runs and this prints "No services to build"
docker compose build
echo "Running the image set in docker-compose.yml (default: pre-built mattronix/atak-onboarding-portal). git pull only updates the scripts and docs here, use docker compose pull to update the portal."

mkdir -p instance datapackages
sudo chown -R 1000:1000 instance/ datapackages/

# Migrations run automatically when the container starts
docker compose up -d

echo "Please navigate to /opt/opentak-onboarding-portal and edit the .env file with sudo (it is root-owned, mode 600); run docker compose with sudo too. After making the necessary changes, run the installer again."
