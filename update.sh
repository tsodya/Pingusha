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

# Автоматический бэкап БД перед пересборкой
BACKUP_DIR="$INSTALL_DIR/backups"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
if docker run --rm -v pingusha-data:/data -v "$BACKUP_DIR":/backup alpine tar czf "/backup/pingusha-db-$STAMP.tar.gz" -C /data . 2>/dev/null; then
  echo "→ Бэкап БД: backups/pingusha-db-$STAMP.tar.gz"
else
  echo "→ ⚠️ Бэкап не удался (нет volume pingusha-data? пропускаем)"
fi

echo "→ Пересборка контейнера..."
docker compose pull 2>&1 | tail -5
docker compose build 2>&1 | tail -5
docker compose up -d

echo ""
echo "=============================================="
echo -e "  ✅ ${BOLD}Pingusha обновлена${NC}"
echo "  Адрес:  http://localhost:45585"
echo "  Данные: сохранены (volume pingusha-data)"
echo "  Пароль/токен: не менялись (.env не трогали)"
echo "=============================================="
