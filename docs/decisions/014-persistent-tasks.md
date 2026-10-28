# PostgreSQL 调查任务与一次性 Worker

## 问题与边界

原 CLI 在进程内运行调查，无法凭任务 ID 查询历史结果。本阶段新增任务表和 `work-once` 入口，复用 frozen snapshot、fixed、agent 和 hypotheses 执行器。任务完成仅表示执行协议完成，不证明诊断正确或业务恢复。

这是第一版持久化实现，尚无 HTTP API、常驻调度器、租约、自动重试、崩溃接管或运行中远程取消。进程在领取后崩溃，任务会留在 running。不能声称 exactly-once 执行。模型调用完成但结果提交失败时也可能处于这一状态。

## 一致性设计

- `idempotency_key` 唯一约束处理并发提交；相同 key 和规范化请求返回原任务，不同参数返回冲突。终态任务也不会因为同 key 再次执行。
- 短事务通过 `FOR UPDATE SKIP LOCKED` 选择 queued 行并更新为 running，事务提交后才执行调查。被锁行会被跳过，因此不保证严格 FIFO。
- 领取生成独立 `run_token`。完成更新同时检查任务 ID、令牌、running 状态及快照 ID；重复完成、错误所有者或其他快照不能覆盖结果。
- 普通结果、假设历史及取消报告按原报告结构保存 JSONB。completed 映射 completed；cancelled 映射 cancelled；其他终止原因映射 failed，保留报告及终止码。
- 队列取消只接受 queued。运行中第一次 Ctrl+C 尽量保存调查取消报告；数据库不可用、第二次中断和强杀无法保证保存。
- 未预期执行异常只保存 `worker_error`，不存原始 SDK 异常。数据库连接或提交错误由 CLI 输出 `database_error`，不自动重新执行模型。

数据库连接每次短事务创建并关闭，设连接、语句与锁等待超时；当前同步数据库访问适合独立单任务 Worker，不应直接放入 Web 事件循环。快照仍在共享目录中，数据库仅保存内容寻址 ID；所有 Worker 必须访问同一份快照。模型配置在执行时加载，不把密钥写进请求记录；相同任务在不同 Worker 配置下可能使用不同模型，配置版本固定留待后续。

## 本地运行

依赖单独放在 `requirements-tasks.txt`，不增加故障实验镜像的数据库依赖。Compose 是独立项目，绑定回环地址 55432。示例口令仅用于本地开发，不可用于共享部署。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-tasks.txt
docker compose -f compose.tasks.yaml up -d --wait
$env:TASK_DATABASE_URL = 'postgresql://investigator:local-task-dev@127.0.0.1:55432/investigations'
.\.venv\Scripts\python.exe -m backend.tasks init
# snapshot_id 使用已有 window_cli query 的输出；提交前校验本地快照完整性
.\.venv\Scripts\python.exe -m backend.tasks submit <snapshot_id> --idempotency-key demo-1 --strategy fixed
.\.venv\Scripts\python.exe -m backend.tasks work-once --worker-id worker-1
.\.venv\Scripts\python.exe -m backend.tasks get <task_id>
.\.venv\Scripts\python.exe -m backend.tasks cancel <queued_task_id>
```

每个 work-once 最多处理一个任务；空队列返回 idle。可以在两个终端用不同 worker-id 执行。agent/hypotheses 会向配置的模型服务发送证据，使用与 window_cli 相同的 `.env` 模型配置和预算。数据库 URL 只读进程环境变量。`init` 仅创建初始表结构，不是版本迁移工具。

## 验证

```powershell
$env:TASK_TEST_DATABASE_URL = $env:TASK_DATABASE_URL
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_tasks.py -v
```

集成测试为每个案例创建独立随机 schema，结束时只删除该 schema。覆盖并发幂等提交、互斥领取、锁跳过、领取事务释放、错误令牌/快照/终态写入拒绝、队列取消、结果重读和取消报告保存。没有数据库环境变量或驱动时明确跳过，跳过不算通过。单元测试覆盖参数边界、策略预算透传、适配器清理、空队列、取消传播以及持久化失败不重跑模型。

2026-10-28 首轮验证因 Docker Desktop 运行时 socket 不可访问，9 项数据库集成用例未运行。引擎恢复后补齐验收，并新增 CLI 全流程和失败报告落库用例：11 项真实 PostgreSQL 集成测试、6 项任务单元测试全部通过，无跳过。CLI 测试只替换隔离 schema 的存储工厂，数据库和 fixed 执行器均真实运行；超时与取消报告保存使用构造报告，不代表本次通过真实模型触发了超时或取消。

并发用例分别启动 6 个提交线程、8 个领取线程，验证同键只有一个任务、8 个任务各被领取一次；另持有一行锁验证另一连接能领取后续任务，使用 NOWAIT 验证调查执行时领取事务已释放。这是功能与一致性验收，不是吞吐量测试。

同次全量回归：设置数据库测试 URL 后运行 `python -m unittest discover -s tests -q`，163 项全部通过，无跳过；未调用真实模型或重跑故障注入实验。

## 来源与版本

自行实现任务表、状态流转和 Worker，没有复制第三方源码。原计划 `postgres:17.6` 首次拉取缓慢，改用本机已有 PostgreSQL 17.11 Alpine 镜像并固定摘要 `postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73`；通过 `postgres --version` 和运行容器内 `SHOW server_version` 核对为 17.11。未验证原计划的 17.6。Python 驱动固定 `psycopg[binary]==3.3.3`。使用 PostgreSQL 原生行锁和唯一约束；参考 [PostgreSQL 17 SELECT / SKIP LOCKED](https://www.postgresql.org/docs/17/sql-select.html) 与 [Psycopg 3 安装说明](https://www.psycopg.org/psycopg3/docs/basic/install.html)。SKIP LOCKED 适用于队列争抢，不适合需要完整一致视图的一般查询。
