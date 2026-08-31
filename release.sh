#!/usr/bin/env bash
#
# Pingusha — release script
# Usage: ./release.sh 1.1.0
#   1. bumps version everywhere (main.py, index.html, install.sh, README)
#   2. commits, tags v<version>, pushes to GitHub
#
set -e

V="$1"
if [ -z "$V" ]; then
  echo "Usage: ./release.sh 1.1.0"
  exit 1
fi

cd "$(dirname "$0")"

echo "→ Версия: $V"

# ── 1. Обновить версию в файлах ─────────────────────
sed -i "s/VERSION = \".*\"/VERSION = \"$V\"/" app/main.py
sed -i "s/v[0-9]*\.[0-9]*\.[0-9]*/v$V/g" app/index.html install.sh
sed -i "s/Version: .*/Version: $V/" README.md
sed -i "s/Версия: .*/Версия: $V/" README.ru.md
echo "→ Версия обновлена в main.py, index.html, install.sh, README"

# ── 2. Проверки ─────────────────────────────────────
bash -n install.sh && bash -n update.sh && bash -n release.sh
python3 -m py_compile app/main.py
python3 - << 'PYEOF'
import re
html = open('app/index.html', encoding='utf-8').read()
scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
open('/tmp/pi18n.js', 'w').write('\n'.join(scripts))
PYEOF
node --check /tmp/pi18n.js
echo "→ Проверки пройдены (bash/python/JS)"

# ── 3. Коммит + тег + пуш ──────────────────────────
git add -A
git commit -m "v$V"
git tag "v$V"
git push origin main --tags
echo ""
echo "=============================================="
echo -e "  ✅ v${V} released: github.com/tsodya/Pingusha"
echo "  Пользователи обновятся: curl .../update.sh | bash"
echo "=============================================="
