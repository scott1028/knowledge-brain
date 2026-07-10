"""End-to-end tests for the recall-engine MCP server.

Each test starts a real streamable-HTTP server in a background thread and drives
it through a real streamablehttp_client + ClientSession. The project uses plain
pytest (no pytest-asyncio), so async flows run via asyncio.run inside sync tests.
"""

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from recall_engine.mcp_server import INSTRUCTIONS, create_server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(token: str | None = None) -> int:
    port = _free_port()
    app = create_server(token).streamable_http_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=lambda: asyncio.run(server.serve()), daemon=True).start()
    time.sleep(1.5)
    return port


async def _call(port, tool, args, *, repo=None, token=None):
    headers = {}
    if repo is not None:
        headers["X-Recall-Repo"] = repo
    if token is not None:
        headers["X-Recall-Token"] = token
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamablehttp_client(url, headers=headers) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            return await session.call_tool(tool, args)


def call_tool(port, tool, args, *, repo=None, token=None):
    """Open a fresh client connection, call one tool, return the CallToolResult."""
    return asyncio.run(_call(port, tool, args, repo=repo, token=token))


def make_repo(base: Path, notes: dict[str, str]) -> Path:
    """Create <base>/src with the given {relative_md_path: text} notes."""
    src = base / "src"
    src.mkdir(parents=True)
    for rel, text in notes.items():
        note = src / rel
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(text)
    return base


def note_path(repo: Path, rel: str) -> str:
    return str((repo / "src" / rel).resolve())


@pytest.fixture(scope="module")
def server_port():
    return _start_server()


@pytest.fixture(scope="module")
def token_server_port():
    return _start_server(token="s3cr3t")


def test_search_knowledge_hit_and_miss(server_port, tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {
            "guide.md": "Deploy uses blue-green rollout.\nRollback is manual.\n",
            "sub/notes.md": "Nothing relevant here.\n",
        },
    )
    hit = call_tool(server_port, "search_knowledge", {"query": "blue-green"}, repo=str(repo))
    assert not hit.isError
    matches = hit.structuredContent["result"]
    assert len(matches) == 1
    assert matches[0]["path"] == note_path(repo, "guide.md")
    assert matches[0]["line"] == 1
    assert "blue-green" in matches[0]["snippet"]

    miss = call_tool(server_port, "search_knowledge", {"query": "kubernetes"}, repo=str(repo))
    assert not miss.isError
    assert miss.structuredContent["result"] == []


def test_header_routing_between_repos(server_port, tmp_path):
    repo_a = make_repo(tmp_path / "a", {"a.md": "alpha keyword here\n"})
    repo_b = make_repo(tmp_path / "b", {"b.md": "alpha keyword here\n"})

    res_a = call_tool(server_port, "search_knowledge", {"query": "alpha"}, repo=str(repo_a))
    res_b = call_tool(server_port, "search_knowledge", {"query": "alpha"}, repo=str(repo_b))

    assert [m["path"] for m in res_a.structuredContent["result"]] == [note_path(repo_a, "a.md")]
    assert [m["path"] for m in res_b.structuredContent["result"]] == [note_path(repo_b, "b.md")]


def test_read_note_valid_and_traversal(server_port, tmp_path):
    repo = make_repo(tmp_path / "repo", {"doc.md": "full note text\nsecond line\n"})

    by_rel = call_tool(server_port, "read_note", {"path": "doc.md"}, repo=str(repo))
    assert not by_rel.isError
    assert by_rel.structuredContent["result"] == "full note text\nsecond line\n"

    by_abs = call_tool(
        server_port, "read_note", {"path": note_path(repo, "doc.md")}, repo=str(repo)
    )
    assert not by_abs.isError
    assert by_abs.content[0].text == "full note text\nsecond line\n"

    (repo / "secret.md").write_text("top secret, outside src\n")
    traversal = call_tool(server_port, "read_note", {"path": "../secret.md"}, repo=str(repo))
    assert traversal.isError
    assert "outside" in traversal.content[0].text


def test_list_notes(server_port, tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {"b.md": "x\n", "a.md": "y\n", "sub/c.md": "z\n"},
    )
    res = call_tool(server_port, "list_notes", {}, repo=str(repo))
    assert not res.isError
    assert res.structuredContent["result"] == [
        note_path(repo, "a.md"),
        note_path(repo, "b.md"),
        note_path(repo, "sub/c.md"),
    ]


def test_missing_repo_header_errors(server_port):
    res = call_tool(server_port, "list_notes", {})
    assert res.isError
    assert "X-Recall-Repo" in res.content[0].text


def test_token_auth(token_server_port, tmp_path):
    repo = make_repo(tmp_path / "repo", {"n.md": "token protected note\n"})

    no_token = call_tool(token_server_port, "list_notes", {}, repo=str(repo))
    assert no_token.isError
    assert "X-Recall-Token" in no_token.content[0].text

    wrong_token = call_tool(
        token_server_port, "list_notes", {}, repo=str(repo), token="nope"
    )
    assert wrong_token.isError

    good = call_tool(
        token_server_port, "list_notes", {}, repo=str(repo), token="s3cr3t"
    )
    assert not good.isError
    assert good.structuredContent["result"] == [note_path(repo, "n.md")]


def test_server_advertises_instructions(server_port):
    async def _init():
        url = f"http://127.0.0.1:{server_port}/mcp"
        async with streamablehttp_client(url, headers={}) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                return await session.initialize()

    result = asyncio.run(_init())
    assert result.instructions == INSTRUCTIONS
