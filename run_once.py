"""Single execution for GitHub Actions with meaningful failure status."""
import os
from bot import TOKEN, CHANNEL, db_connect, run_cycle, send_telegram

if not TOKEN or not CHANNEL:
    raise SystemExit("Отсутствует TELEGRAM_BOT_TOKEN или TELEGRAM_CHANNEL_ID в GitHub Secrets")

if os.getenv("TEST_MESSAGE", "").strip().lower() == "true":
    send_telegram("✅ Startup Radar: тестовая публикация. Бот подключён!")
    print("ТЕСТ: сообщение успешно отправлено в Telegram", flush=True)
else:
    with db_connect() as con:
        stats = run_cycle(con)
    if stats["sources_ok"] == 0:
        raise SystemExit("ОШИБКА: не удалось прочитать ни один источник стартапов")
    if stats["publication_errors"]:
        raise SystemExit(f"ОШИБКА: не опубликовано {stats['publication_errors']} проектов; смотрите журнал")
    if stats["source_errors"]:
        raise SystemExit(f"ОШИБКА: недоступно источников: {stats['source_errors']}; смотрите журнал")
    print(f"УСПЕХ: опубликовано {stats['published']}; новых {stats['new']}; "
          f"всего найдено {stats['found']}", flush=True)
