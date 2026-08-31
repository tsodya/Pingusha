#!/usr/bin/env bash
#
# Pingusha — update (one command)
#   curl -fsSL https://github.com/tsodya/Pingusha/raw/main/update.sh | bash
#
set -e

BOLD='\033[1m'; NC='\033[0m'

INSTALL_DIR="${PINGUSHA_DIR:-$HOME/pingusha}"
cd "$INSTALL_DIR" 2>/dev/null || { echo "❌ Каталог $INSTALL_DIR не найден. Сначала установите Pingusha (install.sh)."; exit 1; }
[ -d .git ] || { echo "❌ $INSTALL_DIR — не похоже на установку Pingusha (нет .git)."; exit 1; }

echo "→ Каталог: $INSTALL_DIR"
echo "→ Обновление кода (git pull)..."
git pull --ff-only

echo "→ Пересборка контейнера..."
docker compose up -d --build

echo ""
echo "=============================================="
echo -e "  ✅ ${BOLD}Pingusha обновлена${NC}"
echo "  Адрес:  http://localhost:8000"
echo "  Данные: сохранены (volume pingusha-data)"
echo "  Пароль/токен: не менялись (.env не трогали)"
echo "=============================================="
