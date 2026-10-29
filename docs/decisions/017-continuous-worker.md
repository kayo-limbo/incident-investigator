# 常驻 Worker：连续消费、有限退避与排空退出

任务API只负责提交，原来每个任务都需要手动启动一次 `work-once`。新增 `work` 命令持续消费，复用原有回收、领取、心跳和终态条件更新；不新增第二套所有权协议。

## 生命周期

- 每个进程只有一个执行槽，完成后立即尝试下一项；横向运行多个进程时由数据库 `SKIP LOCKED` 互斥领取。
- 每轮先回收一批过期任务，再领取一个任务。空队列默认等待1秒，`--poll-seconds`接受0.05至60秒。空闲不持续输出日志。
- 数据库连接异常（OperationalError、InterfaceError）或TaskConflict使本轮停止，之后重新进入回收与领取。不会拿旧令牌直接重复执行或写回。未知异常与SQL编程错误向上传播，CLI返回失败，不用重试掩盖迁移遗漏。
- 基础设施连续失败默认5次后退出1；`--max-errors`接受1至100。第n次重试等待 `uniform(0.5, 1) * min(30, 2^(n-1))` 秒，限制等待并分散多个Worker重连。一次成功轮询（包括空队列）重置连续失败次数。
- 调查本身failed/cancelled已经是持久化终态，记录后继续处理其他任务，不计作基础设施失败，不自动重新调查。
- 收到SIGINT或SIGTERM后设置停止事件，空闲/退避等待立即唤醒；当前轮次已进入时允许其完成，不再进入下一轮。当前调查继续心跳和提交，不因正常部署退出而被标记cancelled。

停止事件在轮次边界检查；信号可能在同步数据库调用中到达，正在进行的回收/领取轮次仍可能领取一项再排空。同步数据库及fixed执行仍可能阻塞事件循环，退出不是严格有界。模型策略沿用任务总时限，清理、文件与数据库等待可能超出时限；强杀依靠后续Worker租约恢复，不保证取消报告落库。重复停止信号不会强制取消；没有增加硬退出计时器。

排空不等于exactly-once。数据库提交结果不确定、租约过期接管仍可能重复外部模型调用；恢复与token预算按尝试计算，沿用三次领取上限。本阶段没有进程管理器、自动拉起、全局并发/费用限制或任务优先级。

## 运行

先按[任务HTTP接口](016-task-http-api.md)启动数据库并完成init，API与Worker使用相同快照目录。在另一个终端运行：

```powershell
$env:TASK_DATABASE_URL = 'postgresql://investigator:local-task-dev@127.0.0.1:55432/investigations'
.\.venv\Scripts\python.exe -m backend.tasks --snapshots .snapshots work `
    --worker-id worker-1 --lease-seconds 30 --poll-seconds 1 --max-errors 5
```

随后通过API提交任务，不再需要逐次执行work-once。Ctrl+C请求排空退出，退出码0；连续基础设施失败达上限为1。每个并行Worker建议使用不同worker-id用于观测，实际排他性由run_token保证。保留work-once方便单次调试。

标准输出为逐行JSON事件：worker_started、task_finished、worker_retry、worker_stopped。只记录任务ID、终态、尝试次数、固定错误码与退避秒数，不输出结果证据、连接串、令牌或异常原文；并不构成持久化审计事件历史。停止事件的processed仅统计本进程成功取得的终态返回数，不是全局成功计数。

## 验证与依据

`tests/test_task_daemon.py` 覆盖空闲唤醒、活动任务排空、退避序列与上限、成功轮询重置错误计数、永久数据库错误不重试、日志不含异常秘密、参数校验、真实SIGTERM处理及处理器恢复、CLI参数传递。真实PostgreSQL验证连续完成三项任务（含过期接管）及停止时当前任务完成而下一项保留queued。后者执行器使用可控等待替身，结果与租约写入真实数据库。

Python信号必须在主线程注册，因此只有CLI的run_service负责安装并在finally恢复处理器；嵌入其他程序使用run_forever并传入asyncio.Event。使用signal.signal兼容Windows，不依赖Windows不支持的loop.add_signal_handler。处理器通过call_soon_threadsafe通知事件循环。来源：[Python信号文档](https://docs.python.org/3/library/signal.html)、[事件循环线程安全调度](https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.call_soon_threadsafe)，没有复制外部代码。

Windows的TerminateProcess/进程terminate不是可捕获的Unix SIGTERM，强杀不能保证排空。本机信号测试使用signal.raise_signal验证Python处理路径，不声称验证了Linux容器停止。本阶段不使用真实模型，不测吞吐量或重连风暴性能。
