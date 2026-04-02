#!/bin/bash
cd /home/user/TG_BOT_TU-UGMK
echo "🔄 Начинаю ПРИНУДИТЕЛЬНОЕ обновление..."
git fetch --all
git reset --hard origin/main
docker compose up -d --build --remove-orphans
echo "✅ Обновление успешно завершено!"
