# OpenTelemetry 调用链验收

日期：2026-10-15。Docker Compose 运行 orders、inventory 和 Jaeger 2.9.0，Python OpenTelemetry SDK / OTLP HTTP exporter 均为 1.45.1。

执行：`docker compose up --build -d --wait`，然后 `.\.venv\Scripts\python.exe -m lab.run_trace_experiment`。每种场景发送一个订单请求；这是端到端功能验收，不是性能统计。

| 场景 | HTTP | span 数 | 订单 SERVER ms | 订单 CLIENT ms | 库存 SERVER ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 正常 | 200 | 3 | 11.537 | 10.113 | 1.561 |
| 注入 500 ms 延迟 | 200 | 3 | 508.187 | 507.187 | 501.693 |
| 停止库存 | 503 | 2 | 2005.648 | 2004.958 | 无 |
| 恢复 | 200 | 3 | 7.060 | 6.290 | 0.852 |

验证了以下内容：

- Jaeger 实际收到订单 SERVER → 订单 CLIENT → 库存 SERVER 的父子关系。
- 三个 span 的 Trace ID 一致，日志中的 Trace ID 与链路一致，Span ID 能匹配链路节点。
- 库存停止时，本次表现为约 2 秒超时；订单 SERVER 与 CLIENT 均标记 error，返回 503，无库存 SERVER span。
- 健康检查返回 200 且没有 X-Trace-ID 响应头。
- Jaeger 页面显示延迟链路包含 2 个服务、深度 3、3 个 span，总耗时约 508.19 ms。
- 实验结束已启动库存并恢复 INVENTORY_DELAY_MS=0。

原始数据见 [trace-results.json](trace-results.json)。所有耗时为本次单个样本，不能用于性能或显著性结论；嵌套 span 耗时不能相加。仅靠这些 span 能定位等待在库存内，无法进一步认定数据库等内部根因。

本次延迟链路：[在 Jaeger 中查看](http://127.0.0.1:16686/trace/221e85d78eef8c5efa2e51ac43dc6ab8)。Jaeger 使用内存存储，重启后此链接对应的数据可能消失，但 JSON 留档仍可读取。
