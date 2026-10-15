# 第一版调用链：显式 OpenTelemetry 埋点 + Jaeger

## 要解决的问题

请求 ID 能关联日志，却没有描述跨服务调用的父子关系。这里引入 Trace ID（一次完整调用链）与 Span ID（链路中的一段工作），把订单与库存的耗时显示在同一条时间线上。

```text
orders: GET /orders/preview                 SERVER
  └─ orders: GET inventory                 CLIENT
       └─ inventory: GET /inventory/{sku}   SERVER
```

三个 span 共享 Trace ID，但 Span ID 不同。库存服务从 HTTP 的 W3C `traceparent` 头恢复父上下文。只有在 CLIENT span 内注入上下文，库存 SERVER span 才会挂在正确的父节点下。

## 实现边界

- 使用 OpenTelemetry API/SDK 1.45.1 和 OTLP HTTP exporter 1.45.1；具体依赖全部固定在 requirements.txt。
- 使用固定 Jaeger 2.9.0 镜像，不表示这是最新版。Jaeger all-in-one 直接接收 OTLP HTTP 并提供查询页面；本阶段不另设 Collector，也不同时部署 Tempo/Grafana。
- 手工编写 SERVER 中间件和 CLIENT 埋点，方便理解传播过程；后续请求类型增多时可替换为框架官方 instrumentation。
- 每个服务进程持有一个 Provider，按服务名创建 Resource。正常关闭时异步等待 Provider 排空队列。
- Compose 通过 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 配置导出位置；未配置时仍可生成关联 ID，但不向远端导出。
- 使用后台批量导出；导出器不可用时会记录错误，业务无需等待遥测上传。队列有界，遥测并非保证不丢失。
- 无上游采样决策时采集全部实验请求；有上游父节点时继承其采样决策。不是生产环境采样配置。
- `/health` 和文档接口不生成 span；不把延迟开关、实验场景名称写入 span。
- SERVER span 到应用准备好响应时结束。当前接口都是小型 JSON，这不是完整网络传输或流式响应耗时。
- `inventory_call` 日志在 CLIENT span 结束后写出，因此其 span_id 属于订单 SERVER span；Trace ID 仍与整条链路一致。
- Jaeger 使用内存存储，重启会丢失历史链路。需要留档时运行验收脚本，将实际查询到的 span 和日志保存为 JSON。

## 阅读与验收

依次阅读 `lab/tracing.py` → `lab/observability.py` → 订单服务调用处 → `lab/run_trace_experiment.py`。

本地测试：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。覆盖父子关系、远端上下文、健康检查排除和超时错误标记。

容器验收：先构建并启动 Compose，然后运行 `.\.venv\Scripts\python.exe -m lab.run_trace_experiment`。检查正常、500 ms 延迟、库存停止、恢复四种状态的实际 Jaeger 数据，并验证日志 Trace ID 一致。此脚本会改变当前实验环境，结束时恢复延迟为 0 并启动库存。

链路能支持“时间主要耗在库存服务中”的判断，但不能仅凭外层 span 认定数据库、CPU 或某项配置是根因；这需要后续证据。

## 官方参考

- [OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [Python context propagation](https://opentelemetry.io/docs/languages/python/propagation/)
- [Jaeger 2.9 all-in-one](https://www.jaegertracing.io/docs/2.9/getting-started/)
