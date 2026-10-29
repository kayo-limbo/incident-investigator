# 任务租约、有限接管与迟到结果

第一版任务在领取后退出会永久留在 running。本阶段为每次领取增加数据库租约和尝试次数，允许后续 Worker 回收失联尝试；不从调查中间步骤续跑。

## 状态与所有权

- 默认租约30秒，Worker每10秒续租；`--lease-seconds` 接受3至3600的整数，心跳间隔为租期的三分之一。
- 领取时更新 `run_token`、`lease_expires_at`，累加 `attempt_count`。时间来自数据库，避免不同Worker主机时钟差异；数据库墙上时钟仍可能调整。
- 续租、成功提交和失败提交均检查当前令牌、running状态和未过期条件。过期Worker即使尚未被接管也不能重新续租或写回。
- `recover_expired()` 每批最多100项，通过行锁跳过被其他事务占用的候选。第1、2次过期重排为queued；第3次过期终止为failed，错误码 `lease_attempts_exhausted`。
- 每次 `work-once` 在领取前回收一批，也提供显式 `recover`。没有常驻调度器；无人调用就不会自动推进。普通调查失败、取消和预算耗尽均为终态，不因租约机制重试。
- 新尝试生成新令牌，旧尝试迟到结果不能覆盖新结果。数据库提交和模型调用不是同一个事务，这不是exactly-once：原供应商请求可能继续执行，接管可能重复调用、重复计费。

接管从同一冻结快照重新开始，时间、token预算按尝试重新计算，最多三次尝试并不等于全任务有严格费用上限。仅保存当前/最后尝试的报告，没有完整尝试事件历史。模型配置仍由执行机器加载。

## Worker 生命周期

调查与心跳是两个异步任务。续租冲突或数据库异常时，取消调查并向上抛出错误，不另写一个可能覆盖其他所有者的失败报告。正常结束或取消时清理两个子任务，之后才提交终态。取消报告通过原调查Task取回，避免 `gather(return_exceptions=True)` 抹去自定义取消异常中的报告。

数据库连接仍是同步短事务，可能阻塞本Worker的事件循环；固定策略也是同步执行，期间无法运行心跳。长时间阻塞导致租约过期时，结果被拒绝，后续尝试受三次上限约束。默认租期应留出数据库延迟余量，3秒仅适合测试。清理依赖协作取消，强杀、数据库不可用或再次中断无法保证取消报告落库。

## 升级与运行

**先停止所有旧版Worker，再运行init。** 旧二进制不检查租约，不能与新版混用。版本2迁移在事务与表锁内执行，添加租约列、次数和索引，记录 `task_schema_versions`。重复init不会重复迁移；queued和已有终态保留。旧版无租约running标记failed / `legacy_running_without_lease`，不自动重跑；确认需要重试时使用新幂等键提交。

```powershell
$env:TASK_DATABASE_URL = 'postgresql://investigator:local-task-dev@127.0.0.1:55432/investigations'
.\.venv\Scripts\python.exe -m backend.tasks init
.\.venv\Scripts\python.exe -m backend.tasks work-once --worker-id worker-1 --lease-seconds 30
# 可单独回收，不会执行模型；超过100项时重复调用
.\.venv\Scripts\python.exe -m backend.tasks recover
$env:TASK_TEST_DATABASE_URL = $env:TASK_DATABASE_URL
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_tasks.py -v
```

## 验收边界

2026-10-29 在PostgreSQL 17.11上专项24项、全量170项均通过，无跳过；实际CLI `init` 迁移成功，空队列 `recover` 返回空列表。

真实PostgreSQL测试覆盖到期前心跳延长、到期后拒绝renew/finish/fail、新旧令牌隔离、四个并发回收者只回收一次、三次上限、迁移与重复init、外部取消保留部分报告和子任务清理。

进程退出用例启动独立Python进程，领取后 `os._exit(0)`，再由测试SQL把租约改为过去，下一Worker成功接管。它验证真实进程退出后持久化状态与接管，不测等待完整租期的故障检测延迟。取消报告用替身构造，本次未调用真实模型，也未证明供应商请求已经取消。
