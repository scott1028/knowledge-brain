"""End-to-end tests for the recall-engine MCP server.
Each test starts a real streamable-HTTP server in a background thread and drives
it through a real streamablehttp_client + ClientSession. The project uses plain
pytest (no pytest-asyncio), so async flows run via asyncio.run inside sync tests.
"""
import asyncio
import base64
import json
import os
import socket
import threading
import time
from pathlib import Path
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from recall_engine import index
from recall_engine.mcp_server import create_server
@pytest.fixture(autouse=True)
def _reset_indexes():
    """Stop each test's observer and clear the registry before tmp cleanup."""
    yield
    index.reset_indexes()
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
async def _list_tools(port):
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamablehttp_client(url, headers={}) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            return await session.list_tools()
def list_tools(port):
    return asyncio.run(_list_tools(port))
async def _list_resource_templates(port):
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamablehttp_client(url, headers={}) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            return await session.list_resource_templates()
def list_resource_templates(port):
    return asyncio.run(_list_resource_templates(port))
async def _list_resources(port):
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamablehttp_client(url, headers={}) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            return await session.list_resources()
def list_resources(port):
    return asyncio.run(_list_resources(port))
async def _read_resource(port, uri, *, repo=None, token=None):
    headers = {}
    if repo is not None:
        headers["X-Recall-Repo"] = repo
    if token is not None:
        headers["X-Recall-Token"] = token
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamablehttp_client(url, headers=headers) as (reader, writer, _):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            return await session.read_resource(uri)
def read_resource(port, uri, *, repo=None, token=None):
    return asyncio.run(_read_resource(port, uri, repo=repo, token=token))
def exception_text(exc: BaseException) -> str:
    messages = [str(exc)]
    nested_exceptions = getattr(exc, "exceptions", None)
    if nested_exceptions:
        for nested in nested_exceptions:
            messages.append(exception_text(nested))
    return "\n".join(messages)
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
def note_resource_uri(rel: str) -> str:
    encoded = base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")
    return f"recall://note/{encoded}"
@pytest.fixture(scope="module")
def server_port():
    return _start_server()
@pytest.fixture(scope="module")
def token_server_port():
    return _start_server(token="s3cr3t")
def test_public_mcp_shape_is_advertised(server_port):
    assert [tool.name for tool in list_tools(server_port).tools] == ["search_knowledge"]
    resources_result = list_resources(server_port)
    resources = {str(resource.uri): resource for resource in resources_result.resources}
    assert "recall://notes/index" in resources
    assert resources["recall://notes/index"].mimeType == "application/json"
    assert (
        "Before calling `search_knowledge` or reading `recall://notes/index`, "
        "predict useful expansion keywords"
    ) in resources["recall://notes/index"].description
    templates_result = list_resource_templates(server_port)
    templates = {
        template.uriTemplate: template for template in templates_result.resourceTemplates
    }
    assert "recall://note/{encoded_path}" in templates
    assert templates["recall://note/{encoded_path}"].mimeType == "text/markdown"
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
    assert matches[0]["resource_uri"] == note_resource_uri("guide.md")
    miss = call_tool(server_port, "search_knowledge", {"query": "kubernetes"}, repo=str(repo))
    assert not miss.isError
    assert miss.structuredContent["result"] == []
def test_search_builds_sqlite_index(server_port, tmp_path):
    repo = make_repo(tmp_path / "repo", {"guide.md": "indexed keyword here\n"})
    db_path = index.index_db_path(repo.resolve())
    assert not db_path.exists()
    hit = call_tool(server_port, "search_knowledge", {"query": "keyword"}, repo=str(repo))
    assert not hit.isError
    assert [m["path"] for m in hit.structuredContent["result"]] == [note_path(repo, "guide.md")]
    # To-be path: the SQLite index is created (in the temp dir) on first search.
    assert db_path.exists()
def test_search_falls_back_to_scan_without_index(server_port, tmp_path, monkeypatch):
    # With the DB in the temp dir, a read-only repo no longer blocks the build;
    # force the index unavailable so search must use the direct-scan fallback.
    repo = make_repo(tmp_path / "fb", {"n.md": "fallback keyword here\n"})
    monkeypatch.setattr("recall_engine.index.ensure_index", lambda repo: None)
    res = call_tool(server_port, "search_knowledge", {"query": "keyword"}, repo=str(repo))
    assert not res.isError
    assert [m["path"] for m in res.structuredContent["result"]] == [note_path(repo, "n.md")]
def test_header_routing_between_repos(server_port, tmp_path):
    repo_a = make_repo(tmp_path / "a", {"a.md": "alpha keyword here\n"})
    repo_b = make_repo(tmp_path / "b", {"b.md": "alpha keyword here\n"})
    res_a = call_tool(server_port, "search_knowledge", {"query": "alpha"}, repo=str(repo_a))
    res_b = call_tool(server_port, "search_knowledge", {"query": "alpha"}, repo=str(repo_b))
    assert [m["path"] for m in res_a.structuredContent["result"]] == [note_path(repo_a, "a.md")]
    assert [m["path"] for m in res_b.structuredContent["result"]] == [note_path(repo_b, "b.md")]
def test_notes_index_resource_uses_request_repo(server_port, tmp_path):
    repo = make_repo(
        tmp_path / "repo",
        {"b.md": "x\n", "a.md": "y\n", "sub/c.md": "z\n"},
    )
    res = read_resource(server_port, "recall://notes/index", repo=str(repo))
    content = res.contents[0]
    assert content.mimeType == "application/json"
    assert json.loads(content.text) == [
        {
            "path": note_path(repo, "a.md"),
            "relative_path": "a.md",
            "resource_uri": note_resource_uri("a.md"),
        },
        {
            "path": note_path(repo, "b.md"),
            "relative_path": "b.md",
            "resource_uri": note_resource_uri("b.md"),
        },
        {
            "path": note_path(repo, "sub/c.md"),
            "relative_path": "sub/c.md",
            "resource_uri": note_resource_uri("sub/c.md"),
        },
    ]
def test_note_resource_reads_nested_markdown_note(server_port, tmp_path):
    repo = make_repo(tmp_path / "repo", {"sub/doc.md": "# Title\nfull note text\n"})
    res = read_resource(server_port, note_resource_uri("sub/doc.md"), repo=str(repo))
    content = res.contents[0]
    assert content.mimeType == "text/markdown"
    assert str(content.uri) == note_resource_uri("sub/doc.md")
    assert content.text == "# Title\nfull note text\n"
def test_note_resource_rejects_invalid_paths(server_port, tmp_path):
    repo = make_repo(tmp_path / "repo", {"doc.md": "safe note\n"})
    (repo / "secret.md").write_text("outside src\n")
    cases = [
        ("recall://note/not-valid-***", "invalid encoded note path"),
        (f"{note_resource_uri('doc.md')}***", "invalid encoded note path"),
        (note_resource_uri("../secret.md"), "outside"),
    ]
    for uri, expected_message in cases:
        with pytest.raises(Exception) as exc_info:
            read_resource(server_port, uri, repo=str(repo))
        assert expected_message in exception_text(exc_info.value)
def test_requests_require_repo_header_for_tool_and_resource(server_port):
    res = call_tool(server_port, "search_knowledge", {"query": "keyword"})
    assert res.isError
    assert "X-Recall-Repo" in res.content[0].text
    with pytest.raises(Exception) as exc_info:
        read_resource(server_port, "recall://notes/index")
    assert "X-Recall-Repo" in exception_text(exc_info.value)
def test_token_auth_for_tool_and_resource(token_server_port, tmp_path):
    repo = make_repo(tmp_path / "repo", {"n.md": "token protected keyword\n"})
    no_token = call_tool(
        token_server_port,
        "search_knowledge",
        {"query": "keyword"},
        repo=str(repo),
    )
    assert no_token.isError
    assert "X-Recall-Token" in no_token.content[0].text
    wrong_token = call_tool(
        token_server_port,
        "search_knowledge",
        {"query": "keyword"},
        repo=str(repo),
        token="nope",
    )
    assert wrong_token.isError
    good = call_tool(
        token_server_port,
        "search_knowledge",
        {"query": "keyword"},
        repo=str(repo),
        token="s3cr3t",
    )
    assert not good.isError
    assert [match["path"] for match in good.structuredContent["result"]] == [
        note_path(repo, "n.md")
    ]
    with pytest.raises(Exception) as exc_info:
        read_resource(token_server_port, "recall://notes/index", repo=str(repo))
    assert "X-Recall-Token" in exception_text(exc_info.value)
    with pytest.raises(Exception):
        read_resource(
            token_server_port,
            note_resource_uri("n.md"),
            repo=str(repo),
            token="nope",
        )
    index = read_resource(
        token_server_port,
        "recall://notes/index",
        repo=str(repo),
        token="s3cr3t",
    )
    assert json.loads(index.contents[0].text) == [
        {
            "path": note_path(repo, "n.md"),
            "relative_path": "n.md",
            "resource_uri": note_resource_uri("n.md"),
        }
    ]
    note = read_resource(
        token_server_port,
        note_resource_uri("n.md"),
        repo=str(repo),
        token="s3cr3t",
    )
    assert note.contents[0].text == "token protected keyword\n"
def test_note_enumeration_skips_symlinks_outside_src(server_port, tmp_path):
    repo = make_repo(tmp_path / "repo", {"safe.md": "inside keyword\n"})
    outside = tmp_path / "outside.md"
    outside.write_text("outside keyword\n")
    os.symlink(outside, repo / "src" / "external.md")
    search = call_tool(
        server_port,
        "search_knowledge",
        {"query": "outside"},
        repo=str(repo),
    )
    assert not search.isError
    assert search.structuredContent["result"] == []
    index = read_resource(server_port, "recall://notes/index", repo=str(repo))
    assert json.loads(index.contents[0].text) == [
        {
            "path": note_path(repo, "safe.md"),
            "relative_path": "safe.md",
            "resource_uri": note_resource_uri("safe.md"),
        }
    ]
