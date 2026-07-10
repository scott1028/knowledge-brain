"""Machine-wide MCP server exposing knowledge repos over streamable HTTP.

A single server serves many knowledge repos. Each client connection names its
target repo in the ``X-Recall-Repo`` request header (an absolute path), and may
carry an auth token in ``X-Recall-Token``. Notes live under ``<repo>/src/**/*.md``.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn
from mcp.server.fastmcp import Context, FastMCP

INSTRUCTIONS = (
    "This server exposes a knowledge base of past processing, notes, and "
    "decisions recorded in Markdown. Before replying to the user, call "
    "search_knowledge with keywords drawn from the user's message, then call "
    "read_note on the most relevant matches, and cite the returned note path "
    "in your answer. All tools are read-only."
)

MAX_MATCHES = 50


def create_server(token: str | None = None) -> FastMCP:
    """Build the recall-engine FastMCP server with the three tools registered.

    If ``token`` is not None, every tool call rejects requests whose
    ``X-Recall-Token`` header does not equal ``token``.
    """
    mcp = FastMCP("recall-engine", stateless_http=True, instructions=INSTRUCTIONS)

    def resolve_repo(ctx: Context) -> Path:
        """Resolve and authorize the target repo from the request headers."""
        headers = ctx.request_context.request.headers
        repo_header = headers.get("x-recall-repo")
        if not repo_header:
            raise ValueError("missing X-Recall-Repo header")
        repo = Path(repo_header).resolve()
        if not repo.is_dir() or not (repo / "src").is_dir():
            raise ValueError(
                f"X-Recall-Repo does not point to a knowledge repo with a "
                f"src/ directory: {repo_header}"
            )
        if token is not None and headers.get("x-recall-token") != token:
            raise ValueError("invalid or missing X-Recall-Token")
        return repo

    @mcp.tool()
    def search_knowledge(query: str, ctx: Context) -> list[dict]:
        """Case-insensitive substring search across <repo>/src/**/*.md."""
        src = resolve_repo(ctx) / "src"
        needle = query.lower()
        matches: list[dict] = []
        for note in sorted(src.rglob("*.md")):
            if not note.is_file():
                continue
            for lineno, line in enumerate(
                note.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if needle in line.lower():
                    matches.append(
                        {
                            "path": str(note),
                            "line": lineno,
                            "snippet": line.strip(),
                        }
                    )
                    if len(matches) >= MAX_MATCHES:
                        return matches
        return matches

    @mcp.tool()
    def read_note(path: str, ctx: Context) -> str:
        """Return a note's full text; path must resolve inside <repo>/src."""
        src = (resolve_repo(ctx) / "src").resolve()
        candidate = Path(path)
        note = candidate if candidate.is_absolute() else src / candidate
        note = note.resolve()
        if note != src and src not in note.parents:
            raise ValueError(f"path is outside the knowledge repo src/ directory: {path}")
        if not note.is_file():
            raise ValueError(f"note not found: {path}")
        return note.read_text(encoding="utf-8", errors="replace")

    @mcp.tool()
    def list_notes(ctx: Context) -> list[str]:
        """Return the sorted *.md file paths under <repo>/src, recursively."""
        src = resolve_repo(ctx) / "src"
        return sorted(str(note) for note in src.rglob("*.md") if note.is_file())

    return mcp


def run_server(host: str, port: int, token: str | None = None) -> None:
    """Serve the recall-engine MCP server over streamable HTTP (blocking)."""
    mcp = create_server(token)
    app = mcp.streamable_http_app()
    uvicorn.run(app, host=host, port=port, log_level="error")
