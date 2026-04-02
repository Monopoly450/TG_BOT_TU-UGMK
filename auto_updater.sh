#!/bin/bash
echo "🚀 Запуск авто-апдейтера для Telegram бота..."
echo "🎧 Слушаем сигналы из Telegram (Redis)..."

# Переходим в корень проекта
cd "$(dirname "$0")"

# Делаем скрипты исполняемыми
chmod +x update.sh

while true; do
    # Пытаемся получить триггер, игнорируя ошибки если контейнер в рестарте
    TRIGGER=$(docker exec redis_db redis-cli get bot_update_trigger 2>/dev/null)
    
    # Если команда завершилась с ошибкой (например, контейнер выключен), просто ждем
    if [ $? -ne 0 ]; then
        sleep 5
        continue
    fi
    
    # Очищаем ответ
    TRIGGER=$(echo "$TRIGGER" | tr -d '\r\n' | tr -d '"')
    
    if [ "$TRIGGER" = "1" ]; then
        echo "========================================="
        echo "🔄 [$(date)] ПРИНЯТ ЗАПРОС НА ОБНОВЛЕНИЕ ИЗ TG!"
        echo "========================================="
        
        # Сбрасываем триггер ПЕРЕД обновлением, чтобы не уйти в цикл
        docker exec redis_db redis-cli del bot_update_trigger > /dev/null 2>&1
        
        # Запускаем штатный скрипт
        ./update.sh
        
        echo "✅ Скрипт обновления отработал. Возвращаюсь в режим ожидания..."
    fi
    
    sleep 5
done
