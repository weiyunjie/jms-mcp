# MCP 工具 API 参考

本服务在自动生成的 JumpServer OpenAPI 只读工具之外，额外提供 7 个手写能力工具。
所有手写工具都返回 JSON 文本（`TextContent`），错误以数据形式返回（见
[错误类别与排错](troubleshooting.md)），不会抛异常给调用方。

命令执行不走 SSH，而是通过 JumpServer Ops Job（`POST /ops/jobs/` + 轮询
`task-detail`）。由于 JumpServer 的 adhoc `summary` 不含 stdout 通道，输出经由
“stdout→stderr + base64 + 分块” 的包装在主机侧暂存、客户端重组解码（详见
[spike-0.1-findings](../../openspec/changes/jumpserver-mcp/spike-0.1-findings.md)）。

---

## discover_hosts

按主机名或 IP 搜索 JumpServer 主机资产，可选按 OS / 资产类型 / 分组过滤。
返回每台主机的资产 id 及候选 runas 账号——正是 `open_session` / `run_command`
需要的输入。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 否 | 主机名、IP 或 CIDR 子网 |
| `os_type` | string | 否 | 按 OS 过滤（如 `linux`） |
| `asset_type` | string | 否 | 按资产类型过滤 |
| `group` | string | 否 | 按节点/分组路径过滤 |
| `limit` | integer | 否 | 返回上限，默认 50（1–200） |
| `offset` | integer | 否 | 分页偏移，默认 0 |
| `with_accounts` | boolean | 否 | 是否为每台主机填充 `runas_candidates`（额外 API 调用），默认 true |

返回：`{ "count": N, "hosts": [ { id, name, address, platform, os_type, asset_type, nodes, accounts_amount, runas_candidates: [...] } ] }`

---

## resolve_users

为某资产解析连接（runas）用户。给定 `user_id`/`username` 时将其映射为 runas 值；
都不给且存在多个账号时，返回候选列表供选择。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `asset_id` | string | 是 | JumpServer 资产 id |
| `user_id` | string | 否 | 指定账号 id |
| `username` | string | 否 | 指定账号用户名 |

返回（已解析）：`{ asset_id, needs_selection: false, runas, account }`
返回（需选择）：`{ asset_id, needs_selection: true, candidates: [...], message }`

> RBAC 不在此处预检（设计决策 14）。解析只做用户→runas 的映射；该用户能否真正执行，
> 由 JumpServer 在执行时裁决，并以 `permission_denied` 反馈。

---

## open_session

打开一个绑定 主机 + runas 用户 的逻辑会话上下文，返回 `session_id` 供
`execute` / `close_session` 使用。若主机有多个用户且未指定，则返回候选列表
（`needs_selection`），不创建上下文。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `host` | string | 是 | 资产 id、主机名或 IP |
| `user_id` | string | 否 | 账号 id |
| `username` | string | 否 | 账号用户名 |
| `preapproved_patterns` | string[] | 否 | 受信自动化可免 Tier-2 审批直接运行的正则 |
| `initiating_user` | string | 否 | 审计用的发起者标识 |

返回：`{ session_id, asset_id, runas, host_name, account, needs_selection: false }`

> 会话是**逻辑上下文**，不是持久连接——每条命令都单独派发一个 Ops Job
> （设计决策 8）。空闲超过 `session_idle_timeout_seconds`（默认 15 分钟）即丢弃。

---

## execute

在已打开的会话中执行一条 shell 命令。先过安全策略（Tier-1 硬阻断 / Tier-2 审批 /
白名单），再派发 Ops Job，返回合并后的 stdout+stderr、真实退出码和执行元数据。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | 是 | `open_session` 返回的 id |
| `command` | string | 是 | 要执行的 shell 命令 |
| `timeout` | integer | 否 | 单命令超时（秒） |
| `chdir` | string | 否 | 命令的工作目录 |

返回（文本输出）：`{ status, exit_code, host, runas, command, stdout, encoding_detected, truncated, task_id, job_id, time_cost, session_id }`
返回（二进制输出）：以 `is_binary: true` + `binary_base64` + `bytes_total/bytes_returned` 代替 `stdout`。
返回（被拦截/待审批超时）：`{ status: "auto_denied" | "denied" | "queued", ... }`

---

## close_session

关闭逻辑会话上下文并释放资源。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_id` | string | 是 | 要关闭的会话 id |

返回：`{ session_id, closed: bool }`

---

## run_command

一站式便捷工具：打开会话 → 执行单条命令 → 关闭。安全管线与 `execute` 相同。
适合无需持久会话的临时命令。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `host` | string | 是 | 资产 id、主机名或 IP |
| `command` | string | 是 | 要执行的命令 |
| `user_id` | string | 否 | 账号 id |
| `username` | string | 否 | 账号用户名 |
| `preapproved_patterns` | string[] | 否 | 免 Tier-2 审批的正则 |
| `initiating_user` | string | 否 | 审计用发起者标识 |
| `timeout` | integer | 否 | 单命令超时（秒） |
| `chdir` | string | 否 | 工作目录 |

返回：与 `execute` 相同。

---

## batch_execute

在多台主机上并行运行同一条命令（各主机独立，无回滚）。返回
“N 成功，M 失败” 汇总及每主机明细。聚合结果过大时溢写为压缩下载文件。
每台主机都过与 `run_command` 相同的安全策略。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hosts` | string[] | 是 | 资产 id、主机名或 IP 列表 |
| `command` | string | 是 | 要执行的命令 |
| `user_id` | string | 否 | 账号 id |
| `username` | string | 否 | 账号用户名 |
| `preapproved_patterns` | string[] | 否 | 免 Tier-2 审批的正则 |
| `initiating_user` | string | 否 | 审计用发起者标识 |
| `timeout` | integer | 否 | 单命令超时（秒） |
| `chdir` | string | 否 | 工作目录 |

返回：`{ batch_id, cancelled, total, summary: "N succeeded, M failed", counts: {...}, results 或 results_download }`
聚合结果超过 `batch_inline_limit_bytes`（默认 256 KiB）时，以 `results_download`
（gzip-json 文件路径）替代内联 `results`。
