# Incident Investigator

面向微服务的故障调查与恢复验证项目，探索如何让 Agent 根据可观测数据提出诊断，并通过独立检查验证处置效果。

## 当前状态

已实现两个最小 FastAPI 服务：订单预览通过 HTTP 查询固定库存，并提供 Docker Compose 配置、可控延迟、请求关联与耗时日志。新增 OpenTelemetry 显式埋点和 Jaeger 配置。尚未接入数据库、调查 Agent 或自建 Web 平台。以下计划能力不代表已完成。

进度按验收划分：

- [x] 双服务健康请求与 Docker Compose 可重置环境。
- [x] 延迟故障、结构化日志和跨服务调用链，已保存独立验收结果。
- [x] 第二类故障：订单的库存地址端口错误；库存自身保持可用，已验证配置修复后的恢复。
- [ ] 固定排查流程基线、只读证据查询工具与数据边界。
- [ ] 最小调查 Agent、假设管理与查询预算。
- [ ] 服务化工作台、审批处置、批量评测。

目前完成的是最小故障实验环境，不代表完整第一阶段评测体系已经完成。

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
