# 持久化状态事件与 SSE 断线续传

任务状态原本只能轮询当前值，无法解释一次租约接管经历了哪些阶段。本阶段保存任务生命周期变化，并提供从游标恢复的SSE接口；不记录每轮模型推理、工具调用或完整调查checkpoint。

## 同事务与任务内顺序

版本3迁移添加 `task_events`，主键为 `(task_id, sequence)`。任务INSERT以及state改变的UPDATE由AFTER触发器写入 `state_changed` 事件，包含状态、尝试次数、固定错误码和数据库时间。事件写入与任务操作在同一事务，任一失败全部回滚；幂等重放、心跳续租、状态未变的更新及被拒绝的旧令牌操作均不新增事件。

编号使用同任务已有事件最大值加一。任务INSERT/UPDATE已经持有对应任务行锁，后续状态写入需等前一事务提交或回滚；触发器函数默认VOLATILE，查询取得新的可见快照。主键进一步防止重复编号。编号只在任务内有序，不提供全局游标或跨任务总排序；时间戳可能受数据库时钟调整影响，不能替代sequence排序。应用使用默认READ COMMITTED短事务。

选择数据库触发器是为了把回收批量更新、失败提交等全部状态写入纳入同一约束，减少某个应用路径漏记事件的风险。代价是状态写入依赖数据库函数，调试和迁移必须同时检查SQL；有数据库管理权限的操作者仍可禁用触发器或删除记录，不宣称不可篡改审计。

现有任务回填一条 `snapshot` 事件，表示迁移时的状态，时间是迁移时间。不推断历史创建、领取或失败经过。迁移在既有初始化事务和任务表锁下执行，有版本标记，重复init不重复回填。

## HTTP协议

`GET /tasks/{task_id}/events?after=0` 返回 `text/event-stream`。重连时 `Last-Event-ID` 优先于after；两者都表示该任务最后已消费的sequence，从严格大于它的事件开始补读。数字游标只适用于URL指定任务，客户端须保存 `(task_id, sequence)`，不能跨任务复用。

```text
retry: 1000

id: 2
event: task_state
data: {"task_id":"...","sequence":2,"kind":"state_changed","state":"running",...}

```

- 每批最多100条，以sequence排序。终态事件发送后关闭；重连发现终态且无未读事件返回204，让EventSource停止重连。
- 非法格式返回422，超出数据库最新编号或bigint范围返回409，不默默跳过尚未出现的事件。不存在任务返回404。
- 首次查询失败沿用503；响应已经开始后的数据库异常发送无id的 `stream_error`（固定错误码），关闭连接。客户端保留最后成功游标，重连补读。
- 没有新事件时发送注释心跳，默认每秒查询一次。连接在循环边界检查30秒期限并关闭，EventSource自动重连；这是连接轮换，不是调查超时或慢客户端硬截止。

SSE交付没有客户端处理确认，断线仍可能重复交付。客户端按任务ID与sequence去重，不能把自动重连描述为exactly-once。终态监听器也可以主动调用EventSource.close()。

## 资源边界

首次查询通过同步FastAPI路由在线程池执行，后续异步生成器使用run_in_threadpool读取短事务；发送和等待期间不持有事务或数据库连接。先读取任务状态再读取事件：如果状态已经是终态，其同事务事件也已提交；避免“先读空事件，后读到终态”而漏发最终事件。

每连接只缓存一批，不设无界内存广播队列。断开由StreamingResponse取消/关闭迭代处理，取消时已进入线程的只读查询可能继续到数据库超时，最终关闭连接。当前仍是每个订阅者轮询，没有连接数限制、数据库连接池、LISTEN/NOTIFY、事件清理或保留期限。没有认证和租户隔离，仅用于本地开发。

## 升级与使用

先停止API和Worker，再按现有环境设置 `TASK_DATABASE_URL`，执行：

```powershell
.\.venv\Scripts\python.exe -m backend.tasks init
# 重新启动API与常驻Worker后，用实际任务ID替换下面的占位符
curl.exe -N "http://127.0.0.1:8010/tasks/<task-id>/events"
# Ctrl+C断开订阅不会取消调查；最后读到id: 2时从2之后续传
curl.exe -N -H "Last-Event-ID: 2" "http://127.0.0.1:8010/tasks/<task-id>/events"
```

`task_events`持久化在原PostgreSQL数据卷中，API进程重启不会清空；没有迁移前历史补全。数据库升级在随机测试schema通过，本次没有自动迁移用户默认schema，启动新增接口前需执行上述init。

## 验证与来源

2026-10-29：专项12项、全量206项全部通过，无跳过。专项含7项真实PostgreSQL测试，验证幂等/心跳无冗余事件、写事件失败回滚状态、迁移snapshot及重复init、并发提交和取消/领取竞争、过期接管、SSE游标错误与终态204。真实Uvicorn和HTTPX TCP测试先收取queued事件后关闭连接，再产生running/failed事件，重连成功补读2和3且不重复1，最后服务器可正常退出。未做浏览器UI、代理缓冲或高并发性能验收，未调用真实模型。

实现沿用PostgreSQL17.11、FastAPI0.143.0、Starlette1.8.0、HTTPX0.28.1。依据：[PostgreSQL触发器](https://www.postgresql.org/docs/17/sql-createtrigger.html)、[VOLATILE快照](https://www.postgresql.org/docs/17/xfunc-volatility.html)、[Starlette StreamingResponse](https://starlette.dev/responses/)、[WHATWG SSE协议](https://html.spec.whatwg.org/multipage/server-sent-events.html)。没有复制外部源码。
