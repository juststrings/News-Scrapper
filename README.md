# Nigerian Briefing Workflow

This repository contains a Python implementation of the `Nigerian Briefing` workflow originally built in n8n.
It includes two main parts:

- Telegram subscriber bot: handles `/start`, `/stop`, and `/help` commands.
- News broadcaster: fetches news from RSS feeds, Reddit, and X/Twitter, evaluates items with Groq AI, and broadcasts briefings to active Telegram subscribers.

## Files

- `news_briefing_workflow.py`: main Python workflow.
- `requirements.txt`: Python dependencies.
- `.env.example`: example environment variable file.

## Setup

1. Clone or open this folder in your terminal.
2. Create a Python virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
```

3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and fill in the values:

```powershell
copy .env.example .env
```

5. Set your environment variables:

```powershell
$env:TELEGRAM_BOT_TOKEN = "your_telegram_bot_token"
$env:APIFY_BEARER_TOKEN = "your_apify_bearer_token"
$env:GROQ_API_KEY = "your_groq_api_key"
```

Alternatively, install `python-dotenv` and load `.env` manually in your shell.

## Usage

### Run the Telegram webhook server

```powershell
python news_briefing_workflow.py runserver --host 0.0.0.0 --port 5000
```

Then configure Telegram webhook:

```powershell
python news_briefing_workflow.py set-webhook --webhook-url https://your-public-host/telegram_webhook
```

### Poll Telegram updates instead of using webhook

```powershell
python news_briefing_workflow.py poll
```

### Run a one-time broadcast immediately

```powershell
python news_briefing_workflow.py broadcast
```

### Run scheduled broadcast every 4 hours

```powershell
python news_briefing_workflow.py scheduler
```

## Notes

- Subscribers are stored in `subscribers.db` by default.
- The workflow fetches content from these sources:
  - Punch news RSS
  - Premium Times RSS
  - Vanguard RSS
  - Daily Post RSS
  - Reddit r/Nigeria and r/NigeriaTech
  - X/Twitter via Apify scraper
- Briefs are generated using Groq AI and sent as Markdown-formatted Telegram messages.

## Customization

- Update `NEWS_RSS_FEEDS` and `REDDIT_FEEDS` directly in `news_briefing_workflow.py`.
- Modify `TELEGRAM_SUBSCRIBE_MESSAGE`, `TELEGRAM_UNSUBSCRIBE_MESSAGE`, and `TELEGRAM_HELP_MESSAGE` to change bot responses.
- Adjust `APIFY_X_SCRAPER_URL` or `evaluate_item_with_groq()` if you switch providers.
