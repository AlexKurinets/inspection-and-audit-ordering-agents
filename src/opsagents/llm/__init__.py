from __future__ import annotations

import os

from .base import LLM, LLMError


def get_llm() -> LLM:
    """LLM_PROVIDER=claude uses the Anthropic API; anything else uses the offline stand-in."""
    if os.environ.get("LLM_PROVIDER", "offline") == "claude":
        from .claude import ClaudeLLM

        return ClaudeLLM()
    from .offline import OfflineLLM

    return OfflineLLM()


__all__ = ["LLM", "LLMError", "get_llm"]
