# Production deploy (Ubuntu + Docker + HTTPS)

Goal: keep the server simple (Docker only), get automatic HTTPS with Let’s Encrypt, and allow CI to publish fresh images.

## 1) Prereqs
- Domain points to the server’s public IP (A/AAAA record); ports 80/443 reachable.
- Tailscale SSH works (for admin access).
- Ubuntu 22.04/24.04 with `sudo` user.

## 2) Install runtime deps on the server
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

## 3) Pull repo + configure env
```bash
git clone https://github.com/brinsoko/LoRa-CP.git lora-kt
cd lora-kt/deploy
cp .env.example .env
```

Edit `deploy/.env`:
- `SECRET_KEY`, `LORA_WEBHOOK_SECRET` (unique random strings)
- `DOMAIN` (e.g., `lora.example.com`) and `ACME_EMAIL` (for Let’s Encrypt notices)
- Optional serial/Google Sheets settings

## 4) Start the stack (with HTTPS)
```bash
cd ~/lora-kt/deploy
docker compose -f docker-compose.prod.yml up -d
```

Caddy will request/renew certificates automatically for `DOMAIN` and reverse-proxy to `web`.
Static assets (including the favicon) are served by the app through the proxy.

## 5) CI: publish images to GHCR
- The workflow `.github/workflows/publish.yml` builds the `lora-kt-web` image and pushes to GHCR on every push to `master` (images: `ghcr.io/brinsoko/lora-cp/...`).
- Make the repo public **or** create a `GHCR_TOKEN` with `packages:read` on the server and log in once:  
  `echo "$GHCR_TOKEN" | docker login ghcr.io -u USERNAME --password-stdin`

## 6) Update the server on new images
```bash
cd ~/lora-kt/deploy
./pull-and-restart.sh
```
This pulls `latest` tags from GHCR and recreates containers with minimal downtime.

Optional: add a deploy step in GitHub Actions after publish:
```yaml
- name: Deploy over SSH
  if: ${{ github.ref == 'refs/heads/main' }}
  uses: appleboy/ssh-action@v1.2.0
  with:
    host: ${{ secrets.DEPLOY_HOST }}
    username: ${{ secrets.DEPLOY_USER }}
    key: ${{ secrets.DEPLOY_KEY }}
    script: |
      cd ~/lora-kt/deploy
      git pull
      ./pull-and-restart.sh
```
(Use your Tailscale SSH host; store the private key as `DEPLOY_KEY`.)

## 7) Directory persistence
- `../instance` and `../data` on the host hold the SQLite DB and data; they’re mounted into the containers.
- `deploy/caddy_data`/`caddy_config` volumes hold certificates and Caddy state.

## 8) Health checks
- App: `https://<DOMAIN>/health`
- Caddy/containers: `docker ps`, `docker compose logs -f`
