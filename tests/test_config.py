"""Tests for startup configuration and the CLI entry point."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_comm_server import __version__
from mcp_comm_server.config import (
    ConfigError,
    ServerConfig,
    build_parser,
    load_config,
)

ENV_VARS = (
    "MCP_COMM_LOG_LEVEL",
    # Removed HTTP/SSE settings: must never influence the stdio-only config.
    "MCP_COMM_TRANSPORT",
    "MCP_COMM_HOST",
    "MCP_COMM_PORT",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults():
    config = load_config([])
    assert config == ServerConfig(log_level="INFO")
    assert config.as_dict() == {"log_level": "INFO"}


def test_environment_values(monkeypatch):
    monkeypatch.setenv("MCP_COMM_LOG_LEVEL", "debug")

    config = load_config([])
    assert config.log_level == "DEBUG"


def test_cli_overrides_environment(monkeypatch):
    monkeypatch.setenv("MCP_COMM_LOG_LEVEL", "ERROR")

    config = load_config(["--log-level", "warning"])
    assert config == ServerConfig(log_level="WARNING")


def test_empty_environment_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("MCP_COMM_LOG_LEVEL", "  ")
    assert load_config([]) == ServerConfig()


def test_removed_transport_environment_variables_are_ignored(monkeypatch):
    monkeypatch.setenv("MCP_COMM_TRANSPORT", "sse")
    monkeypatch.setenv("MCP_COMM_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_COMM_PORT", "9123")

    assert load_config([]) == ServerConfig()


@pytest.mark.parametrize(
    "name,value",
    [
        ("MCP_COMM_LOG_LEVEL", "TRACE"),
    ],
)
def test_invalid_environment_values_raise(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        load_config([])


@pytest.mark.parametrize("flag", ["--transport", "--host", "--port"])
def test_cli_rejects_removed_http_flags(flag):
    value = "sse" if flag == "--transport" else "8000"
    with pytest.raises(SystemExit) as excinfo:
        load_config([flag, value])
    assert excinfo.value.code == 2


def test_parser_exposes_required_flags():
    parser = build_parser()
    options = {action.dest for action in parser._actions}
    assert "log_level" in options
    assert not {"transport", "host", "port"} & options


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        load_config(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_main_returns_2_for_invalid_config(monkeypatch, capsys):
    from mcp_comm_server import server

    monkeypatch.setattr(
        server, "run_server", lambda config: pytest.fail("server must not start")
    )
    monkeypatch.setenv("MCP_COMM_LOG_LEVEL", "nope")
    assert server.main([]) == 2
    error = capsys.readouterr().err
    assert "error:" in error
    assert "nope" in error


def test_main_runs_and_closes_ports(monkeypatch):
    from mcp_comm_server import server

    calls = []
    monkeypatch.setattr(
        server, "configure_logging", lambda level: calls.append(("log", level))
    )
    monkeypatch.setattr(
        server, "run_server", lambda config: calls.append(("run", config.log_level))
    )
    monkeypatch.setattr(
        server, "_close_serial_ports", lambda: calls.append(("close", None))
    )

    assert server.main([]) == 0
    assert calls == [("log", "INFO"), ("run", "INFO"), ("close", None)]


def test_run_server_always_uses_stdio(monkeypatch):
    from mcp_comm_server import server

    calls = []

    class DummyMCP:
        settings = SimpleNamespace(log_level="INFO")

        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(server, "mcp", DummyMCP())
    server.run_server(ServerConfig(log_level="DEBUG"))

    assert calls == [{"transport": "stdio"}]


def test_main_converts_fatal_error_to_exit_code(monkeypatch):
    from mcp_comm_server import server

    monkeypatch.setattr(server, "configure_logging", lambda level: None)
    monkeypatch.setattr(server, "_close_serial_ports", lambda: None)

    def explode(config):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "run_server", explode)
    assert server.main([]) == 1


def test_main_converts_keyboard_interrupt_to_130(monkeypatch):
    from mcp_comm_server import server

    monkeypatch.setattr(server, "configure_logging", lambda level: None)
    monkeypatch.setattr(server, "_close_serial_ports", lambda: None)

    def interrupt(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(server, "run_server", interrupt)
    assert server.main([]) == 130
