#!/bin/bash

# Укажите ваши данные от GitHub (пароль от аккаунта больше не работает, нужен Personal Access Token)
# Токен можно создать здесь: https://github.com/settings/tokens
GITHUB_USER="Monopoly450"
GITHUB_TOKEN="ghp_7j1J8b9v1y1z1a1b1c1d1e1f1g1h1i1j1k1l"

echo "🔄 Начинаю обновление бота..."

# Переходим в директорию скрипта (корень проекта)
cd "$(dirname "$0")"

# 1. Получаем последние изменения из GitHub
echo "📥 Скачиваю обновления с GitHub..."
git pull https://${GITHUB_USER}:${GITHUB_TOKEN}@github.com/Monopoly450/TG_BOT_TU-UGMK.git main


# 2. Пересобираем и перезапускаем Docker контейнеры
echo "🏗 Пересобираю контейнеры..."
docker compose build --no-cache

echo "🚀 Перезапускаю бота..."
docker compose up -d

echo "✅ Обновление успешно завершено!"
