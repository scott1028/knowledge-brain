import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from recall_engine.skill import (
    SkillError,
    detect_active_repo,
    inject_skill,
    restore_skill,
)

# Skills dirs that receive a symlink to the SSOT skill in .agents/skills/.
# codex and agy read .agents/skills directly, so they are not listed here.
LINK_SKILLS_DIRS = (".claude/skills", ".gemini/skills", ".pi/skills", ".opencode/skills")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Run each test inside a fresh project cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def skill_paths(project: Path) -> tuple[Path, Path, Path]:
    skills = project / ".agents" / "skills"
    return (
        skills / "recall-engine",
        skills / ".recall-engine-marker.json",
        skills / ".recall-engine-backup",
    )


def link_paths(project: Path) -> list[Path]:
    return [
        project / skills_dir / "recall-engine"
        for skills_dir in LINK_SKILLS_DIRS
    ]


def dead_pid() -> int:
    """Spawn a short-lived process and return its pid after it exits."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_inject_creates_rendered_skill_and_marker(project, tmp_path):
    repo = tmp_path / "repo"
    inject_skill(repo)

    skill_dir, marker, _ = skill_paths(project)
    content = (skill_dir / "SKILL.md").read_text()
    # The rendered skill points at the recall-engine MCP server tools.
    assert "search_knowledge" in content
    assert "name: recall-engine" in content
    record = json.loads(marker.read_text())
    assert record["pids"] == [os.getpid()]
    assert record["backup"] is None
    assert "knowledge" not in record  # no more in-project .knowledge link
    assert record["repo_path"] == str(repo)
    assert record["injected_at"]


def test_inject_creates_relative_symlinks_for_other_agents(project, tmp_path):
    inject_skill(tmp_path / "repo")

    skill_dir, _, _ = skill_paths(project)
    for link in link_paths(project):
        assert link.is_symlink()
        assert not Path(os.readlink(link)).is_absolute()
        assert link.resolve() == skill_dir.resolve()
        assert (link / "SKILL.md").exists()


def test_inject_skips_agent_dir_that_resolves_to_ssot(project, tmp_path):
    claude_skills = project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    os.symlink(
        os.path.relpath(project / ".agents" / "skills", claude_skills.parent),
        claude_skills,
    )

    inject_skill(tmp_path / "repo")

    skill_dir, _, backup = skill_paths(project)
    assert skill_dir.is_dir()
    assert not skill_dir.is_symlink()
    assert (skill_dir / "SKILL.md").exists()
    assert not backup.exists()
    assert not (project / ".claude" / "skills" / "recall-engine").is_symlink()
    assert (project / ".gemini" / "skills" / "recall-engine").is_symlink()
    assert (project / ".pi" / "skills" / "recall-engine").is_symlink()


def test_inject_does_not_create_knowledge_link(project, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "note.md").write_text("hello\n")

    inject_skill(repo)

    # The MCP server reads the repo directly; no in-project .knowledge link.
    knowledge = project / ".knowledge"
    assert not knowledge.exists()
    assert not knowledge.is_symlink()


def test_inject_leaves_existing_knowledge_untouched(project, tmp_path):
    # We no longer manage .knowledge, so a user's own .knowledge is left alone.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    existing = project / ".knowledge"
    existing.mkdir()
    (existing / "user.md").write_text("user knowledge\n")

    inject_skill(repo)
    assert existing.is_dir()
    assert not existing.is_symlink()
    assert (existing / "user.md").read_text() == "user knowledge\n"

    assert restore_skill(owner_pid=os.getpid()) is True
    assert existing.is_dir()
    assert (existing / "user.md").read_text() == "user knowledge\n"


def test_restore_cleans_legacy_knowledge_link_from_old_marker(project, tmp_path):
    # An old-version marker recorded a .knowledge symlink; restore must clean it.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    inject_skill(repo)
    _, marker, _ = skill_paths(project)

    knowledge = project / ".knowledge"
    os.symlink(repo / "src", knowledge)
    record = json.loads(marker.read_text())
    record["knowledge"] = {"path": str(knowledge), "backup": None}
    marker.write_text(json.dumps(record))

    assert restore_skill(owner_pid=os.getpid()) is True
    assert not knowledge.exists()
    assert not knowledge.is_symlink()


def test_rendered_skill_content(project, tmp_path):
    repo = tmp_path / "repo"
    inject_skill(repo)

    skill_dir, _, _ = skill_paths(project)
    content = (skill_dir / "SKILL.md").read_text()

    # Valid frontmatter: opening/closing --- with name and description lines.
    assert content.startswith("---\n")
    frontmatter = content.split("---")[1]
    assert "name: recall-engine" in frontmatter
    description_lines = [
        line
        for line in frontmatter.splitlines()
        if line.startswith("description:")
    ]
    assert len(description_lines) == 1
    assert description_lines[0].removeprefix("description:").strip()

    # The body points at the recall-engine MCP server tools.
    assert "search_knowledge" in content
    assert "read_note" in content
    assert "recall-engine MCP server" in content

    # No leftover template placeholder or in-project link references.
    assert "{knowledge_dir}" not in content
    assert "{repo_path}" not in content
    assert ".knowledge" not in content


def test_rendered_skill_triggers_for_every_message(project, tmp_path):
    inject_skill(tmp_path / "repo")

    skill_dir, _, _ = skill_paths(project)
    content = (skill_dir / "SKILL.md").read_text()

    assert "Before replying to ANY user message" in content
    assert "existing processing records, notes, or prior handling" in content
    assert "whether you recognize it or not" in content

    # The frontmatter description drives auto-invocation, so an
    # unconditional every-conversation trigger must live there.
    frontmatter = content.split("---")[1]
    description = next(
        line
        for line in frontmatter.splitlines()
        if line.startswith("description:")
    )
    assert "every conversation" in description
    assert "before any response" in description


def test_rendered_skill_exempts_trivial_messages(project, tmp_path):
    inject_skill(tmp_path / "repo")

    skill_dir, _, _ = skill_paths(project)
    content = (skill_dir / "SKILL.md").read_text()

    # Bare greetings/acknowledgements are exempt from the mandatory search.
    assert "trivial messages that carry no searchable keywords" in content


def test_rendered_skill_body_lists_trigger_topics(project, tmp_path):
    inject_skill(tmp_path / "repo")

    skill_dir, _, _ = skill_paths(project)
    content = (skill_dir / "SKILL.md").read_text()

    # The body enumerates concrete topics that should prompt a search.
    for topic in (
        "root-cause analysis",
        "workarounds",
        "lessons learned",
        "retrospectives",
        "decisions",
        "trade-offs",
        "runbooks",
        "best practices",
    ):
        assert topic in content


def test_restore_removes_skill_marker_and_symlinks(project, tmp_path):
    inject_skill(tmp_path / "repo")
    assert restore_skill(owner_pid=os.getpid()) is True

    skill_dir, marker, backup = skill_paths(project)
    assert not skill_dir.exists()
    assert not marker.exists()
    assert not backup.exists()
    for link in link_paths(project):
        assert not link.exists()
        assert not link.is_symlink()


def test_restore_with_nothing_returns_false(project):
    assert restore_skill() is False


def test_preexisting_user_skill_backed_up_and_restored(project, tmp_path, capsys):
    skill_dir, _, backup = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("user's own skill\n")
    (skill_dir / "extra.txt").write_text("distinctive user file\n")

    inject_skill(tmp_path / "repo")
    assert "moved to backup" in capsys.readouterr().err
    assert (backup / "extra.txt").read_text() == "distinctive user file\n"
    assert "knowledge base" in (skill_dir / "SKILL.md").read_text()

    assert restore_skill(owner_pid=os.getpid()) is True
    assert (skill_dir / "SKILL.md").read_text() == "user's own skill\n"
    assert (skill_dir / "extra.txt").read_text() == "distinctive user file\n"
    assert not backup.exists()


def test_preexisting_agent_dir_skill_backed_up_and_restored(project, tmp_path, capsys):
    # A user's own skill in an agent-specific dir (e.g. .claude/skills) must
    # survive the wrap session even though the path becomes a symlink.
    user_skill = project / ".claude" / "skills" / "recall-engine"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("user's claude skill\n")

    inject_skill(tmp_path / "repo")
    assert "moved to backup" in capsys.readouterr().err
    assert user_skill.is_symlink()

    assert restore_skill(owner_pid=os.getpid()) is True
    assert not user_skill.is_symlink()
    assert (user_skill / "SKILL.md").read_text() == "user's claude skill\n"


def test_stale_marker_dead_pid_auto_restored(project, tmp_path, capsys):
    skill_dir, marker, _ = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("leftover from a killed wrapper\n")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pid": dead_pid(), "backup": None}))

    repo = tmp_path / "repo"
    inject_skill(repo)
    assert "stale wrap session" in capsys.readouterr().err
    assert "search_knowledge" in (skill_dir / "SKILL.md").read_text()
    assert json.loads(marker.read_text())["pids"] == [os.getpid()]


def test_stale_marker_without_links_key_is_tolerated(project, tmp_path):
    # Markers written before symlink support carried no "links" entry.
    skill_dir, marker, _ = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("leftover\n")
    marker.write_text(json.dumps({"pid": dead_pid(), "backup": None}))

    assert restore_skill() is True
    assert not skill_dir.exists()
    assert not marker.exists()


def test_restore_handles_ssot_alias_link_from_old_marker(project):
    claude_skills = project / ".claude" / "skills"
    claude_skills.parent.mkdir()
    os.symlink(
        os.path.relpath(project / ".agents" / "skills", claude_skills.parent),
        claude_skills,
    )

    skill_dir, marker, backup = skill_paths(project)
    skill_dir.parent.mkdir(parents=True)
    backup.mkdir()
    (backup / "SKILL.md").write_text("restored user skill\n")
    os.symlink("../../.agents/skills/recall-engine", skill_dir)
    marker.write_text(
        json.dumps(
            {
                "pids": [dead_pid()],
                "backup": str(backup),
                "links": [
                    {
                        "path": str(project / ".claude" / "skills" / "recall-engine"),
                        "backup": str(
                            project / ".claude" / "skills" / ".recall-engine-backup"
                        ),
                    }
                ],
            }
        )
    )

    assert restore_skill(force=True) is True
    assert not marker.exists()
    assert skill_dir.is_dir()
    assert not skill_dir.is_symlink()
    assert (skill_dir / "SKILL.md").read_text() == "restored user skill\n"
    assert not backup.exists()


def test_legacy_claude_skills_marker_cleaned(project):
    # Pre-SSOT sessions left their state under .claude/skills/.
    legacy = project / ".claude" / "skills"
    legacy_skill = legacy / "recall-engine"
    legacy_skill.mkdir(parents=True)
    (legacy_skill / "SKILL.md").write_text("legacy leftover\n")
    (legacy / ".recall-engine-marker.json").write_text(
        json.dumps({"pid": dead_pid(), "backup": None})
    )

    assert restore_skill() is True
    assert not legacy_skill.exists()
    assert not (legacy / ".recall-engine-marker.json").exists()


def live_pid():
    """Spawn a long-lived process; caller must terminate it."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_attach_to_live_session_adds_our_pid(project, tmp_path):
    repo = tmp_path / "repo"
    inject_skill(repo)  # first owner sets up; pids == [os.getpid()]
    _, marker, backup = skill_paths(project)

    other = live_pid()
    try:
        # Simulate another live wrap session owning the injection.
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))

        inject_skill(repo)  # same dir, same repo -> attach, no re-inject
        pids = json.loads(marker.read_text())["pids"]
        assert other.pid in pids and os.getpid() in pids
        # Attach must not create a backup of our own freshly-rendered skill.
        assert not backup.exists()
    finally:
        other.terminate()
        other.wait()


def test_first_owner_leaving_keeps_skill_then_last_restores(project, tmp_path):
    # A pre-existing user skill must survive until the LAST owner leaves.
    skill_dir, marker, backup = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("user's own skill\n")

    inject_skill(tmp_path / "repo")  # backs up user skill; pids == [me]
    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = sorted({os.getpid(), other.pid})
        marker.write_text(json.dumps(record))

        # First owner (us) leaves while `other` is still alive -> keep injection.
        assert restore_skill(owner_pid=os.getpid()) is True
        assert (skill_dir / "SKILL.md").read_text() != "user's own skill\n"
        assert backup.exists()
        assert json.loads(marker.read_text())["pids"] == [other.pid]
    finally:
        other.terminate()
        other.wait()

    # Last owner leaves (its pid now dead) -> full teardown restores user skill.
    assert restore_skill(owner_pid=other.pid) is True
    assert (skill_dir / "SKILL.md").read_text() == "user's own skill\n"
    assert not marker.exists()
    assert not backup.exists()


def test_attach_refused_when_repo_differs(project, tmp_path):
    inject_skill(tmp_path / "repo-a")
    _, marker, _ = skill_paths(project)
    other = live_pid()
    try:
        record = json.loads(marker.read_text())
        record["pids"] = [other.pid]
        marker.write_text(json.dumps(record))
        with pytest.raises(SkillError, match="different"):
            inject_skill(tmp_path / "repo-b")
    finally:
        other.terminate()
        other.wait()


def test_marker_records_repo_path(project, tmp_path):
    repo = tmp_path / "repo"
    inject_skill(repo)
    _, marker, _ = skill_paths(project)
    assert json.loads(marker.read_text())["repo_path"] == str(repo)


def test_detect_active_repo_none_without_marker(project):
    assert detect_active_repo() is None


def test_detect_active_repo_returns_live_session_repo(project, tmp_path):
    repo = tmp_path / "repo"
    inject_skill(repo)  # our own pid is live, so the session counts as active
    assert detect_active_repo() == repo


def test_detect_active_repo_none_when_owner_dead(project, tmp_path):
    repo = tmp_path / "repo"
    inject_skill(repo)
    _, marker, _ = skill_paths(project)
    record = json.loads(marker.read_text())
    record["pids"] = [dead_pid()]
    marker.write_text(json.dumps(record))
    # No live owner -> nothing to inherit from.
    assert detect_active_repo() is None


def test_detect_active_repo_none_without_repo_path_field(project, tmp_path):
    # Legacy live marker (no repo_path) cannot be auto-inherited from.
    skill_dir, marker, _ = skill_paths(project)
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("live but repo unknown\n")
    marker.write_text(json.dumps({"pids": [os.getpid()], "backup": None}))
    assert detect_active_repo() is None
