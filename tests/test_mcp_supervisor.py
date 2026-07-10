"""Supervisor lifecycle tests.

The real MCP server is not needed here: we monkeypatch _spawn_server with a
trivial TCP listener so the PID-file/refcount logic is exercised in isolation.
"""

import os
import subprocess
import sys

import pytest

from recall_engine import mcp_supervisor as sup


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    """Point the supervisor's state/lock/log files at a fresh temp dir."""
    monkeypatch.setattr(sup, "_state_path", lambda: tmp_path / "mcp.json")
    monkeypatch.setattr(sup, "_lock_path", lambda: tmp_path / "mcp.lock")
    monkeypatch.setattr(sup, "_log_path", lambda: tmp_path / "mcp.log")
    return tmp_path


def _fake_listener_proc(port: int) -> subprocess.Popen:
    """A separate process that listens on `port` (stands in for the server)."""
    code = (
        "import socket,sys;"
        "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        f"s.bind(('{sup.HOST}',{port}));s.listen();"
        "\nwhile True:\n c,_=s.accept();c.close()"
    )
    return subprocess.Popen([sys.executable, "-c", code])


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_ensure_spawns_and_records_owner(tmp_state, monkeypatch):
    procs = []

    def fake_spawn(port, token):
        p = _fake_listener_proc(port)
        procs.append(p)
        return p.pid

    monkeypatch.setattr(sup, "_spawn_server", fake_spawn)
    try:
        info = sup.ensure_server(token="tok123")
        assert info.token == "tok123"
        assert info.url.endswith("/mcp")
        record = sup._read_state()
        assert record["owners"] == [os.getpid()]
        assert record["pid"] == info.pid
        assert sup._is_listening(info.port)
    finally:
        for p in procs:
            p.terminate()
            p.wait()


def test_second_owner_reuses_running_server(tmp_state, monkeypatch):
    spawn_calls = []
    procs = []

    def fake_spawn(port, token):
        spawn_calls.append(port)
        p = _fake_listener_proc(port)
        procs.append(p)
        return p.pid

    monkeypatch.setattr(sup, "_spawn_server", fake_spawn)
    try:
        info1 = sup.ensure_server()
        # Simulate a different owner already present, then attach.
        record = sup._read_state()
        other = _fake_listener_proc(sup._free_port())  # just a live pid
        procs.append(other)
        record["owners"] = [other.pid]
        sup.atomic_write_json(sup._state_path(), record)

        info2 = sup.ensure_server()
        assert info2.port == info1.port  # same server reused
        assert len(spawn_calls) == 1  # no second spawn
        owners = sup._read_state()["owners"]
        assert other.pid in owners and os.getpid() in owners
    finally:
        for p in procs:
            p.terminate()
            p.wait()


def test_release_keeps_server_while_other_owner_alive(tmp_state, monkeypatch):
    procs = []
    monkeypatch.setattr(
        sup, "_spawn_server", lambda port, token: procs.append(_fake_listener_proc(port)) or procs[-1].pid
    )
    try:
        sup.ensure_server()
        record = sup._read_state()
        other = _fake_listener_proc(sup._free_port())
        procs.append(other)
        record["owners"] = sorted({os.getpid(), other.pid})
        sup.atomic_write_json(sup._state_path(), record)

        # We leave, but `other` is alive -> server stays.
        assert sup.release_server(owner_pid=os.getpid()) is True
        record = sup._read_state()
        assert record is not None
        assert record["owners"] == [other.pid]
        assert sup.is_pid_alive(record["pid"])
    finally:
        for p in procs:
            p.terminate()
            p.wait()


def test_last_owner_stops_server_and_removes_state(tmp_state, monkeypatch):
    procs = []

    def fake_spawn(port, token):
        p = _fake_listener_proc(port)
        procs.append(p)
        return p.pid

    monkeypatch.setattr(sup, "_spawn_server", fake_spawn)
    try:
        info = sup.ensure_server()
        assert sup.release_server(owner_pid=os.getpid()) is True
        assert sup._read_state() is None
        # Server process received SIGTERM.
        procs[0].wait(timeout=5)
        assert not sup.is_pid_alive(info.pid)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                p.wait()


def test_stale_state_dead_pid_respawns(tmp_state, monkeypatch):
    # A leftover state file with a dead server pid must be replaced.
    sup.atomic_write_json(
        sup._state_path(),
        {"pid": dead_pid(), "port": sup._free_port(), "url": "x", "token": "t", "owners": [dead_pid()]},
    )
    procs = []

    def fake_spawn(port, token):
        p = _fake_listener_proc(port)
        procs.append(p)
        return p.pid

    monkeypatch.setattr(sup, "_spawn_server", fake_spawn)
    try:
        info = sup.ensure_server()
        assert sup._is_listening(info.port)
        assert sup._read_state()["owners"] == [os.getpid()]
    finally:
        for p in procs:
            p.terminate()
            p.wait()


def test_release_without_state_returns_false(tmp_state):
    assert sup.release_server(owner_pid=os.getpid()) is False


def test_server_status_none_without_state(tmp_state):
    assert sup.server_status() is None


def test_server_status_reports_reachable_server(tmp_state, monkeypatch):
    procs = []

    def fake_spawn(port, token):
        p = _fake_listener_proc(port)
        procs.append(p)
        return p.pid

    monkeypatch.setattr(sup, "_spawn_server", fake_spawn)
    try:
        info = sup.ensure_server()
        status = sup.server_status()
        assert status.reachable is True
        assert status.pid == info.pid
        assert status.url == info.url
        assert status.owners == [os.getpid()]
    finally:
        for p in procs:
            p.terminate()
            p.wait()


def test_server_status_reports_stale_state_as_unreachable(tmp_state):
    sup.atomic_write_json(
        sup._state_path(),
        {
            "pid": dead_pid(),
            "port": sup._free_port(),
            "url": "http://127.0.0.1:1/mcp",
            "token": "t",
            "owners": [dead_pid()],
        },
    )
    status = sup.server_status()
    assert status is not None
    assert status.reachable is False


def test_wait_healthy_fails_when_process_dies(tmp_state):
    # A never-listening, already-dead pid must not be reported healthy.
    assert sup._wait_healthy(sup._free_port(), dead_pid(), timeout=1.0) is False
