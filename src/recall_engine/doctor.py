"""Environment diagnostics: one line per check, with actionable fixes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from recall_engine.agents import AGENTS
from recall_engine.config import ConfigError, resolve_settings
from recall_engine.drive import DriveError, build_drive_service, execute
from recall_engine.mcp_supervisor import server_status
from recall_engine.repo import RepoError, resolve_ssh_key


def _ok(name: str, detail: str) -> None:
    print(f"[ok] {name}: {detail}")


def _fail(name: str, detail: str, fix: str) -> None:
    print(f"[fail] {name}: {detail}")
    for line in fix.splitlines():
        print(f"    {line}")


def _check_git() -> bool:
    path = shutil.which("git")
    if path is None:
        _fail("git", "not found on PATH", "install git (apt/brew install git)")
        return False
    _ok("git", path)
    return True


def _check_agents() -> bool:
    """Report each supported agent CLI; pass if at least one is installed."""
    found = False
    for name in AGENTS:
        path = shutil.which(name)
        if path is None:
            print(f"[skip] {name}: not found on PATH")
        else:
            _ok(name, path)
            found = True
    if not found:
        _fail(
            "agent CLIs",
            f"none of {'/'.join(AGENTS)} found on PATH",
            "\n".join(spec.install_hint for spec in AGENTS.values()),
        )
    return found


def _check_mcp() -> bool:
    """The shared knowledge MCP server needs the `mcp` package importable."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        _fail(
            "mcp package",
            "python 'mcp' package not importable",
            "reinstall recall-engine so its dependencies are present",
        )
        return False
    _ok("mcp package", "streamable-HTTP MCP server available")
    return True


def _check_mcp_server() -> bool:
    """Reachability of the shared server. Not running is normal: `wrap` starts
    it on demand. A recorded but unreachable server means stale state.
    """
    status = server_status()
    if status is None:
        print("[skip] mcp server: not running (started on demand by `wrap`)")
        return True
    if status.reachable:
        _ok(
            "mcp server",
            f"reachable at {status.url} (pid {status.pid}, {len(status.owners)} owner(s))",
        )
        return True
    _fail(
        "mcp server",
        f"recorded at {status.url} but not reachable (stale state)",
        "run `recall-engine unwrap` to clear it; the next `wrap` respawns the server",
    )
    return False


def _report_pi_mcp_adapter() -> None:
    """Informational: pi reaches the MCP server only via pi-mcp-adapter."""
    if shutil.which("pi") is None:
        return
    print(
        "[note] pi: install the pi-mcp-adapter extension so pi can reach the "
        "recall-engine MCP server (`pi install pi-mcp-adapter`)"
    )


def _report_codex_trust() -> None:
    """Informational: codex reads a project's .codex/config.toml only for
    trusted projects, so the injected server stays invisible until the project
    is trusted (the first `codex` run in it prompts to trust)."""
    if shutil.which("codex") is None:
        return
    print(
        "[note] codex: reads the injected .codex/config.toml only in trusted "
        "projects; if the recall-engine tools do not appear, run codex once in "
        "the project and accept the trust prompt"
    )


def _check_ssh_key() -> bool:
    ssh_key_env = os.environ.get("SSH_KEY")
    ssh_key = Path(ssh_key_env).expanduser() if ssh_key_env else None
    try:
        key = resolve_ssh_key(ssh_key)
    except RepoError as exc:
        _fail("ssh key", "no usable key", str(exc))
        return False
    _ok("ssh key", str(key))
    return True


def _check_repo_config() -> bool:
    try:
        settings = resolve_settings()
    except ConfigError as exc:
        _fail("repo config", "not configured", str(exc))
        return False
    if settings.repo_mode == "path":
        _ok("repo config", f"path mode: {settings.repo_path}")
    else:
        _ok("repo config", f"ssh mode: {settings.repo_ssh_url} -> {settings.repo_path}")
    return True


def _check_drive_access() -> bool:
    try:
        service = build_drive_service()
        # Trivial call to prove the Drive scope actually works; no folder needed.
        execute(service.files().list(pageSize=1, fields="files(id)"))
    except DriveError as exc:
        _fail("gcloud auth", "Drive access failed", str(exc))
        return False
    except Exception as exc:  # e.g. network down; never show a stack trace
        _fail(
            "gcloud auth",
            f"Drive API call failed ({type(exc).__name__}: {exc})",
            "check network connectivity and retry",
        )
        return False
    _ok("gcloud auth", "credentials with Drive scope verified")
    return True


def _report_drive_folder() -> None:
    """Informational only: the folder is needed just for sync."""
    folder = os.environ.get("KNOWLEDGE_DRIVE_FOLDER")
    if folder:
        _ok("drive folder", folder)
    else:
        print("[skip] drive folder: KNOWLEDGE_DRIVE_FOLDER not set (needed only for sync)")


def run_doctor() -> bool:
    """Run every check; return True only when all required checks pass."""
    results = [
        _check_git(),
        _check_agents(),
        _check_mcp(),
        _check_mcp_server(),
        _check_ssh_key(),
        _check_repo_config(),
        _check_drive_access(),
    ]
    _report_pi_mcp_adapter()
    _report_codex_trust()
    _report_drive_folder()
    return all(results)
