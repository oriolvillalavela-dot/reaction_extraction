"""
PortKey / Galileo AI Gateway client.

Uses the Roche Galileo gateway endpoints (OpenAI-compatible) to call
gemini-2.5-pro. Automatically falls back from the RCN endpoint to the
WAF endpoint on connection errors.
"""

import os
import logging
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read from environment variables with sensible defaults)
# ---------------------------------------------------------------------------

RCN_BASE_URL = os.getenv("PORTKEY_RCN_URL", "https://us.aigw.galileo.roche.com/v1")
WAF_BASE_URL = os.getenv("PORTKEY_WAF_URL", "https://waf-us.aigw.galileo.roche.com/v1")
HEALTH_URL = os.getenv("PORTKEY_HEALTH_URL", "https://us.aigw.galileo.roche.com/v1/health")
MODEL_ID = os.getenv("PORTKEY_MODEL_ID", "gemini-2.5-pro")
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY", "")
TIMEOUT_SECONDS = float(os.getenv("PORTKEY_TIMEOUT", "120"))
MAX_TOKENS = int(os.getenv("PORTKEY_MAX_TOKENS", "8192"))


# ---------------------------------------------------------------------------
# Low-level HTTP client
# ---------------------------------------------------------------------------

class PortKeyClient:
    """
    Minimal async OpenAI-compatible client for the Galileo/PortKey gateway.

    Sends requests to the RCN endpoint first; on network error it retries
    against the WAF endpoint.
    """

    def __init__(
        self,
        api_key: str = PORTKEY_API_KEY,
        model: str = MODEL_ID,
        rcn_url: str = RCN_BASE_URL,
        waf_url: str = WAF_BASE_URL,
        timeout: float = TIMEOUT_SECONDS,
    ):
        self.api_key = api_key
        self.model = model
        self.rcn_url = rcn_url.rstrip("/")
        self.waf_url = waf_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _build_payload(
        self,
        messages: list[dict],
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.0,
        extra: Optional[dict] = None,
    ) -> dict:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if extra:
            payload.update(extra)
        return payload

    def _post_sync(self, base_url: str, payload: dict) -> dict:
        url = f"{base_url}/chat/completions"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            return resp.json()

    async def _post_async(self, base_url: str, payload: dict) -> dict:
        url = f"{base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            return resp.json()

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.0,
        extra: Optional[dict] = None,
    ) -> str:
        """
        Synchronous chat completion. Returns the assistant message content.
        Falls back to WAF endpoint on network failure.
        """
        payload = self._build_payload(messages, max_tokens, temperature, extra)

        for url_label, base_url in [("RCN", self.rcn_url), ("WAF", self.waf_url)]:
            try:
                logger.debug("Calling %s endpoint: %s/chat/completions", url_label, base_url)
                response = self._post_sync(base_url, payload)
                content = response["choices"][0]["message"]["content"]
                logger.debug("Response received from %s (%d chars)", url_label, len(content))
                return content
            except httpx.ConnectError as exc:
                logger.warning("%s endpoint unreachable: %s – trying next…", url_label, exc)
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "%s endpoint HTTP error %s: %s",
                    url_label, exc.response.status_code, exc.response.text[:500],
                )
                raise
            except Exception as exc:
                logger.error("Unexpected error from %s: %s", url_label, exc, exc_info=True)
                raise

        raise RuntimeError("All PortKey endpoints failed. Check network or API key.")

    async def achat(
        self,
        messages: list[dict],
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.0,
        extra: Optional[dict] = None,
    ) -> str:
        """
        Async chat completion. Falls back to WAF endpoint on network failure.
        """
        payload = self._build_payload(messages, max_tokens, temperature, extra)

        for url_label, base_url in [("RCN", self.rcn_url), ("WAF", self.waf_url)]:
            try:
                logger.debug("Async calling %s endpoint", url_label)
                response = await self._post_async(base_url, payload)
                content = response["choices"][0]["message"]["content"]
                return content
            except httpx.ConnectError as exc:
                logger.warning("Async %s unreachable: %s – trying next…", url_label, exc)
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "Async %s HTTP error %s: %s",
                    url_label, exc.response.status_code, exc.response.text[:500],
                )
                raise
            except Exception as exc:
                logger.error("Async unexpected error from %s: %s", url_label, exc, exc_info=True)
                raise

        raise RuntimeError("All PortKey endpoints failed.")


def build_image_message_content(text: str, images: list[dict]) -> list[dict]:
    """
    Build the 'content' list for a user message that includes text and images.

    images: list of {"mime_type": str, "data": str (base64), "path": str}

    Returns a list compatible with the OpenAI vision message format.
    """
    content: list[dict] = [{"type": "text", "text": text}]

    for img in images:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{img['mime_type']};base64,{img['data']}"
            },
        })

    return content


# Singleton convenience instance (created lazily to respect env vars)
_default_client: Optional[PortKeyClient] = None


def get_client() -> PortKeyClient:
    global _default_client
    if _default_client is None:
        _default_client = PortKeyClient()
    return _default_client
