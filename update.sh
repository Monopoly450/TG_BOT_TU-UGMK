#!/bin/bash

echo "🔄 Начинаю обновление бота..."

# Переходим в директорию скрипта (корень проекта)
cd "$(dirname "$0")"

# 1. Получаем последние изменения из GitHub (принудительно!)
echo "📥 Скачиваю обновления с GitHub..."
git fetch --all
git reset --hard origin/main

# 2. Пересобираем и перезапускаем Docker контейнеры
echo "🏗 Пересобираю и запускаю контейнеры..."
docker compose up -d --build --remove-orphans

echo "✅ Обновление успешно завершено!"
