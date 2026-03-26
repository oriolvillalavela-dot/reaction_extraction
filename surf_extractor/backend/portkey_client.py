"""
PortKey / Galileo AI Gateway client.

Uses the portkey-ai SDK to call gemini-2.5-pro via the Roche Galileo gateway.
Automatically falls back from the RCN endpoint to the WAF endpoint on failure.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read from environment variables with sensible defaults)
# ---------------------------------------------------------------------------

RCN_BASE_URL = os.getenv("GALILEO_RCN_ENDPOINT", "https://us.aigw.galileo.roche.com/v1")
WAF_BASE_URL = os.getenv("GALILEO_WAF_ENDPOINT", "https://waf-us.aigw.galileo.roche.com/v1")
HEALTH_URL = os.getenv("PORTKEY_HEALTH_URL", "https://us.aigw.galileo.roche.com/v1/health")
MODEL_ID = os.getenv("PORTKEY_MODEL_ID", "gemini-2.5-pro")
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY", "")
TIMEOUT_SECONDS = float(os.getenv("PORTKEY_TIMEOUT", "120"))
MAX_TOKENS = int(os.getenv("PORTKEY_MAX_TOKENS", "8192"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PortKeyClient:
    """
    Synchronous client for the Galileo/PortKey gateway using the portkey-ai SDK.

    Tries the RCN endpoint first; on failure falls back to the WAF endpoint.
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

    def _get_portkey(self, base_url: str):
        from portkey_ai import Portkey
        return Portkey(api_key=self.api_key, base_url=base_url, debug=False)

    def chat(
        self,
        messages: list[dict],
        max_tokens: int = MAX_TOKENS,
        temperature: float = 0.0,
        extra: Optional[dict] = None,
    ) -> str:
        """
        Synchronous chat completion. Returns the assistant message content.
        Falls back to WAF endpoint on failure.
        """
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if extra:
            kwargs.update(extra)

        for url_label, base_url in [("RCN", self.rcn_url), ("WAF", self.waf_url)]:
            try:
                logger.debug("Calling %s endpoint: %s", url_label, base_url)
                portkey = self._get_portkey(base_url)
                response = portkey.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                logger.debug("Response from %s (%d chars)", url_label, len(content))
                return content
            except Exception as exc:
                logger.warning("%s endpoint failed: %s – trying next…", url_label, exc)

        raise RuntimeError("All PortKey endpoints failed. Check PORTKEY_API_KEY and network access.")


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
