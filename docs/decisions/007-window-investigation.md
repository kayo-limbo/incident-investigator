# 007：绑定冻结窗口的规则与 Agent 调查

## 要解决的问题

006 已实现窗口采集和快照，但旧执行器仍只接受单 Trace。本轮新增独立窗口入口，保留旧入口作为既有功能；让两种窗口策略在相同证据、权限和工具调用上限下完成调查并留下可核查的成本记录。

## 数据与权限边界

`FrozenWindowSession` 启动时读取并校验一个快照，核对投影版本、查询窗口与订单 SERVER 开始时间，将内容载入调查私有内存。摘要、每条请求的紧凑观测索引和下钻都来自该内存快照，对外返回深拷贝。调查中外部文件变化不会改变已绑定输入；新会话仍校验文件哈希。

两种策略都获得 `summary` 和 `trace_index`，包括成功/失败状态、库存耗时、发送前耗时、错误类型、池上限与池超时。索引用于选择要下钻的请求，不包含场景名称、注入控制参数或规则结论。`required_observations_present` 只检查当前双服务契约所需的 span、父子关系和字段；不代表通用分布式链路完整性。

模型动作只有两种：

- `get_snapshot_trace(trace_id)`：只允许本快照成员，不接收 snapshot_id、URL 或命令参数。
- `finish(finding)`：提交观测分类、摘要、字段引用和限制。

每条引用绑定 snapshot、trace、span、field。下钻 span 附带 `reference_fields`，显式列出可引用路径；Schema 要求标签保留 `tags.` 前缀。只有实际下钻过的请求能被引用；拒绝重复引用、不存在的字段、其他快照或未读请求。确定性结论还要求非空引用、质量为 observed 且所有样本满足所需观测结构。部分数据或缺失观测可完成合理拒答，但不能提交确定性分类。`coverage=unknown` 仍保留，不宣称全量。

这些校验只保证引用和范围有效，不自动验证中文摘要的因果语义或引用是否充分。当前没有历史配置版本、显式假设、自动处置或服务化执行。

## 规则基线与分类

`window_baseline.py` 使用公开阈值：库存 P95 ≥250 ms，发送前 P95 ≥150 ms；这两个阈值同样提供给模型，是首版实验业务判定口径，不是普适 SLO。

| 分类 | 表达的观测 |
| --- | --- |
| no_anomaly_observed | 当前样本没有上述异常、没有失败且订单 P95 <250 ms |
| inventory_latency | 库存服务内耗时升高 |
| pre_send_delay | 发送前耗时升高，不能单独证明纯连接池等待 |
| pool_contention | 出现 PoolTimeout，尚不能判定连接池过小是唯一根因 |
| inventory_latency_with_pool_contention | 库存耗时升高与 PoolTimeout 或发送前等待升高并存 |
| insufficient_evidence | 数据缺失、部分数据，或现有观测不能解释异常 |

规则按索引选择库存耗时最大、发送前耗时最大，以及一个 PoolTimeout 请求，下钻后生成字段引用。最多三次下钻，加一次摘要读取。模型可自主选择本快照内其他样本。小池低负载不因配置数字小被直接判为异常。

## 用量和失败

`usage.py` 为每次工具/模型调用记录类型、名称、状态、耗时与可用 token；报告保存总耗时及聚合值。模型失败包含固定错误码，不保存异常原文。无模型调用的规则策略 token 为 0；存在未报告 token 的模型调用时相应总和为 null，不把已知部分冒充总量。

`ChatCompletionsModel` 每次调用开始清空 `last_usage`，解析响应 usage 后再解析动作，因此截断/拒答仍可保留供应商实际返回的用量。`last_usage` 是串行调用接口，不可将同一适配器实例用于并发调查。本轮统一统计覆盖新窗口规则与 Agent 报告；旧单 Trace 报告格式保持原样。

默认工具上限 6，包含一次摘要读取，最多五次下钻；最后保留一次模型结束机会，因此最多六次模型调用。重复下钻仍计费，不实现去重。采集窗口在调查开始前完成：对照报告单列 collection_usage，不把一次采集重复归入两种策略成本。统计不等于累计 token、费用或严格时间预算，当前也无自动重试。

`termination` 区分 completed、source_error、model_error、invalid_action、invalid_finding、budget_exhausted；insufficient_evidence 配合 completed 才属于正常拒答，执行失败不能算定位成功。Schema 合法但引用校验失败的候选放在 `rejected_finding`，不能当作接受的结论使用。

## 运行

先使用 `window_cli query` 获取快照，再对同一 ID 运行：

```powershell
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy fixed --output fixed.private.json
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy agent --output agent.private.json
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 以下命令独占实验环境；both 会发送受限观测至配置的模型服务
.\.venv\Scripts\python.exe -m evals.run_window_comparison --strategy both
# 使用上述记录中的同一批快照回放；保留原记录，不操作实验容器
.\.venv\Scripts\python.exe -m evals.run_window_comparison --strategy both --replay docs/experiments/window-comparison.private.json --output docs/experiments/window-replay.private.json
```

`evals/run_window_comparison.py` 在改变容器前验证模型配置。四个场景各采集一次，规则和 Agent 共用同一 snapshot_id，并检查初始证据内容一致；标签不符仍继续其余案例，最终恢复默认配置并独立请求业务。完整报告默认忽略于 Git。期望标签与场景控制仅由评测器持有。

评测器核对终止状态、期望类别、引用成员关系和真实字段；这属于功能验收，仍需人工抽查中文结论，不能称为泛化准确率，也不能据四个案例证明 Agent 优于规则。见实验记录 `docs/experiments/window-comparison.md`。

## 来源与下一步

2026-10-19 核对 [DeepSeek Chat Completions 官方文档](https://api-docs.deepseek.com/api/create-chat-completion)：usage.prompt_tokens、completion_tokens、total_tokens。继续使用仓库锁定的 HTTPX，不新增依赖，不复制外部源码。模型别名以实际运行报告为准，不保证别名未来对应同一底层权重。

下一步可在这两个可运行策略之上加入显式假设版本、支持/反证和查询目的，再在同一快照上做消融对照。暂不加入 LangGraph、并发调度或 PostgreSQL，避免混入无法归因的变化。
