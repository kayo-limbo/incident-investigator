# 冻结快照重复查询控制

## 为什么现在处理

已有模型回放出现重复读取相同Trace。当前工具绑定不可变快照，返回的完整投影已经存在于会话证据；再查同一Trace不会增加信息。两种异步策略默认停止这类重复动作，并提供allow模式保留对照路径。

本规则只适用于本会话成功下钻过的冻结Trace。不同Trace、不同快照或新调查不共享状态；不缓存实时数据，不将真实数据源查询失败永久记为“已查过”。

## 接口与报告

```powershell
# 默认遇到重复查询就停止
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy agent --output result.private.json
# 两种模型策略均支持显式允许重复，用于对照
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy hypotheses --repeat-query-policy allow --output comparison.private.json
```

异步Python入口参数repeat_query_policy默认stop。FrozenWindowSession与既有同步入口默认allow，固定规则CLI拒绝显式传入此选项。

WindowContext和WindowReport携带repeat_query_policy。模型可见该策略，输入深拷贝不能修改实际会话策略。已有报告缺少字段时按allow读取，符合旧入口行为。

停止时termination=repeated_query，repeated_query字段包含snapshot_id、trace_id、previous_evidence_id和model_call。它指向本会话中先前实际成功的证据与提出重复动作的模型调用序号。普通报告、假设报告均保留相应状态和用量。

## 检查与提交顺序

1. 按既有规则处理取消、时间、模型错误和token预算。
2. 解析动作Schema、检查工具预算与快照范围。非法动作仍为invalid_action，不能借重复检查绕过校验。
3. 普通策略在真正下钻之前检查重复。假设策略还会先校验整批候选及引用，再在正式替换账本前检查重复。
4. 如果重复，抛出专用RepeatedQueryDetected。它不是ValueError，避免被旧输入错误处理吞掉。
5. 公共异步执行器生成停止报告；第二次工具没有执行，候选假设没有提交，也不会再调用模型。

候选假设更新和查询属于同一动作。即使更新本身合法，查询被拒绝时也不部分提交。此前有效账本、证据和查询记录保持不变。独立历史审计继续检查已完成查询与证据对应关系。

边界优先级沿用现有控制：工具预算已经耗尽时是budget_exhausted；token阈值先触发时是token_budget_exhausted；假设更新非法时先报告校验失败，不再把它记为重复。

## 什么计数，什么不计数

模型已经提出重复动作，因此这次模型调用和返回的usage必须计入。仅未执行的工具调用不计入：不新增get_snapshot_trace证据，不增加drilldown_calls，也不伪造缓存命中成功。

例如先读取Trace A，再要求重复读取A：报告model_calls=2、drilldown_calls=1、tool_calls=2（含摘要）。原证据仍在evidence内，停止报告指向它。

allow模式中两次下钻都实际执行并记账，各自有证据ID；由于快照不可变，数据相同。该模式仍受工具次数、时间和可选token预算约束。

## 为什么选择停止，而非自动修复

直接复用原证据可以节省一次本地下钻，但模型可能持续重复决策。反馈错误并要求重新规划则需要额外模型回合、反馈协议和预算策略。本次先提供明确可测的停止规则，不增加隐式修复或修改模型动作。

代价也明确：allow模式下可能在下一轮成功结束的调查，在stop模式下会提前失败。因此更少调用不等于更高效，更早结束不等于诊断正确；需要后续同输入、同模型、同预算对照质量与成本。repeated_query不是completed，独立评测不会把它算作合理拒答。

## 验证与学习入口

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_repeated_query.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

测试覆盖两种策略stop/allow、原证据定位、真实已发生用量保留、假设候选整批不提交、不同Trace与快照/会话隔离、深拷贝策略隔离、校验/预算优先级、CLI报告及同步入口兼容。

2026-10-28：新增13项专项测试通过。首次全量146项中有1项旧测试失败：它依赖两轮80ms等待在130ms预算中恰好进入第二轮，在本机调度延迟下只进入第一轮。改为可控时钟并断言只创建一个预算和超时范围后，全量146项通过；真实HTTP取消测试继续保留。未调用真实模型服务或重跑容器故障实验。

阅读顺序：window_investigation.py的check_repeated_query与drilldown → hypothesis_agent.py中提交候选前的检查 → async_investigation.py的专用异常处理 → tests/test_repeated_query.py的stop/allow对照。

这是运行控制验收，不是真实模型质量评测；不修改既有固定协议结果，也不声称泛化能力或成本改善。
