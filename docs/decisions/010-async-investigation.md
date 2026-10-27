# 普通窗口调查的异步调用、总时限与取消

本文记录普通窗口策略首次迁移时的范围。后续假设策略也已接入，公共调度与状态边界见 [假设策略迁移](011-async-hypotheses.md)；下文关于假设CLI尚未迁移的描述属于本阶段历史状态。

## 问题与范围

同步执行器只能在 model.decide 返回后检查截止时间。模型响应停在网络读取阶段时，即使总预算已耗尽，也无法及时停止本地等待。

本次普通窗口 CLI 改用 AsyncWindowChatModel 和 investigate_window_async。固定规则、假设策略、旧单 Trace 与评测脚本的同步调用保持原接口；旧评测结果没有重算。异步报告 strategy 为 window_react_async_v1，与同步策略区分。

## 执行顺序

1. CLI 读取配置，在一次调查的 async with 中创建并复用 AsyncClient。
2. 执行器创建 InvestigationBudget，随后读取冻结快照。客户端初始化和CLI配置读取不在调查计时范围内。
3. 根据剩余时间进入一次 asyncio.timeout 范围，所有轮次共享该范围，不能每轮重置预算。
4. await model.decide 请求模型。适配器重用同步模型的请求构建、响应解析及用量提取方法；HTTP网络阶段超时仍由HTTPX处理。
5. 不论成功、异常还是取消，finally 都记录本次模型调用。模型返回后提供取消检查点并再次校验截止时间。
6. 同步与异步执行器调用同一个 apply_window_action，统一校验动作、证据引用和工具次数；迟到动作不能触发下钻或提交结论。
7. CLI 退出异步客户端上下文后打印报告，并按 --output 写入UTF-8文件。

快照读取和下钻仍是同步本地操作；不把它们丢进线程池来假装底层工作已取消。事件循环恢复执行后，预算检查会阻止接受迟到结论。

## 三种停止原因

| 条件 | 报告 termination | 处理 |
| --- | --- | --- |
| HTTPX网络阶段超时，但调查总时限未耗尽 | model_error | 调用记录 error_code=timeout |
| 调查总时限到期 | deadline_exceeded | timeout范围取消本地await；保留证据和调用记录 |
| 调用者 task.cancel()，或CLI调查期间第一次SIGINT | cancelled | 库函数抛出携带报告的InvestigationCancelled；CLI转换为报告，退出码130 |

工具次数耗尽仍为 budget_exhausted；合法拒答仍需 termination=completed。取消或执行失败不能计作合理拒答。

InvestigationCancelled 继承 asyncio.CancelledError，不在库层吞掉取消：直接等待调查Task的调用者仍能观察 task.cancelled()。报告通过异常的 report 属性获得。不要先调用 task.exception() 获取取消结果，因为已取消任务的该方法也会抛出取消异常。CLI是明确负责退出和展示的边界，才允许将取消转为普通报告。

执行器记录被中断的模型调用为 status=cancelled、error_code=call_cancelled。报告层区分外部取消与总时限，调用层只说明该调用被取消。响应不完整时usage未知，不能填0，也不能复用前一轮usage。

## 资源与边界

- async with response确保离开响应流时清理连接；CLI的模型上下文负责关闭客户端。库调用者负责模型生命周期。
- 一个适配器实例由一次调查独占，避免并发调用共享last_usage。不同调查应创建不同实例。
- 取消是协作式的，不是强杀线程或进程。同步文件访问、JSON解析、事件循环调度及清理耗时都可能使实际返回晚于截止时间。
- 客户端断开不证明供应商停止计算或计费。
- 任务尚未开始运行就取消时，执行器尚无会话，无法提供部分报告。CLI初始化前中断、二次强制中断、进程被杀也不保证保存报告。
- 报告仍在结束时写文件，没有持久化检查点；不具备崩溃恢复能力。
- 当前没有自动重试，也没有增加真实供应商调用或改变历史评测判分。

## 验证与复现

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_async_investigation.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy agent --timeout-seconds 60 --output investigation.private.json
```

新增测试覆盖：正常动作与非法引用、总预算共享、外部取消与Task状态、不同调查隔离、取消保留已下钻证据、用量未知与重置、流和客户端关闭、网络超时分类、CLI报告落盘、SIGINT退出码、工具预算及快照失败。

真实本机HTTP测试发送响应头及一部分正文后停止发送，等待调查到期，断言服务端收到连接关闭。它验证实际HTTP传输的本地取消，不依赖供应商密钥，也不能代表供应商侧行为。另有模拟流关闭检查。

首轮105项测试出现1项测试读取失败：Windows默认GBK解码UTF-8报告。显式指定UTF-8后取消专项13项测试通过，最终全量108项测试通过；未调用真实模型或重跑容器故障实验。

## 阅读顺序

1. `backend/investigation/window_cli.py`：同步命令行怎样进入事件循环、处理取消报告。
2. `backend/investigation/async_investigation.py`：一次总超时范围、异常传播、finally账本。
3. `backend/investigation/async_model.py`：异步网络等待与响应生命周期。
4. `backend/investigation/model.py`、`window_investigation.py`：同步异步共用的协议与校验。
5. `tests/test_async_investigation.py`：从模型替身到真实HTTP断连和SIGINT验证。

可复现的追问：为什么每轮timeout会放大总时间？为什么finally要记录失败调用？为什么库层不直接返回cancelled报告？为什么读取usage失败不能算作零消耗？

## 依赖依据

使用本地Python 3.12和已锁定的httpx 0.28.1，不增加依赖。实现参考官方API语义，没有复制外部项目源码。

- [Python 3.12任务取消与超时](https://docs.python.org/3.12/library/asyncio-task.html)：CancelledError、asyncio.timeout与协作取消。
- [Python 3.12 Runner与SIGINT](https://docs.python.org/3.12/library/asyncio-runner.html#handling-keyboard-interruption)：首次中断取消主任务。
- [HTTPX异步客户端与响应流](https://www.python-httpx.org/async/)：AsyncClient、异步上下文及关闭生命周期。
