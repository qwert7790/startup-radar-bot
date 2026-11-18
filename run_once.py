"""One scheduled Startup Radar cycle for GitHub Actions."""
import os
from bot import TOKEN, CHANNEL, db_connect, run_cycle, send_telegram

if not TOKEN or not CHANNEL:
    raise SystemExit('Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID as GitHub Actions secrets.')

if os.getenv('TEST_MESSAGE', '').lower() == 'true':
    send_telegram('✅ Startup Radar: тестовая публикация. Бот подключён!')
else:
    with db_connect() as con:
        run_cycle(con)
