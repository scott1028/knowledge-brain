"""Shared process/state primitives reused across skill, MCP config, and the
global MCP server supervisor.

Kept dependency-free on purpose: skill injection, per-agent MCP config
injection, and the /tmp server PID file all need the same three primitives
(liveness check, atomic JSON write, and an flock-based mutex).
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


def is_pid_alive(pid: int) -> bool:
    """True if a process with this pid exists (owned by us or another user)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user.
        return True
    return True


def atomic_write_json(path: Path, data: object) -> None:
    """Write JSON via a temp file + os.replace so readers never see a partial."""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


@contextmanager
def file_lock(path: Path):
    """Exclusive flock on `path`; released when the fd closes (or on death)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock
