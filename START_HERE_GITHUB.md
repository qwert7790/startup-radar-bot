# Бесплатный запуск Startup Radar через GitHub Actions

Для новичка. Никакой платный сервер не нужен. GitHub запускает Python примерно раз в 10 минут (по расписанию, возможны задержки/пропуски). Для публичных репозиториев стандартные GitHub-hosted runners бесплатны. Код и история опубликованных ID будут публичными; **токен хранится только в GitHub Secrets**.

## 1. Создайте канал и бота в Telegram

1. В Telegram создайте **публичный канал**, задайте username, например `@my_startup_radar_news`.
2. Откройте https://t.me/BotFather, введите `/newbot` и получите токен. НЕ публикуйте токен и НЕ присылайте его незнакомым людям.
3. В канале добавьте своего бота в «Администраторы» и дайте право **Публиковать сообщения**.

## 2. Зарегистрируйтесь на GitHub

Откройте https://github.com и создайте бесплатный аккаунт (если его нет).

## 3. Создайте репозиторий

1. Нажмите `+` → **New repository**.
2. Название: `startup-radar-bot`.
3. Выберите **Public**, нажмите **Create repository**.
4. Нажмите **uploading an existing file** (или **Add file → Upload files**). Распакуйте архив из ChatGPT на вашем компьютере и загрузите **содержимое** папки `startup_radar_bot`, включая `bot.py`, `run_once.py`, `requirements.txt`, `.github/workflows/startup-radar.yml` (в папке `.github` хранится расписание). Веб-интерфейс GitHub может не загружать скрытые папки при выборе файлов: если `.github` не попала в репозиторий, добавьте workflow вручную по шагу ниже.
5. Нажмите **Commit changes**.

**Если `.github/workflows/startup-radar.yml` не загрузился:** в репозитории нажмите **Add file → Create new file**, в поле имени файла введите `.github/workflows/startup-radar.yml` и вставьте содержимое этого файла из архива. Затем нажмите **Commit changes**. Это обязательный файл.

НЕ загружайте `.env`, если вы его создали на компьютере. Он может содержать токен. Файл `.env.example` безопасен (значения-шаблоны).

## 4. Добавьте секреты

В репозитории GitHub откройте **Settings → Secrets and variables → Actions → New repository secret**.

Создайте два секрета (точные имена важны):

- `TELEGRAM_BOT_TOKEN` → полный токен из BotFather
- `TELEGRAM_CHANNEL_ID` → username вашего канала, например `@my_startup_radar_news`

Нажмите **Add secret** для каждого. Не добавляйте токен в код, README или сообщения.

## 5. Проверьте отправку тестового сообщения

1. Откройте вкладку **Actions** в репозитории (при необходимости включите workflow).
2. Слева нажмите **Startup Radar publisher**.
3. Нажмите **Run workflow**; установите `test_message = true` и подтвердите **Run workflow**.
4. Откройте созданный запуск и дождитесь зелёной галочки.
5. В Telegram-канале должно появиться сообщение `✅ Startup Radar: тестовая публикация. Бот подключён!`.

Если красный крестик, откройте шаг **Collect and post new projects** и посмотрите ошибку. `chat not found` означает неправильный адрес или отсутствие доступа бота. `not enough rights` — нет права публиковать. `Unauthorized` — неверный токен.

## 6. Запустите сбор публикаций

Повторите **Actions → Startup Radar publisher → Run workflow**, но **не** включайте `test_message`.

Первый запуск сохранит текущие записи и не будет публиковать старые стартапы (защита от массовых постов). Данные будут сохраняться в `data/startup_radar.sqlite3` в репозитории. Новые записи будут публиковаться при следующих запусках по расписанию. Код начнет проверять источники по cron `3,13,23,33,43,53 * * * *` (UTC), то есть каждые 10 минут по плану; GitHub может задержать/пропустить некоторые запуски.

## Важно

- **Не является гарантированной системой 24/7 или мгновенной публикацией.** В публичных репозиториях GitHub автоматически отключает расписание после 60 дней отсутствия активности. Периодически проверяйте вкладку Actions; при необходимости включайте workflow снова.
- GitHub Actions бесплатно для стандартных хостинговых runners в публичных репозиториях, но правила тарификации могут меняться.
- Источники: Product Hunt RSS и Hacker News Show HN; это **не все стартапы мира**.
- Перевод использует неофициальный сервис через deep-translator и иногда может не работать; тогда публикация будет повторно проверена позже.
- Публичный репозиторий показывает код и sqlite-историю ID (но **не** секреты из GitHub Secrets).
- Если в `Actions` показывается ошибка `Permission ... denied to github-actions[bot]`, проверьте **Settings → Actions → General → Workflow permissions**; если доступно, выберите **Read and write permissions**. Workflow также запрашивает `contents: write`.

Официальная документация:
- https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow
- https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
