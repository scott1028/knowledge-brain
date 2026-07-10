import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from recall_engine.agents import AGENTS
from recall_engine.mcp_config import (
    McpConfigError,
    inject_mcp_config,
    restore_mcp_config,
)

URL = "http://127.0.0.1:8765/mcp"
TOKEN = "secret-token"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Run each test inside a fresh project cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def marker_path(project: Path) -> Path:
    return project / ".agents" / "skills" / ".recall-engine-mcp-marker.json"


def config_path(project: Path, agent: str) -> Path:
    return project / AGENTS[agent].mcp.config_path


def dead_pid() -> int:
    """Spawn a short-lived process and return its pid after it exits."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def live_pid():
    """Spawn a long-lived process; caller must terminate it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


# --- 1. JSON round-trips per agent ------------------------------------------


def test_claude_json_roundtrip(project):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL, token=TOKEN)

    path = config_path(project, "claude")
    data = json.loads(path.read_text())
    assert data == {
        "mcpServers": {
            "recall-engine": {
                "url": URL,
                "headers": {
                    "X-Recall-Repo": str(repo),
                    "X-Recall-Token": TOKEN,
                },
                "type": "http",
            }
        }
    }

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert not path.exists()  # file didn't exist before -> removed
    assert not marker_path(project).exists()


def test_gemini_uses_http_url_field(project):
    repo = project / "repo"
    inject_mcp_config("gemini", repo, URL, token=TOKEN)

    path = config_path(project, "gemini")
    entry = json.loads(path.read_text())["mcpServers"]["recall-engine"]
    assert entry["httpUrl"] == URL
    assert "url" not in entry
    assert entry["headers"]["X-Recall-Repo"] == str(repo)
    assert "type" not in entry

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert not path.exists()


def test_opencode_remote_under_mcp_key(project):
    repo = project / "repo"
    inject_mcp_config("opencode", repo, URL, token=TOKEN)

    path = config_path(project, "opencode")
    data = json.loads(path.read_text())
    entry = data["mcp"]["recall-engine"]
    assert entry["url"] == URL
    assert entry["type"] == "remote"
    assert entry["enabled"] is True
    assert entry["headers"]["X-Recall-Token"] == TOKEN

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert not path.exists()


def test_agy_uses_server_url_field(project):
    repo = project / "repo"
    inject_mcp_config("agy", repo, URL, token=TOKEN)

    path = config_path(project, "agy")
    entry = json.loads(path.read_text())["mcpServers"]["recall-engine"]
    assert entry["serverUrl"] == URL
    assert "url" not in entry
    assert entry["headers"]["X-Recall-Repo"] == str(repo)

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert not path.exists()


def test_pi_writes_dot_pi_mcp_json(project):
    repo = project / "repo"
    inject_mcp_config("pi", repo, URL, token=TOKEN)

    path = config_path(project, "pi")
    assert path == project / ".pi" / "mcp.json"
    entry = json.loads(path.read_text())["mcpServers"]["recall-engine"]
    assert entry["url"] == URL
    assert entry["headers"]["X-Recall-Repo"] == str(repo)
    # pi-mcp-adapter only auto-connects servers with lifecycle keep-alive/eager.
    assert entry["lifecycle"] == "keep-alive"
    # directTools exposes the server's tools directly instead of via the proxy.
    assert entry["directTools"] is True

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert not path.exists()


# --- 2. TOML round-trip (codex) ---------------------------------------------


def test_codex_toml_roundtrip(project):
    repo = project / "repo"
    inject_mcp_config("codex", repo, URL, token=TOKEN)

    path = config_path(project, "codex")
    assert path == project / ".codex" / "config.toml"
    data = tomllib.loads(path.read_text())
    server = data["mcp_servers"]["recall-engine"]
    assert server["url"] == URL
    assert server["http_headers"] == {
        "X-Recall-Repo": str(repo),
        "X-Recall-Token": TOKEN,
    }

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert not path.exists()


def test_codex_toml_preserves_user_content(project):
    repo = project / "repo"
    path = config_path(project, "codex")
    path.parent.mkdir(parents=True)
    original = '# my codex config\nmodel = "gpt-5"\n\n[tui]\ntheme = "dark"\n'
    path.write_text(original)

    inject_mcp_config("codex", repo, URL)
    data = tomllib.loads(path.read_text())
    # User content preserved alongside our server table.
    assert data["model"] == "gpt-5"
    assert data["tui"]["theme"] == "dark"
    assert data["mcp_servers"]["recall-engine"]["url"] == URL

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert path.read_text() == original


# --- 3. Pre-existing JSON config backed up and fully restored ---------------


def test_preexisting_json_backed_up_and_restored(project):
    repo = project / "repo"
    path = config_path(project, "claude")
    original = json.dumps({"mcpServers": {"other": {"url": "http://other"}}}, indent=2)
    path.write_text(original)

    inject_mcp_config("claude", repo, URL, token=TOKEN)
    data = json.loads(path.read_text())
    # Our entry added alongside the user's own server.
    assert data["mcpServers"]["other"] == {"url": "http://other"}
    assert data["mcpServers"]["recall-engine"]["url"] == URL

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert path.read_text() == original  # byte-identical restore
    backup = Path(str(path) + ".recall-engine-mcp-backup")
    assert not backup.exists()


# --- 4. token=None omits X-Recall-Token -------------------------------------


def test_token_none_omits_token_header(project):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL)

    entry = json.loads(config_path(project, "claude").read_text())["mcpServers"][
        "recall-engine"
    ]
    assert entry["headers"] == {"X-Recall-Repo": str(repo)}
    assert "X-Recall-Token" not in entry["headers"]


def test_token_none_omits_token_header_toml(project):
    repo = project / "repo"
    inject_mcp_config("codex", repo, URL)

    server = tomllib.loads(config_path(project, "codex").read_text())["mcp_servers"][
        "recall-engine"
    ]
    assert server["http_headers"] == {"X-Recall-Repo": str(repo)}


# --- 5. Refcount across multiple owners -------------------------------------


def test_attach_same_repo_adds_pid_no_duplicate_backup(project):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL, token=TOKEN)  # first owner
    marker = marker_path(project)
    path = config_path(project, "claude")
    backup = Path(str(path) + ".recall-engine-mcp-backup")
    assert not backup.exists()  # nothing pre-existed -> no backup

    other = live_pid()
    try:
        # Simulate another live wrap session owning the injection.
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))

        inject_mcp_config("claude", repo, URL, token=TOKEN)  # attach
        pids = json.loads(marker.read_text())["pids"]
        assert other.pid in pids and os.getpid() in pids
        # Same agent already recorded -> exactly one config, no extra backup.
        configs = json.loads(marker.read_text())["configs"]
        assert len(configs) == 1
        assert not backup.exists()
    finally:
        other.terminate()
        other.wait()


def test_attach_reasserts_entry_to_patch_missing_fields(project):
    # An older recall-engine could have injected a pi entry without `lifecycle`.
    # Attaching to that live session must re-assert the current entry so the
    # missing field is patched in, without creating a duplicate backup.
    repo = project / "repo"
    inject_mcp_config("pi", repo, URL, token=TOKEN)  # first owner
    marker = marker_path(project)
    path = config_path(project, "pi")
    backup = Path(str(path) + ".recall-engine-mcp-backup")

    # Simulate an old-version entry on disk (no lifecycle field).
    data = json.loads(path.read_text())
    del data["mcpServers"]["recall-engine"]["lifecycle"]
    path.write_text(json.dumps(data))

    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))

        inject_mcp_config("pi", repo, URL, token=TOKEN)  # attach -> re-assert
        entry = json.loads(path.read_text())["mcpServers"]["recall-engine"]
        assert entry["lifecycle"] == "keep-alive"  # patched back in
        # Still one config, and no backup was created on the re-assert.
        assert len(json.loads(marker.read_text())["configs"]) == 1
        assert not backup.exists()
    finally:
        other.terminate()
        other.wait()


def test_first_owner_leaving_keeps_config_then_last_restores(project):
    repo = project / "repo"
    path = config_path(project, "claude")
    original = json.dumps({"mcpServers": {"other": {"url": "http://other"}}}, indent=2)
    path.write_text(original)

    inject_mcp_config("claude", repo, URL, token=TOKEN)  # backs up user config
    marker = marker_path(project)
    backup = Path(str(path) + ".recall-engine-mcp-backup")

    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = sorted({os.getpid(), other.pid})
        marker.write_text(json.dumps(record))

        # First owner (us) leaves while `other` is alive -> keep injection.
        assert restore_mcp_config(owner_pid=os.getpid()) is True
        data = json.loads(path.read_text())
        assert "recall-engine" in data["mcpServers"]  # still injected
        assert backup.exists()
        assert json.loads(marker.read_text())["pids"] == [other.pid]
    finally:
        other.terminate()
        other.wait()

    # Last owner leaves (its pid now dead) -> full teardown restores user config.
    assert restore_mcp_config(owner_pid=other.pid) is True
    assert path.read_text() == original
    assert not marker.exists()
    assert not backup.exists()


def test_second_agent_attaches_with_its_own_config(project):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL, token=TOKEN)  # first owner, claude
    marker = marker_path(project)

    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid, os.getpid()]
        marker.write_text(json.dumps(record))

        # A second agent (codex) wraps the same dir/repo -> its config is added.
        inject_mcp_config("codex", repo, URL, token=TOKEN)
        configs = json.loads(marker.read_text())["configs"]
        agents = {c["agent"] for c in configs}
        assert agents == {"claude", "codex"}
        assert config_path(project, "codex").exists()
    finally:
        other.terminate()
        other.wait()

    # Last owner teardown removes both config files.
    assert restore_mcp_config(force=True) is True
    assert not config_path(project, "claude").exists()
    assert not config_path(project, "codex").exists()


# --- 6. Different-repo attach refused ----------------------------------------


def test_attach_refused_when_repo_differs(project):
    inject_mcp_config("claude", project / "repo-a", URL)
    marker = marker_path(project)
    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        with pytest.raises(McpConfigError, match="different"):
            inject_mcp_config("claude", project / "repo-b", URL)
    finally:
        other.terminate()
        other.wait()


# --- 7. Robust handling of pre-existing / malformed configs ------------------


def test_preexisting_jsonc_comments_tolerated_and_restored(project):
    # gemini/opencode allow // and /* */ comments; injection must not choke.
    repo = project / "repo"
    path = config_path(project, "gemini")
    path.parent.mkdir(parents=True)
    original = '{\n  // my gemini settings\n  "theme": "dark"\n}\n'
    path.write_text(original)

    inject_mcp_config("gemini", repo, URL, token=TOKEN)
    data = json.loads(path.read_text())  # injected file is plain, valid JSON
    assert data["theme"] == "dark"
    assert data["mcpServers"]["recall-engine"]["httpUrl"] == URL

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert path.read_text() == original  # comments restored byte-identical


def test_empty_preexisting_config_tolerated_and_restored(project):
    repo = project / "repo"
    path = config_path(project, "claude")
    path.write_text("")  # an empty .mcp.json must not crash json.loads

    inject_mcp_config("claude", repo, URL)
    assert json.loads(path.read_text())["mcpServers"]["recall-engine"]["url"] == URL

    assert restore_mcp_config(owner_pid=os.getpid()) is True
    assert path.read_text() == ""


def test_malformed_json_raises_clean_error_and_preserves_file(project):
    repo = project / "repo"
    path = config_path(project, "claude")
    original = '{"mcpServers": oops'
    path.write_text(original)

    with pytest.raises(McpConfigError, match="not valid JSON"):
        inject_mcp_config("claude", repo, URL)
    # Original untouched; no marker and no stray backup left behind.
    assert path.read_text() == original
    assert not marker_path(project).exists()
    assert not Path(str(path) + ".recall-engine-mcp-backup").exists()


def test_wrong_shape_json_raises_clean_error(project):
    repo = project / "repo"
    path = config_path(project, "claude")
    path.write_text("[]")  # valid JSON but an array, not an object

    with pytest.raises(McpConfigError, match="not a JSON object"):
        inject_mcp_config("claude", repo, URL)
    assert path.read_text() == "[]"


def test_malformed_toml_raises_clean_error_and_preserves_file(project):
    repo = project / "repo"
    path = config_path(project, "codex")
    path.parent.mkdir(parents=True)
    original = 'model = "unterminated\n'
    path.write_text(original)

    with pytest.raises(McpConfigError, match="not valid TOML"):
        inject_mcp_config("codex", repo, URL)
    assert path.read_text() == original
    assert not Path(str(path) + ".recall-engine-mcp-backup").exists()


# --- Misc -------------------------------------------------------------------


def test_restore_with_nothing_returns_false(project):
    assert restore_mcp_config() is False


def test_stale_marker_all_dead_reinjected_fresh(project, capsys):
    repo = project / "repo"
    inject_mcp_config("claude", repo, URL, token=TOKEN)
    marker = marker_path(project)
    record = json.loads(marker.read_text())
    record["pids"] = [dead_pid()]
    marker.write_text(json.dumps(record))

    inject_mcp_config("claude", repo, URL, token=TOKEN)  # stale -> clean + fresh
    assert "stale wrap session" in capsys.readouterr().err
    assert json.loads(marker.read_text())["pids"] == [os.getpid()]
    entry = json.loads(config_path(project, "claude").read_text())["mcpServers"][
        "recall-engine"
    ]
    assert entry["url"] == URL


def test_no_mcp_spec_is_noop(project, monkeypatch):
    # An agent whose spec lacks .mcp must be a no-op (no marker, no error).
    monkeypatch.setitem(AGENTS, "nomcp", AGENTS["claude"].__class__(
        name="nomcp",
        skills_dir=".claude/skills",
        version_signature=None,
        install_hint="",
        mcp=None,
    ))
    inject_mcp_config("nomcp", project / "repo", URL)
    assert not marker_path(project).exists()
