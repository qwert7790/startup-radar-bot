# Startup Radar Bot — автоматический Telegram-канал на русском

Получает свежие проекты из **Product Hunt RSS** и **Hacker News / Show HN**, переводит заголовки и описания на русский, публикует в Telegram-канал и сохраняет ID в SQLite для предотвращения повторов.

**Не является каталогом всех зарегистрированных стартапов мира.** Некоторые Show HN-публикации — личные проекты, а RSS Product Hunt может быть недоступен или задерживаться.

## 1. Создайте канал

В Telegram: «Создать канал» → выберите название, например `Startup Radar` → сделайте канал публичным и задайте уникальный адрес, например `@startup_radar_example` (это только пример).

## 2. Создайте бота

Откройте официального [@BotFather](https://t.me/BotFather) → `/newbot` → придумайте имя и username с окончанием `bot` → получите токен. Никому его не отправляйте и не публикуйте.

В вашем канале: «Управление каналом» → «Администраторы» → «Добавить администратора» → найдите бота → предоставьте право **Публикация сообщений**.

## 3. Заполните настройки

Создайте копию файла `.env.example` под названием `.env` и укажите:

```
TELEGRAM_BOT_TOKEN=токен_от_BotFather
TELEGRAM_CHANNEL_ID=@ваш_публичный_канал
```

Все остальные настройки можно оставить без изменений. **Никогда не загружайте `.env` в публичный Git-репозиторий**.

## 4. Запуск через Docker (рекомендуется для VPS Linux)

Установите Docker с Compose plugin. В папке проекта запустите:

```bash
cp .env.example .env
nano .env  # укажите токен и канал
mkdir -p data
docker compose up -d --build
docker compose logs -f --tail=100
```

Для остановки: `docker compose down`. Для повторного запуска: `docker compose up -d`. Данные о публикациях лежат в `./data/startup_radar.sqlite3` и сохраняются при перезапуске.

## 5. Запуск без Docker

Требуется Python 3.10+:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

В Windows активируйте окружение через `.venv\\Scripts\\activate` и используйте `copy .env.example .env`.

**Важно:** при запуске через Python вне Docker измените `DATABASE_PATH=./data/startup_radar.sqlite3` в `.env`.

## Как проверить работу

1. Убедитесь, что в логе появилась строка `Бот запущен`.
2. В первый проход бот запоминает текущие записи и не публикует старые (`SKIP_EXISTING_ON_FIRST_RUN=true`). Поэтому сразу после первого запуска новых сообщений может не быть.
3. Для тестовой отправки отдельно выполните (с заменой переменных в `.env`):

```bash
python -c 'import os,requests;from dotenv import load_dotenv;load_dotenv();r=requests.post("https://api.telegram.org/bot"+os.environ["TELEGRAM_BOT_TOKEN"]+"/sendMessage",json={"chat_id":os.environ["TELEGRAM_CHANNEL_ID"],"text":"✅ Startup Radar: тестовая публикация"},timeout=20);print(r.status_code,r.text)'
```

Или внутри Docker: `docker compose exec startup-radar python -c '...'` с той же Python-командой.

Если хотите увидеть доступные записи сразу, установите **до первого запуска** `SKIP_EXISTING_ON_FIRST_RUN=false`. Это может создать серию публикаций. После первого запуска смена настройки не возвращает первоначальные записи; для сброса истории придётся удалить базу SQLite при остановленном боте (будут возможны повторы).

## Параметры

- `POLL_INTERVAL_SECONDS=600`: проверка раз в 10 минут (минимум 60 сек).
- `SKIP_EXISTING_ON_FIRST_RUN=true`: не загружать старые публикации при первом запуске.
- `MAX_POSTS_PER_CYCLE=15`: ограничить число сообщений за цикл; `0` снимает лимит.
- `DATABASE_PATH=/app/data/startup_radar.sqlite3`: путь базы в Docker.

## Учитывайте

- Перевод выполняется через библиотеку `deep-translator`, использующую неофициальный веб-интерфейс Google Translate, и может перестать работать или временно блокироваться. Если перевод недоступен, проект остаётся в очереди до следующего цикла.
- Продукт может появиться в RSS с задержкой; момент обнаружения не равен моменту основания стартапа.
- «Show HN» — демонстрации продуктов, а не только юридически зарегистрированные стартапы.
- При отключении интернета источники и отправка будут повторяться на следующих циклах.
- Возможен повтор после сбоя в момент между успешной отправкой и записью в базу.
- Токены и персональные ключи не нужно отправлять в чат.
- Не запускайте две копии бота на одной базе одновременно.

## Документация

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Hacker News API](https://github.com/HackerNews/API)
- [Product Hunt RSS — справка](https://help.producthunt.com/en/articles/484970-does-product-hunt-have-an-rss-feed)
