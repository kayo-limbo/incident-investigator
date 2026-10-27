# 按供应商返回用量控制累计token

## 范围与语义

agent/hypotheses两种异步窗口接口增加可选max_total_tokens，CLI对应--max-total-tokens。必须是正整数；省略时为None，不启用阈值。fixed策略拒绝此参数。既有同步API与评测入口未接入，旧报告没有token_budget字段时仍可读取。

这是响应后的累计停止阈值，不是请求前的硬成本限制。供应商返回usage之后才知道本次实际消耗；单次max_tokens限制输出，不能约束所有轮次的输入与输出总量。没有实现输入token精确预估、费用换算、预留额度或供应商端账单控制。

示例：阈值10000，第一轮返回6000，允许执行该动作。第二轮返回5000，累计11000，记录超额1000，拒绝第二轮动作并停止。最后一次请求已经消耗的token不会因本地停止而撤销。

## 检查顺序

1. 调查开始前校验阈值，非法配置不读取快照、不配置模型请求。
2. 每次调用前检查累计用量是否已达到阈值或存在未知调用。
3. 模型调用的finally记录成功、失败或取消调用及供应商用量；适配器每次开始清空last_usage。
4. 先处理取消和总时限；模型调用失败时保留model_error及原始固定错误代码。
5. 若调用成功且启用了token预算：有未知total_tokens则usage_unavailable；累计大于或等于阈值则token_budget_exhausted。
6. 只有通过上述检查，才进入动作Schema、证据和候选假设校验并执行动作。

恰好达到阈值也停止，finish和tool采用同一规则。因此可能拒绝一个看似正确的结束动作；预算停止不应包装成诊断成功或合理拒答。预算检查不会把模型错误掩盖成预算错误，失败报告仍附带预算状态。

## 报告字段

所有窗口报告新增可选token_budget，未启用或旧同步入口为null。

| 字段 | 含义 |
| --- | --- |
| mode | 固定observed_usage，表明使用响应后的供应商数据 |
| max_total_tokens | 用户配置的停止阈值 |
| known_total_tokens | 已知total_tokens的小计，包括失败调用 |
| total_tokens | 所有模型调用的完整总量；任何一次未知则null |
| remaining_tokens | 完整用量已知时max(0, limit-total)，否则null |
| exceeded_by | 完整用量已知时max(0, total-limit)，否则null |
| unknown_usage_calls | 没有可信total_tokens的调用次数 |
| status | available / exhausted / unknown |

如果第一轮已知4000，第二轮用量未知，known_total_tokens=4000，但total_tokens和remaining_tokens都必须为null。不能从10000-4000推断还剩6000。

仅返回合法total_tokens也能用于阈值，不要求额外拆分输入输出；只有输入输出而无total_tokens时不自行猜测总量。不接受布尔值、负数或已知字段间的算术矛盾：例如prompt_tokens=100而total_tokens=2，即使completion_tokens缺失，也视为用量无效。

预算状态与termination是两个维度：取消时termination=cancelled，预算可能status=unknown；截断响应的termination=model_error，但其usage仍可能使预算status=exhausted。

## 状态与资源

检查发生在执行当前动作之前，所以超额动作不能下钻、不能创建新假设或替换已有版本。此前完成的证据与假设仍保留。报告装饰在公共异步执行器中完成，包含正常结束、Schema错误、数据源失败、总时限以及携带报告的取消异常。

对窗口策略不注入新提示词、不把预算统计当成故障证据。未启用阈值时，缺失usage仍允许按原规则调查，只报告未知用量。该实现只负责本地停止，不改变供应商计费或服务端计算。

## 运行与阅读

```powershell
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy hypotheses --timeout-seconds 60 --max-total-tokens 20000 --output hypothesis.private.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_token_budget.py -v
```

阅读顺序：budget.py中的ObservedTokenBudget → usage.py的用量校验 → async_investigation.py中finally、token_stop与annotate → window_cli.py参数入口 → test_token_budget.py。

测试覆盖两种策略的低于/等于/超过阈值、多轮累计、未知用量、错误优先级、取消与时间耗尽报告、参数拒绝，以及经真实适配器解码的模拟HTTP响应。没有进行真实供应商花费或节约率实验，不能据此声称降低成本或提高诊断准确率。

2026-10-27：新增14项专项测试通过，全量133项测试通过。未调用真实模型服务或重跑容器故障实验。

后续若需要硬成本控制，需要确定输入计量、输出上限和并发预留/结算规则，不能仅将本字段改名为费用预算。
