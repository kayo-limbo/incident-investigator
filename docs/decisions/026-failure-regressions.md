# 失败回归与引用关联反馈

2026-11-04，基于[新快照纠错对照](../experiments/correction-comparison.md)推进一个小里程碑：固定失败边界，并细化`claim_reference_not_linked`的纠错提示。不修改提示词主体、诊断类别、预算、默认开关或证据校验规则。

## 证据与复现边界

| 失败 | 已有证据 | 本轮回归 | 限制 |
| --- | --- | --- | --- |
| 动作JSON解析失败 | 上轮3次`action_json_invalid`及调用用量 | 两个真实异步适配器通过MockTransport接收三种非法JSON，验证只请求一次、保留用量、不纠错、不下钻 | 原报告没有响应content；截断JSON、代码围栏和尾随文字均为合成边界，不能据此推定原始语法错误 |
| 低负载重复查询 | 上轮3次`repeated_query`终止报告 | 健康合成快照连续请求同一Trace，开关两模式均只下钻一次 | 固定模型动作序列，不证明真实模型为何选择重复查询 |
| 引用未关联 | 上轮失败候选及假设状态可读取 | 未选中的反证、同span不同字段、两次重犯、选择反证假设后通过 | 合成修正由测试提供，只验证协议与历史一致性，不作为模型能力或诊断正确率 |

真实失败例的结构是：候选更新同时把A设为supported、B设为refuted，`hypothesis_ids`只选A，但`finding.references`包含B的反证字段。B出现在`updates`不代表B被选为结论解释。校验先在候选状态上执行；失败后A/B都保持此前open状态。

另外在本地对原始`reports/25.private.json`的两次被拒绝候选和对应快照重放：同样两次`claim_reference_not_linked`、3次脚本模型决策、1次下钻，未提交候选假设。原始文件未改写；结果单独保存为`.snapshots/correction-20261103-r2/regression-20261104.private.json`。这不是重新请求模型，也不是完整原始网络会话重放；测试用量为脚本固定值，不能与真实模型成本比较。

## 最小改动

`corrections.py`按固定原因码补充提示，明确：

- 所有结论引用必须来自`hypothesis_ids`选中假设的`supporting_refs`或`counter_refs`，不能使用未选假设的引用。
- 完整匹配`snapshot_id / trace_id / span_id / field`，同Trace或同span不够。
- 检查本次updates后的候选状态；保留排除其他解释的反证时，选择对应已解决的假设并引用其反证，否则删除与所选解释无关的引用。
- 不得为通过校验而伪造引用或添加无关支持/反证。

反馈是固定本地规则，不包含评测真值、不自动选择假设或修改动作。其他原因仍使用原类别反馈。仅影响纠错开启且遇到该原因时新生成的instruction；关闭模式不变。没有新增状态字段或变更检查点协议，已有检查点中保存的待发送反馈继续按原文本恢复。

## 验证与下一步

新增`tests/test_failure_regressions.py`六项测试，可独立运行，不需要密钥、Docker或私有快照：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_failure_regressions.py
```

专项6项通过；完整276项测试在123.576秒内通过、无跳过，包含真实PostgreSQL检查点与纠错恢复；`pip check`通过。首次全量因测试数据库未启动产生22个连接超时，保留失败日志，启动数据库并验证连接后重跑通过。没有真实模型调用，不宣称新反馈提高成功率，也没有放宽JSON解析、增加重复查询机会或开放多Agent。

下一步可固定这版反馈，在另采样本上验证引用关联失败后的实际修正效果；当前快照继续作为开发回归。解析失败的具体根因仍缺原始响应证据，若继续排查，应先设计受限、可选择启用的诊断采样，而不是根据合成测试猜测供应商行为。
