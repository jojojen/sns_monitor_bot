"""SNS source plugins.

Each source (X via Nitter, Reddit, future Discord/YouTube/RSS, …) implements
the `SnsSource` Protocol from `.base` and gets registered in `SOURCES`.

`SnsMonitor` dispatches per-rule fetches via `SOURCES[rule.source]`, which
keeps the monitor loop source-agnostic.
"""

from .base import SnsSource
from .reddit_source import RedditSource
from .x_source import XSource

__all__ = ["SnsSource", "XSource", "RedditSource", "build_default_sources"]


def build_default_sources(x_client=None) -> dict[str, SnsSource]:
    """Build the default SOURCES registry for production use.

    `x_client` is the legacy `XClient` / `XClientWeb` instance (Nitter-based);
    we wrap it in an `XSource` adapter so the monitor sees a uniform Protocol.
    Pass None during tests that don't exercise X.
    """
    sources: dict[str, SnsSource] = {"reddit": RedditSource()}
    if x_client is not None:
        sources["x"] = XSource(x_client)
    return sources
