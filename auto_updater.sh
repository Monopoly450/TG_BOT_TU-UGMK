#!/bin/bash
echo "🚀 Запуск авто-апдейтера для Telegram бота..."
echo "🎧 Слушаем сигналы из Telegram (Redis)..."

# Переходим в корень проекта
cd "$(dirname "$0")"

# Делаем скрипты исполняемыми
chmod +x update.sh

while true; do
    # Пингуем редис
    TRIGGER=$(docker exec redis_db redis-cli get bot_update_trigger 2>/dev/null)
    
    # Очищаем ответ от переносов строк
    TRIGGER=$(echo "$TRIGGER" | tr -d '\r\n')
    
    if [ "$TRIGGER" = "\"1\"" ] || [ "$TRIGGER" = "1" ]; then
        echo "========================================="
        echo "🔄 [$(date)] ПРИНЯТ ЗАПРОС НА ОБНОВЛЕНИЕ ИЗ TG!"
        echo "========================================="
        
        # Сбрасываем триггер
        docker exec redis_db redis-cli del bot_update_trigger > /dev/null
        
        # Запускаем штатный скрипт
        ./update.sh
        
        echo "✅ Скрипт обновления отработал. Возвращаюсь в режим ожидания..."
    fi
    
    sleep 5
done
