# 安全最佳实践指南

## 分层命令策略（design.md 决策 10）

每条命令依次经过：

1. **Layer 0 — Tier-1 毁灭性地板（始终生效，与模式无关）**
   - 灾难性操作的正则集（`rm -rf /`、`mkfs*`、`dd ... of=/dev/sd*`、fork 炸弹、
     `chmod 777 /` 等）。
   - 命中即**硬阻断，无任何放行路径**。在两种模式下都最先执行——即使某命令被误加进
     白名单，仍会在此被拦下。
   - Tier-1 地板**不可被清空**：即使管理员策略文件未提供 tier1，也会回落到内置默认。

2. **主闸门 — `policy_mode`（默认 `blacklist`）**
   - `blacklist`（默认放行）：过 Layer 0 后，命中 Tier-2 风险正则 → `pending_approval`；
     其余放行。
   - `whitelist`（默认拒绝）：过 Layer 0 后，命令须命中白名单正则才放行；不命中直接拒绝。
     （白名单命中但仍属 Tier-2 风险的，仍走审批。）

## 策略仅管理员可改

- 策略配置（模式、tier1/tier2/白名单）只能由部署管理员通过服务端配置文件
  (`policy_config_path`) 或环境变量设置。
- **没有任何 MCP 工具能读取或修改策略**——刻意不暴露 setter，杜绝通过工具自身提权。

## Tier-2 审批流（design.md 决策 11）

- 调用方**阻塞轮询**等待人工决策，默认 5 分钟超时。
- 5 分钟内无人响应 → **自动拒绝**（`auto_denied`），不执行。
- 自动化可在 `open_session`/`run_command` 时**预供允许正则**，对这些模式跳过审批提示。
- 批准后，豁免**精确限定到被触发的那条正则**，且**仅在当前会话内有效**
  （“本会话放行类似命令” = 那一条正则在本会话豁免）。跨会话不继承。

## 审计

- 所有安全相关事件写入本地 SQLite（`audit_db_path`）：命令文本、时间戳、发起用户、
  目标主机、runas、决策结果（`blocked`/`pending_approval`/`approved`/`denied`/
  `auto_denied`/`executed`）、触发的正则、审批人。
- 与 JumpServer 自身审计（ops job 执行历史）**互补而非替代**。

## 部署建议

- 生产环境优先 `whitelist` 模式，把可执行命令收敛到审定集合。
- 凭据（access key / api token）只走环境变量或 `.env`，**不要**走 CLI 参数或工具参数，
  避免泄漏进 agent 上下文/日志。
- 定期轮换 access key；在 JumpServer 侧审查 token 使用。
- 定期审查 SQLite 审计库；规划其留存/轮转策略与磁盘占用。
