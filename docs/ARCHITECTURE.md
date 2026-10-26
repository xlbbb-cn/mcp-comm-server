# mcp-comm-server 架构设计

> 状态：设计稿（M0）。本文只定义架构、目录、依赖、配置与启动方式，不含具体实现。
> 项目名按仓库实际目录写作 `mcp-comm-server`（需求中的 `mcp-com-server` 视为同一项目）。

---

## 1. 目标与范围

**目标**

- 以一个 MCP 服务端进程，把本机串口（RS-232 / RS-485 / UART）能力暴露给 MCP 客户端。
- 提供四个工具：`list_comm`、`get_comm`、`set_comm`、`write_read_data_comm`。
- 单机、单进程、按端口名管理连接；配置可复用，连接可关闭。
- 收发以 **字节** 为单位，不解释上层协议（Modbus、AT、自定义帧由调用方负责）。

**非目标（v1 明确不做）**

- 多用户鉴权、TLS、公网暴露（传输固定为 stdio）。
- 多进程共享同一串口 / 端口池调度。
- 自动协议解析、定时轮询、后台采集。
- 配置持久化到磁盘（v1 配置只存在于进程内存，重启丢失）。

---

## 2. 运行时与技术决策

| 项 | 决策 | 理由 |
| --- | --- | --- |
| Python | **3.11+**（`requires-python = ">=3.11"`） | 当前 venv 为 3.11.17；3.10 已进入 EOL 周期；`mcp` 2.x 本身要求 `>=3.10`。 |
| MCP SDK | **`mcp>=2.3,<3`**，使用 `mcp.server.mcpserver.MCPServer` | 2026 年当前主线为 2.x；1.x 的 `FastMCP` 在 2.x 已移除（导入即报迁移错误）。 |
| 串口库 | **`pyserial>=3.5,<4`** | 事实标准；自带 `loop://`、`socket://`、`spy://` URL handler，可在无硬件时测试全链路。 |
| 并发模型 | 工具函数保持 **同步**，由 SDK 调度到工作线程；`SerialManager` 内部用锁保护 | pyserial 是阻塞 IO，不能在事件循环里直接调用。 |
| 配置来源 | 环境变量 + 工具入参；**不引入** `pydantic-settings` / `python-dotenv` | 减少依赖；MCP 客户端本身通过 `env` 传参。 |
| 传输 | **仅 stdio**（`mcp.run(transport="stdio")`） | stdio 是 MCP 客户端拉起本地服务的标准方式；SSE / Streamable HTTP 已移除，不再提供网络监听。 |
| 日志 | 全部写 **stderr**，stdout 仅用于 MCP 协议 | stdio 模式下任何 `print()` 都会破坏协议帧。 |

> **现状**：`src/` 已迁移到 mcp 2.x：使用 `mcp.server.mcpserver.MCPServer` 与
> `@mcp.tool()` 注册四个工具，串口领域层为包内的
> `mcp_comm_server.core.serial_manager.SerialManager`（无跨目录依赖，
> 可独立安装）。`pytest`、`ruff` 与 stdio 冒烟测试均通过。

---

## 3. 分层架构

依赖方向严格自上而下，下层不感知 MCP：

```
┌──────────────────────────────────────────────────────────┐
│ L0  Entry（stdio 唯一传输）                               │
│     stdio（唯一传输）                                     │
│     __main__.py · config.py · server.main()              │
├──────────────────────────────────────────────────────────┤
│ L1  Server / Registration                                │
│     MCPServer 实例、instructions、工具注册、生命周期关闭    │
│     server.py                                            │
├──────────────────────────────────────────────────────────┤
│ L2  Tool Layer (MCP 契约层)                               │
│     参数校验、格式转换、错误信封、JSON 序列化               │
│     tools/comm.py                                        │
├──────────────────────────────────────────────────────────┤
│ L3  Core (串口领域层)                                     │
│     端口枚举、连接缓存、按端口配置、加锁、超时读写           │
│     core/serial_manager.py (SerialManager) · core/serial.py │
├──────────────────────────────────────────────────────────┤
│ L4  Utils (纯函数)                                        │
│     parity/stopbits/bytesize 解析、text/hex/base64 编解码  │
│     utils/serial_utils.py                                │
└──────────────────────────────────────────────────────────┘
```

**分层规则**

- L2 不直接 `import serial`，只依赖 `SerialManager` / `SerialError`。
- L3 不返回 MCP 类型，只返回 Python 原生结构；失败统一抛 `SerialError`。
- L4 无状态、无 IO，可被单独单测。
- 配置只在 L0/L1 读取一次，L2/L3 通过参数接收，避免散落的 `os.environ`。

---

## 4. 目录结构

```text
mcp-comm-server/
├── pyproject.toml                     # 元数据、依赖、console script、setuptools src 布局
├── requirements.txt                   # 运行时依赖（锁定上界）
├── requirements-dev.txt               # 开发/测试依赖
├── README.md                          # 安装、工具说明、客户端接入
├── .env.example                       # 环境变量样例（仅开发参考）
├── docs/
│   └── ARCHITECTURE.md                # 本文
├── scripts/
│   └── mcp_smoke.py                   # stdio 端到端冒烟：initialize + tools/list + 四个工具
├── src/
│   └── mcp_comm_server/
│       ├── __init__.py                # 版本号
│       ├── __main__.py                # python -m mcp_comm_server
│       ├── config.py                  # ServerConfig：环境变量/CLI 解析与校验
│       ├── server.py                  # MCPServer 实例 + 注册 + run() 固定 stdio
│       ├── core/
│       │   ├── __init__.py            # 导出 SerialManager/SerialError
│       │   └── serial.py              # 串口管理器
│       ├── tools/
│       │   ├── __init__.py
│       │   └── comm.py                # 四个 MCP 工具
│       └── utils/
│           ├── __init__.py
│           └── serial_utils.py        # 纯函数：参数解析 + payload 编解码
└── tests/
    ├── test_comm_tools.py             # utils + core + tools（loop://）
    └── test_server_registration.py    # tools/list 恰好包含四个工具
```

**占位说明**

| 文件 | 状态 |
| --- | --- |
| `docs/ARCHITECTURE.md`、`.env.example`、`requirements-dev.txt` | 设计交付物 |
| `src/mcp_comm_server/config.py` | 已接线：`ServerConfig` + `load_config()` + CLI |
| 其余 `src/` `tests/` `scripts/` | 已存在实现 |

> 根目录的 `server.py` 是早期单文件低层 API 草稿，与 `src/` 包重复；迁移完成后应删除或移入 `docs/legacy/`。

---

## 5. 配置设计

### 5.1 优先级

```text
工具入参  >  该端口已保存的配置  >  ServerConfig 默认值（环境变量）
```

`set_comm(connect=False)` 只写“该端口已保存配置”，不打开端口；配置在进程存活期间一直有效。

### 5.2 环境变量

| 变量 | 默认 | 说明 | 状态 |
| --- | --- | --- | --- |
| `MCP_COMM_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | 已支持 |
| `MCP_COMM_DEFAULT_BAUDRATE` | `9600` | 新开端口默认波特率 | 后续版本 |
| `MCP_COMM_DEFAULT_TIMEOUT` | `1.0` | 默认读超时（秒） | 后续版本 |
| `MCP_COMM_MAX_READ_BYTES` | `1048576` | 单次读取硬上限，防无限读 | 后续版本 |
| `MCP_COMM_ALLOWED_PORTS` | 空（不限制） | 逗号分隔白名单（可选限制） | 后续版本 |

### 5.3 约束

- 不读取 `.env` 文件（避免隐式副作用）；`.env.example` 只作文档。客户端在 `mcpServers.env` 中显式传入。
- 不持久化配置；重启后回到默认值。需要持久化时再引入 `platformdirs` + JSON，属于后续版本。
- 串口参数无敏感信息，不做 secret 管理。
- 仅支持 stdio，不监听网络端口，也不提供网络鉴权；跨机使用需通过 SSH 等外部通道。

---

## 6. 串口管理器（L3）

### 6.1 职责

- 枚举系统串口（含描述、VID/PID、序列号、是否已被本进程打开）。
- 缓存 `port -> serial.Serial` 连接，同一端口在进程内只保留一个句柄。
- 保存每端口配置与全局默认值；关闭端口后配置仍保留。
- 提供带超时的 `write` / `read` / `write_read`。
- 线程安全；进程退出时关闭全部端口。

### 6.2 内部状态

```text
_registry_lock : RLock                      # 保护下面三个字典
_connections   : dict[str, serial.Serial]   # 已打开连接
_configs       : dict[str, SerialConfig]    # 每端口配置
_defaults      : SerialConfig               # 新端口默认配置
_port_locks    : dict[str, RLock]           # 每端口事务锁（写+读原子性）
```

### 6.3 公开 API

| 方法 | 作用 | 失败 |
| --- | --- | --- |
| `list_ports()` | 枚举端口，附 `is_open` | `SerialError` |
| `open(port, **params)` | 打开或复用连接，应用配置 | `SerialError` |
| `close(port=None)` | 关闭单端口 / 全部 | 返回 `bool` |
| `describe(port)` | 配置 + 实时连接状态 | `SerialError` |
| `list_connections()` | 所有已打开连接 | — |
| `save_config(port, **params)` | 只存配置不打开 | `SerialError` |
| `write(port, data, ...)` | 写字节 | `SerialError` |
| `read(port, size, timeout, delimiter)` | 读字节 | `SerialError` |
| `write_read(port, data, ...)` | 事务：写后读 | `SerialError` |

### 6.4 并发与锁

- 注册表操作持 `_registry_lock`。
- `write_read` 期间额外持有该端口的 `_port_locks[port]`，保证一次“写—读”事务不被其他调用插入。
- `close` 需要等待该端口事务锁释放，避免关闭正在读写的句柄。
- 不允许在持有锁时做长时间阻塞 IO 之外的等待；读超时必须有上限。

### 6.5 读取策略

按优先级三选一，均受**总 deadline** 约束：

1. `expected_bytes`：读满 N 字节即返回；超时返回已收到的部分。
2. `read_delimiter`：出现终止符即返回（包含终止符）。
3. 空闲判定：连续 `read_timeout` 无新字节即返回。

附加保护：`max_read_bytes` 硬上限；`expected_bytes <= 0` 视为参数错误。

### 6.6 错误模型

- 底层 `serial.SerialException` / `SerialTimeoutException` / `OSError` 一律包装为 `SerialError`，附端口与操作上下文。
- 工具层再把 `SerialError` 转成 `{"status": "error", "message", "next_step"}`，不把异常抛给 MCP 客户端。
- 连接失效（拔线）时从缓存移除，下一次调用重新打开。

### 6.7 测试友好

端口名允许 pyserial URL：`loop://`（回环）、`socket://host:port`（串口服务器）、`spy://COM3?file=log`（旁路抓包）。CI 全部使用 `loop://`，不需要硬件。

---

## 7. 工具层契约（L2）

### 7.1 统一返回信封

成功：

```json
{ "status": "ok", "...": "tool specific fields" }
```

失败：

```json
{ "status": "error", "message": "human readable", "next_step": "actionable hint", "port": "COM3" }
```

约定：工具永不抛异常；所有字段 JSON 可序列化；文本响应中的控制字符以 `\r` `\n` 转义，同时提供 hex 以保证字节精确。

### 7.2 工具定义

| 工具 | 入参 | 返回要点 |
| --- | --- | --- |
| `list_comm` | `include_open: bool = True` | `count`、`ports[]`（`device`、`description`、`hwid`、`vid`、`pid`、`serial_number`、`manufacturer`、`product`、`interface`、`is_open`） |
| `get_comm` | `port: str \| None` | `defaults`、`open_count`、`connections[]`；传 `port` 时附该端口 `config` / `settings` / `is_open` / `in_waiting` |
| `set_comm` | `port`、可选 `baudrate`/`bytesize`/`parity`/`stopbits`/`timeout`/`write_timeout`/`xonxoff`/`rtscts`/`dsrdtr`、`connect=True`、`close=False` | 端口状态 + 生效配置；只改传入的参数；`close=True` 时返回 `closed` |
| `write_read_data_comm` | `port`、`data`、`data_format=str\|text\|ascii\|hex\|base64`、`encoding="utf-8"`、`expected_bytes`、`read_delimiter`、`read_timeout`、`append_terminator`、`delay_before_read`、`flush_input=True`、`response_format=text\|hex\|both` | `bytes_written`、`bytes_read`、`written_*`、`data`/`data_text`/`data_hex`/`data_dump`、`response_text`/`response_hex`、`elapsed_ms` |

### 7.3 语义要点

- `set_comm` 是“配置 + 打开”的组合：只修改传入参数；`connect=False` 只存配置；`close=True` 关闭连接（配置保留）。
- `write_read_data_comm` 若端口未打开，按已保存配置自动打开；`connect_if_closed=False` 时报错而不是隐式打开。
- `data_format="text"`/`"str"` 时展开转义序列（`\r\n`、`\x41`）；`ascii` 限制为 ASCII；`hex` 忽略空格/逗号/`0x`；`base64` 直接解码。
- `read_delimiter` 同样支持转义文本；`expected_bytes` 优先于 delimiter。
- 空响应不算异常，返回 `empty_response: true` + `hint`，提示检查供电/接线/波特率。

---

## 8. 传输与启动

### 8.1 stdio（唯一传输）

```bash
# 开发：从源码运行
python -m mcp_comm_server

# 安装后
mcp-comm-server
```

客户端配置：

```json
{
  "mcpServers": {
    "comm": {
      "command": "D:/aichat-ws/mcp-comm-server/.venv/Scripts/python.exe",
      "args": ["-m", "mcp_comm_server"],
      "cwd": "D:/aichat-ws/mcp-comm-server",
      "env": { "MCP_COMM_LOG_LEVEL": "INFO" }
    }
  }
}
```

### 8.2 退出

`main()` 在 `finally` 中调用 `SerialManager.close()`；Ctrl-C 走同一路径。

---

## 9. 依赖清单

`requirements.txt`：

```text
mcp>=2.3,<3
pyserial>=3.5,<4
```

`requirements-dev.txt`：

```text
-r requirements.txt
pytest>=8.3
pytest-cov>=5.0
ruff>=0.6
```

`pyproject.toml` 对应片段：

```toml
[project]
requires-python = ">=3.11"
dependencies = ["mcp>=2.3,<3", "pyserial>=3.5,<4"]

[project.optional-dependencies]
dev = ["pytest>=8.3", "pytest-cov>=5.0", "ruff>=0.6"]

[project.scripts]
mcp-comm-server = "mcp_comm_server.server:main"

[tool.setuptools.packages.find]
where = ["src"]
```

> 若决定继续停留在 mcp 1.x，则依赖改为 `mcp>=1.9,<2`，并保留 `FastMCP`。**不要同时支持两套 API。**

---

## 10. 测试策略

| 层级 | 方式 | 覆盖 |
| --- | --- | --- |
| L4 utils | 纯单测 | parity/stopbits/bytesize、hex/base64/text 编解码、转义 |
| L3 core | `loop://` | 打开/复用/关闭、配置保留、超时、delimiter、部分读 |
| L2 tools | 直接调用 | 四个工具的成功/失败信封、参数校验 |
| L1 server | `mcp.list_tools()` | 恰好注册四个工具，schema/描述非空 |
| E2E | `scripts/mcp_smoke.py` | 子进程 stdio 握手 + 真实工具调用 |

CI 矩阵：Python 3.11 / 3.12 / 3.13，Windows + Linux；全部使用 `loop://`，不依赖硬件。

---

## 11. 迁移与实现状态

| 项 | 现状 | 说明 |
| --- | --- | --- |
| SDK API | `MCPServer` + `@mcp.tool()` | 已按 mcp 2.x 实现 |
| 已安装 SDK | 2.3.0 | 与代码一致 |
| 测试 | `pytest` 141 passed；stdio 冒烟通过 | 全部使用 `loop://`，不依赖硬件 |
| `pyproject` 入口 | `mcp_comm_server.server:main` | 正确指向包内入口 |
| Python 下限 | `>=3.11` | 与设计一致 |
| 依赖上界 | `mcp>=2.3,<3`、`pyserial>=3.5,<4` | 避免跨大版本破坏 |
| 配置 | 已收敛到 `config.py`（CLI > env > 默认） | 单一入口、可单测 |
| 旧 `server.py` 草稿 | 已归档到 `docs/legacy_stdio_draft.py`，未使用 | 避免误运行 mcp 1.x 桩实现 |
| 串口核心 | 包内 `core/serial_manager.py` | 工具层不重复实现领域逻辑；无跨目录 sys.path 依赖 |
| 并发 | t2 核心自带注册表锁 + 每端口事务锁 | `write_read` 原子执行 |

工具层已提供四个工具的统一错误信封（`status`/`ok`/`code`/`message`/
`next_step`/`details`）、`CommResult` 出参 schema，以及 str/text/ascii/hex/base64
入参格式。

---

## 12. 里程碑

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| M0 | 架构、目录、依赖、配置、启动方式 | 本文 + 清单 + 占位 |
| M1（完成） | 迁移 mcp 2.x、`config.py` 接线、修入口 | `pytest` 全绿，`mcp-comm-server` 可启动 |
| M2（完成） | 复用 t2 `SerialManager` 事务锁、读上限、连接失效处理 | core 单测覆盖异常路径 |
| M3（完成） | 工具层参数校验与错误信封收敛 | 四工具契约测试 |
| M4 | stdio 单传输、关闭生命周期 | 手工/脚本冒烟 |
| M5 | README、客户端示例、CI | 三平台矩阵通过 |
