#!/usr/bin/env bash
#
# Pingusha — self-hosted network monitor
# One-command install:  curl -fsSL <URL-to-this-script> | bash
#
set -e

BOLD='\033[1m'; NC='\033[0m'

echo ""
echo "=============================================="
echo -e "  ${BOLD}Pingusha v1.0.0${NC} — self-hosted network monitor"
echo "  карта объектов · дерево устройств · Telegram"
echo "=============================================="
echo ""

# ── 1. Выбор языка / language ────────────────────────
echo "Выберите язык / Select language:"
echo "  1) Русский (ru)"
echo "  2) English (en)"
read -rp "Your choice [1]: " lang_choice
case "${lang_choice:-1}" in
  2|en|EN|english) APP_LANG="en" ;;
  *) APP_LANG="ru" ;;
esac
echo -e "→ ${BOLD}${APP_LANG}${NC}"
echo ""

# ── 2. Каталог установки ─────────────────────────────
INSTALL_DIR="${PINGUSHA_DIR:-$HOME/pingusha}"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
echo -e "→ Установка в: ${BOLD}${INSTALL_DIR}${NC}"

# ── 3. Проверка зависимостей ─────────────────────────
command -v docker >/dev/null 2>&1 || { echo "❌ Docker не установлен / Docker not found"; exit 1; }
command -v docker compose >/dev/null 2>&1 || { echo "❌ docker compose не найден"; exit 1; }

# ── 4. Скачивание кода (если каталог пуст) ───────────
if [ ! -f docker-compose.yml ]; then
  REPO_URL="${PINGUSHA_REPO:-https://github.com/tsodya/Pingusha}"
  echo "→ Скачивание кода из $REPO_URL ..."
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 "$REPO_URL" . || { echo "❌ git clone failed"; exit 1; }
  else
    curl -fsSL "${REPO_URL}/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1 || { echo "❌ download failed"; exit 1; }
  fi
else
  echo "→ Код уже есть, пропускаем скачивание"
fi

# ── 5. Токен Telegram-бота (опционально) ─────────────
if [ -z "$TELEGRAM_TOKEN" ]; then
  read -rp "Telegram bot token (optional, Enter to skip): " TELEGRAM_TOKEN
fi

# ── 6. Пароль админа ──────────────────────────────────
if [ -z "$ADMIN_PASSWORD" ]; then
  ADMIN_PASSWORD="$(openssl rand -base64 15 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c 16 || echo "pingusha$(date +%s)")"
fi

# ── 7. .env ──────────────────────────────────────────
if [ -f .env ]; then
  echo "→ .env уже есть — сохраняем (пароль и токен не меняем)"
  SHOW_PASSWORD=0
else
  cat > .env <<EOF
TELEGRAM_TOKEN=${TELEGRAM_TOKEN:-}
ADMIN_PASSWORD=${ADMIN_PASSWORD}
LANG=${APP_LANG}
EOF
  echo "→ .env создан (язык: ${APP_LANG})"
  SHOW_PASSWORD=1
fi

# ── 8. Запуск ────────────────────────────────────────
echo "→ Запуск контейнера..."
docker compose up -d --build

echo ""
echo "=============================================="
echo -e "  ✅ ${BOLD}Pingusha запущена${NC}"
echo "  Адрес:  http://localhost:8000"
echo "  Язык:   ${APP_LANG}"
echo "  Данные: ./data (volume pingusha-data)"
echo ""
if [ "$SHOW_PASSWORD" = "1" ]; then
  echo -e "  🔑 ${BOLD}Пользователь:${NC} admin"
  echo -e "  🔑 ${BOLD}Пароль:      ${NC}${ADMIN_PASSWORD}"
  echo ""
  echo -e "  ⚠️  ${BOLD}СМЕНИТЕ ПАРОЛЬ в настройках после входа!${NC}"
  echo "  Других пользователей нет — создайте в Настройки → Пользователи"
else
  echo -e "  🔑 Вход: admin / пароль из ${BOLD}.env${NC} (ADMIN_PASSWORD)"
  echo -e "  ℹ️  Обновление: curl -fsSL .../update.sh | bash"
fi
echo "=============================================="
