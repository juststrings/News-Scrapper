import argparse
import datetime
import json
import logging
import os
import re
import sqlite3
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import feedparser
from bs4 import BeautifulSoup
import requests
from flask import Flask, jsonify, request
from requests import Response
from urllib.parse import urljoin

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)

SUBSCRIBER_DB = os.getenv("SUBSCRIBER_DB", "subscribers.db")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

USER_AGENT = "news-briefing-workflow/1.0"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

app = Flask(__name__)

NEWS_RSS_FEEDS = [
    "https://rss.punchng.com/v1/category/latest_news",
    "https://www.premiumtimesng.com/feed",
    "https://www.vanguardngr.com/feed/",
    "https://dailypost.ng/feed/",
]

REDDIT_FEEDS = [
    "https://www.reddit.com/r/Nigeria/hot.json?limit=10",
    "https://www.reddit.com/r/NigeriaTech/hot.json?limit=10",
]

GOOGLE_TRENDS_NG_URL = "https://trends.google.com/trending/rss?geo=NG"

NITTER_INSTANCES = [
    instance.strip().rstrip("/")
    for instance in os.getenv(
        "NITTER_INSTANCES",
        "https://nitter.net,https://nitter.poast.org,https://xcancel.com",
    ).split(",")
    if instance.strip()
]

X_SEARCH_QUERY = os.getenv("X_SEARCH_QUERY", '"Nigeria" min_faves:50 -filter:replies')

X_DDG_SEARCH_QUERY = os.getenv(
    "X_DDG_SEARCH_QUERY",
    '(Nigeria OR Nigerian) (Tinubu OR APC OR PDP OR INEC OR EFCC OR "National Assembly" OR Naira OR ASUU) '
    '(site:twitter.com OR site:x.com)',
)

TELEGRAM_SUBSCRIBE_MESSAGE = (
    "👋 *Welcome to the Nigerian Content Briefing!*\n\n"
    "You're now subscribed. Every few hours I scan Nigerian news (Punch, Premium Times, Vanguard, Daily Post), Reddit (r/Nigeria, r/NigeriaTech) and X/Twitter, then send you a numbered *digest* of the top stories with viral scores.\n\n"
    "Reply with a story number (e.g. \"3\") or \"more on 3\" any time to get that story's full AI brief — summary, community sentiment, and 2 ready-to-use video reaction hooks.\n\n"
    "📋 *Commands*\n/start — subscribe (or resubscribe)\n/stop — pause briefings\n/help — show this message again\n\n"
    "Sit tight — your first digest will arrive on the next scheduled run. 🔥"
)

TELEGRAM_UNSUBSCRIBE_MESSAGE = (
    "🛑 You have been unsubscribed from the Nigerian Content Briefing. Send /start anytime to rejoin."
)

TELEGRAM_HELP_MESSAGE = (
    "ℹ️ *Nigerian Content Briefing — Help*\n\n"
    "I deliver AI-scored content briefings from Nigerian news, Reddit and X/Twitter, built for content creators looking for reaction-video ideas.\n\n"
    "📋 *Commands*\n/start — subscribe (or resubscribe)\n/stop — pause briefings\n/help — show this message\n/refresh — fetch the latest news\n/fetch — same as /refresh"
    "\n\nYou can also say things like 'latest news', 'send the latest', 'more', 'again', or 'go back'."
    "\n\nAfter a digest arrives, reply with a story number (e.g. \"3\") or \"more on 3\" to get that story's full brief."
)

DETAIL_INTENT_MARKERS = (
    "detail",
    "details",
    "more on",
    "more about",
    "tell me more",
    "expand",
    "full brief",
    "full story",
    "elaborate",
    "item",
    "number",
    "story",
)


def extract_detail_index(text: str) -> Optional[int]:
    lower = text.strip().lower()
    if not lower:
        return None

    if lower.isdigit():
        value = int(lower)
        return value if 1 <= value <= 10 else None

    if any(marker in lower for marker in DETAIL_INTENT_MARKERS):
        match = re.search(r"\d{1,2}", lower)
        if match:
            value = int(match.group())
            return value if 1 <= value <= 10 else None

    return None

REFRESH_MARKERS = (
    "refresh",
    "latest news",
    "latest",
    "lastest",
    "news update",
    "current news",
    "today's news",
    "today news",
    "what's new",
    "what is new",
    "send news",
    "send the latest",
    "show news",
    "give me news",
    "briefing",
    "headlines",
    "latest briefing",
)

REPEAT_MARKERS = (
    "go back",
    "back",
    "previous",
    "repeat that",
    "show that again",
    "same again",
    "the last one",
    "last one",
    "repeat",
)

FOLLOW_UP_REFRESH_MARKERS = (
    "more",
    "another",
    "another one",
    "next",
    "send more",
    "more news",
    "more updates",
    "more please",
    "latest",
)


def build_command_keyboard() -> Dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "/refresh"}, {"text": "/fetch"}],
            [{"text": "/help"}, {"text": "/start"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "selective": True,
    }


def ensure_db() -> sqlite3.Connection:
    conn = sqlite3.connect(SUBSCRIBER_DB, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id TEXT PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            status TEXT,
            subscribed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_memory (
            chat_id TEXT PRIMARY KEY,
            last_user_text TEXT,
            last_intent TEXT,
            last_news_messages TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_items (
            link TEXT PRIMARY KEY,
            seen_at TEXT
        )
        """
    )
    conn.commit()
    return conn


DB_CONN = ensure_db()


def upsert_subscriber(chat_id: str, first_name: str, username: str, status: str) -> None:
    subscribed_at = datetime.datetime.utcnow().isoformat() + "Z"
    DB_CONN.execute(
        """
        INSERT INTO subscribers (chat_id, first_name, username, status, subscribed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            first_name=excluded.first_name,
            username=excluded.username,
            status=excluded.status,
            subscribed_at=excluded.subscribed_at
        """,
        (chat_id, first_name, username, status, subscribed_at),
    )
    DB_CONN.commit()
    logging.info("Saved subscriber %s status=%s", chat_id, status)


def get_active_subscribers() -> List[Dict[str, str]]:
    cursor = DB_CONN.cursor()
    cursor.execute(
        "SELECT chat_id, first_name, username, subscribed_at FROM subscribers WHERE status = 'active'"
    )
    return [
        {
            "chat_id": row[0],
            "first_name": row[1] or "",
            "username": row[2] or "",
            "subscribed_at": row[3] or "",
        }
        for row in cursor.fetchall()
    ]


def is_item_seen(link: str) -> bool:
    if not link:
        return False
    cursor = DB_CONN.cursor()
    cursor.execute("SELECT 1 FROM seen_items WHERE link = ?", (link,))
    return cursor.fetchone() is not None


def mark_items_seen(links: List[str]) -> None:
    seen_at = datetime.datetime.utcnow().isoformat() + "Z"
    for link in links:
        if not link:
            continue
        DB_CONN.execute(
            "INSERT OR IGNORE INTO seen_items (link, seen_at) VALUES (?, ?)",
            (link, seen_at),
        )
    DB_CONN.commit()


def get_conversation_memory(chat_id: str) -> Dict[str, Any]:
    cursor = DB_CONN.cursor()
    cursor.execute(
        "SELECT last_user_text, last_intent, last_news_messages, updated_at FROM conversation_memory WHERE chat_id = ?",
        (chat_id,),
    )
    row = cursor.fetchone()
    if not row:
        return {
            "last_user_text": "",
            "last_intent": "",
            "last_news_messages": [],
            "updated_at": "",
        }

    last_news_messages: List[str] = []
    raw_news_messages = row[2] or ""
    if raw_news_messages:
        try:
            parsed_messages = json.loads(raw_news_messages)
            if isinstance(parsed_messages, list):
                last_news_messages = [str(message) for message in parsed_messages if str(message).strip()]
        except json.JSONDecodeError:
            last_news_messages = []

    return {
        "last_user_text": row[0] or "",
        "last_intent": row[1] or "",
        "last_news_messages": last_news_messages,
        "updated_at": row[3] or "",
    }


def save_conversation_memory(
    chat_id: str,
    last_user_text: str,
    last_intent: str,
    last_news_messages: Optional[List[str]] = None,
) -> None:
    updated_at = datetime.datetime.utcnow().isoformat() + "Z"
    payload = json.dumps(last_news_messages or [])
    DB_CONN.execute(
        """
        INSERT INTO conversation_memory (chat_id, last_user_text, last_intent, last_news_messages, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            last_user_text=excluded.last_user_text,
            last_intent=excluded.last_intent,
            last_news_messages=excluded.last_news_messages,
            updated_at=excluded.updated_at
        """,
        (chat_id, last_user_text, last_intent, payload, updated_at),
    )
    DB_CONN.commit()


def _post_telegram_message(payload: Dict[str, Any]) -> None:
    url = urljoin(TELEGRAM_API_BASE, "sendMessage")
    response = SESSION.post(url, json=payload, timeout=15)
    if response.status_code != 200:
        logging.error("Telegram sendMessage failed %s: %s", response.status_code, response.text)
        if response.status_code == 400 and "can't parse entities" in response.text and payload.get("parse_mode"):
            fallback_payload = dict(payload)
            fallback_payload.pop("parse_mode")
            _post_telegram_message(fallback_payload)
            return
        response.raise_for_status()


def send_telegram_message(chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    _post_telegram_message(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
        }
    )


def send_telegram_message_with_keyboard(chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    _post_telegram_message(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False,
            "reply_markup": build_command_keyboard(),
        }
    )


def send_chat_action(chat_id: str, action: str = "typing") -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    url = urljoin(TELEGRAM_API_BASE, "sendChatAction")
    try:
        SESSION.post(url, json={"chat_id": chat_id, "action": action}, timeout=10)
    except Exception:
        logging.exception("Failed to send chat action to %s", chat_id)


class TypingIndicator:
    """Keeps Telegram's "typing..." indicator alive for the duration of a `with` block.

    Telegram clears the indicator after ~5s, so it needs to be re-sent
    periodically for operations that take longer (RSS/Groq fetches).
    """

    def __init__(self, chat_id: str, interval: float = 4.0) -> None:
        self.chat_id = chat_id
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            send_chat_action(self.chat_id, "typing")
            self._stop_event.wait(self.interval)

    def __enter__(self) -> "TypingIndicator":
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._stop_event.set()
        self._thread.join(timeout=1)


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def classify_message_intent(message: Dict[str, Any], memory: Dict[str, Any]) -> str:
    text = str(message.get("text", "")).strip()
    lower = normalize_text(text)

    if not lower:
        return "help"

    if lower.startswith("/start") or any(
        phrase in lower
        for phrase in (
            "subscribe",
            "sign me up",
            "join",
            "start updates",
        )
    ):
        return "start"

    if lower.startswith("/stop") or any(
        phrase in lower
        for phrase in (
            "unsubscribe",
            "stop updates",
            "pause",
            "cancel",
            "unsubscribe me",
        )
    ):
        return "stop"

    if lower.startswith("/help"):
        return "help"

    if len(memory.get("last_news_messages", [])) > 1 and extract_detail_index(text) is not None:
        return "detail"

    if lower.startswith("/refresh") or lower.startswith("/fetch"):
        return "refresh"

    if any(phrase in lower for phrase in REPEAT_MARKERS):
        if memory.get("last_news_messages"):
            return "repeat_last"
        return "refresh"

    if any(phrase in lower for phrase in REFRESH_MARKERS):
        return "refresh"

    if any(phrase in lower for phrase in FOLLOW_UP_REFRESH_MARKERS):
        if memory.get("last_intent") in {"refresh", "repeat_last"}:
            return "refresh"
        return "refresh"

    if "news" in lower or "briefing" in lower:
        return "refresh"

    return "help"


def send_latest_news(chat_id: str, user_text: str = "") -> List[str]:
    logging.info("Sending latest news refresh to %s", chat_id)
    with TypingIndicator(chat_id):
        briefing = generate_briefing()
    digest = briefing["digest"]
    if not digest:
        send_telegram_message(chat_id, "No fresh news items were found right now. Please try again later.")
        save_conversation_memory(chat_id, user_text, "refresh", [])
        return []

    try:
        send_telegram_message(chat_id, digest)
    except Exception:
        logging.exception("Failed to send digest message to %s", chat_id)

    combined = [digest] + briefing["details"]
    save_conversation_memory(chat_id, user_text, "digest", combined)
    return combined


def repeat_last_news(chat_id: str, user_text: str, memory: Dict[str, Any]) -> None:
    last_messages = [str(message) for message in memory.get("last_news_messages", []) if str(message).strip()]
    if not last_messages:
        send_latest_news(chat_id, user_text=user_text)
        return

    logging.info("Repeating last digest for %s", chat_id)
    try:
        send_telegram_message(chat_id, last_messages[0])
    except Exception:
        logging.exception("Failed to resend digest to %s", chat_id)

    save_conversation_memory(chat_id, user_text, "digest", last_messages)


def send_story_detail(chat_id: str, index: Optional[int], memory: Dict[str, Any]) -> None:
    messages = memory.get("last_news_messages", [])
    if not index or index >= len(messages):
        send_telegram_message(chat_id, "I couldn't find that story anymore — try /refresh for the latest digest.")
        return

    logging.info("Sending story detail #%d to %s", index, chat_id)
    try:
        send_telegram_message(chat_id, messages[index])
    except Exception:
        logging.exception("Failed to send story detail to %s", chat_id)


def handle_telegram_update(update: Dict[str, Any]) -> Dict[str, Any]:
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return {"status": "ignored"}

    chat = message.get("chat", {})
    sender = message.get("from", {})
    chat_id = str(chat.get("id") or sender.get("id") or "")
    if not chat_id:
        return {"status": "invalid"}

    memory = get_conversation_memory(chat_id)
    command = classify_message_intent(message, memory)
    user_text = str(message.get("text", "")).strip()
    first_name = str(sender.get("first_name") or chat.get("first_name") or "")
    username = str(sender.get("username") or chat.get("username") or "")

    if command == "start":
        upsert_subscriber(chat_id, first_name, username, "active")
        save_conversation_memory(chat_id, user_text, "start", memory.get("last_news_messages", []))
        send_telegram_message_with_keyboard(chat_id, TELEGRAM_SUBSCRIBE_MESSAGE)
        return {"status": "subscribed"}

    if command == "stop":
        upsert_subscriber(chat_id, first_name, username, "unsubscribed")
        save_conversation_memory(chat_id, user_text, "stop", memory.get("last_news_messages", []))
        send_telegram_message_with_keyboard(chat_id, TELEGRAM_UNSUBSCRIBE_MESSAGE)
        return {"status": "unsubscribed"}

    if command == "help":
        save_conversation_memory(chat_id, user_text, "help", memory.get("last_news_messages", []))
        send_telegram_message_with_keyboard(chat_id, TELEGRAM_HELP_MESSAGE)
        return {"status": "help_sent"}

    if command == "refresh":
        send_latest_news(chat_id, user_text=user_text)
        return {"status": "refreshed"}

    if command == "repeat_last":
        repeat_last_news(chat_id, user_text, memory)
        return {"status": "repeated"}

    if command == "detail":
        index = extract_detail_index(user_text)
        send_story_detail(chat_id, index, memory)
        save_conversation_memory(chat_id, user_text, "detail", memory.get("last_news_messages", []))
        return {"status": "detail_sent"}

    save_conversation_memory(chat_id, user_text, "help", memory.get("last_news_messages", []))
    send_telegram_message_with_keyboard(chat_id, TELEGRAM_HELP_MESSAGE)
    return {"status": "help_sent"}


def get_telegram_updates(offset: Optional[int] = None, timeout: int = 60) -> Dict[str, Any]:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = urljoin(TELEGRAM_API_BASE, "getUpdates")
    response = SESSION.get(url, params=params, timeout=timeout + 10)
    response.raise_for_status()
    return response.json()


def fetch_rss_items(url: str) -> List[Dict[str, Any]]:
    logging.info("Fetching RSS feed %s", url)
    feed = feedparser.parse(url)
    items: List[Dict[str, Any]] = []

    for entry in feed.entries:
        published_at = None
        if getattr(entry, "published_parsed", None):
            published_at = datetime.datetime.fromtimestamp(time.mktime(entry.published_parsed), datetime.timezone.utc)
        elif entry.get("published"):
            try:
                published_at = parsedate_to_datetime(entry.get("published"))
            except Exception:
                published_at = None

        items.append(
            {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "content": entry.get("summary", "") or entry.get("description", ""),
                "creator": entry.get("author", "") or entry.get("source", ""),
                "published_at": published_at.isoformat() if published_at else "",
            }
        )

    logging.info("Found %d RSS items from %s", len(items), url)
    return items


def fetch_reddit_posts(url: str) -> List[Dict[str, Any]]:
    logging.info("Fetching Reddit feed %s", url)
    headers = {"User-Agent": "n8n:nigeria.content.briefing:v1.0"}
    response = SESSION.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    data = response.json()

    children = data.get("data", {}).get("children", [])
    items = [child.get("data", {}) for child in children if isinstance(child, dict) and child.get("data")]
    logging.info("Extracted %d Reddit posts from %s", len(items), url)
    return items


def fetch_google_trends(url: str) -> List[Dict[str, Any]]:
    logging.info("Fetching Google Trends feed %s", url)
    response = SESSION.get(url, timeout=20)
    response.raise_for_status()
    feed = feedparser.parse(response.content)

    items: List[Dict[str, Any]] = []
    for entry in feed.entries:
        trend_title = str(entry.get("title", "")).strip()
        if not trend_title:
            continue

        news_title = str(entry.get("ht_news_item_title", "")).strip()
        news_url = str(entry.get("ht_news_item_url", "")).strip()
        traffic_match = re.search(r"(\d+)", str(entry.get("ht_approx_traffic", "")))
        traffic = int(traffic_match.group(1)) if traffic_match else 0

        items.append(
            {
                "title": f"Trending in Nigeria: {trend_title}",
                "content": news_title or trend_title,
                "link": news_url or entry.get("link", ""),
                "source": "Google Trends (NG)",
                "published": entry.get("published", ""),
                "trend_traffic": traffic,
            }
        )

    logging.info("Found %d Google Trends items from %s", len(items), url)
    return items


def fetch_x_posts_nitter() -> List[Dict[str, Any]]:
    logging.info("Fetching X/Twitter posts via Nitter RSS")
    last_error: Optional[Exception] = None

    for instance in NITTER_INSTANCES:
        url = f"{instance}/search/rss"
        try:
            response = SESSION.get(url, params={"f": "tweets", "q": X_SEARCH_QUERY}, timeout=20)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if not feed.entries:
                logging.info("Nitter instance %s returned no entries", instance)
                continue

            items = []
            for entry in feed.entries:
                link = entry.get("link", "")
                handle_match = re.search(r"/([^/]+)/status/", link)
                handle = handle_match.group(1) if handle_match else ""
                items.append(
                    {
                        "full_text": entry.get("title", "") or entry.get("summary", ""),
                        "url": link,
                        "author": {"userName": handle},
                        "createdAt": entry.get("published", ""),
                        "likeCount": 0,
                        "retweetCount": 0,
                    }
                )

            logging.info("Fetched %d tweets from Nitter instance %s", len(items), instance)
            return items
        except Exception as exc:
            last_error = exc
            logging.warning("Nitter instance %s failed: %s", instance, exc)
            continue

    logging.error("All Nitter instances failed for X/Twitter fetch: %s", last_error)
    return []


def fetch_x_search_posts(query: str) -> List[Dict[str, Any]]:
    logging.info("Searching X/Twitter posts via DuckDuckGo: %s", query)
    response = SESSION.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=20,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    items: List[Dict[str, Any]] = []
    for result in soup.select(".result"):
        if "result--ad" in (result.get("class") or []):
            continue

        link_tag = result.select_one(".result__a")
        if not link_tag or not link_tag.get("href"):
            continue

        link = link_tag["href"]
        if "/status/" not in link or ("twitter.com" not in link and "x.com" not in link):
            continue

        title = link_tag.get_text(strip=True)
        snippet_tag = result.select_one(".result__snippet")
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""

        handle_match = re.search(r"(?:twitter|x)\.com/([^/]+)/status/", link)
        handle = handle_match.group(1) if handle_match else ""

        likes_match = re.search(r"([\d,]+)\s*likes?", snippet, re.IGNORECASE)
        likes = int(likes_match.group(1).replace(",", "")) if likes_match else 0
        replies_match = re.search(r"([\d,]+)\s*repl", snippet, re.IGNORECASE)
        replies = int(replies_match.group(1).replace(",", "")) if replies_match else 0

        items.append(
            {
                "full_text": snippet or title,
                "url": link,
                "author": {"userName": handle},
                "likeCount": likes,
                "retweetCount": replies,
            }
        )

    logging.info("Found %d X/Twitter posts via DuckDuckGo search", len(items))
    return items


def parse_datetime(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    if isinstance(value, str) and value:
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            return parsedate_to_datetime(value).astimezone(datetime.timezone.utc)
        except Exception:
            pass
    return datetime.datetime.fromtimestamp(0, datetime.timezone.utc)


def normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    link_str = str(item.get("url") or item.get("link") or "")
    is_reddit = item.get("subreddit") is not None or item.get("permalink") is not None
    is_tweet = item.get("full_text") is not None or "x.com" in link_str or "twitter.com" in link_str

    if is_reddit:
        subreddit = item.get("subreddit") or "reddit"
        source = f"Reddit (r/{subreddit})"
        link = f"https://reddit.com{item.get('permalink')}" if item.get("permalink") else link_str
    elif is_tweet:
        author = item.get("author") or {}
        handle = ""
        if isinstance(author, dict):
            handle = author.get("userName") or author.get("username") or ""
        source = f"X / Twitter {('@' + handle) if handle else ''}".strip()
        link = link_str or item.get("url") or item.get("link") or ""
    else:
        source = str(item.get("creator") or item.get("source") or "News RSS")
        link = link_str or item.get("link") or ""

    content = (
        str(item.get("selftext", ""))
        or str(item.get("contentSnippet", ""))
        or str(item.get("content", ""))
        or str(item.get("full_text", ""))
        or str(item.get("text", ""))
        or str(item.get("title", ""))
        or "No detailed text"
    )

    if is_reddit:
        ups = int(item.get("ups") or 0)
        comments = int(item.get("num_comments") or 0)
        engagement = f"Upvotes: {ups} | Comments: {comments}"
        engagement_score = ups + comments * 2
    elif is_tweet:
        likes = int(item.get("likeCount") or 0)
        retweets = int(item.get("retweetCount") or 0)
        engagement = f"Likes: {likes} | Retweets: {retweets}"
        engagement_score = likes + retweets * 2
    elif item.get("trend_traffic") is not None:
        traffic = int(item.get("trend_traffic") or 0)
        engagement = f"Search interest: {traffic}+"
        engagement_score = traffic
    else:
        engagement = "N/A"
        engagement_score = 0

    title = str(item.get("title") or item.get("text") or item.get("full_text") or "Nigerian Hot Topic")
    published_at = parse_datetime(
        item.get("published_at")
        or item.get("created_at")
        or item.get("createdAt")
        or item.get("date")
        or item.get("published")
    )

    return {
        "title": title,
        "content": content,
        "link": link,
        "source": source,
        "engagement": engagement,
        "engagement_score": engagement_score,
        "published_at": published_at.isoformat(),
        "published_at_ts": int(published_at.timestamp()),
    }


def remove_duplicates(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique = []
    for item in items:
        link = str(item.get("link", "")).strip()
        if not link or link in seen:
            continue
        seen.add(link)
        unique.append(item)
    return unique


def sort_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            item.get("engagement_score", 0),
            item.get("published_at_ts", 0),
        ),
        reverse=True,
    )


def limit_top_items(items: List[Dict[str, Any]], max_items: int = 10) -> List[Dict[str, Any]]:
    return items[:max_items]


def evaluate_item_with_groq(item: Dict[str, Any]) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")

    payload = {
        "model": "llama-3.3-70b-versatile",
        "temperature": 0.7,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an elite content research strategist for a digital content creator. "
                    "Evaluate the item for video reaction potential. Rate viral score (1-10), summarize the main points, highlight community reaction/sentiment, and provide 2 sharp video reaction hooks."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Source: {item.get('source')}\n"
                    f"Engagement: {item.get('engagement')}\n"
                    f"Title: {item.get('title')}\n"
                    f"Content: {item.get('content')}\n"
                    f"Link: {item.get('link')}"
                ),
            },
        ],
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    response = SESSION.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
    try:
        response.raise_for_status()
    except Exception as exc:
        logging.error("Groq request failed: %s %s", exc, response.text)
        return "No brief generated."

    body = response.json()
    choices = body.get("choices") or []
    if choices and isinstance(choices, list):
        first_choice = choices[0]
        message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
        return str(message.get("content", "No brief generated."))
    return "No brief generated."


def build_briefing_message(item: Dict[str, Any], ai_brief: str) -> str:
    return (
        "🔥 *NIGERIAN CONTENT BRIEFING*\n\n"
        f"📌 *Source:* {item.get('source', 'Unknown')}\n"
        f"📊 *Engagement:* {item.get('engagement', 'N/A')}\n"
        f"🔗 *Link:* {item.get('link', '')}\n\n"
        "🧠 *AI Strategy Brief:*\n"
        f"{ai_brief}"
    )


def extract_viral_score(ai_brief: str) -> str:
    match = re.search(r"viral score\D{0,10}(\d{1,2})\s*/\s*10", ai_brief, re.IGNORECASE)
    if match:
        return f"{match.group(1)}/10"
    return "N/A"


def build_digest_message(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return ""

    lines = ["🔥 *NIGERIAN CONTENT DIGEST*", ""]
    for idx, entry in enumerate(entries, start=1):
        item = entry["item"]
        lines.append(f"{idx}. *{item.get('title', 'Untitled')}* — 🔥 {entry['score']}")
        lines.append(item.get("link", ""))
        lines.append("")

    lines.append('Reply with a story number (e.g. "3") or "more on 3" for the full brief on any story.')
    return "\n".join(lines).strip()


def broadcast_news() -> None:
    logging.info("Starting broadcast pipeline")
    briefing = generate_briefing()
    digest = briefing["digest"]

    if not digest:
        logging.info("No briefing content generated; skipping broadcast")
        return

    combined = [digest] + briefing["details"]
    subscribers = get_active_subscribers()
    logging.info("Broadcasting digest to %d active subscribers", len(subscribers))

    for subscriber in subscribers:
        chat_id = subscriber["chat_id"]
        try:
            send_telegram_message(chat_id, digest)
            save_conversation_memory(chat_id, "", "digest", combined)
            time.sleep(1)
        except Exception:
            logging.exception("Failed to send Telegram message to %s", chat_id)

    logging.info("Broadcast pipeline finished")


def generate_briefing() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []

    for feed_url in NEWS_RSS_FEEDS:
        try:
            items.extend(fetch_rss_items(feed_url))
        except Exception:
            logging.exception("Failed to fetch RSS feed %s", feed_url)

    for reddit_url in REDDIT_FEEDS:
        try:
            items.extend(fetch_reddit_posts(reddit_url))
        except Exception:
            logging.exception("Failed to fetch Reddit feed %s", reddit_url)

    try:
        items.extend(fetch_x_posts_nitter())
    except Exception:
        logging.exception("Failed to fetch X/Twitter posts via Nitter")

    try:
        items.extend(fetch_x_search_posts(X_DDG_SEARCH_QUERY))
    except Exception:
        logging.exception("Failed to fetch X/Twitter posts via DuckDuckGo search")

    try:
        items.extend(fetch_google_trends(GOOGLE_TRENDS_NG_URL))
    except Exception:
        logging.exception("Failed to fetch Google Trends feed")

    normalized = [normalize_item(item) for item in items]
    unique_items = remove_duplicates(normalized)
    unseen_items = [item for item in unique_items if not is_item_seen(item.get("link", ""))]
    sorted_items = sort_items(unseen_items)
    top_items = limit_top_items(sorted_items, max_items=10)

    details = []
    digest_entries = []
    for item in top_items:
        try:
            ai_brief = evaluate_item_with_groq(item)
        except Exception:
            logging.exception("Failed to evaluate item with Groq")
            ai_brief = "No brief generated."
        details.append(build_briefing_message(item, ai_brief))
        digest_entries.append({"item": item, "score": extract_viral_score(ai_brief)})

    mark_items_seen([item.get("link", "") for item in top_items])
    return {"digest": build_digest_message(digest_entries), "details": details}


@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook() -> Any:
    update = request.get_json(force=True)
    result = handle_telegram_update(update)
    return jsonify(result)


def poll_telegram_updates() -> None:
    logging.info("Starting Telegram polling loop")
    offset: Optional[int] = None
    while True:
        try:
            body = get_telegram_updates(offset=offset)
            if not body.get("ok"):
                logging.warning("Telegram getUpdates returned not ok: %s", body)
                time.sleep(5)
                continue

            for update in body.get("result", []):
                offset = update.get("update_id", 0) + 1
                try:
                    handle_telegram_update(update)
                except Exception:
                    logging.exception("Error handling Telegram update %s", update)
        except Exception:
            logging.exception("Telegram polling failed")
            time.sleep(5)


def set_telegram_webhook(webhook_url: str) -> Dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    url = urljoin(TELEGRAM_API_BASE, "setWebhook")
    response = SESSION.get(url, params={"url": webhook_url}, timeout=15)
    response.raise_for_status()
    return response.json()


def run_scheduler() -> None:
    logging.info("Running scheduler: broadcast every 4 hours")
    while True:
        try:
            broadcast_news()
        except Exception:
            logging.exception("Scheduled broadcast failed")
        time.sleep(4 * 3600)


def main() -> None:
    parser = argparse.ArgumentParser(description="Nigerian Briefing Python workflow")
    parser.add_argument(
        "command",
        choices=["runserver", "poll", "broadcast", "scheduler", "set-webhook"],
        help="Command to run",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Flask host for runserver")
    parser.add_argument("--port", type=int, default=5000, help="Flask port for runserver")
    parser.add_argument(
        "--webhook-url",
        help="Full webhook URL for Telegram setWebhook",
    )
    args = parser.parse_args()

    if args.command == "runserver":
        app.run(host=args.host, port=args.port)
    elif args.command == "poll":
        poll_telegram_updates()
    elif args.command == "broadcast":
        broadcast_news()
    elif args.command == "scheduler":
        run_scheduler()
    elif args.command == "set-webhook":
        if not args.webhook_url:
            parser.error("--webhook-url is required for set-webhook")
        result = set_telegram_webhook(args.webhook_url)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
