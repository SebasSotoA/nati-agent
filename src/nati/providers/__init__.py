"""Access to the model. One network path only: openai_compat against Ollama Cloud."""

from nati.providers.openai_compat import ProviderError, RawTurn, ToolCall, complete

__all__ = ["ProviderError", "RawTurn", "ToolCall", "complete"]
