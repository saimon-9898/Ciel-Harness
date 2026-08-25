"""Provider adapters for the Agent abstraction (Phase 4).

The adapters define the boundary between the Task Engine and coding-agent
providers.  In Phase 4 **no provider is connected**: every adapter reports
``not_configured`` for health and raises :class:`ProviderNotConfiguredError`
for execution methods.  Fake success is never reported.
"""

from .base import AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter
from .openhands import OpenHandsAdapter

__all__ = [
    "AgentAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "GeminiAdapter",
    "OpenHandsAdapter",
]
