"""Execution. The only package that touches the world.

Everything upstream — diagnosis, the legal set, gates, stops, the runner — is a
pure function of its arguments. This is where that stops, and keeping the
boundary in one module is what makes the rest replayable.
"""

__all__ = []
