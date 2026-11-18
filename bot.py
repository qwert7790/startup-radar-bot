"""Startup Radar: Product Hunt RSS + Hacker News Show HN -> Russian Telegram channel."""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
from deep_translator import GoogleTranslator
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
INTERVAL = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "600")))
SKIP_EXISTING = os.getenv("SKIP_EXISTING_ON_FIRST_RUN", "true").lower() == "true"
DB_PATH = os.getenv("DATABASE_PATH", "./data/startup_radar.sqlite3")
MAX_POSTS = max(0, int(os.getenv("MAX_POSTS_PER_CYCLE", "15")))
HN_API = "https://hacker-news.firebaseio.com/v0"
PH_RSS = "https://www.producthunt.com/feed"
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "StartupRadarBot/1.0 (feed reader)", "Accept": "application/json, application/atom+xml, application/rss+xml, */*"})
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("startup-radar")


def db_connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS source_initialized (source TEXT PRIMARY KEY)")
    con.commit()
    return con


def is_seen(con, item_id):
    return con.execute("SELECT 1 FROM seen WHERE id = ?", (item_id,)).fetchone() is not None


def mark_seen(con, item_id):
    con.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (item_id, datetime.now(timezone.utc).isoformat()))
    con.commit()


def source_initialized(con, source):
    return con.execute("SELECT 1 FROM source_initialized WHERE source = ?", (source,)).fetchone() is not None


def initialize_source(con, source, items):
    if SKIP_EXISTING and not source_initialized(con, source):
        for item in items:
            mark_seen(con, item["id"])
        log.info("Первый запуск: пропущено %s старых записей из %s", len(items), source)
    con.execute("INSERT OR IGNORE INTO source_initialized VALUES (?)", (source,))
    con.commit()


def clean(raw):
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def product_hunt():
    resp = HTTP.get(PH_RSS, timeout=25)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS error: {feed.bozo_exception}")
    items = []
    for entry in feed.entries:
        link = entry.get("link", "").strip()
        title = clean(entry.get("title", ""))
        if not title or not link:
            continue
        description = clean(entry.get("summary", ""))[:700]
        stable_id = entry.get("id") or link
        items.append({"id": "ph:" + hashlib.sha256(stable_id.encode()).hexdigest(),
                      "source": "Product Hunt", "title": title,
                      "description": description, "url": link,
                      "published": entry.get("published_parsed") or entry.get("updated_parsed")})
    return items


def hacker_news():
    # Show HN is a showcase feed, not a verified register of incorporated startups.
    resp = HTTP.get(f"{HN_API}/showstories.json", timeout=25)
    resp.raise_for_status()
    story_ids = resp.json()[:75]
    items = []
    for story_id in story_ids:
        try:
            r = HTTP.get(f"{HN_API}/item/{story_id}.json", timeout=12)
            r.raise_for_status()
            story = r.json() or {}
            title = clean(story.get("title", ""))
            if story.get("deleted") or story.get("dead") or not title.lower().startswith("show hn:"):
                continue
            title = re.sub(r"^Show HN:\s*", "", title, flags=re.I)
            link = story.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
            description = clean(story.get("text", ""))[:700]
            items.append({"id": f"hn:{story_id}", "source": "Hacker News / Show HN",
                          "title": title, "description": description,
                          "url": link, "published": story.get("time", 0)})
        except (requests.RequestException, ValueError) as exc:
            log.warning("Не удалось получить HN item %s: %s", story_id, exc)
    return items


def translate_to_russian(text):
    if not text:
        return ""
    # Unofficial public Google Translate endpoint via deep-translator: availability may vary.
    # Translation failures must not produce English posts in a Russian-language channel.
    parts = [text[i:i + 3500] for i in range(0, min(len(text), 7000), 3500)]
    translator = GoogleTranslator(source="auto", target="ru")
    return " ".join(translator.translate(p) for p in parts)


def format_post(item):
    title_ru = translate_to_russian(item["title"])
    desc_ru = translate_to_russian(item["description"]) if item["description"] else "Описание пока не опубликовано."
    if not title_ru or not desc_ru:
        raise RuntimeError("Пустой перевод")
    # Avoid embedding untrusted HTML in Telegram HTML mode.
    safe_title = html.escape(title_ru[:230])
    safe_desc = html.escape(desc_ru[:1200])
    safe_url = html.escape(item["url"], quote=True)
    safe_source = html.escape(item["source"])
    return (f"🚀 <b>НОВЫЙ ПРОЕКТ: {safe_title}</b>\n\n"
            f"📝 {safe_desc}\n\n"
            f"🌐 <a href=\"{safe_url}\">Открыть проект</a>\n"
            f"📡 Источник: {safe_source}\n\n"
            f"#стартапы #новыепроекты")


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    for attempt in range(4):
        r = HTTP.post(url, json={"chat_id": CHANNEL, "text": text,
                                  "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 429 and attempt < 3:
            wait = min(90, max(1, int(body.get("parameters", {}).get("retry_after", 5))))
            log.warning("Telegram rate limit, ожидание %ss", wait)
            time.sleep(wait)
            continue
        if not r.ok or not body.get("ok"):
            raise RuntimeError(f"Telegram: HTTP {r.status_code}: {body.get('description', r.text[:300])}")
        return
    raise RuntimeError("Telegram rate limit")


def run_cycle(con):
    total = 0
    for source, collector in (("producthunt", product_hunt), ("hackernews", hacker_news)):
        try:
            items = collector()
            # Do not initialize when an empty response could be a transient issue.
            if not items:
                log.warning("Источник %s вернул 0 записей", source)
                continue
            if not source_initialized(con, source):
                initialize_source(con, source, items)
            # HN items are newest-first; use reverse order to post older items first.
            for item in reversed(items):
                if is_seen(con, item["id"]):
                    continue
                if MAX_POSTS and total >= MAX_POSTS:
                    log.info("Достигнут лимит публикаций за цикл: %s", MAX_POSTS)
                    return
                try:
                    message = format_post(item)
                    send_telegram(message)
                    mark_seen(con, item["id"])  # only after successful Telegram send
                    total += 1
                    log.info("Опубликовано: %s / %s", source, item["title"][:75])
                    time.sleep(2)  # reduce flood risk
                except Exception as exc:
                    log.exception("Ошибка публикации %s: %s", item["id"], exc)
                    # Do not mark seen: retry next cycle.
        except Exception as exc:
            log.exception("Ошибка источника %s: %s", source, exc)
    log.info("Цикл завершён. Опубликовано: %s", total)


def main():
    if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN" or not CHANNEL or CHANNEL == "@your_channel_username":
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в файле .env")
    with db_connect() as con:
        log.info("Бот запущен. Интервал: %s секунд. Канал: %s", INTERVAL, CHANNEL)
        while True:
            run_cycle(con)
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
