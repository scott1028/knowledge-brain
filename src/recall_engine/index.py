"""Per-repo SQLite index of Markdown notes, kept live by a filesystem watcher.

The machine-wide MCP server serves many knowledge repos. For each repo the first
search lazily builds a SQLite DB (one row per note: repo-relative path + full
content) and starts a ``watchdog`` observer that keeps it in sync. The DB lives
in the OS temp dir, named by a repo hash so the same repo shares one DB across
every agent on the server; it falls back to a repo-local file when the temp dir
is not writable. The server deletes its DBs on exit. Callers narrow candidate
files with one SQL scan and derive line/snippet from the stored content. Indexes
are singletons keyed by the resolved repo path; when an index cannot be built the
caller falls back to a direct filesystem scan.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

# Temp-dir DB name: mcp_index_<repo_hash>.sqlite3.db (shared per repo).
TMP_DB_PREFIX = "mcp_index_"
# Repo-local fallback name, used only when the temp dir is not writable.
REPO_DB_FILENAME = ".mcp_index.sqlite3.db"


def index_db_path(repo: Path) -> Path:
    """Where the repo's SQLite index lives.

    Prefer the OS temp dir, named by a repo hash so the same repo shares one DB
    across agents (and stays out of the repo). Fall back to a repo-local file
    when the temp dir is not writable (e.g. locked-down hosts).
    """
    tmp = Path(tempfile.gettempdir())
    if os.access(tmp, os.W_OK):
        repo_hash = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:12]
        return tmp / f"{TMP_DB_PREFIX}{repo_hash}.sqlite3.db"
    return repo / REPO_DB_FILENAME


def iter_note_paths(src: Path) -> list[Path]:
    """List *.md notes under src, skipping ones that resolve outside src."""
    notes: list[Path] = []
    for note in sorted(src.rglob("*.md")):
        if not note.is_file():
            continue
        resolved = note.resolve()
        if resolved != src and src not in resolved.parents:
            continue
        notes.append(note)
    return notes


class RepoIndex:
    """SQLite index for one repo's notes, synced by a watchdog observer."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.src = (repo / "src").resolve()
        self.db_path = index_db_path(repo)
        self._lock = threading.Lock()
        self._observer: Observer | None = None

    def start(self) -> None:
        """Build the DB from the current notes, then watch src/ for changes."""
        self._build()
        self._start_observer()

    def stop(self) -> None:
        """Stop the observer; safe to call more than once."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None

    def remove_db(self) -> None:
        """Delete this repo's index DB and its WAL/SHM siblings."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)

    def search(self, query: str, max_matches: int) -> list[tuple[str, str]]:
        """Return (relative_path, content) rows whose content contains query.

        Case-insensitive for ASCII (matches the direct-scan fallback); the
        caller re-scans content line by line for the authoritative matches.
        """
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "SELECT relative_path, content FROM notes "
                "WHERE instr(lower(content), lower(?)) > 0 "
                "ORDER BY relative_path LIMIT ?",
                (query, max_matches),
            )
            return cursor.fetchall()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # Two processes may write this DB (wrap eager-build + server observer);
            # wait for a contended lock instead of failing immediately.
            conn.execute("PRAGMA busy_timeout=3000")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _build(self) -> None:
        rows = [
            (note.relative_to(self.repo).as_posix(),
             note.read_text(encoding="utf-8", errors="replace"))
            for note in iter_note_paths(self.src)
        ]
        with self._lock, self._connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS notes ("
                "relative_path TEXT PRIMARY KEY, content TEXT)"
            )
            conn.execute("DELETE FROM notes")
            conn.executemany(
                "INSERT OR REPLACE INTO notes(relative_path, content) VALUES (?, ?)",
                rows,
            )

    def _start_observer(self) -> None:
        observer = Observer()
        observer.schedule(_NoteEventHandler(self), str(self.src), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def _apply(self, raw_path: str, *, removed: bool = False) -> None:
        """Upsert or delete the row for one note path from a filesystem event."""
        path = Path(raw_path)
        if path.suffix != ".md":
            return
        try:
            rel = path.relative_to(self.repo).as_posix()
        except ValueError:
            return
        if removed or not path.is_file():
            self._delete(rel)
            return
        resolved = path.resolve()
        if resolved != self.src and self.src not in resolved.parents:
            self._delete(rel)  # symlink now points outside src
            return
        self._upsert(rel, path.read_text(encoding="utf-8", errors="replace"))

    def _upsert(self, rel: str, content: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO notes(relative_path, content) VALUES (?, ?)",
                (rel, content),
            )

    def _delete(self, rel: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("DELETE FROM notes WHERE relative_path = ?", (rel,))


class _NoteEventHandler(FileSystemEventHandler):
    """Route watchdog events to the index; rebuild on directory-level changes."""

    def __init__(self, index: RepoIndex) -> None:
        self._index = index

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._index._apply(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._index._apply(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._index._build()
        else:
            self._index._apply(event.src_path, removed=True)

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            self._index._build()
        else:
            self._index._apply(event.src_path, removed=True)
            self._index._apply(event.dest_path)


_indexes: dict[Path, RepoIndex] = {}
_registry_lock = threading.Lock()


def ensure_index(repo: Path) -> RepoIndex | None:
    """Return the singleton index for repo, building it on first use.

    Returns None when the index cannot be built (e.g. a read-only repo), so
    callers fall back to a direct filesystem scan.
    """
    key = repo.resolve()
    with _registry_lock:
        existing = _indexes.get(key)
        if existing is not None:
            return existing
        try:
            new_index = RepoIndex(key)
            new_index.start()
        except Exception:
            return None
        _indexes[key] = new_index
        return new_index


def build_index(repo: Path) -> bool:
    """Build/refresh the repo's index DB now, without starting an observer.

    Best-effort eager build for the wrap startup path: returns False (and does
    nothing) when src/ is missing or the build fails, so callers never block
    launch on index errors. The server still owns the live watchdog singleton.
    """
    key = repo.resolve()
    if not (key / "src").is_dir():
        return False
    try:
        RepoIndex(key)._build()
        return True
    except Exception:
        return False


def reset_indexes() -> None:
    """Stop every observer, delete its DB, and clear the registry.

    Used on server shutdown (drop this server's index DBs once no one needs
    them) and between tests.
    """
    with _registry_lock:
        for repo_index in _indexes.values():
            repo_index.stop()
            repo_index.remove_db()
        _indexes.clear()
