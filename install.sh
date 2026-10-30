#!/usr/bin/env bash
# Usage: bash install.sh   (bot ki files wale folder me chalao)
set -e

APP=/opt/audiobot
RUN_USER=${SUDO_USER:-$USER}

echo "==> Packages install ho rahe hain..."
sudo apt update -y
sudo apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

echo "==> Files $APP me copy ho rahi hain..."
sudo mkdir -p $APP
sudo cp -r ./* $APP/ 2>/dev/null || true
sudo cp .env.example $APP/ 2>/dev/null || true
sudo chown -R $RUN_USER:$RUN_USER $APP
cd $APP

echo "==> Python environment ban raha hai..."
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> .env bharo (API_ID, API_HASH, BOT_TOKEN, TARGET_BOT, ALLOWED_CHATS ...)"
  read -p "Enter dabao nano kholne ke liye..." _
  nano .env
fi

echo "==> Userbot login (phone number + OTP daalo)"
./venv/bin/python login.py

echo "==> Service ban rahi hai..."
sed "s/^User=.*/User=$RUN_USER/" audiobot.service | sudo tee /etc/systemd/system/audiobot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now audiobot

read -p "Razorpay webhook ke liye domain (khali = skip): " DOMAIN
if [ -n "$DOMAIN" ]; then
  PORT=$(grep -E '^WEBHOOK_PORT=' .env | cut -d= -f2); PORT=${PORT:-8080}
  sudo tee /etc/nginx/sites-available/audiobot >/dev/null <<NGX
server {
    server_name $DOMAIN;
    location /rzp-webhook { proxy_pass http://127.0.0.1:$PORT; }
}
NGX
  sudo ln -sf /etc/nginx/sites-available/audiobot /etc/nginx/sites-enabled/audiobot
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot --nginx -d "$DOMAIN"
  echo "Razorpay webhook URL: https://$DOMAIN/rzp-webhook  (event: payment_link.paid)"
fi

echo
echo "Ho gaya! Logs dekhne ke liye: journalctl -u audiobot -f"
echo "Restart: sudo systemctl restart audiobot"
