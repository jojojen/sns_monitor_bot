# SNS Monitor Bot for X (Twitter)

A Python bot for monitoring X (Twitter) posts on specific accounts, keywords, and trending topics. Built for **aka no claw**.

## Features

- **Account monitoring**: Track posts from specific X accounts
- **Keyword search**: Monitor X for specific keywords or topics
- **Trend tracking**: Watch trending topics and get notified when new trends appear
- **Human-like behavior**: Random delays between requests to avoid rate limiting
- **Telegram notifications**: Get alerts via Telegram Bot API
- **Persistent storage**: SQLite database tracks watched rules and seen tweets

## Requirements

- Python 3.10+ (twikit uses Python 3.10+ syntax)
- X (Twitter) account credentials (email + password)
- (Optional) Telegram Bot token for notifications

## Installation

1. Create a Python virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

2. Install dependencies:
```bash
pip install twikit python-dotenv pytest pytest-asyncio
```

3. Or install the package in development mode:
```bash
pip install -e ".[dev]"
```

## Configuration

Create or update `.env` file with your credentials:

```env
X_USERNAME=your_screen_name
X_USER_MAIL=your_email@example.com
X_USER_PASSWORD=your_password
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## Usage

### Command-line Interface

#### Add Account Watch
Monitor a specific X account:
```bash
python -m sns_monitor add-account @username --chat-id 123 --label "My Label" --interval 15
```

#### Add Keyword Watch
Monitor posts containing specific keywords:
```bash
python -m sns_monitor add-keyword "search query" --chat-id 123 --label "My Search" --interval 30
```

#### Add Trend Watch
Monitor trending topics in a category:
```bash
python -m sns_monitor add-trend trending --chat-id 123 --interval 60
```

Trend categories: `trending`, `for-you`, `news`, `sports`, `entertainment`

#### List All Watches
```bash
python -m sns_monitor list
python -m sns_monitor list --kind account  # Filter by type
```

#### Toggle Watch On/Off
```bash
python -m sns_monitor toggle RULE_ID --enabled
python -m sns_monitor toggle RULE_ID --disabled
```

#### Delete Watch
```bash
python -m sns_monitor delete RULE_ID
```

#### Run Monitor Daemon
```bash
python -m sns_monitor run --db data/sns.sqlite3 --interval 60
```

The daemon will continuously monitor all enabled rules and send notifications via Telegram.

### Data Storage

Watch rules and tweet history are stored in SQLite database (default: `data/sns.sqlite3`).

Tables:
- `watch_rules` - Monitored rules (accounts, keywords, trends)
- `seen_tweets` - Tweet history to avoid duplicate notifications
- `trend_snapshots` - Trend history for trend watches

## Architecture

```
sns_monitor/
├── models.py       - Data models (Tweet, AccountWatch, KeywordWatch, TrendWatch)
├── storage.py      - SQLite database operations
├── x_client.py     - Async twikit wrapper with circuit breaker
├── monitor.py      - Background monitoring daemon
├── formatters.py   - Notification message formatting (Traditional Chinese)
├── telegram.py     - Telegram Bot API client
└── __main__.py     - CLI entry point
```

### Key Design Patterns

1. **Async/Sync Boundary**: `monitor.py` runs asyncio loop in a daemon thread, providing sync `start()`/`stop()` API
2. **Circuit Breaker**: `x_client.py` implements exponential backoff on rate limits (15min) and auth errors (10min)
3. **Human-like Behavior**: Random 1.5-4 second delays between API requests
4. **First-scan Baseline**: On first check of a watch rule, all found items are marked notified (prevents flooding on startup)
5. **Deduplication**: SQLite `(tweet_id, rule_id)` unique key prevents processing same tweet twice for same rule

## Testing

Run unit tests:
```bash
pytest tests/
```

Test basic functionality:
```bash
# Add a test watch
python -m sns_monitor add-account elonmusk --chat-id 0 --db data/test.sqlite3

# List watches
python -m sns_monitor list --db data/test.sqlite3

# Run monitor (will attempt X login - requires working credentials)
python -m sns_monitor run --db data/test.sqlite3
```

## Architecture Mirrors price_monitor_bot

This project follows the same architecture patterns as `price_monitor_bot`:
- Frozen dataclasses for models
- SQLite with deterministic IDs (SHA-1)
- Async client with circuit breaker
- Background daemon thread with event loop
- Modular storage/service/client separation

## Known Limitations

1. **Python Version**: Requires Python 3.10+ due to twikit's use of modern union syntax
2. **Rate Limiting**: X/Twitter rate limits may still apply even with human-like delays. Start with conservative intervals (15-60 minutes per rule)
3. **Authentication**: twikit caches cookies in `cookies.json`. Delete this file to force re-authentication
4. **Trend Snapshots**: Trend watches only notify on new trends appearing (not removed trends)

## Troubleshooting

### ImportError with twikit
Make sure you're using Python 3.10 or higher:
```bash
python3 --version
```

### "Unauthorized" errors
Delete `cookies.json` to force re-authentication:
```bash
rm cookies.json
python -m sns_monitor run
```

### Rate limiting (No tweets returned)
The account may be rate-limited. Increase check intervals or wait 15+ minutes before retrying.

### Telegram notifications not working
1. Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`
2. Check bot token is valid: `curl https://api.telegram.org/botTOKEN/getMe`
3. Monitor will log errors in console if notification fails

## Future Enhancements

- [ ] Web UI for rule management
- [ ] Multiple X account support with auto-rotation
- [ ] Discord webhook notifications
- [ ] Advanced filtering (keywords in replies, retweets only, etc.)
- [ ] Tweet archival and search across saved tweets
- [ ] Integration with aka_no_claw agent system

## License

Internal use for aka no claw.
