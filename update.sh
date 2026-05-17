#!/bin/bash
cd "$(dirname "$0")"
echo "🔄 Начинаю ПРИНУДИТЕЛЬНОЕ обновление..."
git fetch --all
git reset --hard origin/main
docker compose up -d --build --remove-orphans
echo "✅ Обновление успешно завершено!"
