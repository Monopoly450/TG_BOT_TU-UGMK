#!/bin/bash
cd /home/user/TG_BOT_TU-UGMK
chmod +x update.sh
while true; do
    TRIGGER=$(docker exec redis_db redis-cli get bot_update_trigger 2>/dev/null)
    if [ $? -eq 0 ]; then
        TRIGGER=$(echo "$TRIGGER" | tr -d '\r\n' | tr -d '"')
        if [ "$TRIGGER" = "1" ]; then
            docker exec redis_db redis-cli del bot_update_trigger > /dev/null 2>&1
            bash ./update.sh
        fi
    fi
    sleep 5
done
