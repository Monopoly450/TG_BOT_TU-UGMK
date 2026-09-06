#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Обновление остановлено: сохраните локальные изменения в Git."
    exit 1
fi
git fetch origin main
git merge --ff-only origin/main
docker compose up -d --build --remove-orphans
echo "Обновление завершено. WEBAPP_URL и данные в томах сохранены."
