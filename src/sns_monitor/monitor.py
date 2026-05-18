from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .filters import filter_tweets_by_keywords
from .formatters import format_account_notification, format_keyword_notification, format_trend_notification
from .models import AccountWatch, KeywordWatch, TrendSnapshot, TrendWatch, utc_now
from .sources import SnsSource, build_default_sources
from .storage import SnsDatabase
from .x_client import XClient

logger = logging.getLogger(__name__)


class SnsMonitor:
    """Background SNS monitoring daemon with asyncio loop in a thread.

    Each watch rule carries a `source` field (e.g. "x", "reddit"); the
    monitor dispatches fetches via the `sources` registry instead of a
    single hardcoded backend.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        notify_fn: Callable[[str, str], None],
        interval_seconds: int = 60,
        sources: dict[str, SnsSource] | None = None,
        x_client: XClient | None = None,
    ) -> None:
        if sources is None:
            sources = build_default_sources(x_client=x_client)
        if "x" not in sources and x_client is not None:
            from .sources import XSource

            sources = {**sources, "x": XSource(x_client)}
        self._db = SnsDatabase(db_path)
        self._sources = sources
        self._x = x_client  # retained only for ensure_logged_in fallback paths
        self._notify_fn = notify_fn
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Start the monitoring daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="sns-monitor", daemon=True)
        self._thread.start()
        logger.info("SnsMonitor started")

    def stop(self) -> None:
        """Signal the monitoring daemon to stop."""
        self._stop.set()
        logger.info("SnsMonitor stop signal sent")

    def is_running(self) -> bool:
        """Check if the monitoring daemon is running."""
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        """Create and run the asyncio event loop in this thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_loop())
        except Exception:
            logger.exception("SnsMonitor event loop error")
        finally:
            loop.close()
            logger.info("SnsMonitor event loop closed")

    async def _async_loop(self) -> None:
        """Main async monitoring loop with auto-retry."""
        x_source = self._sources.get("x")
        if x_source is not None:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    logger.info("Login attempt %d/%d...", attempt + 1, max_retries)
                    await x_source.ensure_logged_in()
                    logger.info("✅ Successfully logged in to X")
                    break
                except Exception as e:
                    logger.warning("❌ Login attempt %d failed: %s", attempt + 1, e)
                    if attempt < max_retries - 1:
                        wait_time = 5 * (attempt + 1)
                        logger.info("⏳ Retrying in %d seconds...", wait_time)
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error("❌ All login attempts failed after %d tries", max_retries)
                        raise

        # Run tick loop
        await self._async_tick()

        while not self._stop.is_set():
            elapsed = 0.0
            while elapsed < self._interval and not self._stop.is_set():
                await asyncio.sleep(1.0)
                elapsed += 1.0
            if not self._stop.is_set():
                try:
                    await self._async_tick()
                except Exception:
                    logger.exception("Tick failed, continuing...")
                    await asyncio.sleep(5)  # Brief pause before retry

    async def _async_tick(self) -> None:
        """Check all enabled watch rules."""
        try:
            rules = self._db.list_watch_rules()
        except Exception:
            logger.exception("Failed to list watch rules")
            return

        enabled = [r for r in rules if r.enabled]
        for rule in enabled:
            if self._is_due(rule):
                try:
                    await self._check_rule(rule)
                except Exception:
                    logger.exception("Check failed rule_id=%s", rule.rule_id)

    def _is_due(self, rule: AccountWatch | KeywordWatch | TrendWatch) -> bool:
        """Check if a rule is due for checking based on schedule."""
        if rule.last_checked_at is None:
            return True
        elapsed = utc_now() - rule.last_checked_at
        return elapsed >= timedelta(minutes=rule.schedule_minutes)

    async def _check_rule(self, rule: AccountWatch | KeywordWatch | TrendWatch) -> None:
        """Dispatch rule checking by type."""
        if isinstance(rule, AccountWatch):
            await self._check_account_watch(rule)
        elif isinstance(rule, KeywordWatch):
            await self._check_keyword_watch(rule)
        elif isinstance(rule, TrendWatch):
            await self._check_trend_watch(rule)

    def _source_for(self, rule) -> SnsSource | None:
        source = self._sources.get(getattr(rule, "source", "x"))
        if source is None:
            logger.warning(
                "No source plugin registered for rule_id=%s source=%s — skipping",
                rule.rule_id,
                getattr(rule, "source", "x"),
            )
        return source

    async def _check_account_watch(self, rule: AccountWatch) -> None:
        """Check a single account watch rule via its source plugin."""
        from dataclasses import replace

        source = self._source_for(rule)
        if source is None:
            return

        user_id = rule.user_id
        if user_id is None:
            user_id = await source.resolve_user_id(rule.screen_name)
            if not user_id:
                logger.warning("Could not resolve user ID for source=%s target=%s", rule.source, rule.screen_name)
                return
            self._db.update_user_id(rule.rule_id, user_id)
            rule = replace(rule, user_id=user_id)

        is_first = rule.last_checked_at is None
        tweets = await source.fetch_account(rule.screen_name, user_id=user_id)
        new_tweets = self._db.record_tweets(rule.rule_id, tweets)
        self._db.mark_rule_checked(rule.rule_id)

        if is_first or not new_tweets:
            return

        matching_tweets = filter_tweets_by_keywords(new_tweets, rule.include_keywords)
        if not matching_tweets:
            return

        text = format_account_notification(rule, matching_tweets)
        try:
            self._notify_fn(rule.chat_id, text)
            self._db.mark_tweets_notified(rule.rule_id, [t.tweet_id for t in matching_tweets])
        except Exception:
            logger.exception("Notification failed for rule_id=%s", rule.rule_id)

    async def _check_keyword_watch(self, rule: KeywordWatch) -> None:
        """Check a keyword watch rule via its source plugin."""
        source = self._source_for(rule)
        if source is None:
            return

        is_first = rule.last_checked_at is None
        tweets = await source.search_keyword(rule.query)
        new_tweets = self._db.record_tweets(rule.rule_id, tweets)
        self._db.mark_rule_checked(rule.rule_id)

        if is_first or not new_tweets:
            return

        text = format_keyword_notification(rule, new_tweets)
        try:
            self._notify_fn(rule.chat_id, text)
            self._db.mark_tweets_notified(rule.rule_id, [t.tweet_id for t in new_tweets])
        except Exception:
            logger.exception("Notification failed for rule_id=%s", rule.rule_id)

    async def _check_trend_watch(self, rule: TrendWatch) -> None:
        """Check a trend watch rule via its source plugin."""
        source = self._source_for(rule)
        if source is None:
            return
        try:
            trend_names = await source.fetch_trend(rule.category)
        except NotImplementedError:
            logger.warning(
                "Trend watches not supported by source=%s — disabling rule_id=%s",
                rule.source,
                rule.rule_id,
            )
            self._db.mark_rule_checked(rule.rule_id)
            return
        if not trend_names:
            self._db.mark_rule_checked(rule.rule_id)
            return

        prev = self._db.latest_trend_snapshot(rule.rule_id)
        is_first = prev is None
        snapshot = TrendSnapshot(
            snapshot_id=SnsDatabase._snapshot_id(rule.rule_id, utc_now().isoformat()),
            rule_id=rule.rule_id,
            names=tuple(trend_names),
        )
        self._db.save_trend_snapshot(snapshot)
        self._db.mark_rule_checked(rule.rule_id)

        if is_first:
            return

        new_trends = [n for n in trend_names if prev and n not in prev.names]
        if new_trends:
            text = format_trend_notification(rule, new_trends, trend_names)
            try:
                self._notify_fn(rule.chat_id, text)
            except Exception:
                logger.exception("Notification failed for rule_id=%s", rule.rule_id)


_monitor_lock = threading.Lock()
_monitor: SnsMonitor | None = None


def ensure_monitor(
    *,
    db_path: str | Path,
    notify_fn: Callable[[str, str], None],
    interval_seconds: int = 60,
    x_client: XClient | None = None,
    sources: dict[str, SnsSource] | None = None,
) -> tuple[SnsMonitor, bool]:
    """Get or create the singleton monitor. Returns (monitor, is_new)."""
    global _monitor
    with _monitor_lock:
        if _monitor is not None and _monitor.is_running():
            return _monitor, False
        _monitor = SnsMonitor(
            db_path=db_path,
            x_client=x_client,
            sources=sources,
            notify_fn=notify_fn,
            interval_seconds=interval_seconds,
        )
        _monitor.start()
        return _monitor, True
