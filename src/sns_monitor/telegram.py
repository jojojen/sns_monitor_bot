from __future__ import annotations

import json
import logging
import ssl
from urllib.error import URLError
from urllib.request import Request, urlopen

import truststore

logger = logging.getLogger(__name__)


class TelegramClient:
    """Telegram Bot API client using stdlib urllib."""

    def __init__(
        self,
        token: str,
        *,
        timeout_seconds: float = 35.0,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._token = token
        self._base_url = f"https://api.telegram.org/bot{token}/"
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl_context or truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Send a text message to a chat, optionally with an inline keyboard."""
        payload: dict[str, object] = {
            "chat_id": str(chat_id),
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def _call(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        """Make a POST request to Telegram Bot API."""
        url = self._base_url + method
        headers = {
            "Content-Type": "application/json",
        }
        body = json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, headers=headers, method="POST")

        logger.debug("Telegram API call method=%s payload=%s", method, payload)
        try:
            with urlopen(request, timeout=self._timeout_seconds, context=self._ssl_context) as response:
                response_text = response.read().decode("utf-8")
                result = json.loads(response_text)
                logger.debug("Telegram API response method=%s ok=%s", method, result.get("ok"))
                return result if isinstance(result, dict) else {}
        except URLError as e:
            logger.error("Telegram API URLError method=%s error=%s", method, e)
            return {"ok": False, "error": str(e)}
        except Exception as e:
            logger.error("Telegram API unexpected error method=%s error=%s", method, e)
            return {"ok": False, "error": str(e)}
