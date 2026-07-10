from pathlib import Path
from unittest.mock import MagicMock

from typer.testing import CliRunner

from recall_engine import doctor
from recall_engine.cli import app
from recall_engine.doctor import run_doctor
from recall_engine.drive import AUTH_HELP, DriveError
from recall_engine.mcp_supervisor import ServerStatus

runner = CliRunner()

GCLOUD_COMMAND = (
    "gcloud auth application-default login "
    "--scopes=https://www.googleapis.com/auth/drive,"
    "https://www.googleapis.com/auth/cloud-platform"
)


def install_passing_env(monkeypatch, tmp_path) -> None:
    """Make every check pass without touching the real environment."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Never read the host's real /tmp state file: a wrap session running on the
    # dev machine would otherwise leak into the assertions.
    monkeypatch.setattr(doctor, "server_status", lambda: None)
    key = tmp_path / "id_ed25519"
    key.write_text("fake key\n")
    monkeypatch.setenv("SSH_KEY", str(key))
    monkeypatch.setenv("KNOWLEDGE_REPO_PATH", str(tmp_path))
    monkeypatch.delenv("KNOWLEDGE_REPO_SSH", raising=False)
    monkeypatch.setenv("KNOWLEDGE_DRIVE_FOLDER", "folder123")
    monkeypatch.setattr(doctor, "build_drive_service", lambda: MagicMock())
    monkeypatch.setattr(doctor, "execute", lambda request: {"files": []})


def test_all_checks_pass(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)

    assert run_doctor() is True
    output = capsys.readouterr().out
    for name in (
        "git",
        "claude",
        "codex",
        "pi",
        "gemini",
        "opencode",
        "agy",
        "ssh key",
        "repo config",
        "gcloud auth",
    ):
        assert f"[ok] {name}:" in output
    assert "[ok] drive folder: folder123" in output
    assert "[fail]" not in output


def test_single_agent_present_passes_with_skips(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("git", "codex") else None,
    )

    assert run_doctor() is True
    output = capsys.readouterr().out
    assert "[ok] codex:" in output
    for name in ("claude", "pi", "gemini", "opencode", "agy"):
        assert f"[skip] {name}: not found on PATH" in output
    assert "[fail]" not in output


def test_all_agents_missing_fails_and_exits_1(monkeypatch, tmp_path):
    install_passing_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        doctor.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "git" else None,
    )

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert (
        "[fail] agent CLIs: none of claude/codex/pi/gemini/opencode/agy found"
        in result.output
    )
    assert "https://claude.com/claude-code" in result.output


def test_no_ssh_key_fails_with_searched_paths(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)
    monkeypatch.delenv("SSH_KEY")
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: empty_home)

    assert run_doctor() is False
    output = capsys.readouterr().out
    assert "[fail] ssh key" in output
    assert str(empty_home / ".ssh" / "id_ed25519") in output
    assert str(empty_home / ".ssh" / "id_rsa") in output


def test_repo_config_missing_fails(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)
    monkeypatch.delenv("KNOWLEDGE_REPO_PATH")

    assert run_doctor() is False
    output = capsys.readouterr().out
    assert "[fail] repo config" in output
    assert "KNOWLEDGE_REPO_PATH or KNOWLEDGE_REPO_SSH" in output


def test_no_credentials_fails_with_gcloud_command(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)

    def raise_credentials_missing():
        raise DriveError(AUTH_HELP)

    monkeypatch.setattr(doctor, "build_drive_service", raise_credentials_missing)

    assert run_doctor() is False
    output = capsys.readouterr().out
    assert "[fail] gcloud auth" in output
    assert GCLOUD_COMMAND in output


def test_unexpected_drive_error_fails_without_traceback(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)

    def raise_network_error(request):
        raise OSError("network unreachable")

    monkeypatch.setattr(doctor, "execute", raise_network_error)

    assert run_doctor() is False
    output = capsys.readouterr().out
    assert "[fail] gcloud auth" in output
    assert "network unreachable" in output
    assert "Traceback" not in output


def test_no_running_mcp_server_is_skip_not_fail(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)

    assert run_doctor() is True
    output = capsys.readouterr().out
    assert "[skip] mcp server: not running (started on demand by `wrap`)" in output
    assert "[fail]" not in output


def test_reachable_mcp_server_reports_url_and_owners(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)
    status = ServerStatus(
        url="http://127.0.0.1:4321/mcp", pid=999, owners=[111, 222], reachable=True
    )
    monkeypatch.setattr(doctor, "server_status", lambda: status)

    assert run_doctor() is True
    output = capsys.readouterr().out
    assert "[ok] mcp server: reachable at http://127.0.0.1:4321/mcp" in output
    assert "pid 999, 2 owner(s)" in output


def test_stale_mcp_server_state_fails_with_unwrap_hint(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)
    status = ServerStatus(
        url="http://127.0.0.1:4321/mcp", pid=999, owners=[111], reachable=False
    )
    monkeypatch.setattr(doctor, "server_status", lambda: status)

    assert run_doctor() is False
    output = capsys.readouterr().out
    assert "[fail] mcp server" in output
    assert "stale state" in output
    assert "recall-engine unwrap" in output


def test_drive_folder_unset_is_skip_not_fail(monkeypatch, tmp_path, capsys):
    install_passing_env(monkeypatch, tmp_path)
    monkeypatch.delenv("KNOWLEDGE_DRIVE_FOLDER")

    assert run_doctor() is True
    output = capsys.readouterr().out
    assert (
        "[skip] drive folder: KNOWLEDGE_DRIVE_FOLDER not set (needed only for sync)"
        in output
    )
