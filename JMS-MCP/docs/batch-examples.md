# 批量操作示例

`batch_execute` 在多台主机上**纯并行**执行同一条命令：每台主机独立、无回滚，
受会话管理器并发上限（默认 10）节流。返回 “N 成功，M 失败” 汇总 + 每主机明细。

## 基本批量

请求 `batch_execute`：
```json
{
  "hosts": ["web-01", "web-02", "web-03"],
  "command": "systemctl is-active nginx",
  "username": "ec2-user",
  "initiating_user": "alice"
}
```
响应：
```json
{
  "batch_id": "f3a9...",
  "cancelled": false,
  "total": 3,
  "summary": "2 succeeded, 1 failed",
  "counts": { "succeeded": 2, "failed": 1, "not_executed": 0, "interrupted": 0 },
  "results_inline": true,
  "results": {
    "web-01": { "status": "succeeded", "result": { "exit_code": 0, "stdout": "active" } },
    "web-02": { "status": "succeeded", "result": { "exit_code": 0, "stdout": "active" } },
    "web-03": { "status": "failed", "error": { "error": "target_unreachable", "message": "..." } }
  }
}
```

## 进度回报

批量运行期间，每 30 秒（可配）回报一次 `completed/total`，完成时再回报一次终值。
进度形如：
```json
{ "batch_id": "f3a9...", "completed": 37, "total": 100, "progress": "37/100" }
```

## 大结果集 → 压缩下载

当聚合结果超过内联上限（`batch_inline_limit_bytes`，默认 256 KiB）时，结果写入
gzip 文件，响应改为给出下载引用而非内联：
```json
{
  "summary": "100 succeeded, 0 failed",
  "results_inline": false,
  "results_download": {
    "path": "jms_mcp_batch_results/batch-f3a9-1782722282.json.gz",
    "format": "gzip-json",
    "bytes": 48211,
    "note": "Aggregate batch results exceeded the inline limit and were written to a compressed file."
  }
}
```

## 取消

取消进行中的批量：停止派发新主机，已完成的照常上报，未派发的标记
`not_executed`，正在执行中的标记 `interrupted`（状态未知，绝不谎报成功）。

取消后的汇总：
```json
{
  "cancelled": true,
  "summary": "12 succeeded, 0 failed",
  "message": "operation cancelled; completed hosts reported, remaining hosts were not executed",
  "completed_hosts": ["web-01", "...", "web-12"],
  "not_executed_hosts": ["web-13", "...", "web-50"]
}
```

## 无回滚语义

任何主机失败都不会触发对已成功主机的补偿/回滚命令，失败主机也**不会自动重试**——
每个失败都带分类错误类别（见 [错误排查](troubleshooting.md)），由调用方决定是否重试。
