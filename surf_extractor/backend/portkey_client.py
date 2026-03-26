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

_RCN_DEFAULT = "https://us.aigw.galileo.roche.com/v1"
_WAF_DEFAULT  = "https://waf-us.aigw.galileo.roche.com/v1"


def _cfg():
    """Read config fresh from env each time (supports late load_dotenv calls)."""
    return {
        "api_key":  os.getenv("PORTKEY_API_KEY", ""),
        "rcn_url":  os.getenv("GALILEO_RCN_ENDPOINT", _RCN_DEFAULT),
        "waf_url":  os.getenv("GALILEO_WAF_ENDPOINT", _WAF_DEFAULT),
        "model":    os.getenv("PORTKEY_MODEL_ID", "gemini-2.5-pro"),
        "timeout":  float(os.getenv("PORTKEY_TIMEOUT", "120")),
        "max_tokens": int(os.getenv("PORTKEY_MAX_TOKENS", "8192")),
    }


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
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        rcn_url: Optional[str] = None,
        waf_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        cfg = _cfg()
        self.api_key = api_key if api_key is not None else cfg["api_key"]
        self.model   = model   if model   is not None else cfg["model"]
        self.rcn_url = (rcn_url if rcn_url is not None else cfg["rcn_url"]).rstrip("/")
        self.waf_url = (waf_url if waf_url is not None else cfg["waf_url"]).rstrip("/")
        self.timeout = timeout if timeout is not None else cfg["timeout"]
        self._max_tokens = cfg["max_tokens"]

    def _get_portkey(self, base_url: str):
        try:
            from portkey_ai import Portkey
        except ImportError:
            raise ImportError(
                "portkey-ai package is not installed. Run: pip install portkey-ai"
            )
        if not self.api_key:
            raise ValueError(
                "PORTKEY_API_KEY is not set. Add it to your .env file."
            )
        return Portkey(api_key=self.api_key, base_url=base_url, debug=False)

    def chat(
        self,
        messages: list[dict],
        max_tokens: Optional[int] = None,
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
            max_tokens=max_tokens if max_tokens is not None else self._max_tokens,
        )
        if extra:
            kwargs.update(extra)

        last_exc: Optional[Exception] = None
        for url_label, base_url in [("RCN", self.rcn_url), ("WAF", self.waf_url)]:
            try:
                logger.info("Calling %s endpoint: %s (model=%s)", url_label, base_url, self.model)
                portkey = self._get_portkey(base_url)
                response = portkey.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                logger.info("Response from %s (%d chars)", url_label, len(content))
                return content
            except Exception as exc:
                logger.error(
                    "%s endpoint failed [%s]: %s",
                    url_label, type(exc).__name__, exc, exc_info=True,
                )
                last_exc = exc

        raise RuntimeError(
            f"All PortKey endpoints failed. Last error: {last_exc!r}\n"
            f"  API key set: {bool(self.api_key)}\n"
            f"  RCN URL: {self.rcn_url}\n"
            f"  WAF URL: {self.waf_url}"
        ) from last_exc


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


def get_client() -> PortKeyClient:
    """Return a fresh client (reads env vars at call time)."""
    return PortKeyClient()
