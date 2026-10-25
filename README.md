# Incident Investigator

面向微服务的故障调查与恢复验证项目，探索如何让 Agent 根据可观测数据提出诊断，并通过独立检查验证处置效果。

## 当前状态

已实现两个最小 FastAPI 服务：订单预览通过 HTTP 查询固定库存，并提供 Docker Compose 配置、可控延迟、请求关联与耗时日志。已有 OpenTelemetry 显式埋点、Jaeger、固定调查流程和受限 Agent 执行器。DeepSeek 适配器与命令行入口已完成真实五场景验收，并确认业务恢复。尚未接入数据库或自建 Web 平台。以下计划能力不代表已完成。

进度按验收划分：

- [x] 双服务健康请求与 Docker Compose 可重置环境。
- [x] 延迟故障、结构化日志和跨服务调用链，已保存独立验收结果。
- [x] 第二类故障：订单的库存地址端口错误；库存自身保持可用，已验证配置修复后的恢复。
- [x] 首版固定排查流程与四个只读证据工具；已有结构化证据和接口边界，尚非进程级隔离。
- [x] 最小调查执行器：结构化动作、工具白名单、调用次数预算与证据引用校验。
- [x] DeepSeek V4.1 Flash 适配器与 CLI：已完成模拟 HTTP 测试和一次真实五场景验收，库存停止时合理拒答。
- [x] 真实 HTTP 连接池竞争实验：七组并发/低负载/慢下游对照，已验证发送前延迟、PoolTimeout 和恢复。
- [x] 冻结窗口接入规则与 Agent：绑定输入、字段引用校验、调用/token 用量及四场景对照入口。
- [x] 显式假设策略：版本历史、支持/反证字段、查询目的与结论关联；提供同快照三策略回放。
- [ ] 重复查询控制与累计 token/时间预算。
- [ ] 服务化工作台、审批处置、批量评测。

目前已完成最小故障实验环境、规则基线和最小模型调查闭环；五场景验收不代表完整评测体系或泛化能力。

## 窗口证据与冻结快照

新增 `WindowEvidenceTools`：订单窗口查询、成功/失败样本摘要、连接池观测投影、本地内容寻址快照和离线下钻。窗口最长五分钟、最多 200 条，区分已观测、空结果、部分数据和不可用；任何查询都不保证全量。设计与运行方法见 [窗口证据与快照](docs/decisions/006-window-evidence.md)。

2026-10-18 新增 10 项测试，全部 55 项本地测试通过；真实 Jaeger 三组共 75 个窗口请求（含预热）验收通过，业务恢复已确认。验收入口为 `python -m evals.run_window_evidence`，独立检查正常、小池、慢下游叠加池超时三组请求及最终业务恢复，结果见 [窗口验收记录](docs/experiments/window-evidence.md)。实验独占本地 Compose 环境，会调整并恢复配置。

新增窗口规则与 Agent 入口，共享快照、摘要、请求索引和默认六次工具调用上限；确定性结论要求引用已下钻的具体字段。窗口报告记录模型/工具调用、token、耗时与终止原因，缺失 token 记为未知。原单 Trace 入口继续保留。设计见 [窗口调查协议](docs/decisions/007-window-investigation.md)，真实结果与失败复盘见 [四场景对照](docs/experiments/window-comparison.md)。

新增 `hypotheses` 策略，在同一快照上维护假设版本、支持与反证、待检查项及查询目的，最终结论关联具体假设版本和观测字段。非法更新整批拒绝，保留已有状态。结构校验不证明语义或因果正确；累计 token/时间预算和进程恢复尚未实现。设计与命令见 [显式假设协议](docs/decisions/008-hypothesis-investigation.md)，真实失败与结果见 [假设策略对照](docs/experiments/hypothesis-comparison.md)。

```powershell
# snapshot_id 来自 window_cli query 输出；默认 fixed 无需模型
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy fixed
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy agent --output window-agent.private.json
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy hypotheses --output hypothesis.private.json
# 独占实验环境；both 使用真实模型，完整报告默认不提交
.\.venv\Scripts\python.exe -m evals.run_window_comparison --strategy both
```

固定协议的新参数验收入口已加入：采集前记录五组计划与代码指纹，独立检查真实观测及业务恢复，再对冻结快照执行预定次数的三策略回放。新增测试覆盖协议变化拒绝、场景未出现、失败轮保留和采集失败后的恢复；方法与命令见 [固定协议验收](docs/decisions/009-fixed-protocol-evaluation.md)。2026-10-23 已采集 95 条窗口请求，五组独立业务检查与规则验收通过、业务恢复通过，全量 88 项测试通过。

2026-10-25 在协议不变的条件下完成上述五组快照的两轮真实模型对照：规则 10/10、普通 ReAct 8/10、假设策略 5/10。失败包含类别与文字不一致、动作格式错误、证据关联失败及响应解析错误，均保留且没有补跑替换。假设策略的较低用量受到提前失败影响，尚不能宣称效率或可靠性提升；详见 [新参数验收记录](docs/experiments/holdout-evaluation.md)。

## 固定流程调查（无需模型）

模型无关的调查执行器已提供 `backend.investigation.agent.investigate_agent` Python 接口。命令行默认运行固定流程，`--strategy agent` 启用 DeepSeek。执行器的接口、预算与实际限制见 [最小调查执行循环](docs/decisions/003-bounded-agent-loop.md)；执行器测试通过不代表模型诊断能力已验证。

先产生一个请求，再将返回的 Trace ID 交给调查入口：

```powershell
$response = Invoke-WebRequest "http://127.0.0.1:8000/orders/preview?sku=demo"
$traceId = [string]($response.Headers['X-Trace-ID'] | Select-Object -First 1)
# SDK 批量导出需短暂等待
Start-Sleep -Seconds 2
.\.venv\Scripts\python.exe -m backend.investigation $traceId
```

程序输出结论、限制和带唯一 ID 的证据；需要保存时增加 `--output investigation.private.json`（报告可包含内部服务信息，按需决定是否提交）。工具仅查询真实链路、固定服务契约、一项运行配置和固定地址业务探针；不读取实验真值。详细规则与限制见 [设计笔记](docs/decisions/002-read-only-investigation.md)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 评测会依次注入故障，结束时恢复正确地址与零延迟
.\.venv\Scripts\python.exe -m evals.run_baseline
```

真实容器下已验证健康、延迟、错误端口、库存停止、恢复五个案例。库存停止时返回“证据不足”，而不是凭空认定宕机。结果见 `docs/experiments/baseline-results.json`；这些是功能验收，不是模型准确率或泛化能力指标。

## DeepSeek 调查入口

默认模型 ID 为 `deepseek-flash`，对应 DeepSeek V4.1 Flash，官方地址为 `https://api.deepseek.com`。首版固定非思考模式，使用 JSON 动作协议。运行 Agent 会将所查询的投影证据发送到配置的模型服务。

在项目根目录 `.env` 中填写 `DEEPSEEK_API_KEY=你的密钥` 并保存，再使用上文新生成的 `$traceId`：

```powershell
.\.venv\Scripts\python.exe -m backend.investigation $traceId --strategy agent --max-tool-calls 6 --output investigation.private.json
```

可选配置见 `.env.example`；程序自动读取项目根目录 `.env`，进程环境变量优先。文件使用单行 KEY=VALUE，可加配对引号，不支持变量展开、多行值或行尾注释；`.env` 已被 Git 忽略。默认单次输出上限 2048 tokens、网络阶段超时 30 秒。模型调用无自动重试，最多调用模型 7 次、查询工具 6 次。这里只限制单次生成和调用次数，没有整个调查的 token/费用预算或严格总时限。

独立五场景验收也可选择模型策略（会临时修改本地实验服务，勿并行运行其他故障实验）：

```powershell
.\.venv\Scripts\python.exe -m evals.run_baseline --strategy agent
```

模型结果默认写入 `docs/experiments/agent-results.private.json`，固定流程仍默认写入原来的结果文件。报告包含耗时、工具次数和执行器 `termination`；模型调用失败、动作非法、证据引用非法、预算耗尽均不计为合理拒答。配置缺失在修改实验环境前报错；诊断类别不符会记录失败并继续其余案例，最终恢复服务。五个案例属于功能验收，不是泛化准确率。

独立五场景验收也可选择模型策略（会临时修改本地实验服务，勿并行运行其他故障实验）：

```powershell
.\.venv\Scripts\python.exe -m evals.run_baseline --strategy agent
```

模型结果默认写入 `docs/experiments/agent-results.private.json`，固定流程仍默认写入原来的结果文件。报告包含耗时、工具次数和执行器 `termination`；模型调用失败、动作非法、证据引用非法、预算耗尽均不计为合理拒答。配置缺失在修改实验环境前报错；诊断类别不符会记录失败并继续其余案例，最终恢复服务。五个案例属于功能验收，不是泛化准确率。

缺少密钥时在发请求前报错。鉴权失败、限流、超时、截断或非法输出会结束调查并保留已有证据；报告中的 `insufficient_evidence` 可能代表执行失败，需同时查看 termination 和 summary。当前 41 项本地测试通过。2026-10-17 使用真实 DeepSeek 完成健康、延迟、错误端口、库存停止、恢复五个案例，均符合预期并恢复业务；这是单次功能验收，不是泛化准确率。实际耗时、工具次数和重复查询问题见 [模型验收记录](docs/experiments/agent-evaluation.md)。接口来源和运行限制见 [DeepSeek 适配器设计](docs/decisions/004-deepseek-adapter.md)。

## 连接池竞争与慢下游对照

```powershell
docker compose build
.\.venv\Scripts\python.exe -m lab.run_pool_experiment
```

七组条件各采集 24 个请求，比较正常/慢库存、不同连接池上限及低负载。实测小池组库存 P95 约 83 ms、发送前 P95 约 572 ms；慢下游宽池组库存 P95 约 503 ms、发送前约 7 ms。慢下游叠加池竞争时 24 请求中 12 次 PoolTimeout，而小池在低负载下全部成功。

结果和限制见 [连接池实验](docs/experiments/pool-experiment.md)。`client.pre_send_ms` 包含排队、建连和调度，不能当成纯连接池等待时间。成功请求与超时请求须一起分析；新窗口策略已接入这些观测，原单 Trace 策略保持原有能力。

手动可设置 `INVENTORY_MAX_CONNECTIONS`（默认 100）与 `INVENTORY_POOL_TIMEOUT_MS`（默认 2000）。实验脚本会恢复默认池配置、正确地址和零延迟，并检查业务恢复；它们使用独占本地实验环境，不要并行执行。结果文件 `pool-results.json` 包含场景真值，不可作为 Agent 输入。

## 配置错误故障实验

```powershell
docker compose up --build -d --wait
.\.venv\Scripts\python.exe -m lab.run_config_experiment
```

脚本依次验证正确地址 `http://inventory:8001`、错误端口 `http://inventory:8002`、恢复正确地址，每组发送 3 次订单请求，并在订单容器内单独探测真实库存接口。实验验证订单返回 503 时库存依然可以返回 200，同时检查 CLIENT span 的 `server.address`、`server.port` 和 `error.type`。

结果写入 `docs/experiments/config-results.json`，每次运行覆盖。结束时恢复库存地址及零延迟。所有实验脚本都会显式设定服务地址，避免继承终端残留的错误地址；它们用于独占的本地实验环境，不应并发运行。

手动实验可以设置 `$env:INVENTORY_BASE_URL="http://inventory:8002"` 后执行 `docker compose up -d --wait`。恢复时将该变量改为 `http://inventory:8001` 并再次执行 `up`。脚本只为其子进程覆盖环境变量，不修改你所在 PowerShell 的变量；手动注入后也需要手动恢复终端变量。

延迟故障有完整的库存 SERVER span，且耗时显著升高；端口错误通常只有订单 SERVER 与 CLIENT span。缺少库存 span 本身不足以证明配置错误，还需要结合目标端口、实际运行配置以及同网络视角的库存探针。详见 [配置错误实验记录](docs/experiments/config-experiment.md)。

## 调用链可视化

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose up --build -d --wait
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
```

打开 [Jaeger](http://127.0.0.1:16686)，选择 `orders` 服务并点击 Find Traces。SDK 批量导出有短暂延迟，首次请求后稍等再搜索。也可以从响应头复制 `X-Trace-ID`，打开 `http://127.0.0.1:16686/trace/这里替换成TraceID`，查看订单 SERVER → 订单 CLIENT → 库存 SERVER 的父子关系。

复用下方 500 ms 延迟操作，重新发请求后，库存 span 应显著变长。日志的 `trace_id` 可关联到同一条链路。不要将三个嵌套 span 的耗时相加。

```powershell
# 不依赖 Docker 的上下文传播与错误处理测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 检查 Jaeger 中真实落地的正常、延迟、不可用、恢复链路
.\.venv\Scripts\python.exe -m lab.run_trace_experiment
```

端到端脚本输出 `docs/experiments/trace-results.json`，每次运行覆盖；包含实际 span、关联日志和实验真值，不应作为未来 Agent 的输入。它在结束时恢复库存与零延迟。Jaeger 内存数据会在容器重启后丢失。`docker compose up --wait` 只等待服务启动及已有健康检查；Jaeger 的实际接收能力由验收脚本确认。

本机运行 Python 服务也可启用导出：在两个服务启动终端分别设置 `$env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:4318/v1/traces"`。未设置时不导出。页面端口可用 `JAEGER_PORT` 覆盖，默认 16686；OTLP HTTP 宿主机端口为 4318。

设计、版本、传播过程和限制见 [调用链设计笔记](docs/decisions/001-tracing.md)。

本阶段验证状态：4 个本地 ASGI 测试通过（包括新增连接失败目标端口校验）；2026-10-15 完成容器构建和 Jaeger 端到端验收。正常、500 ms 延迟、库存不可用、恢复四种场景全部通过，已检查实际入库的 span 父子关系、错误标记和日志 Trace ID，并确认 Jaeger 页面能显示三层调用链。详见 [本次调用链实验记录](docs/experiments/trace-experiment.md)。

## 延迟故障实验

安装本地依赖后，在项目根目录执行：

```powershell
docker compose up --build -d --wait
.\.venv\Scripts\python.exe -m lab.run_delay_experiment
```

脚本依次设置 0、500、0 ms 库存延迟。每组预热一次，然后发送 10 次串行订单请求（并发为 1），检查 HTTP 200、业务响应和请求 ID，并收集同一请求的三条日志。结果写入 `docs/experiments/delay-results.json`；每次运行覆盖该文件。结束时恢复无延迟配置，保留容器运行。脚本会重建当前 Compose 项目的库存容器，请不要与其他故障实验同时运行。

也可以手动修改延迟：

```powershell
$env:INVENTORY_DELAY_MS="500"
docker compose up -d --wait
curl.exe -i -H "X-Request-ID: manual-demo" "http://127.0.0.1:8000/orders/preview?sku=demo"
docker compose logs --tail 50 orders inventory

# 恢复；仅 start/restart 不会更新容器环境变量，需要 up 重建。
$env:INVENTORY_DELAY_MS="0"
docker compose up -d --wait
```

库存服务启动时读取 `INVENTORY_DELAY_MS`（0–10000 的整数），使用 `asyncio.sleep` 模拟异步等待。订单客户端仍采用 2 秒网络阶段超时，因此超过该阈值的故障可能返回 503；500 ms 实验预期仍返回 200。`/health` 只检查进程存活，不受故障延迟影响，不代表业务已恢复。

订单服务接收或生成 `X-Request-ID`，传给库存服务，并在响应头中返回。日志包含 `service`、`event`、`request_id`、`duration_ms` 等字段，使用单调时钟测量耗时。三个观测分别为库存处理、订单等待库存、订单总处理时间；它们是包含关系，不能相加。客户端耗时还包含本机到容器的网络与协议开销。日志不输出延迟开关或场景真值，实验结果包含真值，未来不得作为 Agent 的诊断输入。

阅读顺序：`lab/inventory/main.py` 的异步延迟 → `lab/observability.py` 的请求 ID 与计时 → `lab/orders/main.py` 的下游计时 → `lab/run_delay_experiment.py` 的对照与日志匹配。

## Docker Compose 运行

在项目根目录执行（Docker Desktop 使用 Linux 容器）：

```powershell
docker compose up --build -d --wait
docker compose ps
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
```

预期订单接口返回 HTTP 200、`{"sku":"demo","can_order":true}`。如果之前本机启动的服务仍占用 8000/8001，可先在原终端按 Ctrl+C，或在当前 PowerShell 中设置 `$env:ORDERS_PORT="18000"`、`$env:INVENTORY_PORT="18001"` 后启动，并改用对应端口访问。

验证库存故障与恢复：

```powershell
docker compose stop inventory
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
# 预期 HTTP 503

docker compose start inventory
docker compose up -d --wait
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
# 预期恢复 HTTP 200

docker compose logs --tail 50 orders inventory
docker compose down
```

`Dockerfile` 打包依赖和代码，两个服务复用同一份构建定义，由 Compose 指定不同启动命令。容器内监听 `0.0.0.0`，宿主机端口只绑定 `127.0.0.1`。订单通过 `http://inventory:8001` 访问库存；`inventory` 是 Compose 服务名，容器里的 `127.0.0.1` 仅指向自身。

`depends_on` 等待库存健康后再启动订单，但不负责运行中的自动恢复。订单健康检查只检查 HTTP 服务存活；库存停止时订单仍可能显示 healthy，必须请求订单业务接口判断链路是否恢复。

`.dockerignore` 默认排除全部文件，只放行 Dockerfile、依赖清单和 lab 下的 Python 源码。私人文档、Git 历史、本地虚拟环境和 `.env` 不进入构建上下文。

基础镜像使用 `python:3.12-slim` 并在 Dockerfile 中固定已拉取的 SHA256 摘要；依赖版本见 `requirements.txt`。首次构建需要连接 Docker Hub 和 Python 包源。若拉取镜像超时，可单独运行 `docker pull python:3.12-slim` 检查网络。

验证记录：Compose 配置检查、镜像构建与容器启动通过；真实容器下已验证正常请求 HTTP 200 → 停止库存后 HTTP 503 → 重启库存后恢复 HTTP 200。库存停止时可能表现为连接失败或超时，两者均返回 503。本机 Python 进程下的对应验收也已通过。

参考：[Compose 网络与服务名](https://docs.docker.com/compose/how-tos/networking/)、[Docker 构建上下文与忽略规则](https://docs.docker.com/build/concepts/context/)。

## 本地运行

使用 Python 3.12，在项目根目录的 PowerShell 中安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

打开两个终端，工作目录均为项目根目录，分别启动：

```powershell
# 终端一：库存服务
.\.venv\Scripts\python.exe -m uvicorn lab.inventory.main:app --host 127.0.0.1 --port 8001
```

```powershell
# 终端二：订单服务
.\.venv\Scripts\python.exe -m uvicorn lab.orders.main:app --host 127.0.0.1 --port 8000
```

在第三个终端验收：

```powershell
curl.exe -i http://127.0.0.1:8001/inventory/demo
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
```

预期分别返回 HTTP 200 与 `{"sku":"demo","available":10}`、`{"sku":"demo","can_order":true}`。在库存终端按 Ctrl+C，再请求订单接口，预期 HTTP 503 和 `Inventory service unavailable`；重新启动库存服务后，应恢复 HTTP 200。

订单服务默认连接 `http://127.0.0.1:8001`，可通过启动前设置 `INVENTORY_BASE_URL` 环境变量覆盖。HTTP 客户端各网络阶段的超时为 2 秒，并非整个请求的严格总时限。连接失败、超时和下游非成功状态返回 503；库存响应格式错误返回 502。

阅读顺序：`lab/inventory/main.py` 的路由与响应模型 → `lab/orders/main.py` 的 `preview_order` → `lifespan` 中客户端的创建、复用与关闭。库存目前对任何商品都固定返回 10，订单接口只做预览，不创建订单。

实现参考：[FastAPI 生命周期](https://fastapi.tiangolo.com/advanced/events/)、[HTTPX 异步客户端](https://www.python-httpx.org/async/)。

## 计划能力

- 在隔离环境中运行小型微服务链路，复现故障并重置环境。
- 基于指标、日志、调用链与变更记录收集证据，管理候选根因。
- 在工具调用、时间与 token 预算内完成调查，允许证据不足时无法确定根因。
- 经审批执行预设的受限处置，通过独立业务探针和遥测验证恢复。
- 对比固定排查流程、普通 ReAct 与加入证据管理和预算控制的调查策略。

## 初步技术方向

- 业务 API 与调查服务：Python、FastAPI。
- 实验环境：Docker Compose。
- 可观测性：OpenTelemetry，按需接入指标、日志与链路后端。
- 数据存储：PostgreSQL。
- Web 工作台：React、TypeScript。

具体组件与版本将在实现阶段确认。

## 首个里程碑

搭建两个服务之间的请求链路，在相同负载下完成：

1. 运行健康业务请求。
2. 注入可关闭的延迟故障。
3. 从指标或调用链定位异常环节。
4. 移除故障并验证业务恢复。

这一阶段暂不接入模型或开发网页。当前具备基础请求链路、延迟注入、关联日志对照实验和 OpenTelemetry 调用链实现；更完整的负载实验仍待完成。

## 验证原则

诊断必须引用实际观测证据；故障真值与注入控制信息不向调查 Agent 暴露。处置执行成功不等于业务恢复，评测结果以独立检查为准。仅报告实际完成和测试过的能力。
