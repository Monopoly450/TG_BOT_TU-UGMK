# 🤖 Расписание ТУ УГМК (Telegram Bot)

Телеграм-бот для получения расписания с поддержкой Docker и VPN.

## 🚀 Быстрый запуск (Windows)
1. Установите зависимости: `pip install -r requirements.txt`.
2. Установите браузер: `playwright install chromium`.
3. Запустите: `python bot.py`.

---

## 🐳 Деплой на Ubuntu (Docker + VPN)

Этот метод позволяет боту работать 24/7 и обходить блокировки через ваш VPN.

### 1. Подготовка сервера
```bash
sudo apt update && sudo apt install docker.io docker-compose -y
```

### 2. Настройка VPN (Happ / Hiddify)
Если вы используете Happ или Hiddify на том же сервере или в той же сети:
1. Включите в приложении функцию **"Разрешить подключения из локальной сети"** (Allow LAN).
2. Найдите IP сервера и порт прокси (обычно `2080` или `1080`).

### 3. Настройка и запуск
В файле `docker-compose.yml` вы можете указать прокси, чтобы бот работал через него:

```yaml
environment:
  - PROXY_URL=http://ваш_ip:порт  # Пример: http://192.168.1.10:2080
  - BOT_TOKEN=ваш_токен           # Необязательно, если уже вписан в bot.py
```

**Запуск:**
```bash
docker-compose up -d --build
```

### ⚖️ Распределение нагрузки
Если бот долго грузит расписание, увеличьте количество "парсеров":
```bash
docker-compose up -d --scale raspis=3
```

---

## 🛠 Управление
- `docker-compose logs -f raspis` — смотреть, что делает бот прямо сейчас.
- `docker-compose down` — полностью остановить бота.
- `docker-compose restart raspis` — перезапустить только бота.
