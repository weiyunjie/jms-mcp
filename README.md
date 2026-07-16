# JMS-MCP

JumpServer MCP server — exposes JumpServer host management as MCP tools so AI
agents and automation can discover hosts, resolve runas users, run commands,
and execute batch operations through JumpServer's audited Ops Job API.

The implementation lives in [`JMS-MCP/`](JMS-MCP/). Start there:

- [JMS-MCP/README.md](JMS-MCP/README.md) — overview and doc index
- [JMS-MCP/docs/setup.md](JMS-MCP/docs/setup.md) — install and required environment variables
- [JMS-MCP/docs/api-reference.md](JMS-MCP/docs/api-reference.md) — MCP tool reference
- [JMS-MCP/docs/usage-examples.md](JMS-MCP/docs/usage-examples.md) — usage examples per capability
- [JMS-MCP/docs/security.md](JMS-MCP/docs/security.md) — security model and best practices
- [JMS-MCP/docs/troubleshooting.md](JMS-MCP/docs/troubleshooting.md) — error categories and troubleshooting

Planning artifacts (proposal, design, specs, tasks) are under
[`openspec/`](openspec/).

## Quick start

```bash
cd JMS-MCP
cp .env.example .env   # then fill in your JumpServer URL + Access Key
pip3 install -e '.[dev]'
python3 -m pytest -q
python3 -m jumpserver_mcp_server
```

See [JMS-MCP/docs/setup.md](JMS-MCP/docs/setup.md) for details.

---

# JMS-MCP（中文说明）

JMS-MCP 是一个 **JumpServer MCP（Model Context Protocol）服务**，把 JumpServer 的
主机管理能力封装成一组 MCP 工具，让 AI 智能体和自动化程序能够通过 JumpServer
**自带审计的 Ops Job API** 来发现主机、解析登录账号、执行命令以及批量操作——
全程不绕开 JumpServer 的审计与权限体系。

## 它解决什么问题

运维大规模服务器集群的团队，往往希望让自动化工具或 AI 智能体「直接帮我在某台机器上
跑条命令」。但直接给智能体 SSH 权限会丢掉 JumpServer 的审计链和 RBAC 管控。
JMS-MCP 的做法是：**所有命令都通过 JumpServer 的 Ops Job 下发**，因此

- 每条命令都进 JumpServer 的执行历史，审计不丢
- 登录身份（runas）由 JumpServer 的账号体系决定，权限不旁路
- 本地再叠加一层危险命令管控与审批，双重防线

## 核心特性

| 能力 | 说明 |
|------|------|
| 主机发现 | 按主机名 / IP / 网段搜索资产，返回资产 id 与可用的 runas 账号 |
| 账号解析 | 自动选用唯一账号；多账号且未指定时返回候选列表供选择 |
| 逻辑会话 | `open_session` / `execute` / `close_session`，以及一次性的 `run_command` |
| 命令执行 | 通过 Ops Job 下发，回收合并后的 stdout+stderr、真实退出码与执行元数据 |
| 安全管控 | Tier-1 致命命令硬阻断（不可越权）+ Tier-2 风险命令人工审批；黑/白名单模式可切换 |
| 审批流程 | 阻塞轮询，默认 5 分钟超时自动拒绝；支持自动化预批准模式 |
| 审计 | 本地 SQLite 记录命令、时间、发起者、主机与判定结果 |
| 数据处理 | 三段式编码归一到 UTF-8、二进制流检测、超大输出截断标注 |
| 批量操作 | 多主机纯并行执行、30 秒进度上报、可取消、大结果集 gzip 落盘下载 |

## 架构要点

- **执行走 Ops Job，不是 SSH。** 命令通过 `POST /ops/jobs/` 下发，再轮询
  `GET /ops/job-execution/task-detail/{task_id}/` 取结果。无需维护持久 SSH 通道。
- **「会话」是逻辑上下文，不是活连接。** 由于 Ops Job 是无状态的请求-轮询模型，
  会话只是一条记录 `{ 资产, runas, 会话级豁免正则, 最后活跃时间 }`，每条命令都重新下发一个 Ops Job。
- **输出捕获采用「分块 base64 包装」方案。** 实测发现 JumpServer 的 adhoc `summary`
  是 Ansible 的 play-recap，**没有 stdout 通道**，且承载文本的 `failures` 字段有
  **约 256 KiB 的硬上限、超限会静默丢空**。为此命令被包装为：子壳捕获真实退出码、
  `2>&1` 合并输出、base64 保证二进制安全、在目标主机上 `split` 成小于上限的分片，
  客户端再逐片拉取、重组、解码。详见
  [spike-0.1-findings.md](openspec/changes/jumpserver-mcp/spike-0.1-findings.md)（对真实堡垒机的实测记录）。

## 七个 MCP 工具

| 工具 | 作用 |
|------|------|
| `discover_hosts` | 搜索主机资产（主机名 / IP / 网段，可按 OS、类型、分组过滤） |
| `resolve_users` | 解析某资产的 runas 登录账号 |
| `open_session` | 打开逻辑会话，返回 `session_id` |
| `execute` | 在会话中执行命令（先过安全策略，再下发 Ops Job） |
| `close_session` | 关闭会话上下文 |
| `run_command` | 一次性「开-执行-关」的便捷封装 |
| `batch_execute` | 一条命令在多台主机上并行执行 |

完整参数见 [JMS-MCP/docs/api-reference.md](JMS-MCP/docs/api-reference.md)。

## 安全模型（两层）

1. **Tier-1 致命命令地板（永远生效，与模式无关）**：如 `rm -rf /`、`mkfs*`、
   `dd ... of=/dev/sd*` 等，**直接硬阻断，无任何放行路径**。即使误加进白名单也照样拦。
2. **主闸门 `policy_mode`（默认黑名单）**：
   - 黑名单模式（默认放行）：命中 Tier-2 风险正则 → 返回 `pending_approval` 等待人工审批；其余放行。
   - 白名单模式（默认拒绝）：只有命中白名单的命令才放行；其余一律拒绝。

策略文件**仅部署管理员可改**，没有任何 MCP 工具能修改它。批准后的豁免**只对本次会话、
且只对触发的那条正则**生效。详见 [JMS-MCP/docs/security.md](JMS-MCP/docs/security.md)。

## 错误分类

所有失败都归入五类，方便智能体按原因分支处理：

- `jumpserver_unreachable` — 连不上 JumpServer API（会自动重试 3 次）
- `target_unreachable` — JumpServer 可达但目标主机不可达
- `permission_denied` — JumpServer 在执行时拒绝（RBAC，延迟发现，不预检）
- `command_blocked` — 本地安全策略拦截
- `connection_interrupted` — 命令执行中途状态未知（**绝不自动重试**）

## 配置

所有敏感配置只从环境变量读取（见 [JMS-MCP/.env.example](JMS-MCP/.env.example)）：

| 变量 | 说明 |
|------|------|
| `jumpserver_url` | JumpServer 基础地址（如 `http://jumpserver.example.com`） |
| `access_key_id` / `access_key_secret` | Access Key 鉴权（或用 `api_token`） |
| `policy_mode` | `blacklist`（默认）或 `whitelist` |
| `session_idle_timeout_seconds` | 会话空闲过期，默认 900（15 分钟） |
| `max_concurrent_jobs` | 并发 Ops Job 上限，默认 10 |
| `approval_timeout_seconds` | Tier-2 审批超时，默认 300（5 分钟） |

完整清单见 [JMS-MCP/docs/setup.md](JMS-MCP/docs/setup.md)。

## 快速开始

```bash
cd JMS-MCP
cp .env.example .env          # 填入 JumpServer 地址 + Access Key
pip3 install -e '.[dev]'      # 安装依赖
python3 -m pytest -q          # 运行测试（应全部通过）
python3 -m jumpserver_mcp_server   # 启动服务
```

也可用容器方式启动：

```bash
cd JMS-MCP
docker compose up -d
```

## 开发与测试

代码位于 [`JMS-MCP/jumpserver_mcp_server/`](JMS-MCP/jumpserver_mcp_server/)，
按能力分模块（`host_discovery`、`user_connection`、`sessions`、`command_execution`、
`security_policy`、`approval`、`audit`、`data_handling`、`batch`、`ops_executor`）。
测试在 [`JMS-MCP/tests/`](JMS-MCP/tests/)，用 respx 模拟 JumpServer 的 Ops Job 端点，
覆盖安全、并发、编码、批量与性能等场景。

```bash
cd JMS-MCP && python3 -m pytest -q
```

## 目录结构

```
.
├── JMS-MCP/                    # 服务实现
│   ├── jumpserver_mcp_server/  # 各能力模块
│   ├── tests/                  # 测试套件
│   ├── docs/                   # 文档（安装 / API / 安全 / 排障 / 示例）
│   ├── Dockerfile
│   └── docker-compose.yml
└── openspec/                   # 规划产物（proposal / design / specs / tasks）
```
