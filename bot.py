"""Startup Radar: Product Hunt RSS + Hacker News Show HN -> Russian Telegram channel."""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin

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
        # Product Hunt RSS sometimes contains media thumbnails / logos.
        photo = ""
        for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
            candidate = media.get("url", "")
            if candidate.startswith("https://"):
                photo = candidate
                break
        if not photo:
            for enclosure in entry.get("enclosures", []):
                if enclosure.get("type", "").startswith("image/") and enclosure.get("href", "").startswith("https://"):
                    photo = enclosure["href"]
                    break
        if not photo:
            match = re.search(r'<img[^>]+src=["\']([^"\']+)', entry.get("summary", ""), flags=re.I)
            if match:
                candidate = urljoin(link, html.unescape(match.group(1)))
                if candidate.startswith("https://"):
                    photo = candidate
        items.append({"id": "ph:" + hashlib.sha256(stable_id.encode()).hexdigest(),
                      "source": "Product Hunt", "title": title,
                      "description": description, "url": link, "photo": photo,
                      "published": entry.get("published_parsed") or entry.get("updated_parsed")})
    return items


def hacker_news():
    """Search both fresh HN submissions and the curated Show HN list."""
    story_ids = []
    for endpoint, limit in (("newstories", 180), ("showstories", 75)):
        response = HTTP.get(f"{HN_API}/{endpoint}.json", timeout=25)
        response.raise_for_status()
        story_ids.extend(response.json()[:limit])
    unique_ids = list(dict.fromkeys(story_ids))
    items = []
    fetch_errors = 0

    def fetch(story_id):
        # Thread-local request per call; do not share a mutable Session across workers.
        response = requests.get(f"{HN_API}/item/{story_id}.json", timeout=14)
        response.raise_for_status()
        return story_id, response.json() or {}

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(fetch, sid) for sid in unique_ids]
        for future in as_completed(futures):
            try:
                story_id, story = future.result()
            except (requests.RequestException, ValueError) as exc:
                fetch_errors += 1
                log.warning("Не удалось получить HN item: %s", exc)
                continue
            title = clean(story.get("title", ""))
            if story.get("deleted") or story.get("dead") or not title.lower().startswith("show hn:"):
                continue
            title = re.sub(r"^Show HN:\s*", "", title, flags=re.I)
            link = story.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
            items.append({"id": f"hn:{story_id}", "source": "Hacker News / Show HN",
                          "title": title, "description": clean(story.get("text", ""))[:700],
                          "url": link, "photo": "", "published": story.get("time", 0)})
    if unique_ids and fetch_errors == len(unique_ids):
        raise RuntimeError("Все запросы к Hacker News завершились ошибкой")
    items.sort(key=lambda it: it.get("published") or 0, reverse=True)
    log.info("HN: проверено %s записей, найдено %s Show HN, ошибок загрузки %s",
             len(unique_ids), len(items), fetch_errors)
    return items


def translate_to_russian(text):
    if not text:
        return ""
    # Unofficial public Google Translate endpoint via deep-translator: availability may vary.
    # Translation failures must not produce English posts in a Russian-language channel.
    parts = [text[i:i + 3500] for i in range(0, min(len(text), 7000), 3500)]
    translator = GoogleTranslator(source="auto", target="ru")
    
    try:
        return " ".join(translator.translate(p) for p in parts)
    except Exception as e:
        print(f"Перевод недоступен: {type(e).__name__}. Используем оригинальный текст.")
        return text



def category_for(item):
    text = (item.get("title", "") + " " + item.get("description", "")).lower()
    groups = [
        ("🤖 Искусственный интеллект", ("ai ", " ai", "artificial intelligence", "llm", "gpt", "machine learning")),
        ("💰 Финтех", ("fintech", "payments", "banking", "finance", "crypto")),
        ("🧰 Для бизнеса / SaaS", ("saas", "crm", "automation", "business", "workflow")),
        ("📱 Приложения", ("ios", "android", "mobile", "app ")),
        ("🎮 Игры", ("game", "gaming")),
        ("🏥 Здоровье", ("health", "medical", "fitness")),
        ("🛍 Электронная коммерция", ("shop", "ecommerce", "e-commerce", "marketplace")),
        ("🧑‍💻 Инструменты разработчиков", ("developer", "api", "github", "coding", "open source")),
    ]
    for label, words in groups:
        if any(word in text for word in words):
            return label
    return "🌍 Технологии и новые проекты"


def format_post(item, photo_caption=False):
    title_ru = translate_to_russian(item["title"])
    desc_ru = translate_to_russian(item["description"]) if item["description"] else "Описание пока не опубликовано."
    if not title_ru or not desc_ru:
        raise RuntimeError("Пустой перевод")
    # Telegram sendPhoto captions are limited to 1024 characters after parsing entities.
    # Keep a comfortable margin; the text-only message can be slightly longer.
    max_description = 480 if photo_caption else 1050
    safe_title = html.escape(title_ru[:150])
    safe_desc = html.escape(desc_ru[:max_description])
    safe_url = html.escape(item["url"], quote=True)
    safe_source = html.escape(item["source"])
    safe_category = html.escape(category_for(item))
    return (f"🚀 <b>{safe_title}</b>\n\n"
            f"📝 {safe_desc}\n\n"
            f"🏷 {safe_category}\n"
            f"🌐 <a href=\"{safe_url}\">Сайт проекта</a>\n"
            f"📡 Источник: {safe_source}\n\n"
            f"#стартапы #новыепроекты")


def _telegram_request(method, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    for attempt in range(4):
        try:
            r = HTTP.post(url, json=payload, timeout=30)
        except requests.RequestException:
            if attempt < 3:
                time.sleep(min(2 ** attempt * 2, 12))
                continue
            raise
        try:
            body = r.json()
        except ValueError:
            body = {}
        if (r.status_code == 429 or r.status_code >= 500) and attempt < 3:
            wait = (max(1, int(body.get("parameters", {}).get("retry_after", 5)))
                    if r.status_code == 429 else min(2 ** attempt * 2, 12))
            log.warning("Telegram HTTP %s: повтор через %s секунд", r.status_code, wait)
            time.sleep(wait)
            continue
        if not r.ok or not body.get("ok"):
            raise RuntimeError(f"Telegram: HTTP {r.status_code}: {body.get('description', r.text[:300])}")
        return
    raise RuntimeError("Telegram: не удалось отправить сообщение после повторов")


def send_telegram(text):
    _telegram_request("sendMessage", {"chat_id": CHANNEL, "text": text,
                                     "parse_mode": "HTML", "disable_web_page_preview": True})


def image_for(item):
    # Prefer an actual product image exposed by the RSS feed.
    photo = item.get("photo", "")
    if photo.startswith("https://"):
        return photo
    # Otherwise use a site favicon: this is an icon, not necessarily the official startup logo.
    host = urlparse(item["url"]).hostname or ""
    if host and host not in ("news.ycombinator.com", "www.producthunt.com", "producthunt.com"):
        return f"https://www.google.com/s2/favicons?domain={host}&sz=128"
    return ""


def publish_item(item):
    photo = image_for(item)
    if photo:
        try:
            caption = format_post(item, photo_caption=True)
            _telegram_request("sendPhoto", {"chat_id": CHANNEL, "photo": photo,
                                            "caption": caption, "parse_mode": "HTML"})
            return
        except (RuntimeError, requests.RequestException) as exc:
            log.warning("Фото не отправлено (%s), пробую текстовую публикацию", exc)
    send_telegram(format_post(item))


def run_cycle(con):
    """Run one collection cycle; return counts for GitHub's log and exit code."""
    stats = {"found": 0, "new": 0, "published": 0,
             "sources_ok": 0, "source_errors": 0, "publication_errors": 0,
             "initialized": 0}
    for source, collector in (("producthunt", product_hunt), ("hackernews", hacker_news)):
        try:
            items = collector()
        except Exception as exc:
            stats["source_errors"] += 1
            log.exception("Ошибка загрузки источника %s: %s", source, exc)
            continue
        stats["sources_ok"] += 1
        stats["found"] += len(items)
        if not items:
            log.warning("Источник %s вернул 0 записей — не инициализирую историю", source)
            continue
        was_initialized = source_initialized(con, source)
        if not was_initialized:
            initialize_source(con, source, items)
            stats["initialized"] += 1
        # Older first, keeping new posts in chronological order.
        for item in reversed(items):
            if is_seen(con, item["id"]):
                continue
            stats["new"] += 1
            if MAX_POSTS and stats["published"] >= MAX_POSTS:
                log.info("Достигнут лимит публикаций за цикл: %s", MAX_POSTS)
                break
            try:
                publish_item(item)
                mark_seen(con, item["id"])  # only after a confirmed Telegram send
                stats["published"] += 1
                log.info("ОПУБЛИКОВАНО: %s / %s", source, item["title"][:75])
                time.sleep(1)
            except Exception as exc:
                stats["publication_errors"] += 1
                log.exception("Ошибка публикации %s: %s", item["id"], exc)
    log.info("ИТОГ: найдено=%s, новых=%s, опубликовано=%s, "
             "источников_успешно=%s, ошибок_источников=%s, ошибок_публикаций=%s, "
             "первичная_загрузка=%s", stats["found"], stats["new"], stats["published"],
             stats["sources_ok"], stats["source_errors"], stats["publication_errors"],
             stats["initialized"])
    return stats


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
