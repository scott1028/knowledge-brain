"""Typer app; command dispatch only."""

from __future__ import annotations

import os
from pathlib import Path

import typer

from recall_engine.agents import AGENTS
from recall_engine.config import ConfigError, resolve_settings
from recall_engine.doctor import run_doctor
from recall_engine.drive import (
    DriveError,
    build_drive_service,
    resolve_folder_id,
    sync_download,
    sync_upload,
)
from recall_engine.launcher import (
    PI_MCP_ADAPTER,
    LauncherError,
    detect_agent,
    launch_agent,
    pi_mcp_adapter_installed,
)
from recall_engine.mcp_config import (
    McpConfigError,
    inject_mcp_config,
    restore_mcp_config,
)
from recall_engine.mcp_server import run_server
from recall_engine.mcp_supervisor import (
    SupervisorError,
    ensure_server,
    release_server,
)
from recall_engine.repo import RepoError, ensure_repo
from recall_engine.skill import (
    SkillError,
    detect_active_repo,
    inject_skill,
    restore_skill,
)

app = typer.Typer(
    no_args_is_help=True,
    help="Recall Engine: wrap an agent CLI (claude/codex/pi/gemini/opencode/agy) with a knowledge repo and sync it with Google Drive.",
)


@app.command(
    context_settings={
        # Everything after AGENT is forwarded verbatim to the agent CLI, so
        # `wrap claude foo --bar` behaves like `claude foo --bar` (env vars are
        # inherited automatically).
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
)
def wrap(ctx: typer.Context, agent: str) -> None:
    """Prepare the knowledge repo and launch AGENT (claude/codex/pi/gemini/opencode/agy or a wrapper).

    Extra arguments after AGENT are passed straight through to it.
    """
    agent_args = list(ctx.args)
    # Known names are trusted as-is; other names must classify as a wrapper
    # of one of the supported agent families.
    family = agent if agent in AGENTS else detect_agent(agent)
    if family is None:
        typer.echo(
            f"'{agent}' does not look like a supported agent CLI "
            f"({'/'.join(AGENTS)}); `{agent} --version` did not match any "
            "known agent.",
            err=True,
        )
        raise typer.Exit(2)
    # pi only reaches the MCP server through the pi-mcp-adapter extension; block
    # launch until it is installed (skill-only pi would silently miss the tools).
    # `is False` only: None means pi is not runnable, left to launch_agent's
    # 'install pi' error below.
    if family == "pi" and pi_mcp_adapter_installed(agent) is False:
        typer.echo(
            f"pi is missing the {PI_MCP_ADAPTER} extension, which it needs to "
            "reach the recall-engine MCP server. Install it and re-run:\n"
            f"    pi install {PI_MCP_ADAPTER}",
            err=True,
        )
        raise typer.Exit(2)
    try:
        # A live wrap session in this dir lets a second wrap reuse its repo
        # without KNOWLEDGE_REPO_PATH; env vars still win when set.
        settings = resolve_settings(fallback_repo_path=detect_active_repo())
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    try:
        repo = ensure_repo(settings)
    except RepoError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"knowledge repo: {repo}")
    pid = os.getpid()
    # Inject the skill (the reliable "search first" trigger), start/reuse the
    # shared MCP server, and point this agent's config at it. All three are torn
    # down in the finally, and each restore is idempotent and safe to call even
    # when its setup step did not run.
    try:
        inject_skill(repo)
        server = ensure_server()
        inject_mcp_config(family, repo, server.url, server.token)
        typer.echo(f"launching {agent}...")
        # agy only reads the injected .agents/ config when the project dir is in
        # its workspace, so pass `--add-dir <project>` for it.
        pre_args = (
            ["--add-dir", str(Path.cwd())]
            if AGENTS[family].needs_workspace_dir
            else []
        )
        exit_code = launch_agent(
            repo,
            argv=agent_args,
            agent=agent,
            install_hint=AGENTS[family].install_hint,
            pre_args=pre_args,
        )
    except (SkillError, SupervisorError, McpConfigError, LauncherError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        restore_mcp_config(owner_pid=pid)
        restore_skill(owner_pid=pid)
        release_server(owner_pid=pid)
    raise typer.Exit(exit_code)


@app.command()
def unwrap() -> None:
    """Clean up leftovers from an aborted wrap session."""
    # Skill and MCP-config state are per-project, so force-clean them here.
    cleaned = restore_mcp_config(force=True)
    cleaned = restore_skill(force=True) or cleaned
    # The MCP server is machine-global; only stop it when no live owner remains
    # (never force-kill — other projects may still be using the shared server).
    release_server(owner_pid=os.getpid())
    if cleaned:
        typer.echo("restored leftover skill and MCP state")
    else:
        typer.echo("nothing to clean")


@app.command()
def sync(mode: str) -> None:
    """One-way Drive sync; MODE is 'download' or 'upload'."""
    if mode not in ("download", "upload"):
        typer.echo(f"Invalid mode '{mode}'; use 'download' or 'upload'.", err=True)
        raise typer.Exit(2)
    try:
        settings = resolve_settings()
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    if not settings.drive_folder:
        typer.echo(
            "No Drive folder configured; set KNOWLEDGE_DRIVE_FOLDER to the Drive folder ID or name.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        repo = ensure_repo(settings)
    except RepoError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    src_dir = repo / "src"
    try:
        service = build_drive_service()
        folder_id = resolve_folder_id(service, settings.drive_folder)
        if mode == "download":
            names = sync_download(service, folder_id, src_dir)
        else:
            names = sync_upload(service, folder_id, src_dir)
    except DriveError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    for name in names:
        typer.echo(name)
    typer.echo(f"synced {len(names)} file(s) ({mode})")


@app.command()
def doctor() -> None:
    """Diagnose ssh key, repo config, gcloud Drive access, and agent CLIs on PATH."""
    if not run_doctor():
        raise typer.Exit(1)


@app.command(name="mcp-serve", hidden=True)
def mcp_serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
) -> None:
    """Run the shared knowledge MCP server (spawned by `wrap`; internal use)."""
    run_server(host, port, token or None)
