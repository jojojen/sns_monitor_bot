"""SNS source plugins.

Each source (X via Nitter, future Discord/YouTube/RSS, …) implements the
`SnsSource` Protocol from `.base` and gets registered in `SOURCES`.

`SnsMonitor` dispatches per-rule fetches via `SOURCES[rule.source]`, which
keeps the monitor loop source-agnostic.

Reddit was removed: Reddit now blocks unauthenticated access and gates API app
registration behind its Responsible Builder Policy. Keyword buzz moved to the
user-triggered ``/snsbuzz`` (4chan, see ``fourchan_buzz``); the background
monitor deliberately does NOT poll 4chan to avoid any standing ban risk.
"""

from .base import SnsSource
from .x_source import XSource

__all__ = ["SnsSource", "XSource", "build_default_sources"]


def build_default_sources(x_client=None) -> dict[str, SnsSource]:
    """Build the default SOURCES registry for production use.

    `x_client` is the legacy `XClient` / `XClientWeb` instance (Nitter-based);
    we wrap it in an `XSource` adapter so the monitor sees a uniform Protocol.
    Pass None during tests that don't exercise X.
    """
    sources: dict[str, SnsSource] = {}
    if x_client is not None:
        sources["x"] = XSource(x_client)
    return sources
