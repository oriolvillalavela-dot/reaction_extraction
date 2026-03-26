"""
Base class for all agents.
"""

from __future__ import annotations
import logging
from typing import Optional
from backend.portkey_client import PortKeyClient, get_client


class BaseAgent:
    """Minimal base for agents that call the Galileo/PortKey LLM gateway."""

    def __init__(self, client: Optional[PortKeyClient] = None, name: str = "Agent"):
        self.client = client or get_client()
        self.name = name
        self.logger = logging.getLogger(f"agents.{name}")

    def _chat(self, system: str, user: str, max_tokens: int = 8192, temperature: float = 0.0) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        self.logger.info("[%s] Calling LLM (%d user chars)…", self.name, len(user))
        result = self.client.chat(messages, max_tokens=max_tokens, temperature=temperature)
        self.logger.info("[%s] LLM responded (%d chars).", self.name, len(result))
        return result

    def _chat_with_images(
        self,
        system: str,
        text: str,
        images: list[dict],
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> str:
        from backend.portkey_client import build_image_message_content
        content = build_image_message_content(text, images)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        self.logger.info(
            "[%s] Calling LLM with %d images (%d text chars)…", self.name, len(images), len(text)
        )
        result = self.client.chat(messages, max_tokens=max_tokens, temperature=temperature)
        self.logger.info("[%s] LLM responded (%d chars).", self.name, len(result))
        return result
