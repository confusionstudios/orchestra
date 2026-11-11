"""Shared cancellation signal for the database, runner and worker."""


class QueuePaused(Exception):
    """The durable pause gate interrupted a task lifecycle."""


class AgentRegistrationError(RuntimeError):
    """A launched child could not be durably registered."""
