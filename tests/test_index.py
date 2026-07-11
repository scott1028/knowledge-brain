"""Tests for the per-repo SQLite note index: watchdog sync, singleton, search."""
import time
from pathlib import Path

from recall_engine.index import RepoIndex, ensure_index, reset_indexes


def make_repo(base: Path, notes: dict[str, str]) -> Path:
    src = base / "src"
    src.mkdir(parents=True)
    for rel, text in notes.items():
        note = src / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(text)
    return base


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_watchdog_syncs_created_and_deleted_notes(tmp_path):
    repo = make_repo(tmp_path / "repo", {"a.md": "alpha alpha\n"})
    idx = RepoIndex(repo.resolve())
    idx.start()
    try:
        (repo / "src" / "b.md").write_text("bravo keyword\n")
        assert wait_until(lambda: any(r[0] == "src/b.md" for r in idx.search("bravo", 50)))
        (repo / "src" / "a.md").unlink()
        assert wait_until(lambda: idx.search("alpha", 50) == [])
    finally:
        idx.stop()
        idx.remove_db()


def test_ensure_index_returns_singleton_per_repo(tmp_path):
    # One index per KNOWLEDGE_REPO_PATH, shared by every agent that reaches the
    # same shared server; a different repo gets its own index.
    repo_a = make_repo(tmp_path / "a", {"a.md": "alpha\n"})
    repo_b = make_repo(tmp_path / "b", {"b.md": "bravo\n"})
    try:
        assert ensure_index(repo_a) is ensure_index(repo_a)
        assert ensure_index(repo_a) is not ensure_index(repo_b)
    finally:
        reset_indexes()


def test_search_is_case_insensitive(tmp_path):
    # input "abc" must find "AbC" (and "ABC" too); miss returns nothing.
    repo = make_repo(tmp_path / "repo", {"a.md": "Deploy AbC line\n"})
    idx = RepoIndex(repo.resolve())
    idx.start()
    try:
        assert idx.search("abc", 50) == [("src/a.md", "Deploy AbC line\n")]
        assert idx.search("ABC", 50) == [("src/a.md", "Deploy AbC line\n")]
        assert idx.search("absent", 50) == []
    finally:
        idx.stop()
        idx.remove_db()
