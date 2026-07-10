"""Reversible injection of the recall-engine skill into the project.

The rendered skill is written once to $CWD/.agents/skills/ (Agent Skills SSOT);
the other agents' skills dirs (.claude/.gemini/.pi) get relative symlinks to it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from recall_engine.agents import AGENTS, SKILLS_SSOT_DIR
from recall_engine.state import atomic_write_json, file_lock, is_pid_alive

SKILL_NAME = "recall-engine"
MARKER_NAME = ".recall-engine-marker.json"
LOCK_NAME = ".recall-engine.lock"
BACKUP_NAME = ".recall-engine-backup"

# Pre-SSOT versions kept their marker under .claude/skills/.
LEGACY_SKILLS_DIR = ".claude/skills"


class SkillError(Exception):
    """Skill injection cannot proceed."""


def _paths() -> tuple[Path, Path, Path]:
    """Return (skill dir, marker file, backup dir) under $CWD/.agents/skills/."""
    skills = Path.cwd() / SKILLS_SSOT_DIR
    return skills / SKILL_NAME, skills / MARKER_NAME, skills / BACKUP_NAME


def _link_dirs() -> list[Path]:
    """Skills dirs that need a symlink to the SSOT skill (all but the SSOT)."""
    cwd = Path.cwd()
    ssot = (cwd / SKILLS_SSOT_DIR).resolve(strict=False)
    dirs: list[Path] = []
    for spec in AGENTS.values():
        skills_dir = cwd / spec.skills_dir
        if spec.skills_dir == SKILLS_SSOT_DIR:
            continue
        if skills_dir.resolve(strict=False) == ssot:
            continue
        dirs.append(skills_dir)
    return dirs


def _is_pid_alive(pid: int) -> bool:
    return is_pid_alive(pid)


def _lock_path() -> Path:
    return Path.cwd() / SKILLS_SSOT_DIR / LOCK_NAME


def _skills_lock():
    """Serialize inject/restore for this project dir; released on fd close/death."""
    return file_lock(_lock_path())


def _marker_pids(record: dict) -> list[int]:
    """Owner pids from a marker; tolerate the legacy single-"pid" schema."""
    pids = record.get("pids")
    if pids is None and "pid" in record:
        pids = [record["pid"]]
    return [int(p) for p in (pids or [])]


def _write_marker(marker: Path, record: dict) -> None:
    """Atomic marker write so concurrent readers never see a partial file."""
    atomic_write_json(marker, record)


def _is_same_skill_path(path: Path, skill_dir: Path) -> bool:
    if path.name != skill_dir.name:
        return False
    return path.parent.resolve(strict=False) == skill_dir.parent.resolve(strict=False)


def _assert_same_repo(record: dict, repo_path: Path) -> None:
    """Refuse to attach when the live session points at a different repo."""
    recorded = record.get("repo_path")
    if recorded is not None and recorded != str(repo_path):
        raise SkillError(
            "another wrap session is active in this directory with a different "
            "knowledge repo; refusing to attach. Run "
            "'recall-engine unwrap' if that session is gone."
        )


def detect_active_repo() -> Path | None:
    """Knowledge repo of a live wrap session in this dir, or None.

    Lets a second wrap in the same project inherit the running session's repo
    without re-specifying KNOWLEDGE_REPO_PATH. Best-effort read (marker writes
    are atomic); inject_skill re-validates liveness under the lock.
    """
    skill_dir, marker, _ = _paths()
    if not marker.exists():
        return None
    try:
        record = json.loads(marker.read_text())
    except json.JSONDecodeError:
        return None
    live = any(_is_pid_alive(p) for p in _marker_pids(record))
    if not live or not (skill_dir / "SKILL.md").exists():
        return None
    repo_path = record.get("repo_path")
    return Path(repo_path) if repo_path else None


def inject_skill(repo_path: Path) -> None:
    """Write the rendered skill into the project; backup any pre-existing skill."""
    with _skills_lock():
        _inject_locked(repo_path)


def _inject_locked(repo_path: Path) -> None:
    """Inject logic; assumes the skills lock is already held."""
    skill_dir, marker, backup = _paths()

    if marker.exists():
        try:
            record = json.loads(marker.read_text())
        except json.JSONDecodeError:
            record = {}
        live = [p for p in _marker_pids(record) if _is_pid_alive(p)]
        if live and (skill_dir / "SKILL.md").exists():
            # A live session already set up the skill here: attach to it.
            _assert_same_repo(record, repo_path)
            record["pids"] = sorted(set(live) | {os.getpid()})
            record.pop("pid", None)  # migrate off the legacy single-pid field
            record.setdefault("repo_path", str(repo_path))  # backfill legacy markers
            _write_marker(marker, record)
            return
        # No live owner (all dead) or broken state: clean up, then re-inject.
        print(
            "warning: stale wrap session detected; cleaning it up first.",
            file=sys.stderr,
        )
        _restore_locked(force=True)

    if skill_dir.exists():
        # User's own skill without our marker: move it aside until restore.
        print(
            f"warning: existing skill at {skill_dir} moved to backup; "
            "it will be restored when the wrap session ends.",
            file=sys.stderr,
        )
        if backup.exists():
            shutil.rmtree(backup)
        skill_dir.rename(backup)

    # The skill is static: it points at the recall-engine MCP server's tools,
    # which read the knowledge repo directly (no in-project link needed).
    template = files("recall_engine").joinpath("templates/SKILL.md").read_text()
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(template)

    links: list[dict[str, str | None]] = []
    for skills_dir in _link_dirs():
        link = skills_dir / SKILL_NAME
        skills_dir.mkdir(parents=True, exist_ok=True)
        link_backup = None
        if link.exists() or link.is_symlink():
            print(
                f"warning: existing skill at {link} moved to backup; "
                "it will be restored when the wrap session ends.",
                file=sys.stderr,
            )
            link_backup = skills_dir / BACKUP_NAME
            if link_backup.exists() or link_backup.is_symlink():
                if link_backup.is_symlink():
                    link_backup.unlink()
                else:
                    shutil.rmtree(link_backup)
            link.rename(link_backup)
        # Relative so the project can be moved without breaking the link.
        os.symlink(os.path.relpath(skill_dir, link.parent), link)
        links.append(
            {"path": str(link), "backup": str(link_backup) if link_backup else None}
        )

    _write_marker(
        marker,
        {
            "pids": [os.getpid()],
            "repo_path": str(repo_path),
            "backup": str(backup) if backup.exists() else None,
            "links": links,
            "injected_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def restore_skill(owner_pid: int | None = None, *, force: bool = False) -> bool:
    """Undo an injection recorded by the marker; best-effort and idempotent.

    Returns True if anything was cleaned, False when no marker exists.
    """
    with _skills_lock():
        return _restore_locked(owner_pid, force=force)


def _restore_locked(owner_pid: int | None = None, *, force: bool = False) -> bool:
    """Restore logic; assumes the skills lock is already held."""
    skill_dir, marker, backup = _paths()
    if not marker.exists():
        return _restore_legacy_skill()
    try:
        record = json.loads(marker.read_text())
    except json.JSONDecodeError:
        record = {}

    if not force:
        remaining = [
            p for p in _marker_pids(record)
            if _is_pid_alive(p) and p != owner_pid
        ]
        if remaining:
            # Other live sessions still need the skill: keep it, drop our pid.
            # Pure marker rewrite preserving backup/links/injected_at; must never
            # fall through to teardown or the last owner would restore nothing.
            record["pids"] = remaining
            record.pop("pid", None)
            _write_marker(marker, record)
            return True

    # force, or we were the last live owner -> full teardown (existing logic).
    for entry in record.get("links", []):
        link = Path(entry["path"])
        if _is_same_skill_path(link, skill_dir):
            continue
        if link.is_symlink() or link.exists():
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link)
        link_backup = Path(entry["backup"]) if entry.get("backup") else None
        if link_backup and (link_backup.exists() or link_backup.is_symlink()):
            link_backup.rename(link)

    kb = record.get("knowledge")
    if kb:
        kb_link = Path(kb["path"])
        if kb_link.is_symlink() or kb_link.exists():
            if kb_link.is_symlink() or kb_link.is_file():
                kb_link.unlink()
            else:
                shutil.rmtree(kb_link)
        kb_backup = Path(kb["backup"]) if kb.get("backup") else None
        if kb_backup and (kb_backup.exists() or kb_backup.is_symlink()):
            kb_backup.rename(kb_link)

    if skill_dir.is_symlink() or skill_dir.exists():
        if skill_dir.is_symlink() or skill_dir.is_file():
            skill_dir.unlink()
        else:
            shutil.rmtree(skill_dir)
    backup_dir = Path(record["backup"]) if record.get("backup") else backup
    if backup_dir.exists():
        backup_dir.rename(skill_dir)
    marker.unlink(missing_ok=True)
    return True


def _restore_legacy_skill() -> bool:
    """Clean leftovers from a pre-SSOT session (marker under .claude/skills/)."""
    skills = Path.cwd() / LEGACY_SKILLS_DIR
    marker = skills / MARKER_NAME
    if not marker.exists():
        return False
    try:
        record = json.loads(marker.read_text())
    except json.JSONDecodeError:
        record = {}

    skill_dir = skills / SKILL_NAME
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    backup_dir = Path(record["backup"]) if record.get("backup") else skills / BACKUP_NAME
    if backup_dir.exists():
        backup_dir.rename(skill_dir)
    marker.unlink(missing_ok=True)
    return True
