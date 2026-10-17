# 连接池竞争的观测与对照设计

## 目标

在已有 orders → inventory 两服务链路上加入真实 HTTP 客户端连接池竞争，区分下游服务处理慢与订单侧发送前等待。先完成可重复负载和人工可解释的观测，再扩展调查工具与模型/规则对照，不预设 Agent 一定优于规则。

## 实现

订单进程复用一个 HTTPX AsyncClient。新增 INVENTORY_MAX_CONNECTIONS（默认 100，范围 1–1000）和 INVENTORY_POOL_TIMEOUT_MS（默认 2000，范围 1–10000）。通过 HTTPX Limits 和 Timeout(pool=...) 配置真实连接池，固定 HTTP/1.1，不使用额外 semaphore 模拟连接池。PoolTimeout 单独映射为订单 HTTP 503，日志 outcome=pool_timeout；CLIENT span 保留实际异常类型。

lab/client_observation.py 通过 AsyncHTTPTransport 子类与公开 trace 扩展，给现有 CLIENT span 附加字段，不新增 span，不读取或替换 httpcore 私有状态。

| 字段 | 定义与限制 |
| --- | --- |
| client.max_connections | 当前客户端配置的连接上限 |
| client.pool_timeout_ms | 当前配置的单次连接池等待超时；不是请求总截止时间 |
| client.in_flight_at_start | transport 入口时未完成的 transport 调用数，包含等待者；不是已占用连接数 |
| client.request_headers_started | 是否观察到 HTTP/1.1 请求头开始发送事件 |
| client.pre_send_ms | transport 入口到开始发送请求头，包含排队、建连和调度；不是纯连接池等待时间。未发送时缺失 |
| client.connect_ms | TCP 建连阶段时间；连接复用时可能缺失，不能把缺失值解释为 0 |
| client.transport_ms | transport 入口到取得响应头或异常，包含发送前等待，但不含完整响应体读取 |

所有时长使用同一进程的单调时钟。每次请求保留独立回调状态，finally 在异常和取消时递减 in_flight。该计数面向单进程单事件循环，不是集群指标。

不将 CLIENT 与 SERVER 的耗时差标为 pool wait。成功请求的 pre_send 变长只提供发送前延迟证据，还需结合连接配置、并发量、建连时间和同窗口库存 SERVER span。PoolTimeout 则直接表明等待可用连接失败，但无法单独证明连接池配置不合理：下游变慢也会让连接长时间被占用。

## 实验设计

使用同一异步库存端点，正常工作负载统一设置为 80 ms 模拟处理时间，慢下游设置为 500 ms。每组预热一次，不计入结果；随后 24 请求按固定并发度分批发送，批次完成后才开始下一批。不是固定到达率压测，也不测最大吞吐。

七组条件：健康（池 4/并发 8）、健康宽池（8/8）、慢下游（8/8）、小池（1/8）、慢下游导致池竞争（4/8）、小池低负载（1/1）、恢复到健康条件（4/8）。各组 pool timeout 均为 250 ms；前两组为对应干预提供对照。

实验结束，包括异常退出后，恢复零延迟、正确库存地址、池 100、pool timeout 2000 ms，并用订单业务请求验收恢复。所有已有 compose helper 调用也显式设置连接池默认值，避免继承终端残留故障配置。

评测器保存场景真值、每个请求 Trace ID、观测字段和汇总；不将这些结果作为调查输入。现有调查工具的投影白名单与结论类型尚未扩展，当前阶段不宣称模型已经能够诊断新场景。

## 来源与版本

使用项目已锁定的 HTTPX 0.28.1、httpcore 1.0.9。依据公开接口独立编写，没有复制开源项目实现。2026-10-17 核对：

- [HTTPX 资源限制](https://www.python-httpx.org/advanced/resource-limits/)：Limits 控制连接上限。
- [HTTPX 超时](https://www.python-httpx.org/advanced/timeouts/)：pool timeout 与读/写/连接超时的区别。
- [HTTPX trace 扩展](https://www.python-httpx.org/advanced/extensions/)：异步回调与请求头发送、TCP 建连事件；事件名可能随版本变化。
- [httpcore 1.0.9 连接池源码](https://github.com/encode/httpcore/blob/1.0.9/httpcore/_async/connection_pool.py)：核对分配连接、重试连接分配与等待语义。一次请求可能经历多次连接分配等待，pool timeout 不等于累计发送前耗时上限。
