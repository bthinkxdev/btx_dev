"""Scheduled work for working time. Safe to run twice: it only closes days that are already stale."""
from .services import close_stale  # noqa: F401
