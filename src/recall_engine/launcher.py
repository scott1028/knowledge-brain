"""Spawn an agent CLI as a child process with signal forwarding."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from types import FrameType

from recall_engine.agents import AGENTS

# Safe command-name charset; agent is interpolated into a shell string
# (literal interpolation keeps alias/function resolution working).
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9._/-]+$")

# pi reaches the MCP server only through this extension; without it the injected
# .pi/mcp.json is inert, so `wrap pi` must block until the user installs it.
PI_MCP_ADAPTER = "pi-mcp-adapter"


class LauncherError(Exception):
    """The agent cannot be launched."""


def _resolve_shell() -> str:
    return os.environ.get("SHELL") or shutil.which("bash") or "/bin/bash"


def detect_agent(agent: str) -> str | None:
    """Classify an unknown command name into a supported agent family.

    Runs `agent --version` inside the user's interactive shell so wrappers
    defined as aliases or shell functions are detected too. Matches the
    stdout against known version signatures first (claude/codex), then falls
    back to name tokens for agents whose --version prints a bare version
    number (pi/gemini/opencode/agy). Returns the family name or None.
    """
    if not _AGENT_NAME_RE.match(agent):
        return None
    try:
        probe = subprocess.run(
            [_resolve_shell(), "-i", "-c", f"{agent} --version"],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if probe.returncode != 0:
        return None
    stdout = probe.stdout.lower()
    for family, spec in AGENTS.items():
        if spec.version_signature and spec.version_signature.encode() in stdout:
            return family
    # Bare-version CLIs: classify by exact name tokens (avoids pip -> pi).
    tokens = re.split(r"[-._]", Path(agent).name)
    for family in AGENTS:
        if family in tokens:
            return family
    return None


def pi_mcp_adapter_installed(agent: str) -> bool | None:
    """Whether `<agent> list` reports the pi-mcp-adapter extension.

    Returns True when the adapter is present, False when pi runs but the
    adapter is absent, and None when it cannot be determined (pi not runnable,
    probe failed/timed out) so the caller falls back to the normal launch path
    and its 'install pi' error. Probed through the interactive shell like
    detect_agent/launch so pi wrappers (aliases/functions) resolve too; stdin
    is closed so pi's project-trust prompt can never block the probe.
    """
    if not _AGENT_NAME_RE.match(agent):
        return None
    try:
        probe = subprocess.run(
            [_resolve_shell(), "-i", "-c", f"{agent} list"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None
    if probe.returncode != 0:
        return None
    return PI_MCP_ADAPTER in (probe.stdout + probe.stderr).decode(errors="replace")


def launch_agent(
    repo_path: Path,
    argv: list[str] | None = None,
    agent: str = "claude",
    install_hint: str | None = None,
    pre_args: list[str] | None = None,
) -> int:
    """Run the agent command with RECALL_REPO_PATH set; return its exit code.

    The agent is launched through the user's interactive shell ($SHELL -i -c)
    so rc-file shell functions and aliases (e.g. ~/.bashrc.d/claude) apply,
    matching what typing the command in a terminal does.
    stdin/stdout/stderr are inherited so the interactive TUI works.
    SIGINT/SIGTERM/SIGHUP are forwarded to the child while it runs; handling
    SIGHUP keeps this process alive long enough to tear down the injected
    config when the terminal is closed (default SIGHUP would kill it first).

    pre_args are placed before the caller's argv (e.g. agy's `--add-dir
    <project>`, needed for it to see the injected .agents/ config).
    """
    if not _AGENT_NAME_RE.match(agent):
        raise LauncherError(f"Invalid agent name '{agent}'.")
    shell = _resolve_shell()

    # Probe inside an interactive shell so functions/aliases count too.
    probe = subprocess.run(
        [shell, "-i", "-c", f"command -v {agent}"],
        capture_output=True,
    )
    if probe.returncode != 0:
        raise LauncherError(
            f"'{agent}' not found in your shell environment; "
            f"{install_hint or 'fix your PATH.'}"
        )

    env = {**os.environ, "RECALL_REPO_PATH": str(repo_path)}
    child = subprocess.Popen(
        [shell, "-i", "-c", f'{agent} "$@"', agent, *(pre_args or []), *(argv or [])],
        env=env,
    )

    def forward(signum: int, _frame: FrameType | None) -> None:
        child.send_signal(signum)

    previous = {
        sig: signal.signal(sig, forward)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        returncode = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    # Signal death (negative returncode) maps to the conventional 128+signum.
    return 128 - returncode if returncode < 0 else returncode
