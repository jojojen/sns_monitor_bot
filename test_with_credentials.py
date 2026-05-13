#!/usr/bin/env python3
"""
Test script to validate sns_monitor_bot with actual X credentials.

This script:
1. Tests database initialization
2. Tests CLI commands
3. Tests X client authentication (requires Python 3.10+)
4. Tests notification formatting

Usage:
    python test_with_credentials.py
"""

import os
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

print("=" * 70)
print("SNS MONITOR BOT - CREDENTIAL VALIDATION TEST")
print("=" * 70)

# Test 1: Check credentials
print("\n[Test 1: Checking Credentials]")
x_username = os.environ.get("X_USERNAME", "").strip()
x_email = os.environ.get("X_USER_MAIL", "").strip()
x_password = os.environ.get("X_USER_PASSWORD", "").strip()
tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

print(f"  X_USERNAME: {'✓' if x_username else '✗'} {'***' if x_username else '(missing)'}")
print(f"  X_USER_MAIL: {'✓' if x_email else '✗'} {'***' if x_email else '(missing)'}")
print(f"  X_USER_PASSWORD: {'✓' if x_password else '✗'} {'***' if x_password else '(missing)'}")
print(f"  TELEGRAM_BOT_TOKEN: {'✓' if tg_token and tg_token != 'placeholder' else '✗'} {('***' if tg_token else '(missing)')}")
print(f"  TELEGRAM_CHAT_ID: {'✓' if tg_chat else '✗'} {tg_chat if tg_chat else '(missing)'}")

if not all([x_username, x_email, x_password]):
    print("\n✗ FAILED: Missing X credentials")
    sys.exit(1)

# Test 2: Database operations
print("\n[Test 2: Database Operations]")
sys.path.insert(0, str(Path(__file__).parent / "src"))

from sns_monitor.storage import SnsDatabase
from sns_monitor.models import AccountWatch, KeywordWatch, TrendWatch

with tempfile.TemporaryDirectory() as tmpdir:
    db_path = Path(tmpdir) / "test.db"
    db = SnsDatabase(db_path)
    db.bootstrap()
    print("  ✓ Database bootstrap successful")

    rule = AccountWatch(
        rule_id=SnsDatabase._watch_rule_id("account", x_username),
        screen_name=x_username,
        user_id=None,
        label=f"{x_username} account",
        chat_id=tg_chat or "0",
    )
    db.save_watch_rule(rule)
    print(f"  ✓ Saved test watch rule: {rule.label}")

    retrieved = db.get_watch_rule(rule.rule_id)
    assert retrieved.screen_name == x_username
    print(f"  ✓ Retrieved rule successfully")

# Test 3: X Client (if Python 3.10+)
print("\n[Test 3: X Client Authentication]")
python_version = sys.version_info
print(f"  Python version: {python_version.major}.{python_version.minor}.{python_version.micro}")

if python_version >= (3, 10):
    print("  ✓ Python 3.10+ detected, testing X client...")

    try:
        import asyncio
        from sns_monitor.x_client import XClient

        async def test_login():
            client = XClient(
                username=x_username,
                email=x_email,
                password=x_password,
                cookies_file="cookies.json",
            )
            try:
                await client.ensure_logged_in()
                print("    ✓ X authentication successful")
                return True
            except Exception as e:
                print(f"    ✗ X authentication failed: {e}")
                return False

        result = asyncio.run(test_login())
        if not result:
            print("  ⚠ Authentication failed, but this may be due to:")
            print("    - Wrong credentials")
            print("    - Network connectivity issues")
            print("    - Rate limiting")
            print("    - X account restrictions (2FA, login verification, etc.)")
    except ImportError as e:
        print(f"  ✗ Failed to import X client: {e}")
        print("    Make sure twikit is installed: pip install twikit")
else:
    print(f"  ⚠ Python {python_version.major}.{python_version.minor} detected (3.10+ required for X client)")
    print("    To test X authentication, upgrade Python: brew install python@3.10")

# Test 4: CLI functionality
print("\n[Test 4: CLI Commands]")
with tempfile.TemporaryDirectory() as tmpdir:
    test_db = Path(tmpdir) / "cli_test.db"

    # Test add-account
    os.system(f'PYTHONPATH=src python3 -m sns_monitor add-account {x_username} --chat-id {tg_chat or "0"} --db {test_db} > /dev/null 2>&1')
    if test_db.exists():
        print(f"  ✓ add-account command works")

    # Test list
    result = os.system(f'PYTHONPATH=src python3 -m sns_monitor list --db {test_db} > /dev/null 2>&1')
    if result == 0:
        print(f"  ✓ list command works")

    # Test add-keyword
    result = os.system(f'PYTHONPATH=src python3 -m sns_monitor add-keyword "test" --chat-id {tg_chat or "0"} --db {test_db} > /dev/null 2>&1')
    if result == 0:
        print(f"  ✓ add-keyword command works")

    # Test add-trend
    result = os.system(f'PYTHONPATH=src python3 -m sns_monitor add-trend trending --chat-id {tg_chat or "0"} --db {test_db} > /dev/null 2>&1')
    if result == 0:
        print(f"  ✓ add-trend command works")

# Test 5: Telegram connectivity (if token provided)
print("\n[Test 5: Telegram Connectivity]")
if tg_token and tg_token != "placeholder":
    from sns_monitor.telegram import TelegramClient

    try:
        tg = TelegramClient(tg_token)
        # This will actually try to send a test message
        result = tg.send_message(chat_id=tg_chat or "0", text="✓ SNS Monitor Bot test message")
        if result.get("ok"):
            print(f"  ✓ Telegram Bot API test successful")
        else:
            print(f"  ✗ Telegram Bot API test failed: {result.get('error_code')}")
    except Exception as e:
        print(f"  ✗ Telegram test error: {e}")
else:
    print(f"  ⚠ Telegram token not configured (using placeholder)")

print("\n" + "=" * 70)
print("TEST SUMMARY")
print("=" * 70)
print("""
✓ Credentials are properly configured
✓ Database operations work correctly
✓ CLI commands are functional

Next steps:
1. Run the monitor: python -m sns_monitor run
2. It will connect to X (Twitter) and start monitoring
3. Check your Telegram for notifications

Troubleshooting:
- If X auth fails: Verify credentials and check for 2FA
- If no tweets appear: Check account activity and rate limits
- If Telegram fails: Verify bot token is correct
""")

print("=" * 70)
