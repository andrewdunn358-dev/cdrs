# Synthesis IT Billing System — Ubuntu Server Setup Guide

---

## Step 1 — Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

---

## Step 2 — Install Docker

```bash
# Install prerequisites
sudo apt install -y ca-certificates curl gnupg git

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine + Compose plugin
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Add your user to the docker group
sudo usermod -aG docker $USER
newgrp docker

# Verify
docker run --rm hello-world
```

---

## Step 3 — Clone the repo

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/synthesis-billing.git billing
cd billing
```

If the repo is private:
```bash
# Option A — HTTPS with a Personal Access Token
git clone https://YOUR_PAT@github.com/YOUR_USERNAME/synthesis-billing.git billing

# Option B — SSH (if you've added a deploy key to the repo)
git clone git@github.com:YOUR_USERNAME/synthesis-billing.git billing
```

---

## Step 4 — Configure environment

```bash
cp .env.example .env

# Generate a secure secret key
python3 -c "import secrets; print(secrets.token_hex(32))"

# Paste it into .env
nano .env
```

The file should look like:
```
SECRET_KEY=your_long_random_key_here
```

---

## Step 5 — Build and start

```bash
docker compose up -d --build
```

Watch the logs to confirm startup:
```bash
docker compose logs -f
# You should see: "Listening at: http://0.0.0.0:5000"
# Press Ctrl+C to stop watching
```

---

## Step 6 — Open the firewall

```bash
sudo ufw allow 5000/tcp
sudo ufw enable   # if not already on
```

If on a cloud provider (Hetzner, DigitalOcean, AWS etc.) also open port 5000 in the provider's firewall panel.

---

## Step 7 — First login

```
http://YOUR_SERVER_IP:5000
Username: admin
Password: changeme123
```

**Change the password immediately** — Settings → Users.

---

## Updating to a new version

```bash
cd ~/billing
git pull
docker compose up -d --build
```

Your database is in a Docker volume and is never touched by updates.

---

## Optional — Nginx on port 80

```bash
sudo apt install -y nginx

sudo nano /etc/nginx/sites-available/billing
```

Paste:
```nginx
server {
    listen 80;
    server_name YOUR_SERVER_IP_OR_DOMAIN;
    client_max_body_size 50M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/billing /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl restart nginx
sudo ufw allow 80/tcp
```

---

## Optional — HTTPS with Let's Encrypt (requires a domain name)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

---

## Useful commands

```bash
docker compose ps                    # check status
docker compose logs -f               # live logs
docker compose down                  # stop
docker compose up -d                 # start
docker compose restart               # restart
docker compose up -d --build         # rebuild after update

# Backup database
docker compose cp billing:/data/billing.db ~/billing-$(date +%Y%m%d).db

# Restore database
docker compose cp ~/billing-20260401.db billing:/data/billing.db
docker compose restart billing
```

## Reset forgotten admin password

```bash
docker compose exec billing python3 -c "
from app import app, db
from models import User
with app.app_context():
    u = User.query.filter_by(username='admin').first()
    u.set_password('newpassword123')
    db.session.commit()
    print('Password reset')
"
```

---

## Automated daily backup (optional)

```bash
mkdir -p ~/backups
cat > ~/backup-billing.sh << 'SCRIPT'
#!/bin/bash
cd ~/billing
docker compose cp billing:/data/billing.db ~/backups/billing-$(date +%Y%m%d).db
find ~/backups -name "billing-*.db" -mtime +30 -delete
SCRIPT

chmod +x ~/backup-billing.sh

# Schedule at 2am daily
(crontab -l 2>/dev/null; echo "0 2 * * * /home/$USER/backup-billing.sh") | crontab -
```
