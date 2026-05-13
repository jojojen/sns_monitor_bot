from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

from .models import AccountWatch, KeywordWatch, TrendWatch
from .storage import SnsDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(prog="sns-monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    # add-account
    add_account = sub.add_parser("add-account")
    add_account.add_argument("screen_name")
    add_account.add_argument("--label", default="")
    add_account.add_argument("--chat-id", required=True)
    add_account.add_argument("--interval", type=int, default=15)
    add_account.add_argument("--db", default="data/sns.sqlite3")

    # add-keyword
    add_kw = sub.add_parser("add-keyword")
    add_kw.add_argument("query")
    add_kw.add_argument("--label", default="")
    add_kw.add_argument("--chat-id", required=True)
    add_kw.add_argument("--interval", type=int, default=30)
    add_kw.add_argument("--db", default="data/sns.sqlite3")

    # add-trend
    add_tr = sub.add_parser("add-trend")
    add_tr.add_argument("category", choices=["trending", "for-you", "news", "sports", "entertainment"])
    add_tr.add_argument("--chat-id", required=True)
    add_tr.add_argument("--interval", type=int, default=60)
    add_tr.add_argument("--db", default="data/sns.sqlite3")

    # list
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--db", default="data/sns.sqlite3")
    list_cmd.add_argument("--kind", choices=["account", "keyword", "trend"], default=None)

    # delete
    delete_cmd = sub.add_parser("delete")
    delete_cmd.add_argument("rule_id")
    delete_cmd.add_argument("--db", default="data/sns.sqlite3")

    # toggle
    toggle_cmd = sub.add_parser("toggle")
    toggle_cmd.add_argument("rule_id")
    toggle_cmd.add_argument("--enabled", action="store_true", default=None)
    toggle_cmd.add_argument("--disabled", action="store_true", default=None)
    toggle_cmd.add_argument("--db", default="data/sns.sqlite3")

    # run
    run_cmd = sub.add_parser("run")
    run_cmd.add_argument("--db", default="data/sns.sqlite3")
    run_cmd.add_argument("--interval", type=int, default=60)

    args = parser.parse_args()

    if args.command == "add-account":
        db_path = Path(args.db)
        db = SnsDatabase(db_path)
        db.bootstrap()

        screen_name = args.screen_name.lstrip("@")
        rule_id = SnsDatabase._watch_rule_id("account", screen_name)
        rule = AccountWatch(
            rule_id=rule_id,
            screen_name=screen_name,
            user_id=None,
            label=args.label or f"@{screen_name}",
            schedule_minutes=args.interval,
            chat_id=args.chat_id,
        )
        db.save_watch_rule(rule)
        print(f"✓ Added account watch: @{screen_name} (id={rule.rule_id})")
        return 0

    elif args.command == "add-keyword":
        db_path = Path(args.db)
        db = SnsDatabase(db_path)
        db.bootstrap()

        rule_id = SnsDatabase._watch_rule_id("keyword", args.query)
        rule = KeywordWatch(
            rule_id=rule_id,
            query=args.query,
            label=args.label or args.query,
            schedule_minutes=args.interval,
            chat_id=args.chat_id,
        )
        db.save_watch_rule(rule)
        print(f"✓ Added keyword watch: '{args.query}' (id={rule.rule_id})")
        return 0

    elif args.command == "add-trend":
        db_path = Path(args.db)
        db = SnsDatabase(db_path)
        db.bootstrap()

        rule_id = SnsDatabase._watch_rule_id("trend", args.category)
        rule = TrendWatch(
            rule_id=rule_id,
            category=args.category,
            label=f"Trends: {args.category}",
            schedule_minutes=args.interval,
            chat_id=args.chat_id,
        )
        db.save_watch_rule(rule)
        print(f"✓ Added trend watch: {args.category} (id={rule.rule_id})")
        return 0

    elif args.command == "list":
        db_path = Path(args.db)
        db = SnsDatabase(db_path)
        db.bootstrap()

        rules = db.list_watch_rules(kind=args.kind)
        if not rules:
            print("No watch rules found.")
            return 0

        for rule in rules:
            status = "✓ ENABLED" if rule.enabled else "✗ DISABLED"
            last_check = rule.last_checked_at.isoformat() if rule.last_checked_at else "Never"
            if isinstance(rule, AccountWatch):
                print(f"{status} | @{rule.screen_name} ({rule.label})")
            elif isinstance(rule, KeywordWatch):
                print(f"{status} | Keyword: {rule.query} ({rule.label})")
            elif isinstance(rule, TrendWatch):
                print(f"{status} | Trend: {rule.category} ({rule.label})")
            print(f"         ID: {rule.rule_id} | Last: {last_check}")
        return 0

    elif args.command == "delete":
        db_path = Path(args.db)
        db = SnsDatabase(db_path)
        db.bootstrap()

        if db.delete_watch_rule(args.rule_id):
            print(f"✓ Deleted rule: {args.rule_id}")
            return 0
        else:
            print(f"✗ Rule not found: {args.rule_id}")
            return 1

    elif args.command == "toggle":
        db_path = Path(args.db)
        db = SnsDatabase(db_path)
        db.bootstrap()

        if args.enabled and args.disabled:
            print("✗ Cannot specify both --enabled and --disabled")
            return 1

        if args.enabled:
            enabled = True
        elif args.disabled:
            enabled = False
        else:
            print("✗ Must specify --enabled or --disabled")
            return 1

        if db.toggle_watch_rule(args.rule_id, enabled=enabled):
            status = "ENABLED" if enabled else "DISABLED"
            print(f"✓ Rule {status}: {args.rule_id}")
            return 0
        else:
            print(f"✗ Rule not found: {args.rule_id}")
            return 1

    elif args.command == "run":
        # Lazy import twikit-dependent modules
        from .monitor import ensure_monitor
        from .telegram import TelegramClient
        from .x_client import XClient

        db_path = Path(args.db)
        db = SnsDatabase(db_path)
        db.bootstrap()

        # Load credentials
        x_username = os.environ.get("X_USERNAME", "").strip()
        x_email = os.environ.get("X_USER_MAIL", "").strip()
        x_password = os.environ.get("X_USER_PASSWORD", "").strip()
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "0").strip()

        if not all([x_username, x_email, x_password]):
            print("✗ Missing X credentials in .env: X_USERNAME, X_USER_MAIL, X_USER_PASSWORD")
            return 1

        x_client = XClient(username=x_username, email=x_email, password=x_password)

        if tg_token and tg_token != "placeholder":
            tg = TelegramClient(tg_token)

            def notify_fn(chat_id: str, text: str) -> None:
                result = tg.send_message(chat_id=chat_id or tg_chat, text=text)
                if result.get("ok"):
                    logger.info("Telegram notification sent chat_id=%s", chat_id or tg_chat)
                else:
                    logger.error("Telegram notification failed: %s", result)
        else:
            def notify_fn(chat_id: str, text: str) -> None:
                logger.info("[NOTIFY chat=%s]\n%s\n", chat_id or tg_chat, text)

        monitor, is_new = ensure_monitor(
            db_path=db_path,
            x_client=x_client,
            notify_fn=notify_fn,
            interval_seconds=args.interval,
        )

        logger.info("sns-monitor running. Press Ctrl+C to stop.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            monitor.stop()
            while monitor.is_running():
                threading.Event().wait(timeout=0.1)
            logger.info("Shutdown complete")

        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
