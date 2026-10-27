# 假设策略的异步调用与状态边界

## 这次解决的问题

普通窗口CLI可取消，但假设CLI仍在同步等待模型；其假设账本、查询任务和报告生成又嵌在同步执行循环里。复制一份异步策略会让版本与证据校验出现两套实现。

本次将假设调查的状态与调度拆开，保留原动作协议与提示词。两种窗口CLI均支持默认120秒总预算、--timeout-seconds及第一次Ctrl+C后的部分报告。异步假设报告使用window_hypotheses_async_v1，便于与原同步结果区分。

## 文件与职责

| 文件 | 变化与职责 |
| --- | --- |
| backend/investigation/hypothesis_agent.py | HypothesisInvestigationState持有一次调查的账本和查询记录；context构造深拷贝输入，apply验证并提交候选，report保留完整假设报告。同步入口复用此对象。 |
| backend/investigation/async_investigation.py | 两个异步入口各自创建预算、会话和策略状态；共用_run_async的计时、取消、调用记账与异常处理。 |
| backend/investigation/async_model.py | AsyncChatCompletionsModel共用HTTP传输，两种子类仅选择原有提示词与Schema。每次调查独占一个模型实例。 |
| backend/investigation/window_cli.py | 根据策略选择适配器和执行器；两种模型策略统一执行async with、保存报告和返回退出码。fixed不接受时间参数。 |

没有引入框架、任务队列或新的依赖。状态对象属于本次进程内执行，不是持久化检查点。

## 一轮假设更新的顺序

1. 从当前账本生成上下文深拷贝，模型看不到可修改的内部账本或版本控制字段。
2. 等待模型响应，记录成功、错误或取消调用。返回后先处理待决取消，再检查时间预算。
3. 验证动作Schema，生成独立候选账本，验证引用、假设ID、结论关联及工具预算。
4. **候选全部合法后、正式替换账本前，再检查截止时间。** 校验通过但已超时的更新不能生效。
5. 提交候选。finish生成带版本引用的结论；tool记录查询用途与所关联的假设版本，再执行本地快照下钻。
6. 下钻完成后更新查询完成记录，再检查时间。已经取得的证据与已完成的操作仍保留。

当前apply内没有await。单个调查由一个任务独占，外部取消通常在模型等待或轮间检查点交付。这只是当前协作式调度下的执行边界，不是数据库事务；未来若下钻改成异步网络工具，必须重新定义在途查询与取消状态，不能简单添加await就宣称原子性仍然成立。

## 取消和超时时保留什么

| 停止位置 | 保留的内容 | 不接受的内容 |
| --- | --- | --- |
| 第二轮模型等待时取消 | 第一轮假设版本、已完成查询、证据、两次调用记录 | 尚未返回的模型更新 |
| 模型响应迟到 | 已提交状态及迟到调用的用量（若已知） | 迟到动作中的新假设、查询或结论 |
| 候选校验期间超时 | 上一个完整有效版本 | 已验证但尚未提交的候选 |
| 本地下钻结束时发现超时 | 已提交版本、完成的查询记录与新证据 | 后续模型调用 |
| 一批更新中部分非法 | 上一个完整有效版本和拒绝诊断 | 整批候选，不能只提交合法部分 |

外部取消依然抛出InvestigationCancelled（继承CancelledError）；exception.report的实际类型是HypothesisReport，包含hypothesis_history、hypotheses、query_tasks及用量。只有CLI将它转为保存报告并退出130。总时限耗尽返回deadline_exceeded，不作为合理拒答。

## 验证与边界

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_async_hypotheses.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy hypotheses --timeout-seconds 60 --output hypothesis.private.json
```

专项测试验证同步与异步联合故障结论及版本历史一致，并用已有独立audit_history检查；同时覆盖取消、校验期间超时、迟到响应、非法批次、Schema错误、工具预算、快照失败、本地下钻后超时及真实SIGINT报告保存。模型策略结果来自脚本化替身与HTTP模拟，不证明真实模型诊断能力提高。

2026-10-27验证：新增11项专项测试通过，全量119项测试通过（包含已有真实本机HTTP半截响应断连检查）。未调用真实模型服务，未重跑容器故障实验。

原同步hypotheses API未增加默认时间限制，既有评测继续调用同步入口。旧固定协议结果不变；代码指纹已变化，不能将新代码用于旧协议并回填为原测试成功。

仍不支持进程重启恢复、持久化任务、累计token预算；同步文件读取/解析/清理可能越过总时限。用户强制终止进程或二次中断不保证保存报告，本地取消也不代表供应商停止生成或计费。

## 对照提交阅读

先看hypothesis_agent.py中context/apply/report的边界，再看async_investigation.py如何只负责调度，最后运行test_async_hypotheses.py。

重点追问：为什么候选合法仍需提交前检查时间？为何超时报告可以包含刚完成的查询？为什么异常必须携带HypothesisReport而不只是WindowReport？为什么当前内存提交不能称为数据库事务或崩溃恢复？
