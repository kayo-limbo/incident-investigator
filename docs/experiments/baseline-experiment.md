# 固定流程基线验收

2026-10-16，运行 `.\.venv\Scripts\python.exe -m evals.run_baseline`。每种状态只产生一个用于调查的订单请求，评测层持有真值；独立调查进程只接收 Trace ID。

| 验收案例 | 预期/实际输出 | 证据工具调用数 |
| --- | --- | ---: |
| 健康请求 | no_anomaly_observed | 1 |
| 库存延迟 500 ms | inventory_latency | 1 |
| 订单库存端口错误 | endpoint_mismatch | 4 |
| 库存服务停止 | insufficient_evidence | 3 |
| 恢复 | no_anomaly_observed | 1 |

五个案例全部通过，原始调查报告（含证据、来源、查询参数、时间和引用）见 [baseline-results.json](baseline-results.json)。评测结束后独立业务请求确认恢复，`recovery_verified=true`。

库存停止时，规则看到连接失败，但配置仍符合服务契约。它没有读取容器停止真值，因此没有把未知网络原因断言为服务宕机。这是当前能力边界的预期行为，而不是把拒答算作根因定位正确。

本地 14 项测试通过，包含原有 4 项调用链测试及 10 项调查工具/流程测试。这是功能验收，不是准确率实验；没有划分开发/测试故障参数，也没有模型对照、重复统计或负载变化实验。

完整规则与数据边界见 [设计笔记](../decisions/002-read-only-investigation.md)。
