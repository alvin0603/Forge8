"""Incremental readers for append-only event files."""

from .follower import JsonlFollower, MalformedEvent

__all__ = ["JsonlFollower", "MalformedEvent"]
