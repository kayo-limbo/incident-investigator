# 订单故障调查 Agent

订单接口返回 503，应该先查库存服务，还是订单客户端的连接池？这个项目让 Agent 在有限的查询预算内读取告警和调用链，选择下一项只读取证工具。调查报告给出首查方向、支持判断的观测，以及仍然缺少的证据。

实验环境由两个 FastAPI 服务组成：订单服务通过 HTTP 查询库存，OpenTelemetry 记录跨服务调用，Jaeger 用于查看链路。同一份冻结快照可供模型策略与固定流程反复调查。

## 一个案例：最慢的请求里找不到 503

在三次同时出现库存延迟和连接池超时的实验中，每次都有 12 个请求返回 503，但每个窗口耗时最长的三条请求全部返回了 200。成功请求等到了慢库存的响应；失败请求在订单客户端等待连接时就已超时。

针对“503 发生在哪一步”，取证工具支持按状态选取请求，再检查同一条链路上的父子 span。以下三个字段共同支持报告中的首查建议：

| 链路位置 | 观测字段 | 本次请求的值 |
| --- | --- | --- |
| 订单入口的服务端 span | `http.response.status_code` | `503` |
| 调用库存的客户端子 span | `error.type` | `PoolTimeout` |
| 同一客户端子 span | `client.request_headers_started` | `false` |

> 该 503 请求调用库存时，在发出请求头前发生 PoolTimeout，首查订单客户端连接池。

报告附有这三个字段各自的快照、trace 和 span 引用。库存变慢是否导致其他请求等池超时，仍需额外证据。

在 25 份历史冻结窗口的离线对照中，7 份具备上述证据。每窗最多查询两次，按耗时取样时能交付带引用建议的窗口为 **0/7**，按 503 取样后为 **7/7**；其余 18 份均未误派至连接池。实际逻辑查询总数从 25 增至 32，多出的 7 次用于下钻失败请求。两组使用同一条确定性分派规则。[实验与复现命令](docs/experiments/active-query-comparison.md#订单-503-的单请求取证)

## 调查功能

- **预算内选择查询。** 在双窗口调查中，模型可读取参考或事件窗口摘要、下钻已公开的请求 ID、比较连接池配置与入口速率。执行器校验查询范围和剩余预算，记录每步动作、结果和停止原因。[主动调查协议](docs/decisions/035-query-policy-adapters.md)
- **核对报告中的统计主张。** 验证器按指标、窗口、样本范围和原文精度核对结构化主张。曾有报告把最慢订单对应的库存耗时 352.213 ms 写成库存 P95；从原始 span 分别排序得到的库存 P95 是 352.727 ms。可核验范围及自由文本限制见[自然报告审计](docs/experiments/bundle-comparison.md)。
- **同快照比较调查策略。** 固定流程与模型共享工具和停止条件，结果可离线回放。在来自 14 次采集的 38 个开发视图上，固定流程已达到最短成功路径，合计 132 次查询。当前样本中尚未观察到模型选路减少调用次数。[查询路径对照](docs/experiments/active-query-comparison.md)

## 快速开始

下面的 PowerShell 示例使用 Python 3.12、Docker 和 Docker Compose，从一次新的订单请求开始。默认运行固定调查流程，无需模型密钥。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose up --build -d --wait

$response = Invoke-WebRequest "http://127.0.0.1:8000/orders/preview?sku=demo"
$traceId = [string]($response.Headers['X-Trace-ID'] | Select-Object -First 1)
Start-Sleep -Seconds 2
.\.venv\Scripts\python.exe -m backend.investigation $traceId
```

正常请求返回 HTTP 200。调查命令输出 JSON 报告，可查看结论、证据和限制。打开本机 [Jaeger](http://127.0.0.1:16686)，按同一个 trace ID 查看订单入口、客户端调用和库存服务端的耗时。

要让模型选择查询，可按[配置示例](.env.example)在本地 `.env` 设置 `DEEPSEEK_API_KEY`，再运行下方命令。该模式会将查得的观测发送至配置的模型服务并产生调用费用。

```powershell
.\.venv\Scripts\python.exe -m backend.investigation $traceId --strategy agent --max-tool-calls 6
```

体验结束后运行 `docker compose down`。若要复现连接池竞争或主动调查对照，可从以下入口继续：

| 我想做什么 | 入口 |
| --- | --- |
| 在本地生成慢库存、小连接池和超时请求 | [连接池实验](docs/experiments/pool-experiment.md) |
| 采集窗口、冻结证据并离线读取 | [窗口证据与采集验收](docs/experiments/window-evidence.md) |
| 比较固定流程、ReAct 和主动查询策略 | [冻结对照协议](docs/decisions/036-frozen-query-comparison.md) |
| 通过 HTTP 提交持久化调查任务 | [任务 API 与 Worker](docs/decisions/016-task-http-api.md) |
