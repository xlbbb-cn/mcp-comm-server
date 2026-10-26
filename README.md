# mcp-comm-server

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets an LLM (Claude Desktop,
Cursor, or any MCP client) talk to devices over a **serial port** — RS-232 / RS-485 / UART,
`COM3` on Windows, `/dev/ttyUSB0` on Linux, `/dev/tty.usbserial-*` on macOS.

Built on Python 3.11+, [`mcp`](https://github.com/modelcontextprotocol/python-sdk) 2.x and
[`pyserial`](https://github.com/pyserial/pyserial). The architecture is described in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Tools

| Tool | Purpose | Required arguments |
| --- | --- | --- |
| `list_comm` | Enumerate the serial ports of this machine | — |
| `get_comm` | Inspect server defaults, open connections, or one port's config | — |
| `set_comm` | Configure a port and open it (or close it) | `port` |
| `write_read_data_comm` | Write a payload, then read the device response | `port`, `data` |

Every tool returns a JSON object with `status: "ok"` or `status: "error"`; errors carry `code`,
`message` and a `next_step` hint, so the model can recover instead of guessing.

## Requirements

- Python **3.11+**
- Windows, Linux or macOS
- A serial device (or a pyserial URL such as `loop://` for testing without hardware)

## Install

```bash
cd mcp-comm-server

# with uv (recommended)
uv venv
uv pip install -e ".[dev]"

# or plain pip
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

The console script and module entry point are equivalent after installation:

```bash
mcp-comm-server --help
python -m mcp_comm_server --help
```

## Run

The server speaks MCP over **stdio only** — that is what desktop MCP clients
expect, and no transport flag is needed:

```bash
python -m mcp_comm_server
# or
mcp-comm-server
```

### Command-line options

| Flag | Default | Description |
| --- | --- | --- |
| `--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` | `INFO` | stderr log verbosity. |
| `--version` | — | Print the version and exit. |
| `-h`, `--help` | — | Show help and exit. |

### Environment variables

The flag has an environment-variable equivalent. Precedence is:

```text
command-line flag  >  MCP_COMM_* environment variable  >  built-in default
```

| Variable | Default | Description |
| --- | --- | --- |
| `MCP_COMM_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

`.env` files are **not** loaded automatically. MCP clients pass the environment explicitly in
`mcpServers.env`; `.env.example` is documentation only.

### Logs, exit codes and errors

- Logs are written to **stderr** at all times. stdout is reserved for the MCP protocol, so adding
  `print()` calls to the server or its dependencies would corrupt stdio sessions.
- Config errors exit with code `2` and print a single `mcp-comm-server: error: ...` line.
- Unexpected fatal errors are logged with a traceback and exit with code `1`.
- Ctrl-C exits with code `130`.
- Open serial ports are closed on every exit path, including Ctrl-C.

## Connect it to Claude Desktop

Claude Desktop configuration lives here:

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

Add a `serial` server entry, using an **absolute** interpreter path. MCP clients do not inherit an
activated shell or its `PATH`, so `python` alone often points at the wrong interpreter.

```json
{
  "mcpServers": {
    "serial": {
      "command": "D:/aichat-ws/mcp-comm-server/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_comm_server"],
      "cwd": "D:/aichat-ws/mcp-comm-server",
      "env": {
        "MCP_COMM_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

macOS / Linux variant:

```json
{
  "mcpServers": {
    "serial": {
      "command": "/absolute/path/to/mcp-comm-server/.venv/bin/python",
      "args": ["-m", "mcp_comm_server"],
      "cwd": "/absolute/path/to/mcp-comm-server",
      "env": { "MCP_COMM_LOG_LEVEL": "INFO" }
    }
  }
}
```

If the package is installed globally and the executable is on the client's `PATH`, this also works:

```json
{
  "mcpServers": {
    "serial": { "command": "mcp-comm-server" }
  }
}
```

After saving the file, fully quit and restart Claude Desktop (reloading the window is not enough).
The tools then appear under the hammer/tools icon. Desktop logs usually live at
`%APPDATA%\Claude\logs\` (Windows) or `~/Library/Logs/Claude/` (macOS); check them first if the
server does not appear.

> The server always uses the stdio transport. Claude Desktop's `mcpServers` block launches stdio
> servers, so no transport option is needed or accepted.

## Usage walkthrough

```text
1. list_comm()                                            -> find "COM3"
2. set_comm(port="COM3", baudrate=115200, parity="N",
            stopbits=1, timeout=0.5)
3. write_read_data_comm(port="COM3", data="AT\\r\\n", read_timeout=1.0)
4. get_comm(port="COM3")                                  -> verify state
5. set_comm(port="COM3", close=true)                      -> release the port
```

### Result envelope

Success:

```json
{ "status": "ok", "ok": true, "...": "tool specific fields" }
```

Failure — tools never raise into the MCP client:

```json
{
  "status": "error",
  "ok": false,
  "code": "PORT_BUSY",
  "message": "cannot open COM3: access denied",
  "next_step": "close other programs using the port, then retry",
  "port": "COM3"
}
```

---

## Tool reference

### `list_comm`

Enumerate serial ports. Call this first to discover the `port` value used by the other tools.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `include_open` | boolean | `true` | Add `is_open` per port (open in this server process). |

Call:

```json
{ "include_open": true }
```

Response (abridged):

```json
{
  "status": "ok",
  "ok": true,
  "count": 1,
  "ports": [
    {
      "device": "COM3",
      "name": "COM3",
      "description": "USB-SERIAL CH340",
      "hwid": "USB VID:PID=1A86:7523",
      "vid": 6790,
      "pid": 29987,
      "serial_number": null,
      "manufacturer": "wch.cn",
      "product": "USB-SERIAL CH340",
      "interface": null,
      "location": "1-3",
      "is_open": false
    }
  ]
}
```

### `get_comm`

Query server defaults and open connections, or one port's stored/live configuration.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `port` | string or null | `null` | Port name, e.g. `"COM3"` or `"/dev/ttyUSB0"`. Omit for a summary. |

Calls:

```json
{}
```

```json
{ "port": "COM3" }
```

Without `port`:

```json
{
  "status": "ok",
  "ok": true,
  "defaults": {
    "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1.0,
    "timeout": 1.0, "write_timeout": null,
    "xonxoff": false, "rtscts": false, "dsrdtr": false
  },
  "connections": [],
  "open_count": 0
}
```

With `port`:

```json
{
  "status": "ok",
  "ok": true,
  "port": "COM3",
  "is_open": true,
  "source": "live",
  "config": { "baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1.0,
              "timeout": 0.5, "write_timeout": null,
              "xonxoff": false, "rtscts": false, "dsrdtr": false },
  "settings": { "...": "same as config" },
  "in_waiting": 0
}
```

`source` is `live` (read from the open handle), `stored` (saved for the port) or `default` (server
defaults).

### `set_comm`

Configure a port and/or open or close it. Only supplied parameters change; omitted ones keep the
value stored for that port. Settings survive `close` and are reused by the next
`write_read_data_comm`.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `port` | string | **required** | Port name from `list_comm`, or a pyserial URL. |
| `baudrate` | integer | unchanged | e.g. `9600`, `57600`, `115200`, `921600`. |
| `bytesize` | integer | unchanged | Data bits: `5`, `6`, `7`, `8`. |
| `parity` | string | unchanged | `N`/`E`/`O`/`M`/`S`; long names like `"even"` accepted. |
| `stopbits` | number | unchanged | `1`, `1.5`, `2`. |
| `timeout` | number | unchanged | Default read timeout in seconds; `0` = non-blocking. |
| `write_timeout` | number | unchanged | Write timeout in seconds; omit for blocking writes. |
| `xonxoff` | boolean | unchanged | Software flow control. |
| `rtscts` | boolean | unchanged | Hardware RTS/CTS flow control. |
| `dsrdtr` | boolean | unchanged | Hardware DSR/DTR flow control. |
| `connect` | boolean | `true` | Open after applying settings. |
| `close` | boolean | `false` | Close instead of opening; takes precedence over `connect`; settings kept. |

Open a device at 115200 8N1:

```json
{
  "port": "COM3",
  "baudrate": 115200,
  "bytesize": 8,
  "parity": "N",
  "stopbits": 1,
  "timeout": 0.5
}
```

Store settings without opening:

```json
{ "port": "COM3", "baudrate": 9600, "connect": false }
```

Release the port (settings are kept):

```json
{ "port": "COM3", "close": true }
```

Open response (abridged):

```json
{
  "status": "ok",
  "ok": true,
  "port": "COM3",
  "is_open": true,
  "source": "live",
  "config": { "baudrate": 115200, "bytesize": 8, "parity": "N", "stopbits": 1.0,
              "timeout": 0.5, "write_timeout": null,
              "xonxoff": false, "rtscts": false, "dsrdtr": false },
  "settings": { "...": "same as config" },
  "changed": ["baudrate", "bytesize", "parity", "stopbits", "timeout"]
}
```

Close response:

```json
{ "status": "ok", "ok": true, "port": "COM3", "closed": true, "is_open": false,
  "source": "stored", "config": { "...": "retained settings" } }
```

### `write_read_data_comm`

Write a payload, then read the device response. An open connection from `set_comm` is reused; if it
is closed it is opened with the stored configuration.

| Argument | Type | Default | Description |
| --- | --- | --- | --- |
| `port` | string | **required** | Port name, e.g. `"COM3"`. |
| `data` | string | **required** | Payload interpreted according to `data_format`. |
| `data_format` | enum | `"str"` | `str`, `text`, `ascii`, `hex`, `base64`. |
| `encoding` | string | `"utf-8"` | Text encoding for str/text and response decoding. |
| `expected_bytes` | integer | `null` | Read exactly N bytes (bounded by `read_timeout`). |
| `read_delimiter` | string | `null` | Stop at this terminator, e.g. `"\r\n"` or `"OK"`. |
| `read_timeout` | number | port timeout | Seconds to wait for the response. |
| `write_timeout` | number | port setting | Per-transaction write timeout. |
| `append_terminator` | string | `null` | Text appended before sending, e.g. `"\r\n"` (text formats). |
| `delay_before_read` | number | `0.0` | Seconds between write and read (slow devices). |
| `flush_input` | boolean | `true` | Discard stale buffered bytes before writing. |
| `response_format` | enum | `"both"` | `text`, `hex` or `both`. |
| `baudrate` | integer | `null` | Used only when the port must be opened. |
| `connect_if_closed` | boolean | `true` | Auto-open a closed port; `false` fails with `PORT_NOT_OPEN`. |

Payload formats:

* `str` / `text` (default) — escape sequences are expanded: `"AT\\r\\n"` sends `AT<CR><LF>`.
* `ascii` — ASCII text with the same escape expansion.
* `hex` — `"01 03 00 00 00 0A"` (spaces, commas, `0x`, `-`, `:` are ignored).
* `base64` — standard base64, e.g. `"QVQNCg=="`.

Reading priority:

1. `expected_bytes` — read exactly N bytes, stopping early at `read_timeout`.
2. `read_delimiter` — stop as soon as the terminator appears (included in the response).
3. otherwise — read until the line is idle for `read_timeout`.

Modbus-style hex request:

```json
{
  "port": "COM3",
  "data": "01 03 00 00 00 0A",
  "data_format": "hex",
  "expected_bytes": 25,
  "read_timeout": 1.0
}
```

AT command with a text reply:

```json
{
  "port": "COM3",
  "data": "AT+GMR",
  "append_terminator": "\\r\\n",
  "read_delimiter": "OK",
  "read_timeout": 2.0
}
```

ASCII response after a slow device wakes up:

```json
{
  "port": "COM3",
  "data": "PING",
  "append_terminator": "\\r\\n",
  "delay_before_read": 0.2,
  "read_timeout": 2.0,
  "response_format": "both"
}
```

Response (abridged):

```json
{
  "status": "ok",
  "ok": true,
  "port": "COM3",
  "bytes_written": 6,
  "bytes_read": 5,
  "data": "hello\\r\\n",
  "data_text": "hello\\r\\n",
  "data_hex": "68656c6c6f0d0a",
  "data_dump": "68 65 6C 6C 6F 0D 0A",
  "written": "AT+GMR\\r\\n",
  "written_hex": "41542b474d520d0a",
  "response_text": "hello\\r\\n",
  "response_hex": "68656c6c6f0d0a",
  "complete": true,
  "timed_out": false,
  "elapsed_ms": 12.4,
  "read_timeout": 2.0,
  "expected_bytes": null,
  "read_delimiter": "OK",
  "data_format": "str",
  "encoding": "utf-8"
}
```

`data_text` / `response_text` keep control characters escaped (`\r`, `\n`, `\t`) so replies stay
readable in JSON; `data_hex` / `response_hex` / `data_dump` are byte-exact. An empty read is not an
exception: it returns `status: "ok"` with `empty_response: true` and a `hint` about power, wiring,
baud rate and timeout.

## Test without hardware

pyserial ships URL-based transports, which is how the test suite runs with no serial device:

* `loop://` — loopback: everything written is read back
* `socket://127.0.0.1:7000` — serial-over-TCP (ser2net, ESP-Link, …)
* `spy://COM3?file=session.log` — tap an existing connection

```bash
pytest                              # unit + tool tests on loop://
python scripts/mcp_smoke.py         # full MCP handshake + all four tools over stdio
```

`scripts/mcp_smoke.py` spawns the real server, performs `initialize`, `tools/list`, then calls
`list_comm`, `set_comm`, `write_read_data_comm`, `get_comm` and closes the port.

You can also try the four tools manually against loopback:

```json
{ "port": "loop://", "baudrate": 115200, "timeout": 0.2 }
```

```json
{ "port": "loop://", "data": "AT", "append_terminator": "\\r\\n", "read_timeout": 0.2 }
```

## FAQ and troubleshooting

### `Permission denied` when opening a port

* **Linux:** add your user to the serial group and re-login (group changes do not apply to existing
  sessions):

  ```bash
  sudo usermod -aG dialout "$USER"
  # some distributions use uucp instead of dialout
  ```

  Check with `ls -l /dev/ttyUSB0`; the owning group must match.
* **macOS:** use the callout device (`/dev/cu.usbserial-*` or `/dev/cu.SLAB_USBtoUART`) rather than
  the dial-in `/dev/tty.*` device. Install the USB-serial driver (CH340/CP210x/FTDI) if no device
  appears.
* **Windows:** install the vendor driver and check Device Manager for a warning icon. A port can
  also be reserved by another process; see the next item.

### `PORT_BUSY` / `Access is denied` / "could not open port"

A serial port allows one owner at a time. Close whatever else has it open:

* Arduino IDE Serial Monitor, PuTTY, Tera Term, vendor configuration tools, another agent terminal.
* Other instances of this MCP server. One process can share a port between its own tool calls, but
  two processes cannot.
* On Windows, unplug/replug the adapter if a crashed application left the handle behind; a reboot
  clears it. `Get-CimInstance Win32_SerialPort | Select DeviceID,Description` confirms the OS name.
* In WSL/Docker, the device is not passed through by default; expose it explicitly or run the
  server on the host.

After closing the other program, retry `set_comm`; no server restart is needed.

### The port is not listed by `list_comm`

* Check the cable and whether the board is powered; some adapters enumerate only when the device is
  on.
* Install the USB-serial driver (CH340, CP210x, FTDI, PL2303).
* Linux: check `dmesg | tail` after plugging in and confirm `/dev/ttyUSB*` or `/dev/ttyACM*` exists.
* macOS: look for `/dev/cu.*`.
* Virtual COM ports created by Bluetooth or vendor software may not report a description; use the
  `device` value directly.

### Empty response or `READ_TIMEOUT`

* Baud rate / parity / stop bits must match the device exactly.
* Many devices need a terminator (`\r`, `\n` or `\r\n`); set `append_terminator`.
* Swap TX/RX if you wired the adapter yourself; RS-485 adapters may need direction control.
* Increase `read_timeout`, or use `delay_before_read` for devices that boot slowly.
* Prefer `expected_bytes` when the reply length is known — it is more reliable than idle detection.
* The device may echo the command; `flush_input=true` (default) clears stale bytes before writing.

### Garbled text / wrong bytes

Use `data_format: "hex"` for binary protocols and compare `data_hex` / `response_hex` instead of
text. For text, set the correct `encoding` (e.g. `latin-1` for some instruments).

### Claude Desktop does not show the tools

* Validate the JSON (no trailing commas) and use an **absolute** `command` path to the virtualenv
  interpreter.
* Include `"cwd": "D:/path/to/mcp-comm-server"` so `-m mcp_comm_server` resolves.
* Fully quit and restart Claude Desktop.
* Set `"MCP_COMM_LOG_LEVEL": "DEBUG"` and inspect the client logs.
* Run the exact command from a terminal first: if it fails there, it will fail in the client too.

### Is there an HTTP/SSE transport?

No. The server is **stdio only** by design: that is how desktop MCP clients launch a local server,
and it keeps the process bound to its user with no listening socket and no authentication to get
wrong. There is no `--transport`, `--host` or `--port` flag — passing one exits with code `2` — and
leftover `MCP_COMM_TRANSPORT` / `MCP_COMM_HOST` / `MCP_COMM_PORT` variables in an old client config
are ignored (the process still speaks stdio, so nothing listens). If a network endpoint is ever
required, put a proxy in front of the stdio process instead of adding a transport here.

## Project layout

```text
mcp-comm-server/
├── pyproject.toml
├── README.md
├── .env.example                      # env variable reference (not auto-loaded)
├── docs/ARCHITECTURE.md              # design and milestones
├── scripts/mcp_smoke.py              # end-to-end stdio smoke test
├── tests/
│   ├── test_comm_tools.py            # core layer + all four tools (loop://)
│   ├── test_config.py                # CLI/env precedence and validation
│   ├── test_serial_manager.py        # SerialManager unit tests (fake device)
│   ├── test_integration_serial.py    # lifecycle, timeout, concurrency faults
│   ├── serial_fakes.py               # fake pyserial factory used by the tests
│   └── test_server_registration.py   # tools/list contains the four tools
└── src/mcp_comm_server/
    ├── __main__.py                   # python -m mcp_comm_server
    ├── config.py                     # ServerConfig: CLI + MCP_COMM_* parsing
    ├── server.py                     # MCPServer instance, registration, main()
    ├── schemas.py                    # CommResult output schema
    ├── core/serial_manager.py        # thread-safe SerialManager (open/read/write)
    ├── core/serial.py                # stable import path / re-export
    ├── tools/comm.py                 # the four MCP tools (validation, errors)
    └── utils/serial_utils.py         # parity/stopbits/bytesize, hex & text codecs
```

## Notes and limitations

* One server process keeps **one open handle per port**, shared by all tool calls. A port held by
  another program (terminal, vendor tool) cannot be opened — close it first.
* `set_comm` does not toggle DTR/RTS/break lines or flush buffers directly; use a device that
  resets on open, or the `reset_input_buffer()` side effect of `flush_input=true`.
* For slow devices raise `read_timeout`, or insert `delay_before_read` (e.g. `0.2`) before reading.
* Configuration is per process and is not persisted; a restart returns to defaults.
