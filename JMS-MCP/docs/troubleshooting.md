# 错误分类与排障

## 五类错误（design.md 决策 14）

所有运行期失败都归入五个机器可区分的类别，便于 agent 按**原因**分支处理。
错误以 JSON 返回：`{"error": "<category>", "message": "...", "detail": {...}}`。

| 类别 | 含义 | 是否自动重试 | 调用方应对 |
|---|---|---|---|
| `jumpserver_unreachable` | 连不上 JumpServer HTTP API（传输错误或 5xx） | **是**，API 层重试 3 次后才上抛 | 检查堡垒机可达性、网络/代理、凭据 |
| `target_unreachable` | JumpServer 可达，但目标主机连接级失败（Ansible `dark` 桶） | 否 | 检查目标主机在线状态、JumpServer 与目标的连通性 |
| `permission_denied` | JumpServer 在执行时拒绝该 runas 用户（RBAC，惰性发现，从不预检） | 否 | 换用有权限的 runas 用户，或在 JumpServer 调整授权 |
| `command_blocked` | 本地安全策略拦截（Tier-1 硬阻断 / 白名单拒绝） | 否 | Tier-1 无放行路径；白名单模式需管理员加白 |
| `connection_interrupted` | 在途命令状态未知（轮询超时 / 任务中途失败） | **从不**自动重试 | 先自行核实命令是否已完成，再决定是否重试 |

## 中途中断（决策 14）

- MCP **绝不**自动重试在途中断的命令——SSH/ops job 无法保证恰好一次，静默重试会重复执行
  非幂等命令。
- 返回 “execution status unknown, connection interrupted at task X”，把重试决策交给
  agent/人工（须先自行核实是否已完成）。

## 重试策略

- 仅 **JumpServer-unreachable（HTTP API 调用）** 自动重试，默认 3 次
  （`jumpserver_unreachable_retries`），带指数退避。
- 命令执行本身**永不**自动重试。

## 常见问题

**所有工具返回 `not_configured`**
缺少必需配置。按提示设置 `JUMPSERVER_URL` 与认证（`API_TOKEN` 或
`ACCESS_KEY_ID`+`ACCESS_KEY_SECRET`）。详见 [setup.md](setup.md)。

**脚本本地直连报 502 / 走了代理**
本机代理（如 `127.0.0.1:7897`）拦截了请求。httpx 客户端用 `trust_env=False` 绕过
环境代理；独立脚本也应如此。

**`.venv` 里 `ModuleNotFoundError: httpx`**
项目 `.venv` 缺依赖。用系统 `python3`（已装 httpx/mcp/fastapi_mcp），或在 venv 内
`pip3 install httpx mcp fastapi-mcp`。

**命令成功但 stdout 为空**
JumpServer ops-job `summary` 是 Ansible 战报，成功命令不带输出（见 spike 0.1）。
本项目用 chunked-base64 包装器回收输出；若仍为空，检查包装器是否被改动，或目标命令是否真的无输出。

**大输出被截断/丢失**
单个 ops-job `failures` 通道有 ~256 KiB 硬上限，超限会**整体丢空**。包装器在主机侧
`split` 成 ~200KB 分片分别回传以规避；客户端按 `max_output_bytes` 上限重组，超出则标注截断。

**Tier-2 命令一直 `pending_approval` 然后 `auto_denied`**
5 分钟内无人审批即自动拒绝。自动化场景请在 `open_session`/`run_command` 时
`preapproved_patterns` 预供允许正则。
