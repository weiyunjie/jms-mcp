# 用法示例

下列示例展示每个能力工具的典型调用。请求体即 MCP `call_tool` 的 `arguments`，
响应为工具返回的 JSON（节选）。

## 1. 主机发现（host-discovery）

按主机名/IP 搜索，并拿到资产 id + runas 候选：

请求 `discover_hosts`：
```json
{ "query": "203.0.113.10", "os_type": "linux" }
```
响应：
```json
{
  "count": 1,
  "hosts": [
    {
      "id": "00000000-0000-0000-0000-000000000000",
      "name": "demo-linux-host",
      "address": "203.0.113.10",
      "platform": "Linux",
      "os_type": "linux",
      "accounts_amount": 1,
      "runas_candidates": [
        { "id": "11111111-...", "username": "ec2-user", "privileged": false, "secret_type": "ssh_key" }
      ]
    }
  ]
}
```

子网搜索：`{ "query": "203.0.113.0/24" }` 会把结果收窄到该网段内的地址。

## 2. 用户解析（user-connection）

资产有多个账号且未指定时，返回候选让调用方选择：

请求 `resolve_users`：
```json
{ "asset_id": "00000000-0000-0000-0000-000000000000" }
```
单账号时直接解析：
```json
{ "asset_id": "00000000-...", "needs_selection": false, "runas": "ec2-user", "account": { "username": "ec2-user" } }
```

指定用户（自动化路径）：`{ "asset_id": "...", "username": "ec2-user" }`。

## 3. 会话管理（session-management）

打开 → 执行多条 → 关闭：

```json
// open_session
{ "host": "203.0.113.10", "initiating_user": "alice" }
// -> { "session_id": "ab12...", "runas": "ec2-user", "host_name": "demo-linux-host" }

// execute
{ "session_id": "ab12...", "command": "df -h /" }

// execute（同一上下文复用 runas）
{ "session_id": "ab12...", "command": "uptime" }

// close_session
{ "session_id": "ab12..." }
```

## 4. 命令执行（command-execution）

`execute` 返回合并 stdout+stderr、真实退出码与元数据：
```json
{
  "status": "ok",
  "exit_code": 0,
  "host": "demo-linux-host",
  "runas": "ec2-user",
  "command": "echo hi; id -un",
  "stdout": "hi\nec2-user",
  "encoding_detected": "utf-8",
  "truncated": false,
  "time_cost": 2.1
}
```

二进制输出（如 `head -c 256 /bin/ls`）会以 base64 返回：
```json
{ "is_binary": true, "binary_base64": "f0VMR...", "bytes_total": 256, "exit_code": 0 }
```

## 5. 一站式 run_command

无需持久会话的临时命令：
```json
{ "host": "203.0.113.10", "command": "uname -s", "username": "ec2-user" }
// -> { "status": "ok", "exit_code": 0, "stdout": "Linux", ... }
```

## 6. 安全策略交互

Tier-1（毁灭性）硬阻断，无覆盖路径：
```json
// execute  { "session_id": "...", "command": "rm -rf /" }
{ "error": "command_blocked", "message": "Command hard-blocked: matches a Tier-1 destructive pattern...", "detail": { "tier": 1 } }
```

Tier-2（高风险）需人工审批；超时（默认 5 分钟）自动拒绝：
```json
// execute  { "session_id": "...", "command": "rm -f /var/log/old.log" }
{ "status": "auto_denied", "command": "rm -f /var/log/old.log", "matched_pattern": "...", "approval_id": "..." }
```

受信自动化可在开会话时预批，跳过提示：
```json
{ "host": "...", "command": "rm -f /tmp/x", "preapproved_patterns": ["rm\\s+-f"] }
```

## 7. 批量操作

见 [批量操作示例](batch-examples.md)。
