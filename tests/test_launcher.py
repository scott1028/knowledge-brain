import os
from pathlib import Path

import pytest

from recall_engine.launcher import (
    LauncherError,
    detect_agent,
    launch_agent,
)


def isolate_shell(tmp_path, monkeypatch) -> None:
    """Point HOME/SHELL at the sandbox so the real ~/.bashrc is not sourced."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")


def install_fake_claude(
    tmp_path, monkeypatch, script: str, name: str = "claude"
) -> Path:
    """Put a fake claude shell script on PATH; return its bin dir."""
    isolate_shell(tmp_path, monkeypatch)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return bin_dir


def test_exit_code_zero(tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    assert launch_agent(tmp_path) == 0


def test_exit_code_propagated(tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, "exit 7")
    assert launch_agent(tmp_path) == 7


def test_env_propagation(tmp_path, monkeypatch):
    out = tmp_path / "env.txt"
    install_fake_claude(
        tmp_path, monkeypatch, f'echo "$RECALL_REPO_PATH" > "{out}"'
    )
    repo = tmp_path / "repo"
    launch_agent(repo)
    assert out.read_text().strip() == str(repo)


def test_extra_argv_forwarded(tmp_path, monkeypatch):
    out = tmp_path / "args.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "$@" > "{out}"')
    launch_agent(tmp_path, ["--resume", "abc"])
    assert out.read_text().strip() == "--resume abc"


def test_pre_args_precede_user_argv(tmp_path, monkeypatch):
    # pre_args (e.g. agy's --add-dir <project>) must come before the user's argv.
    out = tmp_path / "args.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "$@" > "{out}"')
    launch_agent(
        tmp_path, ["--resume", "x"], pre_args=["--add-dir", "/proj"]
    )
    assert out.read_text().strip() == "--add-dir /proj --resume x"


def test_shell_function_takes_priority_over_binary(tmp_path, monkeypatch):
    # Regression: a claude() function in the rc file must win over the
    # PATH binary, like typing `claude` in a real terminal.
    out = tmp_path / "who.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "binary $@" > "{out}"')
    bashrc = Path(os.environ["HOME"]) / ".bashrc"
    bashrc.write_text(f'claude() {{ echo "function $@" > "{out}"; }}\n')
    launch_agent(tmp_path, ["--flag"])
    assert out.read_text().strip() == "function --flag"


def test_missing_claude_raises(tmp_path, monkeypatch):
    isolate_shell(tmp_path, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(LauncherError, match="not found in your shell"):
        launch_agent(tmp_path)


def test_launch_custom_agent_name(tmp_path, monkeypatch):
    out = tmp_path / "who.txt"
    install_fake_claude(
        tmp_path, monkeypatch, f'echo "company $@" > "{out}"', name="claude-company"
    )
    assert launch_agent(tmp_path, ["--flag"], agent="claude-company") == 0
    assert out.read_text().strip() == "company --flag"


def test_launch_rejects_unsafe_agent_name(tmp_path, monkeypatch):
    isolate_shell(tmp_path, monkeypatch)
    with pytest.raises(LauncherError, match="Invalid agent name"):
        launch_agent(tmp_path, agent="claude; rm -rf /")


def test_detect_agent_claude_by_version_signature(tmp_path, monkeypatch):
    install_fake_claude(
        tmp_path, monkeypatch, 'echo "2.0.0 (Claude Code)"', name="claude-company"
    )
    assert detect_agent("claude-company") == "claude"


def test_detect_agent_codex_by_version_signature(tmp_path, monkeypatch):
    install_fake_claude(
        tmp_path, monkeypatch, 'echo "codex-cli 0.144.1"', name="my-codex-wrapper"
    )
    assert detect_agent("my-codex-wrapper") == "codex"


def test_detect_agent_gemini_by_name_token(tmp_path, monkeypatch):
    # gemini --version prints a bare version number; the name token decides.
    install_fake_claude(
        tmp_path, monkeypatch, 'echo "0.8.1"', name="gemini-company"
    )
    assert detect_agent("gemini-company") == "gemini"


def test_detect_agent_opencode_by_name_token(tmp_path, monkeypatch):
    # opencode --version prints a bare version number; the name token decides.
    install_fake_claude(
        tmp_path, monkeypatch, 'echo "1.17.18"', name="opencode-company"
    )
    assert detect_agent("opencode-company") == "opencode"


def test_detect_agent_agy_by_name_token(tmp_path, monkeypatch):
    # agy --version prints a bare version number; the name token decides.
    install_fake_claude(tmp_path, monkeypatch, 'echo "1.1.1"', name="agy-company")
    assert detect_agent("agy-company") == "agy"


def test_detect_agent_ignores_pi_substring_in_name(tmp_path, monkeypatch):
    # "pip" must not be classified as a pi wrapper.
    install_fake_claude(tmp_path, monkeypatch, 'echo "25.0"', name="pip")
    assert detect_agent("pip") is None


def test_detect_agent_none_on_other_tool(tmp_path, monkeypatch):
    install_fake_claude(
        tmp_path, monkeypatch, 'echo "some-other-tool 1.0"', name="othertool"
    )
    assert detect_agent("othertool") is None


def test_detect_agent_none_on_missing_command(tmp_path, monkeypatch):
    isolate_shell(tmp_path, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert detect_agent("no-such-agent") is None


def test_detect_agent_none_on_unsafe_name(tmp_path, monkeypatch):
    isolate_shell(tmp_path, monkeypatch)
    assert detect_agent("claude; rm -rf /") is None


def test_detect_agent_via_shell_function(tmp_path, monkeypatch):
    # A wrapper defined as a shell function must also be detectable.
    isolate_shell(tmp_path, monkeypatch)
    bashrc = Path(os.environ["HOME"]) / ".bashrc"
    bashrc.write_text(
        'claude-company() { echo "2.0.0 (Claude Code)"; }\n'
    )
    assert detect_agent("claude-company") == "claude"
