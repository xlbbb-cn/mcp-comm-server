"""End-to-end smoke test: start the server over stdio and call every tool.

Run it directly (not collected by pytest):

    python scripts/mcp_smoke.py

It spawns ``python -m mcp_comm_server``, performs the MCP handshake, lists the
tools, then calls ``list_comm``, ``set_comm``, ``write_read_data_comm`` and
``get_comm`` against the pyserial ``loop://`` loopback port, so no serial
hardware is needed.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

TIMEOUT = 20.0

# pyserial may not be able to use loop:// on old versions; it has shipped since 3.0
LOOPBACK = "loop://"


class StdioClient:
    def __init__(self, command: list[str]) -> None:
        env = dict(os.environ)
        env["MCP_COMM_LOG_LEVEL"] = "WARNING"
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        self._queue: queue.Queue[str] = queue.Queue()
        self._stderr: list[str] = []
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._id = 0

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._queue.put(line)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line.rstrip())

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        request_id = self._id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                line = self._queue.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"server exited with code {self.proc.returncode}: {self.stderr()}"
                    )
                continue
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})
        raise TimeoutError(f"no response for {method} within {TIMEOUT}s: {self.stderr()}")

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def stderr(self) -> str:
        return "\n".join(self._stderr[-20:])

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup in a smoke script
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - fall back to kill
            self.proc.kill()


def call_tool(client: StdioClient, name: str, arguments: dict) -> dict:
    result = client.request("tools/call", {"name": name, "arguments": arguments})
    content = result.get("content") or []
    if result.get("isError"):
        raise RuntimeError(f"tool {name} returned isError: {content}")
    text = next((item["text"] for item in content if item.get("type") == "text"), "{}")
    return json.loads(text)


def main() -> int:
    command = [sys.executable, "-m", "mcp_comm_server"]
    client = StdioClient(command)
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {label}{(' - ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(label)

    try:
        init = client.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-comm-smoke", "version": "0.1.0"},
            },
        )
        check("initialize handshake", bool(init.get("serverInfo")), json.dumps(init)[:200])
        client.notify("notifications/initialized")

        tools = client.request("tools/list").get("tools", [])
        names = sorted(tool["name"] for tool in tools)
        expected = ["get_comm", "list_comm", "set_comm", "write_read_data_comm"]
        check("tools/list exposes 4 tools", names == expected, f"got {names}")
        check(
            "every tool documents its inputs and outputs",
            all("inputSchema" in tool and "outputSchema" in tool for tool in tools),
        )

        listed = call_tool(client, "list_comm", {})
        check("list_comm returns ok", listed.get("status") == "ok", listed.get("message", ""))
        check("list_comm reports a port list", isinstance(listed.get("ports"), list))

        opened = call_tool(client, "set_comm", {"port": LOOPBACK, "baudrate": 115200, "timeout": 0.2})
        check("set_comm opens loopback", opened.get("status") == "ok" and opened.get("is_open") is True,
              opened.get("message", ""))

        io_result = call_tool(
            client,
            "write_read_data_comm",
            {
                "port": LOOPBACK,
                "data": "AT",
                "append_terminator": "\\r\\n",
                "read_timeout": 0.2,
            },
        )
        check("write_read_data_comm writes 4 bytes", io_result.get("bytes_written") == 4,
              json.dumps(io_result)[:200])
        check("loopback echoes the payload", io_result.get("response_hex") == "41540d0a",
              json.dumps(io_result)[:200])

        state = call_tool(client, "get_comm", {"port": LOOPBACK})
        check("get_comm reports the open port", state.get("is_open") is True,
              json.dumps(state)[:200])

        closed = call_tool(client, "set_comm", {"port": LOOPBACK, "close": True})
        check("set_comm closes the port",
              closed.get("closed") is True and closed.get("is_open") is False,
              json.dumps(closed)[:200])

        bad = call_tool(client, "write_read_data_comm", {"port": "", "data": "AT"})
        check("errors are returned as status=error", bad.get("status") == "error", json.dumps(bad)[:200])
    finally:
        client.close()

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\nMCP stdio smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
