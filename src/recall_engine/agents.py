"""Registry of supported agent CLIs and their skills conventions."""

from __future__ import annotations

from dataclasses import dataclass, field

# Agent Skills spec location; the real skill is written here (SSOT) and the
# other agents' skills dirs get symlinks pointing at it.
SKILLS_SSOT_DIR = ".agents/skills"

# Name of the shared MCP server as registered in each agent's config.
MCP_SERVER_NAME = "recall-engine"


@dataclass(frozen=True)
class McpConfigSpec:
    """How to register the shared MCP server in one agent's config file.

    A single running HTTP server serves every repo; the per-project config
    carries the repo in a request header (see header_field), so the server
    routes each connection to the right knowledge repo. The fields below
    capture the per-agent schema differences (top-level key, URL field name,
    optional transport `type`, header field, file format).
    """

    config_path: str  # project-relative config file
    fmt: str  # "json" | "toml"
    servers_key: str  # top-level table/object key ("mcpServers" | "mcp" | "mcp_servers")
    url_field: str  # "url" | "httpUrl" | "serverUrl"
    header_field: str  # "headers" | "http_headers"
    type_value: str | None = None  # transport tag: "http" (claude) | "remote" (opencode)
    extra_fields: dict = field(default_factory=dict)  # e.g. {"enabled": True}


@dataclass(frozen=True)
class AgentSpec:
    name: str
    skills_dir: str  # project-level skills discovery dir (relative path)
    version_signature: str | None  # lowercase substring of `--version` stdout
    install_hint: str
    mcp: McpConfigSpec | None = None  # how to register the shared MCP server
    # agy reads a project's .agents/ config (skill + MCP) only when the dir is
    # in its active workspace, so it must be launched with `--add-dir <project>`.
    needs_workspace_dir: bool = False


AGENTS: dict[str, AgentSpec] = {
    "claude": AgentSpec(
        name="claude",
        skills_dir=".claude/skills",
        version_signature="claude code",
        install_hint="install Claude Code: https://claude.com/claude-code",
        # Claude Code: project-root .mcp.json, {"type":"http","url":...}.
        mcp=McpConfigSpec(
            config_path=".mcp.json",
            fmt="json",
            servers_key="mcpServers",
            url_field="url",
            header_field="headers",
            type_value="http",
        ),
    ),
    "codex": AgentSpec(
        name="codex",
        skills_dir=SKILLS_SSOT_DIR,
        version_signature="codex",
        install_hint="install Codex CLI: https://developers.openai.com/codex/cli",
        # Codex: [mcp_servers.<name>] with url=; headers under a subtable.
        # Project .codex/config.toml applies only in trusted projects.
        mcp=McpConfigSpec(
            config_path=".codex/config.toml",
            fmt="toml",
            servers_key="mcp_servers",
            url_field="url",
            header_field="http_headers",
        ),
    ),
    "pi": AgentSpec(
        name="pi",
        skills_dir=".pi/skills",
        version_signature=None,  # `pi --version` prints a bare version number
        install_hint="install pi: https://github.com/earendil-works/pi",
        # pi via pi-mcp-adapter reads .pi/mcp.json (kept distinct from Claude's
        # project-root .mcp.json to avoid a collision).
        mcp=McpConfigSpec(
            config_path=".pi/mcp.json",
            fmt="json",
            servers_key="mcpServers",
            url_field="url",
            header_field="headers",
        ),
    ),
    "gemini": AgentSpec(
        name="gemini",
        skills_dir=".gemini/skills",
        version_signature=None,  # `gemini --version` prints a bare version number
        install_hint="install Gemini CLI: https://github.com/google-gemini/gemini-cli",
        # Gemini CLI: .gemini/settings.json, streamable HTTP via httpUrl.
        mcp=McpConfigSpec(
            config_path=".gemini/settings.json",
            fmt="json",
            servers_key="mcpServers",
            url_field="httpUrl",
            header_field="headers",
        ),
    ),
    "opencode": AgentSpec(
        name="opencode",
        skills_dir=".opencode/skills",  # opencode reads only .opencode/skills per project
        version_signature=None,  # `opencode --version` prints a bare version number
        install_hint="install the opencode CLI",
        # opencode: opencode.json, remote server under top-level "mcp".
        mcp=McpConfigSpec(
            config_path="opencode.json",
            fmt="json",
            servers_key="mcp",
            url_field="url",
            header_field="headers",
            type_value="remote",
            extra_fields={"enabled": True},
        ),
    ),
    "agy": AgentSpec(
        name="agy",
        skills_dir=SKILLS_SSOT_DIR,  # agy reads .agents/skills directly
        version_signature=None,  # `agy --version` prints a bare version number
        install_hint="install the agy CLI",
        # agy (Antigravity): .agents/mcp_config.json, remote via serverUrl.
        # Verified (agy 1.1.1): project-local config loads and forwards custom
        # headers only when the project dir is added to the active workspace,
        # hence needs_workspace_dir (launched with `--add-dir <project>`).
        mcp=McpConfigSpec(
            config_path=".agents/mcp_config.json",
            fmt="json",
            servers_key="mcpServers",
            url_field="serverUrl",
            header_field="headers",
        ),
        needs_workspace_dir=True,
    ),
}
