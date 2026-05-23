"""Telegram inline-button feedback for SNS post notifications.

Three reactions on each SNS post (X tweet / Reddit post) notification:
- 👍 up      → record only (positive signal preserves the rule as-is)
- 💰 bought  → record + halve rule.schedule_minutes (floor 15) so it polls
                more often
- 👎 down    → record, 24h cooldown on the rule; 3 in 7d auto-disables the
                rule (enabled=0)

Per-post feedback rows live in ``sns_post_feedback`` so the time-series
can power the 3-strike auto-disable. Rule-level signals (cooldown,
schedule, enabled) live on ``watch_rules`` and gate the monitor loop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .storage import SnsDatabase

logger = logging.getLogger(__name__)


FEEDBACK_KINDS: frozenset[str] = frozenset({"up", "down", "bought"})

DEFAULT_DOWN_COOLDOWN_HOURS: int = 24
DEFAULT_DOWN_DISMISS_THRESHOLD: int = 3
DEFAULT_DOWN_COUNT_WINDOW_DAYS: int = 7

# Floor for the "bought = halve schedule" boost. Mercari/Rakuma watchers
# poll at 1 min already; SNS rules should ramp up to roughly 15-min cadence
# at the most aggressive — anything tighter overloads Nitter and the LLM.
DEFAULT_BOUGHT_SCHEDULE_FLOOR_MINUTES: int = 15


def record_sns_feedback(
    *,
    db: SnsDatabase,
    tweet_id: str,
    rule_id: str,
    chat_id: str,
    kind: str,
    down_cooldown_hours: int = DEFAULT_DOWN_COOLDOWN_HOURS,
    down_dismiss_threshold: int = DEFAULT_DOWN_DISMISS_THRESHOLD,
    down_count_window_days: int = DEFAULT_DOWN_COUNT_WINDOW_DAYS,
    bought_schedule_floor_minutes: int = DEFAULT_BOUGHT_SCHEDULE_FLOOR_MINUTES,
) -> dict[str, object]:
    """Apply a feedback to a SNS post + aggregate to its rule.

    Returns a dict the Telegram callback handler uses to render a toast:
      {
        "status": "ok"|"rejected",
        "kind": str,
        "rule_id": str,
        "tweet_id": str,
        "side_effects": [str, ...],
        "down_count_in_window": int (down kind only),
        "new_schedule_minutes": int (bought kind only, if changed),
      }
    """
    if kind not in FEEDBACK_KINDS:
        return {"status": "rejected", "reason": f"unknown kind: {kind}"}

    rule = db.get_watch_rule(rule_id)
    if rule is None:
        return {"status": "rejected", "reason": "rule not found"}

    # Persist the per-post row first so the time-series is always complete,
    # even if a downstream rule mutation later fails.
    db.record_sns_post_feedback(
        tweet_id=tweet_id, rule_id=rule_id, chat_id=chat_id, feedback_kind=kind,
    )

    result: dict[str, object] = {
        "status": "ok",
        "kind": kind,
        "rule_id": rule_id,
        "tweet_id": tweet_id,
        "side_effects": [],
    }
    side_effects: list[str] = result["side_effects"]  # type: ignore[assignment]

    if kind == "up":
        # No rule-level mutation; the positive signal alone is the effect
        # (it keeps the rule visible in any future aggregation reports).
        logger.info(
            "SNS feedback up rule_id=%s tweet_id=%s — no rule-level action",
            rule_id, tweet_id,
        )
        return result

    if kind == "bought":
        new_minutes = max(
            bought_schedule_floor_minutes, int(rule.schedule_minutes) // 2,
        )
        if new_minutes < rule.schedule_minutes:
            db.update_rule_schedule(rule_id, new_minutes)
            side_effects.append("rule_schedule_shortened")
            result["new_schedule_minutes"] = new_minutes
        else:
            result["new_schedule_minutes"] = rule.schedule_minutes
        logger.info(
            "SNS feedback bought rule_id=%s tweet_id=%s schedule %d -> %d",
            rule_id, tweet_id, rule.schedule_minutes, new_minutes,
        )
        return result

    # kind == "down"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cooldown_until = (now + timedelta(hours=down_cooldown_hours)).isoformat()
    db.set_rule_cooldown(rule_id, cooldown_until)
    side_effects.append("cooldown_started")
    result["cooldown_until"] = cooldown_until

    window_start = (now - timedelta(days=down_count_window_days)).isoformat()
    down_count = db.count_recent_post_feedback(
        rule_id=rule_id, feedback_kind="down", since_iso=window_start,
    )
    result["down_count_in_window"] = down_count

    if down_count >= down_dismiss_threshold:
        db.toggle_watch_rule(rule_id, enabled=False)
        side_effects.append("rule_disabled")
        # Cooldown becomes irrelevant once disabled — clear it for clean state.
        db.set_rule_cooldown(rule_id, None)
        logger.info(
            "SNS feedback auto-disabled rule_id=%s after %d downs in window",
            rule_id, down_count,
        )
    else:
        logger.info(
            "SNS feedback down rule_id=%s tweet_id=%s down_count=%d cooldown_until=%s",
            rule_id, tweet_id, down_count, cooldown_until,
        )
    return result
