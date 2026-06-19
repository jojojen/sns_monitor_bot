"""Deep-dive: fetch top-N thread replies for concrete product/character signal."""

from __future__ import annotations

from datetime import datetime, timezone

import sns_monitor.fourchan_buzz as fb
from sns_monitor.fourchan_buzz import FourchanBuzzClient
from sns_monitor.models import Tweet


def _tweet(board: str, no: str, replies: int) -> Tweet:
    return Tweet(
        tweet_id=no,
        author_handle=f"/{board}/",
        author_id="",
        text="subj",
        created_at=datetime.now(timezone.utc),
        like_count=replies,
    )


def test_deep_context_pulls_op_and_replies(monkeypatch):
    posts = {
        ("vg", "111"): [
            {"sub": "/pjsk/ - Project Sekai", "com": "OP body"},
            {"com": "Leo/need new event card is <b>insane</b>"},
            {"com": "25-ji limited acrylic restock when"},
        ],
    }
    monkeypatch.setattr(fb, "_fetch_thread", lambda b, n: posts.get((b, str(n)), []))

    client = FourchanBuzzClient()
    blob = client.deep_context([_tweet("vg", "111", 420)], top_n=1)

    assert "/pjsk/ - Project Sekai" in blob
    assert "Leo/need new event card is insane" in blob   # HTML stripped
    assert "25-ji limited acrylic restock when" in blob
    assert "回覆420" in blob


def test_deep_context_caps_top_n(monkeypatch):
    calls: list[str] = []

    def fake_fetch(board, no):
        calls.append(str(no))
        return [{"sub": f"t{no}", "com": "x"}]

    monkeypatch.setattr(fb, "_fetch_thread", fake_fetch)

    client = FourchanBuzzClient()
    tweets = [_tweet("vg", str(i), 100 - i) for i in range(10)]
    client.deep_context(tweets, top_n=3)

    # Only the top 3 threads are fetched — politeness to 4chan's rate limit.
    assert calls == ["0", "1", "2"]


def test_deep_context_skips_empty_threads(monkeypatch):
    monkeypatch.setattr(fb, "_fetch_thread", lambda b, n: [])
    client = FourchanBuzzClient()
    assert client.deep_context([_tweet("vg", "1", 5)], top_n=1) == ""


def test_vg_board_is_covered():
    # PJSK / ウマ娘 etc. live on /vg/ game generals.
    assert "vg" in FourchanBuzzClient()._boards
