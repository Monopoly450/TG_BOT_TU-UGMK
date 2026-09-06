# ТУ УГМК — бот расписания и Mini App

Telegram-бот и мини-приложение: расписание своей группы, подключение к Толку, ИИ-чат с текстом и фотографиями, афиша и панель старосты.

## Контейнеры

| Сервис | Назначение | Доступ |
|---|---|---|
| `bot` | Telegram, меню, подписки и уведомления | Telegram API |
| `miniapp` | Mini App и его API, включая панель старосты | `127.0.0.1:8080`, публично через HTTPS |
| `dashboard` | Отдельный сайт администратора | `127.0.0.1:8081` |
| `worker` | 5 обработчиков расписаний на Playwright | Сайт университета **без прокси** |
| `redis` | Кэш, подписки и очередь расписаний | Внутренняя сеть Docker |
| `db` | PostgreSQL: пользователи, история, настройки | Внутренняя сеть Docker |
| `caddy` | Постоянный домен, HTTPS и сертификаты | Порты 80 и 443, профиль `https` |

У мини-приложения собственный контейнер `tu_miniapp`, процесс и проверка работоспособности. Перезапуск бота не перезапускает Mini App. Общие обработчики API находятся в `dashboard.py`, а отдельные приложения собираются в `web_runtime.py`: сайт администратора не публикуется через контейнер Mini App. Административные функции внутри Mini App требуют подписанных данных Telegram и прав администратора.

## Установка

Нужны Git, Docker и Docker Compose. На Ubuntu/Debian Docker можно установить по [официальной инструкции](https://docs.docker.com/engine/install/ubuntu/).

```bash
git clone git@github.com:Monopoly450/TG_BOT_TU-UGMK.git
cd TG_BOT_TU-UGMK
cp .env.example .env
nano .env
```

Заполните `.env`:

```dotenv
BOT_TOKEN=ТОКЕН_ОТ_BOTFATHER
BOT_USERNAME=ИМЯ_БОТА_БЕЗ_СОБАКИ
LOGIN=ЛОГИН_ПОРТАЛА
PASSWORD=ПАРОЛЬ_ПОРТАЛА
STORAGE_PASSWORD=ДЛИННЫЙ_СЛУЧАЙНЫЙ_ПАРОЛЬ
STAROSTA_PASS=ПАРОЛЬ_СТАРОСТЫ
ADMIN_DASHBOARD_PASS=ПАРОЛЬ_АДМИНИСТРАТОРА
OPENROUTER_API_KEY=КЛЮЧ_OPENROUTER
PROXY_URL=
WEBAPP_URL=https://app.example.ru
MINIAPP_DOMAIN=app.example.ru
MINIAPP_PORT=8080
ADMIN_PORT=8081
COMPOSE_PROFILES=
```

Не публикуйте `.env`, токены и приватные SSH-ключи. `.env` исключён из Git и Docker-образов. Не меняйте `STORAGE_PASSWORD` на работающей установке без миграции зашифрованных данных.

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8080/healthz
```

Для локальной проверки Mini App доступен по `http://127.0.0.1:8080/webapp`; для Telegram нужен публичный HTTPS-адрес. Админ-панель: `http://127.0.0.1:8081`. На удалённом сервере откройте её через SSH:

```bash
ssh -L 8081:127.0.0.1:8081 user@SERVER_IP
```

## Прокси: Telegram через прокси, расписание напрямую

В `.env` используется одна настройка для Telegram:

```dotenv
PROXY_URL=socks5://USER:PASSWORD@PROXY_HOST:1080
```

Также поддерживается `http://USER:PASSWORD@PROXY_HOST:PORT`. Спецсимволы в логине и пароле должны быть URL-кодированы. Пустое значение означает прямое соединение.

`PROXY_URL` применяется к боту и Telegram-запросам из веб-приложений: объявлениям, уведомлениям и получению имени бота. Это не системный прокси для всех контейнеров. OpenRouter отдельно использует стандартное прямое подключение; `PROXY_URL` не направляет ИИ-запросы через Telegram-прокси.

**Парсер всегда обращается к порталу напрямую:**

- Compose очищает `PROXY_URL`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` и их варианты в нижнем регистре у `worker`.
- Перед запуском Playwright эти переменные повторно удаляются, `NO_PROXY=*`.
- Chromium запускается с `--no-proxy-server`.

Это распространяется и на страницу входа/SSO университета. Внешний VPN или прозрачная маршрутизация на самом сервере управляются отдельно от приложения.

После изменения прокси пересоздайте нужные контейнеры, чтобы они перечитали `.env`:

```bash
docker compose up -d --force-recreate bot miniapp dashboard worker
```

## Постоянная ссылка Mini App

`WEBAPP_URL` — фиксированный публичный адрес **без `/webapp`**. Бот добавляет путь сам. Например:

```dotenv
WEBAPP_URL=https://app.example.ru
MINIAPP_DOMAIN=app.example.ru
COMPOSE_PROFILES=https
```

Итоговая ссылка: `https://app.example.ru/webapp`. Обновление образов и перезапуск контейнеров не меняют этот адрес. Скрипт обновления не генерирует туннели и не переписывает `WEBAPP_URL`.

### Вариант 1: собственный домен и Caddy

1. Направьте DNS-запись `A` поддомена на публичный IPv4 сервера. `AAAA` добавляйте только при рабочем IPv6.
2. Откройте входящие TCP-порты 80 и 443. Если на них уже работает Nginx/Caddy, используйте существующий сервер как обратный прокси на `127.0.0.1:8080` вместо запуска второго.
3. Задайте домен в `.env`, как показано выше, и включите `COMPOSE_PROFILES=https`.
4. Запустите сервисы:

```bash
docker compose up -d --build
curl --fail https://app.example.ru/healthz
```

Caddy направляет запросы в `miniapp:8080` и управляет HTTPS-сертификатами. Сертификаты сохраняются в томе `caddy_data`. [Документация Caddy](https://caddyserver.com/docs/automatic-https).

### Вариант 2: постоянный Cloudflare Tunnel

Если сервер не имеет входящего публичного IP, создайте именованный Cloudflare Tunnel в своём аккаунте и привяжите к нему свой домен. Если `cloudflared` работает на хосте, upstream — `http://127.0.0.1:8080`; если в общей сети Compose — `http://miniapp:8080`. Оставьте `COMPOSE_PROFILES` пустым, чтобы не запускать Caddy. В `WEBAPP_URL` укажите постоянный домен туннеля.

Случайные адреса `*.trycloudflare.com` предназначены для временной проверки и не гарантируют сохранение ссылки после перезапуска туннеля. Их нельзя превратить в постоянные одной настройкой `.env`. При переносе уже работающего временного туннеля с админ-панели на Mini App порт 8080 сохраняется, поэтому сам туннель можно не перезапускать. [Ограничения Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/).

После первоначального изменения домена:

```bash
docker compose up -d --force-recreate bot miniapp dashboard
```

Бот при запуске обновляет кнопку приложения в Telegram. В BotFather настройте Main Mini App/Menu Button на тот же `https://app.example.ru/webapp`, если используете эти настройки вручную. В главном меню бота также появляется сообщение с кнопкой открытия приложения.

## GitHub и SSH-синхронизация

Репозиторий: [Monopoly450/TG_BOT_TU-UGMK](https://github.com/Monopoly450/TG_BOT_TU-UGMK).

Значение, начинающееся с `github_pat_`, — персональный токен доступа, **не SSH-ключ**. Не добавляйте его в README, `.env`, команды клонирования или адрес `origin`. Опубликованный токен нужно отозвать в GitHub Settings → Developer settings → Personal access tokens.

Для сервера рекомендуется отдельный SSH deploy key. Если ключа ещё нет:

```bash
ssh-keygen -t ed25519 -C "tu-ugmk-deploy" -f ~/.ssh/id_ed25519_github
cat ~/.ssh/id_ed25519_github.pub
```

Добавьте **публичный** `.pub` ключ в GitHub → репозиторий → Settings → Deploy keys. Для скачивания обновлений достаточно чтения; для отправки изменений нужен доступ на запись. Приватный файл без `.pub` остаётся на сервере. Не перезаписывайте уже существующий ключ.

В `~/.ssh/config` добавьте:

```sshconfig
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_github
    IdentitiesOnly yes
```

Проверьте подключение и настройте репозиторий:

```bash
ssh -T git@github.com
git remote set-url origin git@github.com:Monopoly450/TG_BOT_TU-UGMK.git
git fetch origin main
```

Сообщение GitHub `successfully authenticated, but GitHub does not provide shell access` означает успешную аутентификацию; команда может вернуть код 1. При первом соединении сверяйте отпечаток ключа сервера с [официальными отпечатками GitHub](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints).

## Обновление и перезапуск

```bash
bash update.sh
```

Скрипт останавливается при локальных изменениях, выполняет `git fetch` и `git merge --ff-only`, затем пересобирает контейнеры. Принудительного `git reset --hard` нет. `.env`, адрес Mini App и именованные тома сохраняются.

Отдельные сервисы можно обновлять независимо:

```bash
docker compose up -d --build --no-deps miniapp
docker compose up -d --build --no-deps bot
docker compose up -d --build --no-deps worker
```

Кнопка обновления из панели администратора ставит флаг в Redis. Для её работы `auto_updater.sh` должен запускаться на хосте с доступом к Docker и SSH-ключу GitHub, например через имеющийся `tg_bot_updater.service`. Проверьте пути в unit-файле под свою установку. Docker socket внутрь Mini App не передаётся.

```bash
chmod +x update.sh auto_updater.sh
sudo cp tg_bot_updater.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg_bot_updater
```

## Резервные копии

Перед обновлением существующей установки сохраните PostgreSQL и Redis. Для согласованной копии временно остановите приложения:

```bash
mkdir -p backups
chmod 700 backups
umask 077
docker compose stop bot worker miniapp dashboard
docker compose exec -T db pg_dump -U postgres -d tu_bot -Fc > backups/postgres.dump
docker compose exec -T redis redis-cli SAVE
docker cp redis_db:/data/dump.rdb backups/redis.rdb
docker compose up -d
```

Не используйте `docker compose down -v`: параметр `-v` удаляет тома с данными. Сохраните отдельно `.env` и том `bot_data` с зашифрованными данными. Резервные копии не входят в Git и Docker-образ.

При запуске выполняется идемпотентная миграция `migrations/001_remove_legacy_services.sql`: она удаляет отключённые VPN, покупки и внутренний баланс ИИ, сохраняя пользователей, личные API-ключи и историю. Нормализация групп выполняется через `schedule_config.py`.

## Расписание и ИИ

- Каждый день в 08:00 по Екатеринбургу (UTC+5) воркеры автоматически обновляют кэш всех групп за текущую и следующую недели. Если сервис включился позже 08:00, пропущенное обновление запускается при старте. Общая отметка в Redis предотвращает повторный запуск пятью воркерами. При сбое портала прежнее расписание остаётся доступным.
- Бот и Mini App используют общий ограничитель нагрузки ИИ в Redis: до четырёх одновременных запросов на API-ключ (`AI_MAX_CONCURRENCY`). Остальные ожидают до 30 секунд. Бесплатные запросы запускаются не чаще одного раза в 3,1 секунды (`AI_FREE_INTERVAL_MS`, можно увеличить). При 429 учитывается `Retry-After`, выполняется не более трёх попыток, общий срок ожидания — 70 секунд. Платные модели не выбираются автоматически вместо бесплатных. Дневную квоту OpenRouter параллельность не увеличивает: при её исчерпании нужно дождаться сброса или самостоятельно выбрать недорогую модель. [Лимиты OpenRouter](https://openrouter.ai/docs/faq).
- Доступны текущая и следующая недели. Кнопка «Подключиться онлайн» открывает ссылку занятия или комнату Толк.
- У бота, API и воркеров общий каталог `schedule_config.py`. Группы наборов 22 и 23 убраны, ИТ-24107 объединена без подгрупп. Её составной `objectId` берётся из ссылки портала.
- Подписки и избранное нормализуются при запуске. Перед кэшированием проверяется группа в занятиях. Старые кнопки Telegram определяют группу по названию, а не по изменившемуся номеру в списке.
- Каталог ИИ загружается из OpenRouter `/api/v1/models` при обращении раз в час (`OPENROUTER_MODELS_CACHE_TTL`). При сбое сохраняется последний каталог, повторная попытка — через минуту.
- По умолчанию используется `openrouter/free`. Недорогие модели ограничены $0.30 за 1 млн входных и $1.50 за 1 млн выходных токенов, без платы за запрос; отдельная цена изображения — до $0.001. Эти пределы передаются провайдеру; для бесплатных моделей они нулевые.
- Внутреннего баланса и покупок нет. Платные модели расходуют средства API-ключа сервиса или личного ключа пользователя. Ограничения OpenRouter сохраняются.
- ИИ-чат поддерживает фото, сохранение черновика и компактное поле ввода. Панель старосты содержит объявления с предпросмотром, афишу и настройки.

## Диагностика и тесты

```bash
docker compose ps
docker compose logs --tail 50 bot miniapp dashboard worker
curl --fail http://127.0.0.1:8080/healthz
docker compose run --rm --no-deps --entrypoint python miniapp -m unittest discover -s tests
```

Браузерные сценарии с тестовыми API, без рассылок реальным пользователям:

```bash
docker compose run --rm --no-deps --entrypoint python miniapp tests/miniapp_browser.py
```

Параллельная нагрузка и защита планировщика от дублей проверяются на Redis с изолированными тестовыми ключами, без обращений к OpenRouter:

```bash
docker compose run --rm --no-deps -e PYTHONPATH=/app --entrypoint python miniapp tests/load_integration.py
```

Проверки охватывают каталог моделей, фотографии, подписки, старые кнопки групп, защиту от чужого расписания, раздельные веб-приложения, прямое соединение парсера, интерфейс старосты и работу клавиатуры.
