import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest
from recall_engine.launcher import (
    LauncherError,
    detect_agent,
    launch_agent,
    pi_mcp_adapter_installed,
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
def test_launch_agent_forwards_env_args_pre_args_and_exit_code(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    env_out = tmp_path / "env.txt"
    args_out = tmp_path / "args.txt"
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'echo "$RECALL_REPO_PATH" > "{env_out}"\n'
        f'echo "$@" > "{args_out}"\n'
        "exit 7",
        name="claude-company",
    )
    assert (
        launch_agent(
            repo,
            ["--resume", "x"],
            agent="claude-company",
            pre_args=["--add-dir", "/proj"],
        )
        == 7
    )
    assert env_out.read_text().strip() == str(repo)
    assert args_out.read_text().strip() == "--add-dir /proj --resume x"
def test_shell_function_takes_priority_over_binary(tmp_path, monkeypatch):
    # Regression: a claude() function in the rc file must win over the
    # PATH binary, like typing `claude` in a real terminal.
    out = tmp_path / "who.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "binary $@" > "{out}"')
    bashrc = Path(os.environ["HOME"]) / ".bashrc"
    bashrc.write_text(f'claude() {{ echo "function $@" > "{out}"; }}\n')
    launch_agent(tmp_path, ["--flag"])
    assert out.read_text().strip() == "function --flag"
def test_rc_path_prepend_cannot_shadow_the_inherited_path(tmp_path, monkeypatch):
    # Regression: a re-sourced rc file must not reorder PATH ahead of the
    # terminal environment that launched recall-engine.
    out = tmp_path / "node.txt"
    bin_dir = install_fake_claude(tmp_path, monkeypatch, "exit 0")
    real = bin_dir / "node"
    real.write_text('#!/bin/sh\necho REAL\n')
    real.chmod(0o755)
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    stub = shadow_dir / "node"
    stub.write_text('#!/bin/sh\necho STUB\n')
    stub.chmod(0o755)
    # Terminal PATH: the real node wins, the shadow dir trails behind it.
    monkeypatch.setenv("PATH", os.environ["PATH"] + os.pathsep + str(shadow_dir))
    bashrc = Path(os.environ["HOME"]) / ".bashrc"
    bashrc.write_text(
        f'export PATH="{shadow_dir}:$PATH"\n'
        f'claude() {{ node > "{out}"; }}\n'
    )
    launch_agent(tmp_path)
    assert out.read_text().strip() == "REAL"


def test_bash_file_command_launches_directly_without_interactive_shell(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "launch.txt"
    pi = tmp_path / "pi"
    pi.write_text(f'#!/bin/sh\necho direct > "{out}"\n')
    pi.chmod(0o755)
    fake_bash = tmp_path / "bash"
    fake_bash.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+m" ] && [ "$2" = "-i" ] && [ "$3" = "-c" ]; then\n'
        '  if [ "$4" = "command -v pi" ]; then exit 0; fi\n'
        '  case "$4" in\n'
        '    *"type -t pi"*)\n'
        f'    printf "startup noise\\nfile\\n%s\\n" "{pi}"\n'
        "    exit 0\n"
        "    ;;\n"
        "  esac\n"
        f'  echo interactive-shell > "{out}"\n'
        "  exit 42\n"
        "fi\n"
        "exit 99\n"
    )
    fake_bash.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_bash))
    monkeypatch.setenv("PATH", str(tmp_path))

    assert launch_agent(repo, agent="pi") == 0
    assert out.read_text().strip() == "direct"


def test_pi_adapter_probe_preserves_inherited_path_priority(tmp_path, monkeypatch):
    # Regression: the probe must use the same pi that the current terminal
    # would run, even if the rc file prepends another pi on shell startup.
    install_fake_pi(tmp_path, monkeypatch, "User packages: npm:pi-mcp-adapter")
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    stub = shadow_dir / "pi"
    stub.write_text('#!/bin/sh\nif [ "$1" = "list" ]; then echo "User packages:"; fi\n')
    stub.chmod(0o755)
    # Terminal PATH: the real pi wins, the shadow dir trails behind it.
    monkeypatch.setenv("PATH", os.environ["PATH"] + os.pathsep + str(shadow_dir))
    bashrc = Path(os.environ["HOME"]) / ".bashrc"
    bashrc.write_text(f'export PATH="{shadow_dir}:$PATH"\n')

    assert pi_mcp_adapter_installed("pi") is True


def test_pi_adapter_probe_runs_file_command_directly(tmp_path, monkeypatch):
    out = tmp_path / "probe.txt"
    pi = tmp_path / "pi"
    pi.write_text(
        "#!/bin/sh\n"
        f'echo "$1" > "{out}"\n'
        'if [ "$1" = "list" ]; then echo "npm:pi-mcp-adapter"; fi\n'
    )
    pi.chmod(0o755)
    fake_bash = tmp_path / "bash"
    fake_bash.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "+m" ] && [ "$2" = "-i" ] && [ "$3" = "-c" ]; then\n'
        '  case "$4" in\n'
        '    *"type -t pi"*)\n'
        f'      printf "file\\n%s\\n" "{pi}"\n'
        "      exit 0\n"
        "      ;;\n"
        '    *"pi list"*)\n'
        f'      echo shell-list > "{out}"\n'
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        "exit 99\n"
    )
    fake_bash.chmod(0o755)
    monkeypatch.setenv("SHELL", str(fake_bash))
    monkeypatch.setenv("PATH", str(tmp_path))

    assert pi_mcp_adapter_installed("pi") is True
    assert out.read_text().strip() == "list"
def test_missing_claude_raises(tmp_path, monkeypatch):
    isolate_shell(tmp_path, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(LauncherError, match="not found in your shell"):
        launch_agent(tmp_path)
def test_launch_rejects_unsafe_agent_name(tmp_path, monkeypatch):
    isolate_shell(tmp_path, monkeypatch)
    with pytest.raises(LauncherError, match="Invalid agent name"):
        launch_agent(tmp_path, agent="claude; rm -rf /")
def test_detect_agent_supported_matrix_including_shell_function(tmp_path, monkeypatch):
    cases = (
        ("claude-company", 'echo "2.0.0 (Claude Code)"', "claude"),
        ("my-codex-wrapper", 'echo "codex-cli 0.144.1"', "codex"),
        ("pi-company", 'echo "0.1.0"', "pi"),
        ("gemini-company", 'echo "0.8.1"', "gemini"),
        ("opencode-company", 'echo "1.17.18"', "opencode"),
        ("agy-company", 'echo "1.1.1"', "agy"),
    )
    for name, script, expected in cases:
        install_fake_claude(tmp_path, monkeypatch, script, name=name)
        assert detect_agent(name) == expected
    isolate_shell(tmp_path, monkeypatch)
    empty = tmp_path / "function-only-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    bashrc = Path(os.environ["HOME"]) / ".bashrc"
    bashrc.write_text('shell-function-agent() { echo "2.0.0 (Claude Code)"; }\n')
    assert detect_agent("shell-function-agent") == "claude"
def test_detect_agent_rejects_unknown_missing_and_unsafe_names(tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, 'echo "25.0"', name="pip")
    assert detect_agent("pip") is None
    install_fake_claude(
        tmp_path, monkeypatch, 'echo "some-other-tool 1.0"', name="othertool"
    )
    assert detect_agent("othertool") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert detect_agent("no-such-agent") is None
    assert detect_agent("claude; rm -rf /") is None
def install_fake_pi(tmp_path, monkeypatch, list_output: str, name: str = "pi") -> Path:
    """Put a fake pi on PATH whose `list` subcommand prints list_output."""
    bin_dir = install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "list" ]; then\n'
        f"  printf '%s\\n' '{list_output}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0",
        name=name,
    )
    return bin_dir
def test_pi_mcp_adapter_detection_matrix(tmp_path, monkeypatch):
    install_fake_pi(tmp_path, monkeypatch, "User packages:  npm:pi-mcp-adapter")
    assert pi_mcp_adapter_installed("pi") is True
    install_fake_pi(tmp_path, monkeypatch, "User packages:  npm:pi-web-access")
    assert pi_mcp_adapter_installed("pi") is False
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert pi_mcp_adapter_installed("pi") is None
    assert pi_mcp_adapter_installed("pi; rm -rf /") is None
def test_sighup_does_not_kill_wrapper_before_teardown(tmp_path, monkeypatch):
    # Closing the terminal sends SIGHUP. Default SIGHUP would terminate the
    # wrapper before wrap's finally can restore the injected config; launch_agent
    # must catch/forward it so the process survives to tear down. Run in a
    # subprocess so a regression fails cleanly instead of hangup-killing pytest.
    started = tmp_path / "started"
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f"trap '' HUP INT TERM\necho started > \"{started}\"\nsleep 3\n",
    )
    runner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from pathlib import Path\n"
            "from recall_engine.launcher import launch_agent\n"
            "sys.exit(launch_agent(Path(sys.argv[1]), agent='claude'))",
            str(tmp_path / "repo"),
        ],
        env=os.environ.copy(),
    )
    try:
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        assert started.exists(), "fake agent never started"
        runner.send_signal(signal.SIGHUP)
        returncode = runner.wait(timeout=15)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
    # Survived SIGHUP (would be -SIGHUP if the default handler had killed it).
    assert returncode != -signal.SIGHUP
