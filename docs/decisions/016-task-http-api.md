# 调查任务 HTTP API：提交与执行分离

现有任务只可通过 CLI 提交。本阶段增加 FastAPI 接口，将持久化队列开放给后续工作台，复用既有租约 Worker；HTTP 请求不执行模型，也不启动进程内后台调查。

## 协议与一致性

| 接口 | 成功响应 | 语义 |
| --- | --- | --- |
| `POST /tasks` | 202 + `Location: /tasks/{id}` | 必填 `Idempotency-Key`，body 使用既有 TaskRequest |
| `GET /tasks/{id}` | 200 | 返回任务当前状态、参数、尝试次数及结果 |
| `POST /tasks/{id}/cancel` | 200 | 只取消 queued 任务 |

同键、同规范化参数返回同一个任务，即使已完成也不再排队；同键、不同参数返回409。首次提交与重放统一202，表示接受该任务身份，不表示一定正在排队，当前状态以 body 为准。默认参数省略与显式提供语义相同，由既有存储层规范化并通过数据库唯一约束处理并发；没有应用层“先查再插”窗口。

客户端遇到连接中断或503不能据此推断写入未发生，应保留同一幂等键与相同参数重试。客户端断开不保证取消线程内的数据库操作；主动取消需调用取消接口。幂等键目前全局共享，没有租户隔离。

取消前查询用于区分404，但是否允许取消由 `WHERE state='queued'` 的原子更新决定。查询后即使被Worker领取，取消也会返回409，不会撤销running任务。已经取消的重复取消也返回409。HTTP层不接受run_token，不提供完成、续租或领取接口。

## 资源与错误边界

所有文件与数据库操作位于同步 `def` 路由中，FastAPI在线程池执行；避免直接在事件循环中调用同步连接。每次存储操作仍使用独立短事务，API与Worker不共享连接。未引入数据库连接池、应用限流或负载容量保证，线程池饱和仍会排队。

提交前校验本地快照存在、大小及内容哈希；API与Worker必须配置同一快照目录。快照在校验后被删除仍可能导致Worker失败；重放提交也需要快照可读。这里未实现快照上传、跨机器复制或快照与数据库之间的原子事务。

响应使用显式TaskView，只暴露公开任务字段，不返回领取令牌、worker_id、request_hash或幂等键。结果本身仍含调查证据，并非脱敏报告。错误统一形如 `{"error":{"code":"invalid_request"}}`：

- 422：请求、UUID、幂等键或快照完整性无效，不回显用户输入与验证异常上下文。
- 404：任务或快照不存在。
- 409：幂等参数冲突，或任务已经不在排队状态。
- 503：数据库或快照存储不可用，不返回数据库地址、SQL或原始异常。

工厂启动不迁移数据库，不加载模型凭据；迁移继续通过显式CLI执行。此阶段没有认证、租户授权、SSE、列表分页或运行中远程取消。仅作为本地开发接口，以下命令绑定127.0.0.1。

## 运行

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-tasks.txt
docker compose -f compose.tasks.yaml up -d --wait
$env:TASK_DATABASE_URL = 'postgresql://investigator:local-task-dev@127.0.0.1:55432/investigations'
# 升级前停止旧Worker；init会执行既有租约迁移
.\.venv\Scripts\python.exe -m backend.tasks init
$env:TASK_SNAPSHOT_DIR = (Resolve-Path .snapshots).Path
.\.venv\Scripts\python.exe -m uvicorn backend.tasks.api:create_app --factory --host 127.0.0.1 --port 8010
```

打开 http://127.0.0.1:8010/docs ，使用已有窗口查询生成的snapshot_id提交任务。也可在另一终端执行：

```powershell
$body = @{ snapshot_id = '<已有的64位snapshot_id>'; strategy = 'fixed' } | ConvertTo-Json
$task = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8010/tasks `
    -Headers @{ 'Idempotency-Key' = 'demo-1' } -ContentType 'application/json' -Body $body
# Worker终端也需设置TASK_DATABASE_URL，--snapshots必须与API指向同一目录
.\.venv\Scripts\python.exe -m backend.tasks --snapshots .snapshots work-once --worker-id worker-1
Invoke-RestMethod "http://127.0.0.1:8010/tasks/$($task.id)"
```

## 验证与来源

`tests/test_task_api.py` 包含HTTP协议、隐私字段投影、非法输入不回显、快照错误、数据库错误以及阻塞数据库调用不阻塞其他请求的测试。真实PostgreSQL用例验证六路并发同键提交只生成一个可领取任务、参数冲突、HTTP提交→fixed Worker→结果查询→终态重放，以及八轮取消/领取竞争。测试使用独立随机schema并清理，不读写已有任务。

版本沿用锁定依赖：FastAPI 0.143.0、Starlette 1.8.0、HTTPX 0.28.1、psycopg 3.3.3；本地核对FastAPI路由的 `run_in_threadpool` 调用。线程池设计依据 [FastAPI同步与异步路由说明](https://fastapi.tiangolo.com/async/)，没有复制外部源码。本版本Starlette提示TestClient的HTTPX支持将弃用；当前测试可用，未为消除警告变更全项目依赖。

本次是接口与一致性验收，不是吞吐量测试或新的模型诊断准确率评测；具体运行结果在README记录。
