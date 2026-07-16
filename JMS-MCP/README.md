# JumpServer MCP Server

通过 MCP（Model Context Protocol）暴露 JumpServer 的服务器管理能力：让 AI agent 与自动化工具
通过 JumpServer 既有的安全框架来发现主机、选择登录身份、执行命令、批量运维。

命令执行走 JumpServer 的 **Ops Job**（`POST /ops/jobs/` + 轮询 `task-detail`），不走持久 SSH 通道，
天然继承 JumpServer 的审计与 RBAC。

## 能力

- **主机发现** — 按主机名 / IP / CIDR 搜索资产，返回 asset id 与候选 runas 账号
- **用户连接** — 解析登录（runas）身份；多账号时返回候选列表供选择
- **会话管理** — 逻辑会话上下文（非持久连接）：`open_session` / `execute` / `close_session` 与一次性 `run_command`
- **命令执行** — 经 chunked-base64 包装器回收合并的 stdout+stderr、真实退出码，二进制安全
- **安全管控** — 两层命令策略（Tier-1 硬阻断 / Tier-2 人工审批）、黑/白名单模式、会话级豁免、SQLite 审计
- **批量运维** — 跨主机纯并行执行、每主机成败统计、30s 进度、可取消、大结果集压缩下载
- **数据处理** — 编码归一化到 UTF-8、二进制流检测、可配置最大输出截断

## 快速开始

```bash
access_key_id='YOUR_ID' \
access_key_secret='YOUR_SECRET' \
jumpserver_url='http://jumpserver.example.com' \
api_key='test-mcp-key' \
base_path='/mcp' \
server_port='8000' \
python3 -m jumpserver_mcp_server
```

服务启动后在 `/mcp` 提供 **Streamable HTTP** 端点（`GET`/`POST`/`DELETE`）。

MCP 客户端接入（以 Claude Code 为例）：

```bash
claude mcp add jms-mcp \
  --transport http \
  --url http://localhost:8000/mcp \
  --header "Authorization: Bearer test-mcp-key"
```

详细安装、环境变量与客户端配置见 [docs/setup.md](docs/setup.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 环境变量、安装、首次连接 |
| [docs/api-reference.md](docs/api-reference.md) | 全部 MCP 工具的参数与返回 |
| [docs/usage-examples.md](docs/usage-examples.md) | 各能力的用法示例 |
| [docs/batch-examples.md](docs/batch-examples.md) | 批量运维示例 |
| [docs/security.md](docs/security.md) | 安全最佳实践（策略模式、黑名单分层、审批、审计） |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 五类错误与排障 |

## 测试

```bash
python3 -m pytest JMS-MCP/tests/ -q
```

## 架构要点

- 命令执行无法用 ops-job 退出码直接区分成败（`summary` 是 Ansible 战报，成功命令不带输出）。
  本项目用包装器把 stdout+stderr 合并、追加真实退出码标记、base64 编码后在主机侧 `split`
  分片回传，客户端重组解码。详见 `openspec/changes/jumpserver-mcp/spike-0.1-findings.md`。
- 单 ops-job 输出通道有 ~256 KiB 硬上限（超限整体丢空），故必须分片传输。
- “会话”是逻辑上下文记录（绑定 host + runas + 会话级审批豁免），不是持久连接——
  每条命令都是一个独立 ops job。
