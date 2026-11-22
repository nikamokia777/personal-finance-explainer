#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

REPO_URL="$1"
DEPLOY_PUBLIC_KEY="$2"
APP_DIR=/home/deploy/finance-explainer

if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

apt-get -o DPkg::Lock::Timeout=300 update
apt-get -o DPkg::Lock::Timeout=300 install -y docker.io docker-compose-v2 nginx git ufw openssl
systemctl enable --now docker

if ! id deploy > /dev/null 2>&1; then
    adduser --disabled-password --gecos "" deploy
fi
usermod -aG docker deploy

mkdir -p /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys
grep -qxF "$DEPLOY_PUBLIC_KEY" /home/deploy/.ssh/authorized_keys || echo "$DEPLOY_PUBLIC_KEY" >> /home/deploy/.ssh/authorized_keys
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh

if [ ! -d "$APP_DIR" ]; then
    sudo -u deploy git clone "$REPO_URL" "$APP_DIR"
fi

if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 16)/" "$APP_DIR/.env"
    sed -i "s/^JWT_SECRET=.*/JWT_SECRET=$(openssl rand -hex 32)/" "$APP_DIR/.env"
    chown deploy:deploy "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/finance
ln -sf /etc/nginx/sites-available/finance /etc/nginx/sites-enabled/finance
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

ufw allow OpenSSH
ufw allow "Nginx HTTP"
ufw --force enable

echo "Server is ready. Next: nano $APP_DIR/.env and set GOOGLE_API_KEY"
