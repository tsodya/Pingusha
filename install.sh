#!/usr/bin/env bash
#
# Pingusha — self-hosted network monitor
# One-command install:  curl -fsSL https://github.com/tsodya/Pingusha/raw/main/install.sh | bash
#
set -e

BOLD='\033[1m'; NC='\033[0m'

echo ""
echo "=============================================="
echo -e "  ${BOLD}Pingusha v1.0.1${NC} — self-hosted network monitor"
echo "  map of sites · device tree · Telegram"
echo "=============================================="
echo ""

# ── 1. Language (interface, not installer) ───────────
# Installer is always English. UI language: ru/en (default en, changeable in-app).
APP_LANG="${APP_LANG:-en}"

# ── 2. Install directory ─────────────────────────────
INSTALL_DIR="${PINGUSHA_DIR:-$HOME/pingusha}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
echo -e "→ Install to: ${BOLD}${INSTALL_DIR}${NC}"

# ── 3. Dependencies (installed automatically) ────────
if [ "$(id -u)" != "0" ]; then
  echo "❌ Root required. Run it like this:"
  echo "   curl -fsSL <URL> | sudo bash"
  exit 1
fi

NEED_APT=0
command -v curl >/dev/null 2>&1 || NEED_APT=1
command -v git >/dev/null 2>&1 || NEED_APT=1
command -v docker >/dev/null 2>&1 || NEED_APT=1
docker compose version >/dev/null 2>&1 || NEED_APT=1

if [ "$NEED_APT" = "1" ]; then
  echo "→ Installing dependencies (curl, git, docker, docker compose)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # docker compose plugin: docker-compose-v2 (Ubuntu 22.04+) or docker-compose-plugin (24.04+)
  if ! apt-get install -y -qq curl git docker.io docker-compose-v2 2>/dev/null; then
    apt-get install -y -qq curl git docker.io docker-compose-plugin
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  echo "→ Docker installed: $(docker --version)"
else
  echo "→ Dependencies already present (curl, git, docker)"
fi

# ── 4. Code (install or update) ───────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
  # Уже установлено → режим обновления
  echo "→ Existing installation found — updating..."
  git pull --ff-only
  echo -e "→ Version: ${BOLD}$(git describe --tags 2>/dev/null || git log --oneline -1)${NC}"
  # Автобэкап БД перед пересборкой
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^pingusha$'; then
    mkdir -p "$INSTALL_DIR/backups"
    STAMP=$(date +%Y%m%d-%H%M%S)
    docker run --rm -v pingusha-data:/data -v "$INSTALL_DIR/backups":/backup alpine tar czf "/backup/pingusha-db-$STAMP.tar.gz" -C /data . 2>/dev/null \
      && echo "→ DB backup: backups/pingusha-db-$STAMP.tar.gz" || echo "⚠️  DB backup skipped"
  fi
elif [ ! -f docker-compose.yml ]; then
  REPO_URL="${PINGUSHA_REPO:-https://github.com/tsodya/Pingusha}"
  echo "→ Downloading code from $REPO_URL ..."
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 "$REPO_URL" . || { echo "❌ git clone failed"; exit 1; }
  else
    curl -fsSL "${REPO_URL}/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1 || { echo "❌ download failed"; exit 1; }
  fi
fi

# ── 5. Admin password ─────────────────────────────────
if [ -z "$ADMIN_PASSWORD" ]; then
  ADMIN_PASSWORD="$(openssl rand -base64 15 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c 16 || echo "pingusha$(date +%s)")"
fi

# ── 6. .env ──────────────────────────────────────────
# Telegram bot token is NOT set here — admin enters it in the web UI
# (Settings → Telegram bot token). The app falls back to the DB value.
if [ -f .env ]; then
  echo "→ .env exists — keeping it (password unchanged)"
  SHOW_PASSWORD=0
else
  cat > .env <<EOF
TELEGRAM_TOKEN=
ADMIN_PASSWORD=${ADMIN_PASSWORD}
LANG=${APP_LANG}
EOF
  echo "→ .env created (language: ${APP_LANG})"
  SHOW_PASSWORD=1
fi

# ── 7. Launch ────────────────────────────────────────
# Прозрачный прогресс: pull → build → start
echo "→ Pulling Docker images (first run downloads ~50 MB base image, may take a few minutes)..."
docker compose pull 2>&1 | tail -8
echo "→ Building image..."
docker compose build 2>&1 | tail -8
echo "→ Starting container..."
docker compose up -d

echo ""
echo "=============================================="
echo -e "  ✅ ${BOLD}Pingusha is running${NC}"
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "  URL:     http://${HOST_IP:-localhost}:45585"
echo "  Data:    volume pingusha-data"
echo ""
if [ "$SHOW_PASSWORD" = "1" ]; then
  echo -e "  🔑 ${BOLD}User:${NC}     admin"
  echo -e "  🔑 ${BOLD}Password:${NC} ${ADMIN_PASSWORD}"
  echo ""
  echo -e "  ⚠️  ${BOLD}Change the password in Settings after login!${NC}"
  echo "  No other users — create them in Settings → Users"
else
  echo -e "  🔑 Login: admin / password from ${BOLD}.env${NC} (ADMIN_PASSWORD)"
  echo -e "  ℹ️  Update: curl -fsSL https://github.com/tsodya/Pingusha/raw/main/update.sh | bash"
fi
echo "=============================================="
