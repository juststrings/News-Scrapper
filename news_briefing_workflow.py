import argparse
import datetime
import json
import logging
import os
import sqlite3
import time
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import feedparser
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
APIFY_BEARER_TOKEN = os.getenv("APIFY_BEARER_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
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

APIFY_X_SCRAPER_URL = (
    "https://api.apify.com/v2/acts/apidojo~twitter-scraper-lite/run-sync-get-dataset-items"
)

TELEGRAM_SUBSCRIBE_MESSAGE = (
    "👋 *Welcome to the Nigerian Content Briefing!*\n\n"
    "You're now subscribed. Every few hours I scan Nigerian news (Punch, Premium Times, Vanguard, Daily Post), Reddit (r/Nigeria, r/NigeriaTech) and X/Twitter, then send you AI-scored *content briefings* — each with a viral score, a summary, community sentiment, and 2 ready-to-use video reaction hooks.\n\n"
    "📋 *Commands*\n/start — subscribe (or resubscribe)\n/stop — pause briefings\n/help — show this message again\n\n"
    "Sit tight — your first briefing will arrive on the next scheduled run. 🔥"
)

TELEGRAM_UNSUBSCRIBE_MESSAGE = (
    "🛑 You have been unsubscribed from the Nigerian Content Briefing. Send /start anytime to rejoin."
)

TELEGRAM_HELP_MESSAGE = (
    "ℹ️ *Nigerian Content Briefing — Help*\n\n"
    "I deliver AI-scored content briefings from Nigerian news, Reddit and X/Twitter, built for content creators looking for reaction-video ideas.\n\n"
    "📋 *Commands*\n/start — subscribe (or resubscribe)\n/stop — pause briefings\n/help — show this message"
)


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


def send_telegram_message(chat_id: str, text: str, parse_mode: str = "Markdown") -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    url = urljoin(TELEGRAM_API_BASE, "sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    response = SESSION.post(url, json=payload, timeout=15)
    if response.status_code != 200:
        logging.error("Telegram sendMessage failed %s: %s", response.status_code, response.text)
        response.raise_for_status()


def classify_message_intent(message: Dict[str, Any]) -> str:
    text = str(message.get("text", "")).strip()
    lower = text.lower()

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

    if any(
        phrase in lower
        for phrase in (
            "refresh",
            "latest news",
            "news update",
            "current news",
            "today's news",
            "today news",
            "what's new",
            "what is new",
            "send news",
            "show news",
            "give me news",
            "briefing",
            "headlines",
            "latest briefing",
        )
    ):
        return "refresh"

    return "help"


def build_news_refresh_message(messages: List[str]) -> str:
    if not messages:
        return "No fresh news items were found right now. Please try again later."

    return "\n\n".join(messages)


def send_latest_news(chat_id: str) -> None:
    logging.info("Sending latest news refresh to %s", chat_id)
    messages = generate_briefing_messages()
    send_telegram_message(chat_id, build_news_refresh_message(messages))


def handle_telegram_update(update: Dict[str, Any]) -> Dict[str, Any]:
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return {"status": "ignored"}

    chat = message.get("chat", {})
    sender = message.get("from", {})
    chat_id = str(chat.get("id") or sender.get("id") or "")
    if not chat_id:
        return {"status": "invalid"}

    command = classify_message_intent(message)
    first_name = str(sender.get("first_name") or chat.get("first_name") or "")
    username = str(sender.get("username") or chat.get("username") or "")

    if command == "start":
        upsert_subscriber(chat_id, first_name, username, "active")
        send_telegram_message(chat_id, TELEGRAM_SUBSCRIBE_MESSAGE)
        return {"status": "subscribed"}

    if command == "stop":
        upsert_subscriber(chat_id, first_name, username, "unsubscribed")
        send_telegram_message(chat_id, TELEGRAM_UNSUBSCRIBE_MESSAGE)
        return {"status": "unsubscribed"}

    if command == "help":
        send_telegram_message(chat_id, TELEGRAM_HELP_MESSAGE)
        return {"status": "help_sent"}

    if command == "refresh":
        send_latest_news(chat_id)
        return {"status": "refreshed"}

    send_telegram_message(chat_id, TELEGRAM_HELP_MESSAGE)
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


def fetch_x_posts() -> List[Dict[str, Any]]:
    logging.info("Fetching X/Twitter posts from Apify")
    if not APIFY_BEARER_TOKEN:
        raise RuntimeError("APIFY_BEARER_TOKEN is not set")

    headers = {
        "Authorization": f"Bearer {APIFY_BEARER_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "searchTerms": ["\"Nigeria\" min_faves:50 -filter:replies"],
        "sort": "Latest",
        "maxItems": 10,
    }
    response = SESSION.post(APIFY_X_SCRAPER_URL, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    data = response.json()

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data.get("items"):
        return data.get("items", [])
    return []


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


def broadcast_news() -> None:
    logging.info("Starting broadcast pipeline")
    messages = generate_briefing_messages()

    subscribers = get_active_subscribers()
    logging.info("Broadcasting %d messages to %d active subscribers", len(messages), len(subscribers))

    for subscriber in subscribers:
        chat_id = subscriber["chat_id"]
        for text in messages:
            try:
                send_telegram_message(chat_id, text)
                time.sleep(1)
            except Exception:
                logging.exception("Failed to send Telegram message to %s", chat_id)

    logging.info("Broadcast pipeline finished")


def generate_briefing_messages() -> List[str]:
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
        items.extend(fetch_x_posts())
    except Exception:
        logging.exception("Failed to fetch X/Twitter posts")

    normalized = [normalize_item(item) for item in items]
    unique_items = remove_duplicates(normalized)
    sorted_items = sort_items(unique_items)
    top_items = limit_top_items(sorted_items, max_items=10)

    messages = []
    for item in top_items:
        try:
            ai_brief = evaluate_item_with_groq(item)
        except Exception:
            logging.exception("Failed to evaluate item with Groq")
            ai_brief = "No brief generated."
        messages.append(build_briefing_message(item, ai_brief))

    return messages


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
