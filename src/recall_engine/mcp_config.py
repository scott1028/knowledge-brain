"""Reversible injection of the shared MCP server into each agent's config.

A single machine-wide HTTP MCP server serves many repos; each agent's config
carries the repo in the `X-Recall-Repo` request header (plus an optional
`X-Recall-Token`), so the server routes per connection. This module writes that
per-agent config entry on `wrap` and removes it when the session ends.

Structure mirrors skill.py: a per-project-cwd marker holds a pid refcount so
multiple wrap sessions in the same dir (same repo) attach instead of
re-injecting; full-file backups let restore return each config byte-identical.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from recall_engine.agents import AGENTS, MCP_SERVER_NAME, McpConfigSpec
from recall_engine.state import atomic_write_json, file_lock, is_pid_alive

MARKER_NAME = ".recall-engine-mcp-marker.json"
LOCK_NAME = ".recall-engine-mcp.lock"
BACKUP_SUFFIX = ".recall-engine-mcp-backup"

# Marker lives alongside the skill state, under the project's Agent Skills dir.
MARKER_DIR = ".agents/skills"


class McpConfigError(Exception):
    """MCP config injection cannot proceed."""


def _marker_path() -> Path:
    return Path.cwd() / MARKER_DIR / MARKER_NAME


def _lock_path() -> Path:
    return Path.cwd() / MARKER_DIR / LOCK_NAME


def _mcp_lock():
    """Serialize inject/restore for this project dir; released on fd close/death."""
    return file_lock(_lock_path())


def _marker_pids(record: dict) -> list[int]:
    """Owner pids from a marker; tolerate the legacy single-"pid" schema."""
    pids = record.get("pids")
    if pids is None and "pid" in record:
        pids = [record["pid"]]
    return [int(p) for p in (pids or [])]


def _config_path(spec: McpConfigSpec) -> Path:
    return Path.cwd() / spec.config_path


def _backup_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + BACKUP_SUFFIX)


def _build_headers(repo_path: Path, token: str | None) -> dict[str, str]:
    headers = {"X-Recall-Repo": str(repo_path)}
    if token is not None:
        headers["X-Recall-Token"] = token
    return headers


def _write_entry(spec: McpConfigSpec, repo_path: Path, url: str, token: str | None) -> None:
    """Write our server entry into the agent's config, preserving user content."""
    headers = _build_headers(repo_path, token)
    config_path = _config_path(spec)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if spec.fmt == "toml":
        _write_toml_entry(spec, config_path, url, headers)
    else:
        _write_json_entry(spec, config_path, url, headers)


def _write_json_entry(
    spec: McpConfigSpec, config_path: Path, url: str, headers: dict[str, str]
) -> None:
    if config_path.exists():
        existing = json.loads(config_path.read_text())
    else:
        existing = {}
    entry = {
        spec.url_field: url,
        spec.header_field: headers,
        **({"type": spec.type_value} if spec.type_value else {}),
        **spec.extra_fields,
    }
    servers = existing.setdefault(spec.servers_key, {})
    servers[MCP_SERVER_NAME] = entry
    config_path.write_text(json.dumps(existing, indent=2))


def _write_toml_entry(
    spec: McpConfigSpec, config_path: Path, url: str, headers: dict[str, str]
) -> None:
    import tomlkit

    if config_path.exists():
        doc = tomlkit.parse(config_path.read_text())
    else:
        doc = tomlkit.document()
    if spec.servers_key not in doc:
        doc[spec.servers_key] = tomlkit.table()
    servers = doc[spec.servers_key]
    server = tomlkit.table()
    server[spec.url_field] = url
    header_table = tomlkit.table()
    for key, value in headers.items():
        header_table[key] = value
    server[spec.header_field] = header_table
    servers[MCP_SERVER_NAME] = server
    config_path.write_text(tomlkit.dumps(doc))


def inject_mcp_config(
    agent: str, repo_path: Path, url: str, token: str | None = None
) -> None:
    """Register the shared server in AGENTS[agent]'s config file. Idempotent;
    multiple wrap sessions in the same dir with the SAME repo attach (refcount by
    pid) instead of re-injecting. Refuse to attach if a live session recorded a
    different repo (raise McpConfigError). If agent has no .mcp spec, no-op."""
    agent_spec = AGENTS.get(agent)
    if agent_spec is None or agent_spec.mcp is None:
        return
    with _mcp_lock():
        _inject_locked(agent, agent_spec.mcp, repo_path, url, token)


def _inject_locked(
    agent: str, spec: McpConfigSpec, repo_path: Path, url: str, token: str | None
) -> None:
    """Inject logic; assumes the MCP lock is already held."""
    marker = _marker_path()
    config_path = _config_path(spec)

    if marker.exists():
        try:
            record = json.loads(marker.read_text())
        except json.JSONDecodeError:
            record = {}
        live = [p for p in _marker_pids(record) if is_pid_alive(p)]
        if live:
            # A live session already registered the server here: attach to it.
            recorded_repo = record.get("repo_path")
            if recorded_repo is not None and recorded_repo != str(repo_path):
                raise McpConfigError(
                    "another wrap session is active in this directory with a "
                    "different knowledge repo; refusing to attach. Run "
                    "'recall-engine unwrap' if that session is gone."
                )
            record["pids"] = sorted(set(live) | {os.getpid()})
            record.pop("pid", None)  # migrate off the legacy single-pid field
            record.setdefault("repo_path", str(repo_path))
            configs = record.setdefault("configs", [])
            if not any(c["path"] == str(config_path) for c in configs):
                # A new agent joins this session: back up + write its config.
                configs.append(
                    _backup_and_write(agent, spec, config_path, repo_path, url, token)
                )
            atomic_write_json(marker, record)
            return
        # No live owner (all dead): clean up stale state, then re-inject fresh.
        print(
            "warning: stale wrap session detected; cleaning it up first.",
            file=sys.stderr,
        )
        _restore_locked(force=True)

    # Fresh inject: back up + write this agent's config, record a new marker.
    entry = _backup_and_write(agent, spec, config_path, repo_path, url, token)
    atomic_write_json(
        marker,
        {
            "pids": [os.getpid()],
            "repo_path": str(repo_path),
            "configs": [entry],
            "injected_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _backup_and_write(
    agent: str,
    spec: McpConfigSpec,
    config_path: Path,
    repo_path: Path,
    url: str,
    token: str | None,
) -> dict:
    """Back up a pre-existing config, write our entry, return the marker record."""
    existed = config_path.exists()
    backup = None
    if existed:
        backup = _backup_path(config_path)
        shutil.copy2(config_path, backup)
    _write_entry(spec, repo_path, url, token)
    return {
        "agent": agent,
        "path": str(config_path),
        "backup": str(backup) if backup else None,
        "existed": existed,
    }


def restore_mcp_config(owner_pid: int | None = None, *, force: bool = False) -> bool:
    """Undo injections recorded by the marker. Drop owner_pid from the refcount;
    if other live owners remain, keep the config and return True. If we are the
    last live owner (or force), restore every recorded config file and clear the
    marker. Returns True if a marker existed, False otherwise. Idempotent."""
    with _mcp_lock():
        return _restore_locked(owner_pid, force=force)


def _restore_locked(owner_pid: int | None = None, *, force: bool = False) -> bool:
    """Restore logic; assumes the MCP lock is already held."""
    marker = _marker_path()
    if not marker.exists():
        return False
    try:
        record = json.loads(marker.read_text())
    except json.JSONDecodeError:
        record = {}

    if not force:
        remaining = [
            p
            for p in _marker_pids(record)
            if is_pid_alive(p) and p != owner_pid
        ]
        if remaining:
            # Other live sessions still need the server: keep it, drop our pid.
            record["pids"] = remaining
            record.pop("pid", None)
            atomic_write_json(marker, record)
            return True

    # force, or we were the last live owner -> restore every recorded config.
    for entry in record.get("configs", []):
        path = Path(entry["path"])
        backup = entry.get("backup")
        if backup and Path(backup).exists():
            os.replace(backup, path)  # full restore of the user's original file
        elif not entry.get("existed"):
            path.unlink(missing_ok=True)  # file only held our entry
    marker.unlink(missing_ok=True)
    return True
