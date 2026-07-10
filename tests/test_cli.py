import json
import os

import pytest
from typer.testing import CliRunner

from recall_engine.cli import app
from recall_engine.mcp_supervisor import ServerInfo

runner = CliRunner()


@pytest.fixture(autouse=True)
def stub_mcp_server(monkeypatch):
    """Keep `wrap`/`unwrap` from spawning a real server or touching the global
    /tmp state file. The per-project MCP-config injection still runs for real."""
    monkeypatch.setattr(
        "recall_engine.cli.ensure_server",
        lambda token=None: ServerInfo(
            url="http://127.0.0.1:9/mcp", port=9, token="testtok", pid=os.getpid()
        ),
    )
    monkeypatch.setattr(
        "recall_engine.cli.release_server",
        lambda owner_pid=None, force=False: False,
    )


def install_fake_claude(tmp_path, monkeypatch, script: str, name: str = "claude") -> None:
    """Put a fake claude shell script on PATH."""
    # Point HOME/SHELL at the sandbox so the real ~/.bashrc is not sourced.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / name
    exe.write_text(f"#!/bin/sh\n{script}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])


def test_wrap_rejects_unknown_agent(monkeypatch, tmp_path):
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'echo "some-other-tool 1.0"',
        name="notclaude",
    )
    result = runner.invoke(app, ["wrap", "notclaude"])
    assert result.exit_code == 2
    assert "does not look like a supported agent CLI" in result.output


def test_wrap_rejects_missing_agent(monkeypatch, tmp_path):
    # Agent command not on PATH at all -> detection fails.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = runner.invoke(app, ["wrap", "no-such-agent"])
    assert result.exit_code == 2
    assert "does not look like a supported agent CLI" in result.output


def test_wrap_detects_claude_wrapper(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    # A wrapper named claude-company that reports itself as Claude Code.
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "--version" ]; then echo "2.0.0 (Claude Code)"; exit 0; fi\n'
        "exit 7",
        name="claude-company",
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "claude-company"])
    assert result.exit_code == 7
    assert "launching claude-company..." in result.output


def test_wrap_pi_blocks_without_adapter(monkeypatch, tmp_path):
    # pi runs but lacks pi-mcp-adapter -> wrap refuses before any injection.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "list" ]; then echo "  npm:pi-web-access"; exit 0; fi\nexit 0',
        name="pi",
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "pi"])
    assert result.exit_code == 2
    assert "pi install pi-mcp-adapter" in result.output
    # Blocked before setup: nothing injected, nothing launched.
    assert "launching pi..." not in result.output
    assert not (project / ".pi" / "mcp.json").exists()


def test_wrap_pi_launches_with_adapter(monkeypatch, tmp_path):
    # pi reports the adapter -> wrap proceeds through the full lifecycle.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(
        tmp_path,
        monkeypatch,
        'if [ "$1" = "list" ]; then echo "  npm:pi-mcp-adapter"; exit 0; fi\nexit 0',
        name="pi",
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "pi"])
    assert result.exit_code == 0
    assert "launching pi..." in result.output
    # Injected .pi/mcp.json is cleaned up on exit.
    assert not (project / ".pi" / "mcp.json").exists()


def test_wrap_claude_repo_error_exits_1(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(tmp_path / "missing"))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    result = runner.invoke(app, ["wrap", "claude"])
    assert result.exit_code == 1


def test_wrap_claude_full_lifecycle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "recall-engine"
    claude_link = project / ".claude" / "skills" / "recall-engine"
    probe = tmp_path / "probe.txt"
    # Fake claude checks the injected skill exists while it runs, then exits 7.
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'if [ -f "{claude_link / "SKILL.md"}" ]; then echo present > "{probe}"; fi\n'
        "exit 7",
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "claude"])
    assert result.exit_code == 7
    assert f"knowledge repo: {repo.resolve()}" in result.output
    assert "launching claude..." in result.output
    # Skill was reachable through the .claude symlink during the child run
    # and cleaned up afterwards.
    assert probe.read_text().strip() == "present"
    assert not skill_dir.exists()
    assert not claude_link.is_symlink()
    assert not (
        project / ".agents" / "skills" / ".recall-engine-marker.json"
    ).exists()


def test_wrap_injects_and_restores_mcp_config(monkeypatch, tmp_path):
    # The agent's MCP config points at the shared server (with the repo header)
    # while it runs, and is cleaned up afterwards. No .knowledge link is created.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    mcp_json = project / ".mcp.json"
    probe = tmp_path / "probe.txt"
    # Fake claude records the injected .mcp.json contents while it runs.
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'cat "{mcp_json}" > "{probe}"\nexit 0',
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "claude"])
    assert result.exit_code == 0
    # The MCP config was present during the child run and pinned the repo header.
    injected = json.loads(probe.read_text())
    entry = injected["mcpServers"]["recall-engine"]
    assert entry["type"] == "http"
    assert entry["url"] == "http://127.0.0.1:9/mcp"
    assert entry["headers"]["X-Recall-Repo"] == str(repo.resolve())
    # ...and it is cleaned up afterwards; no .knowledge link is ever created.
    assert not mcp_json.exists()
    assert not (project / ".knowledge").exists()


def test_wrap_forwards_extra_args_to_agent(monkeypatch, tmp_path):
    # `wrap claude arg1 arg2` must reach claude as `claude arg1 arg2`.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    out = tmp_path / "args.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "$@" > "{out}"\nexit 0')
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "claude", "arg1", "arg2", "--resume"])
    assert result.exit_code == 0
    assert out.read_text().strip() == "arg1 arg2 --resume"


def test_wrap_gemini_full_lifecycle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "recall-engine"
    gemini_link = project / ".gemini" / "skills" / "recall-engine"
    probe = tmp_path / "probe.txt"
    # Fake gemini checks SSOT skill and its own symlink while it runs.
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'if [ -f "{skill_dir / "SKILL.md"}" ] && [ -f "{gemini_link / "SKILL.md"}" ];'
        f' then echo present > "{probe}"; fi\n'
        "exit 0",
        name="gemini",
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "gemini"])
    assert result.exit_code == 0
    assert "launching gemini..." in result.output
    assert probe.read_text().strip() == "present"
    assert not skill_dir.exists()
    assert not gemini_link.is_symlink()


def test_wrap_opencode_full_lifecycle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "recall-engine"
    opencode_link = project / ".opencode" / "skills" / "recall-engine"
    probe = tmp_path / "probe.txt"
    # Fake opencode checks the SSOT skill and its own symlink while it runs.
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'if [ -f "{skill_dir / "SKILL.md"}" ] && [ -f "{opencode_link / "SKILL.md"}" ];'
        f' then echo present > "{probe}"; fi\n'
        "exit 0",
        name="opencode",
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "opencode"])
    assert result.exit_code == 0
    assert "launching opencode..." in result.output
    assert probe.read_text().strip() == "present"
    assert not skill_dir.exists()
    assert not opencode_link.is_symlink()


def test_wrap_agy_full_lifecycle(monkeypatch, tmp_path):
    # agy reads .agents/skills directly, so no agent-specific symlink is made.
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "recall-engine"
    probe = tmp_path / "probe.txt"
    install_fake_claude(
        tmp_path,
        monkeypatch,
        f'if [ -f "{skill_dir / "SKILL.md"}" ]; then echo present > "{probe}"; fi\n'
        "exit 0",
        name="agy",
    )
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "agy"])
    assert result.exit_code == 0
    assert "launching agy..." in result.output
    # Skill was reachable directly at the SSOT during the child run; agy needs
    # no symlink of its own.
    assert probe.read_text().strip() == "present"
    assert not (project / ".agy").exists()
    assert not skill_dir.exists()


def test_wrap_agy_launched_with_add_dir(monkeypatch, tmp_path):
    # agy only reads the injected .agents/ config when the project dir is in its
    # workspace, so wrap must launch it with `--add-dir <project>`.
    from pathlib import Path

    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    out = tmp_path / "args.txt"
    install_fake_claude(tmp_path, monkeypatch, f'echo "$@" > "{out}"\nexit 0', name="agy")
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "agy", "chat"])
    assert result.exit_code == 0
    parts = out.read_text().split()
    assert parts[0] == "--add-dir"
    assert Path(parts[1]).resolve() == project.resolve()
    assert parts[2] == "chat"  # user args still forwarded, after the pre-args


def test_wrap_claude_attaches_to_live_session(monkeypatch, tmp_path):
    import subprocess
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    # A first live session already injected the skill for the SAME repo.
    from recall_engine.skill import inject_skill

    inject_skill(repo)
    marker = project / ".agents" / "skills" / ".recall-engine-marker.json"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))

        result = runner.invoke(app, ["wrap", "claude"])
        assert result.exit_code == 0            # attached, not refused
        assert marker.exists()                  # other session survives
        assert other.pid in json.loads(marker.read_text())["pids"]
    finally:
        other.terminate()
        other.wait()


def test_wrap_auto_detects_repo_from_live_session(monkeypatch, tmp_path):
    # Second wrap in the same project inherits the running session's repo
    # without KNOWLEDGE_REPO_PATH being set.
    import subprocess
    import sys

    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.delenv("KNOWLEDGE_REPO_PATH", raising=False)
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    from recall_engine.skill import inject_skill

    inject_skill(repo)  # a first session set up the injection for `repo`
    marker = project / ".agents" / "skills" / ".recall-engine-marker.json"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))

        # No repo env var: the wrapper must auto-detect `repo` and attach.
        result = runner.invoke(app, ["wrap", "claude"])
        assert result.exit_code == 0
        assert f"knowledge repo: {repo.resolve()}" in result.output
        assert other.pid in json.loads(marker.read_text())["pids"]
    finally:
        other.terminate()
        other.wait()


def test_wrap_without_config_and_no_session_exits_2(monkeypatch, tmp_path):
    # No repo env var and no live session -> the config error still fires.
    project = tmp_path / "project"
    project.mkdir()
    install_fake_claude(tmp_path, monkeypatch, "exit 0")
    monkeypatch.delenv("KNOWLEDGE_REPO_PATH", raising=False)
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "claude"])
    assert result.exit_code == 2
    assert "KNOWLEDGE_REPO_PATH or KNOWLEDGE_REPO_SSH" in result.output


def test_wrap_claude_missing_claude_restores_and_exits_1(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(repo))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.chdir(project)

    result = runner.invoke(app, ["wrap", "claude"])
    assert result.exit_code == 1
    # Injection was rolled back on the launcher error path.
    assert not (project / ".agents" / "skills" / "recall-engine").exists()
    assert not (
        project / ".agents" / "skills" / ".recall-engine-marker.json"
    ).exists()
    assert not (
        project / ".claude" / "skills" / "recall-engine"
    ).is_symlink()


def test_unwrap_cleans_stale_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    skill_dir = tmp_path / ".agents" / "skills" / "recall-engine"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("leftover\n")
    marker = tmp_path / ".agents" / "skills" / ".recall-engine-marker.json"
    marker.write_text(json.dumps({"pid": 1, "backup": None}))

    result = runner.invoke(app, ["unwrap"])
    assert result.exit_code == 0
    assert "restored leftover skill and MCP state" in result.output
    assert not skill_dir.exists()
    assert not marker.exists()


def test_unwrap_with_nothing_to_clean(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["unwrap"])
    assert result.exit_code == 0
    assert "nothing to clean" in result.output


def test_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("wrap", "unwrap", "sync", "doctor"):
        assert command in result.output
