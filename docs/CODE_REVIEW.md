# t1–t4 代码审查与修复报告

日期：2026-10-26  
范围：`mcp-comm-server`（t1 契约/架构、t2 `SerialManager` 核心、t3 工具与编解码、t4 启动配置与测试）  
方式：静态审查 + 故障注入（fake serial）+ 真实 `loop://` 集成 + stdio 端到端冒烟

## 1. 结论

**审查结论：修复后可以合入。** 初版在资源释放、无限超时、hex/base64 解析和工具返回信封上存在 1 个句柄泄漏组合缺陷和多个可复现的正确性问题；本次全部修复并补充了回归/集成测试。

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| pytest | 101 passed（两套目录分开跑） | **141 passed**（单包统一跑） |
| ruff | 未检查 | **All checks passed** |
| MCP stdio 冒烟 | 通过 | **通过（11 项）** |
| 并发用例 | 核心层 10 线程 | 核心层 + **工具层 12 线程** |
| 句柄泄漏 | `close()` 失败即丢句柄；`open()` 失败不回收半开句柄 | 均可重试/回收 |

## 2. 问题清单与修复

### P0 / 资源释放

1. **`close()` 先 `pop` 再 `close`，关闭失败即泄漏句柄**
   - 位置：`SerialManager.close()`
   - 现象：`conn.close()` 抛异常时连接已从 `_connections` 删除，第二次 `close()` 返回“port was not open”，OS 句柄无法再释放。
   - 修复：只在 `close()` 成功后才从注册表移除；失败时保留句柄并在 `details.retained` 标记，支持重试。若后端已报告关闭则清理陈旧条目。

2. **`open()` 失败不回收已创建的半开句柄**
   - 位置：`SerialManager.open()` 的 factory / `open()` 路径
   - 现象：`serial.Serial(...)` 构造成功后 `open()` 失败，异常直接返回，句柄泄漏。
   - 修复：新增 `_close_quietly()`；factory 返回 None、显式 `open()` 抛错、“did not open”三种路径都会尽力关闭。

3. **`reset()` 强制丢弃残留连接，且清空端口锁**
   - 位置：`SerialManager.reset()`
   - 风险：`close_all()` 失败后连接仍打开却被丢弃；同时 `_port_locks.clear()` 可能让两个线程对同一端口持有不同锁，破坏写读原子性。
   - 修复：reset 先 `close_all()`，再对残留句柄强制 `_close_quietly()`；**保留端口锁对象**（锁数量仅与出现过的端口名成正比）。

4. **shutdown 关闭失败只计数不记录**
   - 位置：`server._close_serial_ports()`
   - 修复：逐个记录 `failed[]` 的端口与原因，便于定位驱动/权限问题。

### P0 / 超时阻塞

5. **`timeout=None` 会无限阻塞并长期占用端口事务锁**
   - 位置：`SerialManager._read_from_connection()` / `read()` / `write_read()`
   - 现象：pyserial 的 `timeout=None` 表示永久阻塞；当端口配置为 `timeout=None` 且调用未传 `read_timeout` 时，`write_read` 可在持锁状态下永不返回。
   - 修复：新增 `DEFAULT_MAX_READ_TIMEOUT = 30.0`；所有读取的有效等待时间被 `max_read_timeout` 封顶（`None → 30s`，显式值取 `min`），返回值/错误 `details.timeout` 报告**实际生效**的超时。
   - 同时新增 `max_bytes` 参数，并拒绝 `expected_bytes > max_bytes`（默认 1 MiB）。

6. **非法超时参数在打开端口之后才校验**
   - 位置：`write_read_data_comm()`
   - 风险：`read_timeout=-1` 等参数会先打开串口再返回错误，留下副作用。
   - 修复：`read_timeout` / `write_timeout` / `delay_before_read` / `expected_bytes` / `baudrate` 全部在打开端口前校验（有限、非负、上限）。

### P1 / hex、base64、转义解析

7. **全局 `replace("0x", "")` 会静默篡改数据**
   - 现象：`"A0x1"` → `b"\xa1"`；`"0x"` → 空字节；错误输入被“修正”为合法数据。
   - 修复：按 token 拆分，仅去掉 token 开头的 `0x`/`0X`；`"0x41 0x42"` 仍正常，`"A0x1"`/`"0x"` 明确报 `INVALID_DATA`。

8. **base64 `validate=False` 吞掉非法字符**
   - 现象：`"!!!"` 解码为 `b""`，工具误报“payload is empty”；非法输入被当作空数据。
   - 修复：先去除空白，再用 `validate=True` 严格解码；空字符串仍返回 `b""`。

9. **`\xNN` 转义按字符重编码，破坏字节语义**
   - 现象：`"\\xff"` 经 UTF-8 编码成 `b"\xc3\xbf"`，而设备期望单字节 `0xFF`。
   - 修复：新增 `encode_escaped_text()`，在字节层展开转义；text/ascii/terminator/delimiter 全部改用它。`decode_escapes()` 保持展示语义并保留畸形转义原文。

10. **`decode_text()` 对未知编码仍会抛 `LookupError`**
    - 文档承诺“never raising”，但 fallback 又用同一个非法编码解码。
    - 修复：`LookupError` 时回退 `utf-8 + errors="replace"`。

11. **`make_json_safe()` 未处理 `memoryview` / 非有限浮点数**
    - 修复：`memoryview` 与 bytes/bytearray 一样转 hex；`NaN`/`Infinity` 转为字符串，避免产生非法 JSON 数字。

### P1 / 工具返回格式一致性

12. **空响应分支不可达**
    - 现象：核心 `write_read()` 无数据时返回 `READ_TIMEOUT` 错误，工具直接透传，导致文档中的 `empty_response + hint` 永远不会出现；与架构 §7.3“空响应不算异常”冲突。
    - 修复：工具层把 `READ_TIMEOUT` 转换为 `status="ok"`、`empty_response=true`、`timed_out=true`、`bytes_read=0`、`hint=...`，并保留 `bytes_written`、实际超时、写入 hex 等字段。

13. **`response_format` 未校验**
    - 现象：传入任意字符串仍返回 `ok`，但既无 `response_text` 也无 `response_hex`。
    - 修复：只接受 `text|hex|both`，大小写归一；非法值返回 `INVALID_PARAMETER`。

14. **`set_comm(close=True)` 静默丢弃参数**
    - 现象：`set_comm(port, baudrate=..., close=True)` 不校验、不保存参数；`baudrate=-1` 也返回 `ok`。
    - 修复：先 `set_params()` 校验并保存，再 `close()`；返回值补 `changed`。

15. **`connect_if_closed=False` + 非法端口返回错误码不一致**
    - 修复：工具层先校验端口非空，统一返回 `INVALID_PORT`（此前会返回 `PORT_NOT_OPEN`）。

16. **错误信封 `port` 类型不统一**
    - 修复：`_error()` 统一 `str(port)`，并支持从 `details.port` 提升到顶层；`_passthrough()` 继续做兜底。

17. **`_is_connection_open()` 遇到异常属性会击穿管理器**
    - 修复：捕获后端异常并视为未打开，避免 `is_open()`/`open()` 因驱动异常崩溃。

### t4 / 配置与启动

18. 复查 `config.py` 的 CLI > env > 默认优先级、端口范围、空白值回退，未发现需修改的问题。
19. `server.main()` 的 `finally` 关闭路径正确；本次只补充关闭失败日志（见第 4 条）。

## 3. 新增/补强的测试

`tests/test_integration_serial.py`（新增，16 个用例）：

- 失败 open 回收半开句柄；close 失败保留句柄并可重试；`reset()` 保持锁对象稳定。
- `loop://` 连续 15 次 open/close 不增长连接注册表。
- 端口 `timeout=None` 时读取仍被封顶到 30 s；`expected_bytes` 超限拒绝且不写数据。
- 工具层空响应 → `ok + empty_response + hint`，并遵守 `response_format`。
- 10 类非法参数在打开端口前被拒绝，且不留下已打开端口。
- 非法 hex/base64、`0x` token 前缀、`set_comm(close=True)` 参数持久化与校验。
- **12 线程并发 `write_read_data_comm`**，逐条校验响应与请求一一对应。

`tests/test_comm_tools.py`（补强）：

- 畸形转义保留、`\xFF` 单字节语义、hex token 前缀、base64 严格校验。
- 未知编码 `decode_text` 不抛异常、`memoryview` JSON 安全。
- 四工具成功/失败信封一致性 + `json.dumps()` 可序列化 + 错误必含 `code/message/next_step`。

`tests/test_serial_manager.py`（随核心迁入包内，补强）：

- 半开句柄回收、close 失败重试、读超时封顶（`read` 与 `write_read`）、超大 `expected_bytes`、reset 锁稳定性。

## 4. 验证命令与结果

```powershell
cd D:\aichat-ws\mcp-comm-server
.\.venv\Scripts\ruff.exe check src tests          # All checks passed
.\.venv\Scripts\python.exe -m pytest -q tests     # 141 passed
.\.venv\Scripts\python.exe scripts\mcp_smoke.py   # 11 项 PASS
```

覆盖情况：未生成 `--cov` 报告（当前 venv 未安装 `pytest-cov`），因此本报告不声称行覆盖率数字。

## 5. 未修复 / 已知残余风险

| 项 | 说明 | 建议 |
| --- | --- | --- |
| `write_timeout=None` 阻塞写 | pyserial 语义为永久阻塞；本模块未强制写上限 | 生产环境显式设置 `write_timeout`，或在 v2 增加默认写超时 |
| `delay_before_read` 无上限 | 仅校验非负，理论上可长时间持锁 | 增加上限（如 300 s）或改为可取消等待 |
| `_apply_settings` 部分失败无回滚 | 后端逐项赋值，某项失败时前面的参数已生效 | 失败时关闭并重开句柄，或恢复快照 |
| 工具层 `is_open` → `open` 竞态 | 另一线程恰好在两步之间关闭端口会得到 `PORT_NOT_OPEN` | 让核心提供 `write_read(open_if_closed=True)` 原子入口 |
| `_port_locks` 不回收 | 不同端口名越多，锁字典越大（有意保留以避免替换正在使用的锁） | 低优先级；可在确认无等待者后惰性清理 |
| 硬件/平台覆盖 | 仅用 `loop://` 与 fake device；Windows 环境无 socat | CI 增加 Linux `socat` 虚拟串口和真实 USB 设备冒烟 |

## 6. 修复 commit 说明

建议一个提交（或按 core / tools 拆成两个）：

```text
fix(serial): 修复串口句柄泄漏、无限阻塞与载荷解析缺陷

核心层 (core/serial_manager.py)
- close() 成功后才移除注册表条目，失败时保留句柄供重试，避免 OS 句柄泄漏
- open() 在 factory/显式 open/did-not-open 失败路径回收半开句柄
- 新增 DEFAULT_MAX_READ_TIMEOUT=30s，read()/write_read() 的有效超时统一封顶，
  并把实际生效超时写入返回值和错误 details
- write_read() 增加 max_bytes 并拒绝 expected_bytes 超限，防止无界缓冲
- reset() 强制释放残留句柄，且不再清空 _port_locks，避免并发下锁被替换
- _is_connection_open() 捕获后端异常；shutdown 记录关闭失败详情

工具层 (tools/comm.py, utils/serial_utils.py)
- 空响应转换为 ok + empty_response + hint，修复不可达分支与文档契约冲突
- 校验 response_format；端口/数值参数在打开串口前校验，避免副作用
- set_comm(close=True) 先校验并保存参数，不再静默丢弃
- hex 仅去除每个 token 的 0x 前缀，拒绝 A0x1/0x 等静默篡改
- base64 改为 validate=True，非法字符不再被吞成空载荷
- 新增字节级 encode_escaped_text()，修复 \xFF 被 UTF-8 重编码的问题
- decode_text() 对未知编码回退 utf-8，保持 "never raise" 契约

测试
- 新增 tests/test_integration_serial.py：句柄生命周期、超时封顶、空响应、
  参数前置校验、malformed hex/base64、12 线程并发隔离
- 补强 tests/test_serial_manager.py 与 tests/test_comm_tools.py 回归用例

验证
- ruff check src tests
- pytest -q tests            # 141 passed
- python scripts/mcp_smoke.py # 11 项 PASS
```

若拆分为两个提交，建议：

1. `fix(core): 串口句柄生命周期与读取超时上限`
2. `fix(tools): 统一返回信封并修复 hex/base64/转义解析`

第二个提交依赖第一个提交中的 `DEFAULT_MAX_READ_TIMEOUT` / `max_read_timeout`，请保持顺序。
