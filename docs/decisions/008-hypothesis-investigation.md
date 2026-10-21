# 显式假设与查询目的

本轮在冻结窗口上加入 `hypotheses` 策略，与原 `fixed`、`agent` 共用观测、下钻工具、字段引用校验和 HTTP 适配器。目标是让候选解释、反证和结论变化可审查，暂不引入数据库、LangGraph 或后台 Worker。

## 数据与执行约束

| 对象 | 保存内容 | 程序约束 |
| --- | --- | --- |
| HypothesisRevision | 命题、影响服务、状态、支持字段、反证、待检查项、变更说明、版本、模型轮次、当时证据数量 | 新建版本从 1 开始；expected_version 匹配上一版；同 id 命题和服务不变；最多 6 个假设 |
| QueryTask | 请求 ID、查询目的、要检验的假设及版本、执行状态、证据 ID、调用成本 | 只允许绑定快照中的 trace；查询至少关联一个已有或本动作新建的假设 |
| 最终 Finding | 类别、摘要、字段引用、限制及关联的假设版本 | 关联 supported/refuted 假设，引用其支持或反证字段；异常结论至少有一个 supported 假设；入选假设都要有字段被引用 |

`open`、`supported`、`refuted`、`inconclusive` 是当前证据状态，没有经过校准的置信概率。`supported` 要有支持引用，`refuted` 要有反证；缺失数据不能自动变成反证。允许同时保留多个 supported 假设，也允许后续版本推翻旧状态。

每次更新提交完整的新版假设，旧版本保留。字段引用必须已被前面的查询返回，不能引用本动作尚未执行的下钻结果；同一假设版本不接受重复字段或同时作为支持和反证的同一字段。不同假设可以对同一字段作不同解释。

账本先构造独立候选，整个动作的版本、目标和引用校验通过后才接纳。任何一项错误都保留原账本，不重试。查询执行完成后记录证据 ID；模型失败仍导出已有版本和查询结果。报告带 `validation_error` 固定错误码，Schema 错误只记录字段位置和错误类型，不带供应商原文或输入值；通过 Schema 但未通过状态校验的动作放入 `rejected_action`，不混入历史。

结论可以引用反证解释为什么排除了某个竞争原因，所以 `claim_hypothesis_versions` 既包含保留的解释，也可包含被反驳的解释。它不是“全部成立的根因列表”，需要结合对应版本的 status 阅读。每个 supported 假设至少引用一个支持字段，每个 refuted 假设至少引用一个反证字段，同时可引用其他关联字段交代冲突。

上下文只带当前假设内容与已执行查询，完整版本历史保留在报告。执行器在调用模型前捕获各假设版本，再为模型提议填入 `expected_version`，账本校验并递增版本。模型不接收或生成版本、轮次、证据计数，避免复制只读元数据；查询与结论关联的具体版本也由执行器填写。`change_reason` 和 `purpose` 是简短的可审查决策说明，不要求模型输出内部思维过程。

## 调用与对照

```powershell
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy hypotheses --output hypothesis.private.json
.\.venv\Scripts\python.exe -m evals.run_hypothesis_comparison --replay docs/experiments/window-comparison-final.private.json --output docs/experiments/hypothesis-comparison-r1.private.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

回放读取已有四场景的快照 ID，标签只留在评测进程，不传给模型。不启动或修改容器。输出必须是新文件；每个策略结束保存一次，保留失败结果，不自动覆盖或重试。

三策略默认都最多 6 次工具调用（摘要占 1 次）。两种模型策略默认最多 6 次模型调用；更新附在查询或 finish 动作里，不增加专用更新轮次；同样使用模型配置中的单次输出上限、网络超时与上下文大小限制。它们的提示、Schema 和状态长度不同，实际 token 与耗时需单列，不能声称相同调用上限意味着相同费用。

评测分开检查结论类别、执行终止状态、已下钻字段引用，以及假设历史的版本顺序、证据可见时序、查询与结果对应关系。原 ReAct 不加假设状态，作为基线保留。数据和完整报告默认属于本地产物，公开结果仅保留技术摘要。

## 实现位置与边界

- `hypotheses.py`：领域结构、版本账本、引用关联。
- `hypothesis_agent.py`：动作协议、同步执行、报告导出。
- `hypothesis_model.py`：复用现有模型适配器，替换领域提示和 Schema。
- `run_hypothesis_comparison.py`：同快照三策略回放与历史审计。

这是同步内存执行：expected_version 不是数据库锁，也不能防止多 Worker 竞争；进程退出后不能从报告继续执行。QueryTask 尚无依赖调度、租约或取消。重复查询、累计 token/时间预算、上下文压缩和持久化仍待实现。

程序检查字段真实存在及关联一致性，不能证明中文命题受到这些字段的语义支持。正常结论也可能过度依赖个别成功样本；联合结论不自动证明唯一根因。必须结合失败请求、独立类别评测和人工抽查。当前阈值与窗口完整性限制沿用 [窗口调查协议](007-window-investigation.md)。

本轮未新增外部依赖或复制开源源码；账本和查询关联由本项目实现，模型连接复用既有适配器。
