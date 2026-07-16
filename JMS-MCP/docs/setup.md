# 环境变量与安装指南

## 必需配置

| 变量 | 说明 |
|---|---|
| `jumpserver_url` | 堡垒机基础 URL，如 `http://jumpserver.example.com` |
| 认证（二选一） | 见下 |

**认证方式（二选一）：**

- **Access Key（推荐）**：`access_key_id` + `access_key_secret`，走 JumpServer 的
  HTTP Signature 鉴权。
- **API Token**：`api_token`，走 Bearer 鉴权。

若必需配置缺失，每个手写工具都会返回 `not_configured` 错误并附带可操作提示，而不是
静默失败。

## 可选配置（均有默认值，可用环境变量覆盖）

| 变量 | 默认 | 说明 |
|---|---|---|
| `server_port` | 8000 | 守护进程监听端口 |
| `api_key` | 空 | MCP 客户端访问本服务的 Bearer 口令 |
| `base_path` | `/mcp` | Streamable HTTP 传输挂载路径 |
| `policy_mode` | `blacklist` | 主命令闸门模式：`blacklist`（默认放行）或 `whitelist`（默认拒绝） |
| `policy_config_path` | 空 | 管理员策略 JSON 路径（tier1/tier2/whitelist/mode），仅管理员可改 |
| `session_idle_timeout_seconds` | 900 | 会话上下文空闲过期（15 分钟） |
| `max_concurrent_jobs` | 10 | 在飞 ops job 并发上限 |
| `max_output_bytes` | 100 MiB | 单命令输出客户端重组上限 |
| `output_chunk_bytes` | 200 KiB | 主机端分片大小（须 < 256 KiB 上限） |
| `approval_timeout_seconds` | 300 | Tier-2 审批超时（5 分钟）后自动拒绝 |
| `ops_job_timeout_seconds` | 120 | 单个 ops job 超时 |
| `ops_poll_max_attempts` | 120 | task-detail 轮询次数上限 |
| `ops_poll_interval_seconds` | 1.0 | 轮询间隔 |
| `jumpserver_unreachable_retries` | 3 | 仅对“堡垒机不可达”的 HTTP 调用重试 |
| `audit_db_path` | `jms_mcp_audit.sqlite3` | 本地 SQLite 审计库路径 |
| `batch_inline_limit_bytes` | 256 KiB | 批量聚合结果超此值则压缩落盘 |
| `batch_spill_dir` | `jms_mcp_batch_results` | 压缩批量结果目录 |
| `host_cache_ttl_seconds` | 60 | 主机查询内存缓存 TTL |

## 安装

```bash
cd JMS-MCP
pip3 install -e .
```

## 运行

通过环境变量或 `.env` 文件（与 `config.py` 同目录）提供配置：

```bash
access_key_id='YOUR_ID' \
access_key_secret='YOUR_SECRET' \
jumpserver_url='http://jumpserver.example.com' \
api_key='test-mcp-key' \
base_path='/mcp' \
server_port='8000' \
python3 -m jumpserver_mcp_server
```

MCP 客户端连接时附带头：
```text
Authorization: Bearer test-mcp-key
```

## 传输协议

本服务使用 **Streamable HTTP** 传输（MCP 规范 2025-03-26），在 `BASE_PATH`（默认 `/mcp`）上
提供三个 HTTP 方法：

| 方法 | 作用 |
|---|---|
| `GET /mcp` | 建立会话，返回 `Mcp-Session-Id` 响应头 |
| `POST /mcp` | 发送 JSON-RPC 请求（携带 `Mcp-Session-Id`） |
| `DELETE /mcp` | 终止会话 |

所有请求必须携带 `Authorization: Bearer <api_key>` 头，否则返回 401。

## MCP 客户端接入

### Claude Code（CLI）

```bash
claude mcp add jms-mcp \
  --transport http \
  --url http://localhost:8000/mcp \
  --header "Authorization: Bearer YOUR_API_KEY"
```

### Claude Desktop

在 `claude_desktop_config.json` 中添加：

```json
{
  "mcpServers": {
    "jms-mcp": {
      "transport": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

### 其他 MCP 客户端

使用 HTTP 传输连接 `http://<host>:<port>/mcp`，请求头带 `Authorization: Bearer <key>`。

## 从 SSE 传输迁移

如果你之前使用 SSE 端点（`/sse`），需要：

1. **传输类型**：从 `sse` 改为 `http`
2. **端点路径**：从 `/sse` 改为 `/mcp`
3. **会话标识**：查询参数 `?session_id=` 已被响应头 `Mcp-Session-Id` 取代（客户端自动处理）
4. **认证**：不变，仍为 `Authorization: Bearer <key>`

## 运行测试

```bash
cd JMS-MCP
python3 -m pytest          # 全部单元 + 集成 + 性能测试（mock，无需真实堡垒机）
```
