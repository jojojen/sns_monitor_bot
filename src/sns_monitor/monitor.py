from __future__ import annotations

import asyncio
import logging
import threading
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .filters import filter_tweets_by_keywords
from datetime import datetime as _dt_datetime, timezone as _dt_timezone, timedelta as _dt_timedelta

from .entity_extractor import _AliasSource, extract_entities
from .formatters import (
    build_sns_feedback_keyboard,
    format_account_notification,
    format_account_post_one,
    format_keyword_notification,
    format_keyword_post_one,
    format_signal_notification,
    format_trend_notification,
)
from .interest_profile import (
    UserInterestProfile,
    aggregate_rule_feedback,
    build_user_interest_profile,
)
from .models import AccountWatch, KeywordWatch, TrendSnapshot, TrendWatch, Tweet, utc_now
from .signal_classifier import (
    DEFAULT_MIN_SCORE_TO_PUSH,
    SnsPostSignal,
    classify_sns_signal,
    decide_push_reason,
)
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
        # ── Two-opportunity RAG classifier (all optional) ────────────────
        # When ``classifier_llm_fn`` is None the monitor falls back to the
        # legacy per-tweet notify path (no scoring, no signal headlines).
        # Without these wired in production behaves identically to before.
        classifier_llm_fn: Callable[[str], str] | None = None,
        entity_extraction_llm_fn: Callable[[str], str] | None = None,
        alias_source: _AliasSource | None = None,
        knowledge_retriever: Callable[[tuple[str, ...]], str] | None = None,
        entity_research_fn: Callable[[str], bool] | None = None,
        ip_heat_retriever: Callable[[tuple[str, ...]], str] | None = None,
        monitor_db_path: str | Path | None = None,
        opportunity_db_path: str | Path | None = None,
        min_score_to_push: int = DEFAULT_MIN_SCORE_TO_PUSH,
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
        # ── Classifier dependencies (None-safe; legacy path used if absent) ─
        self._classifier_llm_fn = classifier_llm_fn
        self._entity_extraction_llm_fn = entity_extraction_llm_fn
        self._alias_source = alias_source
        self._knowledge_retriever = knowledge_retriever
        self._entity_research_fn = entity_research_fn
        self._ip_heat_retriever = ip_heat_retriever
        self._monitor_db_path = Path(monitor_db_path) if monitor_db_path else None
        self._opportunity_db_path = Path(opportunity_db_path) if opportunity_db_path else None
        self._db_path = Path(db_path)
        self._min_score_to_push = min_score_to_push
        # Cache user profile per chat_id within one tick — built lazily,
        # cleared every tick so feedback updates are reflected quickly.
        self._profile_cache: dict[str, UserInterestProfile] = {}

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

        # Clear interest-profile cache so feedback / watchlist edits from the
        # previous tick are picked up.
        self._profile_cache.clear()

        enabled = [r for r in rules if r.enabled]
        for rule in enabled:
            if self._is_due(rule):
                try:
                    await self._check_rule(rule)
                except Exception:
                    logger.exception("Check failed rule_id=%s", rule.rule_id)

    def _is_due(self, rule: AccountWatch | KeywordWatch | TrendWatch) -> bool:
        """Check if a rule is due for checking based on schedule.

        A rule that's currently in cooldown (set by 👎 feedback) is never
        due — even if its schedule_minutes would otherwise fire.
        """
        cooldown_until = getattr(rule, "cooldown_until", None)
        if cooldown_until is not None:
            try:
                if isinstance(cooldown_until, str):
                    until_dt = _dt_datetime.fromisoformat(cooldown_until)
                else:
                    until_dt = cooldown_until
                if until_dt.tzinfo is None:
                    until_dt = until_dt.replace(tzinfo=_dt_timezone.utc)
                if until_dt > _dt_datetime.now(_dt_timezone.utc):
                    return False
            except (ValueError, TypeError):
                pass  # malformed cooldown_until → fall through, treat as no cooldown
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

        if self._classifier_llm_fn is not None:
            self._classify_and_notify(rule=rule, tweets=matching_tweets)
        else:
            self._notify_each_tweet(
                rule=rule, tweets=matching_tweets,
                format_one=lambda tw, counts: format_account_post_one(rule, tw, feedback_counts=counts),
            )

    def _notify_each_tweet(
        self,
        *,
        rule: AccountWatch | KeywordWatch,
        tweets: list[Tweet],
        format_one,
    ) -> None:
        """Send one Telegram message per tweet so each carries its own
        👍/👎/💰 feedback keyboard. Failures on individual tweets don't block
        the others — they're logged and the tweet stays unnotified for retry
        on the next check."""
        # Look up the per-rule 30-day feedback aggregate once per batch so we
        # don't hammer the DB per message.
        try:
            since_iso = (
                _dt_datetime.now(_dt_timezone.utc) - _dt_timedelta(days=30)
            ).isoformat()
            feedback_counts = self._db.feedback_counts_for_rule(
                rule_id=rule.rule_id, since_iso=since_iso,
            )
        except Exception:
            logger.exception(
                "Failed to load feedback counts for rule_id=%s — proceeding without footer",
                rule.rule_id,
            )
            feedback_counts = {}

        delivered: list[str] = []
        for tweet in tweets:
            text = format_one(tweet, feedback_counts)
            reply_markup = build_sns_feedback_keyboard(
                tweet_id=tweet.tweet_id, rule_id=rule.rule_id,
            )
            try:
                self._notify_fn(rule.chat_id, text, reply_markup)
                delivered.append(tweet.tweet_id)
            except TypeError:
                # Fallback for legacy notify_fn signatures (chat_id, text).
                # We still attempt delivery without the keyboard so the user
                # doesn't lose the alert entirely.
                try:
                    self._notify_fn(rule.chat_id, text)  # type: ignore[call-arg]
                    delivered.append(tweet.tweet_id)
                    logger.warning(
                        "notify_fn doesn't accept reply_markup — feedback "
                        "keyboard skipped for rule_id=%s tweet_id=%s",
                        rule.rule_id, tweet.tweet_id,
                    )
                except Exception:
                    logger.exception(
                        "Notification failed (no-keyboard fallback) rule_id=%s tweet_id=%s",
                        rule.rule_id, tweet.tweet_id,
                    )
            except Exception:
                logger.exception(
                    "Notification failed rule_id=%s tweet_id=%s",
                    rule.rule_id, tweet.tweet_id,
                )

        if delivered:
            self._db.mark_tweets_notified(rule.rule_id, delivered)

    # ── Two-opportunity classifier pipeline ────────────────────────────────

    def _get_or_build_profile(self, chat_id: str) -> UserInterestProfile:
        """Build (and cache, per-tick) the user's interest profile.

        If the cross-DB paths aren't configured we still return a profile
        struct — just with empty watchlist / pinned / feedback fields, so the
        classifier doesn't crash and treats it as 'unconstrained'."""
        cached = self._profile_cache.get(chat_id)
        if cached is not None:
            return cached
        try:
            profile = build_user_interest_profile(
                chat_id=chat_id,
                sns_db_path=self._db_path,
                monitor_db_path=self._monitor_db_path or self._db_path,
                opportunity_db_path=self._opportunity_db_path or self._db_path,
            )
        except Exception:
            logger.exception("classifier: failed to build user profile chat_id=%s", chat_id)
            profile = UserInterestProfile(chat_id=str(chat_id))
        self._profile_cache[chat_id] = profile
        return profile

    def _find_matched_keyword(
        self, rule: AccountWatch | KeywordWatch, tweet_text: str
    ) -> str | None:
        """Return the first include_keyword that appears in the tweet, or None.

        For KeywordWatch this is N/A (the caller passes ``force_bypass_keyword``
        directly with ``rule.query``)."""
        if not isinstance(rule, AccountWatch):
            return None
        if not rule.include_keywords:
            return None
        text_lower = (tweet_text or "").lower()
        for kw in rule.include_keywords:
            if kw and kw.lower() in text_lower:
                return kw
        return None

    def _retrieve_knowledge(
        self, tweet_text: str
    ) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
        """Run entity extraction + knowledge retrieval. Returns
        ``(matched_entities, knowledge_block, novel_mentions)``.

        Schedules novel mentions for background research if a research
        function was wired in."""
        if self._alias_source is None:
            return (), "(無)", ()
        try:
            known, novel = extract_entities(
                tweet_text,
                alias_source=self._alias_source,
                llm_fn=self._entity_extraction_llm_fn,
            )
        except Exception:
            logger.exception("classifier: entity extraction failed")
            return (), "(無)", ()
        block = "(無)"
        if self._knowledge_retriever is not None and known:
            try:
                block = self._knowledge_retriever(known) or "(無)"
            except Exception:
                logger.exception("classifier: knowledge retrieval failed entities=%s", known)
        if self._entity_research_fn is not None:
            for entity in novel:
                try:
                    self._entity_research_fn(entity)
                except Exception:
                    logger.exception("classifier: entity research enqueue failed entity=%s", entity)
        return known, block, novel

    def _classify_and_notify(
        self,
        *,
        rule: AccountWatch | KeywordWatch,
        tweets: list[Tweet],
        force_bypass_keyword: str | None = None,
    ) -> None:
        """RAG-driven push path: classify every tweet, persist score, push
        only when Bypass A fires or a score gate clears.

        Even dropped tweets are written to ``sns_post_signals`` with
        ``pushed=0`` so /digest can mine them later.
        """
        profile = self._get_or_build_profile(rule.chat_id)
        feedback_for_rule = aggregate_rule_feedback(profile, rule.rule_id)
        # Same per-batch footer as the legacy path.
        try:
            since_iso = (
                _dt_datetime.now(_dt_timezone.utc) - _dt_timedelta(days=30)
            ).isoformat()
            footer_counts = self._db.feedback_counts_for_rule(
                rule_id=rule.rule_id, since_iso=since_iso,
            )
        except Exception:
            logger.exception(
                "classifier: feedback counts lookup failed rule_id=%s", rule.rule_id,
            )
            footer_counts = {}

        delivered: list[str] = []
        for tweet in tweets:
            try:
                self._classify_one_and_maybe_push(
                    rule=rule, tweet=tweet,
                    profile=profile,
                    feedback_for_rule=feedback_for_rule,
                    footer_counts=footer_counts,
                    force_bypass_keyword=force_bypass_keyword,
                    delivered=delivered,
                )
            except Exception:
                logger.exception(
                    "classifier: per-tweet pipeline failed rule_id=%s tweet_id=%s",
                    rule.rule_id, tweet.tweet_id,
                )
        if delivered:
            self._db.mark_tweets_notified(rule.rule_id, delivered)

    def _classify_one_and_maybe_push(
        self,
        *,
        rule: AccountWatch | KeywordWatch,
        tweet: Tweet,
        profile: UserInterestProfile,
        feedback_for_rule: dict[str, int],
        footer_counts: dict[str, int],
        force_bypass_keyword: str | None,
        delivered: list[str],
    ) -> None:
        # Skip re-classifying a tweet already scored in a previous tick.
        cached = self._db.get_sns_signal(tweet_id=tweet.tweet_id, rule_id=rule.rule_id)
        if cached is not None and cached.get("pushed"):
            return

        matched_entities, knowledge_block, _novel = self._retrieve_knowledge(tweet.text)

        heat_block = ""
        if self._ip_heat_retriever is not None and matched_entities:
            try:
                heat_block = self._ip_heat_retriever(matched_entities) or ""
            except Exception:
                logger.exception(
                    "classifier: ip_heat_retriever failed entities=%s", matched_entities
                )

        signal = classify_sns_signal(
            tweet_id=tweet.tweet_id,
            rule_id=rule.rule_id,
            author_handle=tweet.author_handle,
            created_at=tweet.created_at.strftime("%Y-%m-%d %H:%M UTC"),
            tweet_text=tweet.text,
            watchlist_queries=profile.watchlist_queries,
            pinned_targets=profile.pinned_targets,
            feedback_for_rule=feedback_for_rule,
            knowledge_block=knowledge_block,
            heat_block=heat_block,
            matched_entities=matched_entities,
            llm_fn=self._classifier_llm_fn,
        )

        if force_bypass_keyword is not None:
            bypass_keyword = force_bypass_keyword
            keyword_matched = True
        else:
            bypass_keyword = self._find_matched_keyword(rule, tweet.text)
            keyword_matched = bypass_keyword is not None

        reason = decide_push_reason(
            signal=signal,
            keyword_matched=keyword_matched,
            min_score=self._min_score_to_push,
        )
        should_push = reason != "none"

        try:
            self._db.record_sns_signal(
                tweet_id=signal.tweet_id, rule_id=signal.rule_id,
                long_term_score=signal.long_term_score,
                arbitrage_score=signal.arbitrage_score,
                matched_products=signal.matched_products,
                matched_keywords=signal.matched_keywords,
                matched_entities=signal.matched_entities,
                suggested_action=signal.suggested_action,
                rationale=signal.rationale,
                deadline=signal.deadline_iso,
                bypass_reason=reason,
                pushed=1 if should_push else 0,
            )
        except Exception:
            logger.exception(
                "classifier: failed to persist signal rule_id=%s tweet_id=%s",
                rule.rule_id, tweet.tweet_id,
            )

        if not should_push:
            logger.info(
                "classifier: drop tweet_id=%s rule_id=%s lt=%d arb=%d reason=%s",
                tweet.tweet_id, rule.rule_id,
                signal.long_term_score, signal.arbitrage_score, reason,
            )
            return

        text = format_signal_notification(
            rule=rule, tweet=tweet, signal=signal,
            bypass_reason=reason,
            bypass_keyword=bypass_keyword if reason == "explicit_keyword" else None,
            feedback_counts=footer_counts,
        )
        reply_markup = build_sns_feedback_keyboard(
            tweet_id=tweet.tweet_id, rule_id=rule.rule_id,
        )
        try:
            self._notify_fn(rule.chat_id, text, reply_markup)
            delivered.append(tweet.tweet_id)
        except TypeError:
            try:
                self._notify_fn(rule.chat_id, text)  # type: ignore[call-arg]
                delivered.append(tweet.tweet_id)
            except Exception:
                logger.exception(
                    "classifier: notify failed (no-keyboard fallback) rule_id=%s tweet_id=%s",
                    rule.rule_id, tweet.tweet_id,
                )
        except Exception:
            logger.exception(
                "classifier: notify failed rule_id=%s tweet_id=%s",
                rule.rule_id, tweet.tweet_id,
            )

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

        if self._classifier_llm_fn is not None:
            # Keyword watches are by definition "explicit keyword" — the user
            # searched for ``rule.query``, so Bypass A always applies. We still
            # run the classifier so the DB has a score record per tweet, but
            # gate decision is always 'explicit_keyword' for keyword watches.
            self._classify_and_notify(rule=rule, tweets=new_tweets, force_bypass_keyword=rule.query)
        else:
            self._notify_each_tweet(
                rule=rule, tweets=new_tweets,
                format_one=lambda tw, counts: format_keyword_post_one(rule, tw, feedback_counts=counts),
            )

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
    classifier_llm_fn: Callable[[str], str] | None = None,
    entity_extraction_llm_fn: Callable[[str], str] | None = None,
    alias_source: _AliasSource | None = None,
    knowledge_retriever: Callable[[tuple[str, ...]], str] | None = None,
    entity_research_fn: Callable[[str], bool] | None = None,
    monitor_db_path: str | Path | None = None,
    opportunity_db_path: str | Path | None = None,
    min_score_to_push: int = DEFAULT_MIN_SCORE_TO_PUSH,
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
            classifier_llm_fn=classifier_llm_fn,
            entity_extraction_llm_fn=entity_extraction_llm_fn,
            alias_source=alias_source,
            knowledge_retriever=knowledge_retriever,
            entity_research_fn=entity_research_fn,
            monitor_db_path=monitor_db_path,
            opportunity_db_path=opportunity_db_path,
            min_score_to_push=min_score_to_push,
        )
        _monitor.start()
        return _monitor, True
