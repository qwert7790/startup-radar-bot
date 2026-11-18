"""Startup Radar: Product Hunt RSS + Hacker News Show HN -> Russian Telegram channel."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlparse, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
INTERVAL = max(5, int(os.getenv("POLL_INTERVAL_SECONDS", "15")))
PH_INTERVAL = max(INTERVAL, int(os.getenv("PRODUCT_HUNT_POLL_SECONDS", "60")))
TRANSLATE_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "").strip()
PH_TOKEN = os.getenv("PRODUCT_HUNT_TOKEN", "").strip()
PH_GRAPHQL = "https://api.producthunt.com/v2/api/graphql"
HN_CACHE = OrderedDict()
PH_FEED_CACHE = {}

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
    con.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, source TEXT NOT NULL, payload TEXT NOT NULL)")
    con.commit()
    return con


def is_seen(con, item_id):
    return con.execute("SELECT 1 FROM seen WHERE id = ?", (item_id,)).fetchone() is not None


def source_initialized(con, source):
    return con.execute("SELECT 1 FROM source_initialized WHERE source = ?", (source,)).fetchone() is not None


def initialize_source(con, source, items):
    if SKIP_EXISTING and not source_initialized(con, source):
        for item in items:
            con.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)",
                        (item["id"], datetime.now(timezone.utc).isoformat()))
        log.info("Первый запуск: пропущено %s старых записей из %s", len(items), source)
    con.execute("INSERT OR IGNORE INTO source_initialized VALUES (?)", (source,))
    con.commit()


def clean(raw):
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def product_hunt():
    headers = PH_FEED_CACHE.get("headers", {})
    resp = HTTP.get(PH_RSS, headers=headers, timeout=10)
    if resp.status_code == 304:
        return PH_FEED_CACHE["items"]
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if not feed.entries and (feed.bozo or not feed.version):
        raise RuntimeError("Product Hunt не вернул корректную RSS/Atom-ленту")
    items = []
    for entry in feed.entries:
        link = entry.get("link", "").strip()
        title = clean(entry.get("title", ""))
        if not title or not link:
            continue
        content = entry.get("content", [])
        raw = content[0].get("value", "") if content else entry.get("summary", "")
        # The Atom feed adds a separate paragraph with Discussion and Link.
        description = clean(re.split(r"<p>\s*<a[^>]+>\s*Discussion", raw,
                                     maxsplit=1, flags=re.I)[0])[:6000]
        stable_id = entry.get("id") or link
        post_id = re.search(r":Post/(\d+)$", stable_id)
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
                      "published": entry.get("published_parsed") or entry.get("updated_parsed"),
                      "ph_id": post_id.group(1) if post_id else ""})
    PH_FEED_CACHE["headers"] = {
        condition: resp.headers[header]
        for header, condition in (("ETag", "If-None-Match"), ("Last-Modified", "If-Modified-Since"))
        if header in resp.headers
    }
    PH_FEED_CACHE["items"] = items
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

    now = time.monotonic()
    to_fetch = [sid for sid in unique_ids if HN_CACHE.get(sid, 0) <= now]
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(fetch, sid) for sid in to_fetch]
        for future in as_completed(futures):
            try:
                story_id, story = future.result()
            except (requests.RequestException, ValueError) as exc:
                fetch_errors += 1
                log.warning("Не удалось получить HN item: %s", exc)
                continue
            title = clean(story.get("title", ""))
            if story.get("deleted") or story.get("dead") or not title.lower().startswith("show hn:"):
                if title:
                    HN_CACHE[story_id] = now + 300
                    HN_CACHE.move_to_end(story_id)
                continue
            title = re.sub(r"^Show HN:\s*", "", title, flags=re.I)
            link = story.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
            items.append({"id": f"hn:{story_id}", "source": "Hacker News / Show HN",
                          "title": title, "description": clean(story.get("text", ""))[:6000],
                          "author": story.get("by"), "kids": story.get("kids", [])[:8],
                          "source_url": f"https://news.ycombinator.com/item?id={story_id}",
                          "url": link, "photo": "", "published": story.get("time", 0)})
    while len(HN_CACHE) > 1500:
        HN_CACHE.popitem(last=False)
    if to_fetch and fetch_errors == len(to_fetch):
        raise RuntimeError("Все запросы к Hacker News завершились ошибкой")
    items.sort(key=lambda it: it.get("published") or 0, reverse=True)
    log.info("HN: проверено %s записей, найдено %s Show HN, ошибок загрузки %s",
             len(to_fetch), len(items), fetch_errors)
    return items


def enrich(item):
    item = item.copy()
    if item.get("ph_id", "").isdigit() and PH_TOKEN:
        response = HTTP.post(PH_GRAPHQL, timeout=10, headers={"Authorization": f"Bearer {PH_TOKEN}"},
                             json={"query": "query($id: ID!) { post(id: $id) { name tagline description url website } }",
                                   "variables": {"id": item["ph_id"]}})
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise RuntimeError("Product Hunt GraphQL вернул ошибку")
        post = (data.get("data") or {}).get("post")
        if post:
            item["title"] = clean(post.get("name")) or item["title"]
            item["source_url"] = post.get("url") or item["url"]
            item["url"] = post.get("website") or item["url"]
            fields = [clean(post.get("tagline")), clean(post.get("description"))]
            item["description"] = ("\n\n".join(dict.fromkeys(field for field in fields if field))
                                   or item["description"])
    elif item.get("author") and len(item["description"]) < 600:
        for comment_id in item.get("kids", []):
            try:
                response = HTTP.get(f"{HN_API}/item/{comment_id}.json", timeout=2)
                response.raise_for_status()
                comment = response.json() or {}
            except (requests.RequestException, ValueError):
                continue
            if comment.get("by") == item["author"] and not (comment.get("dead") or comment.get("deleted")):
                extra = clean(comment.get("text"))
                if extra and extra not in item["description"]:
                    item["description"] = "\n\n".join(filter(None, (item["description"], extra)))
                    break
    return item


def translate_to_russian(text):
    if not text:
        return ""
    if re.search(r"[А-Яа-яЁё]", text) and len(re.findall(r"[А-Яа-яЁё]", text)) > len(re.findall(r"[A-Za-z]", text)):
        return text
    try:
        if TRANSLATE_KEY:
            response = HTTP.post("https://translation.googleapis.com/language/translate/v2",
                                 headers={"X-Goog-Api-Key": TRANSLATE_KEY},
                                 json={"q": text, "target": "ru", "format": "text"}, timeout=8)
            response.raise_for_status()
            result = response.json()["data"]["translations"][0]["translatedText"]
        else:
            response = HTTP.get("https://translate.google.com/m",
                                params={"sl": "auto", "tl": "ru", "q": text}, timeout=8)
            response.raise_for_status()
            node = BeautifulSoup(response.text, "html.parser").select_one(".result-container, .t0")
            result = node.get_text(" ", strip=True) if node else ""
    except (requests.RequestException, ValueError, KeyError, IndexError):
        raise RuntimeError("Перевод недоступен, публикация остаётся в очереди") from None
    if not result or not re.search(r"[А-Яа-яЁё]", result):
        raise RuntimeError("Переводчик не вернул русский текст")
    return result


def shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0] + "…"


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


def web_url(url):
    parsed = urlparse(url)
    return url if parsed.scheme in ("http", "https") and parsed.hostname else ""


def format_post(item):
    # Keep the original product name. In Show HN the headline can be a full sentence.
    title = item["title"]
    if item["source"].startswith("Hacker News") and len(title.split()) > 3:
        title = translate_to_russian(shorten(title, 350))
    desc = shorten(item["description"], 3300)
    translated = translate_to_russian(desc) if desc else "Источник пока не опубликовал описание."
    within_budget = translated.encode("utf-16-le")[:5400].decode("utf-16-le", errors="ignore")
    desc_ru = shorten(translated, len(within_budget))
    project_url = web_url(item["url"])
    source_url = web_url(item.get("source_url") or item["url"])
    source = html.escape(item["source"])
    source_link = f'<a href="{html.escape(source_url, quote=True)}">{source}</a>' if source_url else source
    project_link = (f'🌐 <a href="{html.escape(project_url, quote=True)}">Сайт проекта</a>\n'
                    if project_url and project_url != source_url else "")
    return (f"🚀 <b>{html.escape(shorten(title, 180))}</b>\n\n"
            f"📝 {html.escape(desc_ru)}\n\n"
            f"🏷 {html.escape(category_for(item))}\n"
            f"{project_link}📡 Источник: {source_link}\n\n"
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
            raise RuntimeError("Telegram: сетевая ошибка") from None
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
            raise RuntimeError(f"Telegram: HTTP {r.status_code}: {str(body.get('description', ''))[:300].replace(TOKEN, '[скрыто]')}")
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
    item = enrich(item)
    text = format_post(item)
    photo = image_for(item)
    if photo and len(text.encode("utf-16-le")) <= 2000:
        try:
            _telegram_request("sendPhoto", {"chat_id": CHANNEL, "photo": photo,
                                            "caption": text, "parse_mode": "HTML"})
            return
        except RuntimeError as exc:
            log.warning("Фото не отправлено (%s), пробую текстовую публикацию", exc)
    send_telegram(text)


def run_cycle(con, check_product_hunt=True):
    """Run one collection cycle; return counts for GitHub's log and exit code."""
    stats = {"found": 0, "new": 0, "published": 0,
             "sources_ok": 0, "source_errors": 0, "publication_errors": 0,
             "initialized": 0}
    for source, collector in (("producthunt", product_hunt), ("hackernews", hacker_news)):
        items = []
        if source != "producthunt" or check_product_hunt:
            try:
                items = collector()
                stats["sources_ok"] += 1
                stats["found"] += len(items)
            except Exception as exc:
                stats["source_errors"] += 1
                log.warning("Ошибка загрузки %s: %s", source, type(exc).__name__)
        if items and not source_initialized(con, source):
            initialize_source(con, source, items)
            stats["initialized"] += 1
        with con:
            for item in reversed(items):
                if not is_seen(con, item["id"]):
                    con.execute("INSERT INTO pending VALUES (?, ?, ?) "
                                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                                (item["id"], source, json.dumps(item, ensure_ascii=False)))
        queued = con.execute("SELECT id, payload FROM pending WHERE source = ? ORDER BY rowid",
                             (source,)).fetchall()
        for item_id, payload in queued:
            item = json.loads(payload)
            stats["new"] += 1
            if MAX_POSTS and stats["published"] >= MAX_POSTS:
                break
            try:
                if not is_seen(con, item_id):
                    publish_item(item)
                    with con:
                        con.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)",
                                    (item_id, datetime.now(timezone.utc).isoformat()))
                        con.execute("DELETE FROM pending WHERE id = ?", (item_id,))
                    if source == "hackernews":
                        HN_CACHE[int(item_id.split(":", 1)[1])] = time.monotonic() + 300
                    stats["published"] += 1
                    log.info("ОПУБЛИКОВАНО: %s / %s", source, item["title"][:75])
                    time.sleep(1)
                else:
                    with con:
                        con.execute("DELETE FROM pending WHERE id = ?", (item_id,))
            except Exception as exc:
                stats["publication_errors"] += 1
                # Request exceptions can contain credentials or service URLs.
                detail = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                log.warning("Ошибка публикации %s: %s", item_id, detail)
    log.info("ИТОГ: найдено=%s, новых=%s, опубликовано=%s, "
             "источников_успешно=%s, ошибок_источников=%s, ошибок_публикаций=%s, "
             "первичная_загрузка=%s", stats["found"], stats["new"], stats["published"],
             stats["sources_ok"], stats["source_errors"], stats["publication_errors"],
             stats["initialized"])
    return stats


def watch_hacker_news(wakeup):
    """Wake the worker when Firebase updates the incoming story list."""
    while True:
        try:
            with requests.get(f"{HN_API}/newstories.json", headers={"Accept": "text/event-stream"},
                              stream=True, timeout=(5, 75)) as response:
                response.raise_for_status()
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise RuntimeError("Firebase не вернул поток событий")
                for line in response.iter_lines(chunk_size=64):
                    if line in (b"event: put", b"event: patch"):
                        wakeup.set()
                    elif line in (b"event: cancel", b"event: auth_revoked"):
                        break
        except (requests.RequestException, RuntimeError) as exc:
            log.warning("Поток HN недоступен (%s), продолжаю опрос", type(exc).__name__)
        time.sleep(10)


def main():
    if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN" or not CHANNEL or CHANNEL == "@your_channel_username":
        raise SystemExit("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в файле .env")
    with db_connect() as con:
        log.info("Бот запущен. Интервал: %s секунд. Канал: %s", INTERVAL, CHANNEL)
        next_ph = 0.0
        wakeup = Event()
        Thread(target=watch_hacker_news, args=(wakeup,), daemon=True).start()
        while True:
            wakeup.clear()
            start = time.monotonic()
            check_ph = start >= next_ph
            if check_ph:
                next_ph = start + PH_INTERVAL
            run_cycle(con, check_product_hunt=check_ph)
            # Coalesce bursts, then wait for an event or the next regular check.
            time.sleep(max(0, min(3, INTERVAL) - (time.monotonic() - start)))
            wakeup.wait(max(0, INTERVAL - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
