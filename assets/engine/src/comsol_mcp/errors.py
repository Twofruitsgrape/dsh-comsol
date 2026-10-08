"""Error types with actionable hints.

Every error raised towards the agent should tell it what to do next, not only
what went wrong. `hint` is rendered into the tool result by the server layer.
"""

from __future__ import annotations


class ComsolMcpError(RuntimeError):
    """Base class for all errors raised by this package."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_text(self) -> str:
        if self.hint:
            return f"{self.message}\nHint: {self.hint}"
        return self.message


class SessionError(ComsolMcpError):
    """The COMSOL server / client / desktop session cannot be used as requested."""


class ModelError(ComsolMcpError):
    """The main model is missing, changed or in an unexpected state."""


class NodeError(ComsolMcpError):
    """A model-tree node path or property does not exist."""


class UnsupportedError(ComsolMcpError):
    """The requested operation is not supported by this COMSOL/MPh version."""
