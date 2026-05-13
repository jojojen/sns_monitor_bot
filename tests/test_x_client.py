from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sns_monitor.x_client import XClient


@pytest.fixture
def client():
    return XClient(username="testuser", email="test@example.com", password="testpass")


@pytest.mark.asyncio
async def test_circuit_breaker_on_rate_limit(client):
    """Circuit breaker should suppress calls after TooManyRequests."""
    import twikit

    client._client = AsyncMock()
    client._client.search_tweet = AsyncMock(side_effect=twikit.TooManyRequests("rate limited"))

    # First call should trigger circuit breaker
    result = await client.search("test")
    assert result == []
    assert client._is_disabled()

    # Second call should be suppressed (circuit open)
    client._client.search_tweet.reset_mock()
    result = await client.search("test")
    assert result == []
    client._client.search_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_circuit_breaker_on_unauthorized(client):
    """Circuit breaker should handle Unauthorized error."""
    import twikit

    client._client = AsyncMock()
    client._client.get_user_tweets = AsyncMock(side_effect=twikit.Unauthorized("not authenticated"))

    result = await client.get_timeline("12345")
    assert result == []
    assert client._is_disabled()
    assert client._client is None  # Should reset client


def test_convert_tweet_parses_datetime():
    """Test that tweet datetime is parsed correctly."""
    from sns_monitor.x_client import _convert_tweet
    from datetime import datetime, timezone

    mock_tweet = MagicMock()
    mock_tweet.id = "987"
    mock_tweet.user.screen_name = "testuser"
    mock_tweet.user.id = "111"
    mock_tweet.text = "hello world"
    mock_tweet.created_at = "Mon Jan 01 00:00:00 +0000 2024"
    mock_tweet.retweet_count = 5
    mock_tweet.favorite_count = 10
    mock_tweet.lang = "en"

    tweet = _convert_tweet(mock_tweet)

    assert tweet.tweet_id == "987"
    assert tweet.author_handle == "testuser"
    assert tweet.text == "hello world"
    assert tweet.retweet_count == 5
    assert tweet.like_count == 10
    assert tweet.url == "https://x.com/testuser/status/987"
    assert isinstance(tweet.created_at, datetime)
