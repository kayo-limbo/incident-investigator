# 小批开发查询策略对照协议

## 2026-11-15 mixed-localization开发套件

本套件已完成一次明确授权的真实模型小批：实际13/15次请求、19,386 token；fixed、active、active_no_scoring通过，react因下钻缺失window被拒绝，未重试。四份报告离线审计通过，完整结果见[混合场景对照](../experiments/active-query-comparison.md)。此前offline-only批次未改配置，新模型批次独立冻结。本轮授权已用完，不延伸到后续实验。

采集输入计划声明`localization=mixed_slowest`时，正式入口冻结`mixed-localization`套件：只选择downstream的repeat=0完整视图，不替换未通过样本，不加入来源消融。默认原套件仍为原8个视图。新采集计划限制为development/downstream；原计划缺省`localization=none`。采集与audit均重算mixed窗口和最慢采样请求准入条件。

freeze从原始Trace独立计算`expected_localization`（请求ID、阶段、范围），与快照一同冻结为评测数据。score先逐步重建报告，再要求局部状态observed且匹配这些预期，输出`localization_accepted`；类别正确但局部验收失败不算通过。该预期不进入Agent上下文。采集器和执行器仍复用底层原始Trace解析，因此这不是独立遥测或通用语义验证。

沿用原入口：`python -m evals.collect_paired_windows collect .snapshots/NEW_MIXED --plan evals/plans/paired-mixed-development-v1.json`，然后`python -m evals.run_active_comparison freeze .snapshots/NEW_RUN --source .snapshots/NEW_MIXED`及`fixed`/`audit`。models仍需单独明确授权。小套件每变体1次，三种模型变体最多15次请求，其余单次预算不变；不能套用原24次调查的授权。

2026-11-12：新增`evals/run_active_comparison.py`。这是开发集协议验收与查询选择对照，不是模型独立根因诊断或泛化评测。诊断和停止继续使用035共同规则。

## 预定批次

从2026-11-09开发采集预先选择repeat=0的stable、downstream、capacity、arrival、restart、compound六份完整快照，再加同一compound的trace_only/runtime_only，共8个视图。不是8份独立故障样本；repeat=1未作为本次选择候选，不按结果择优。选中样本缺失或不准入时拒绝冻结，不自动换样本。

固定基线8次；react、active、active_no_scoring各8次，共24次模型调查，每个视图每个变体一轮。变体顺序固定；本批不估计模型随机方差，也不宣称顺序效应已消除。active_no_scoring只移除分数展示和排名，不是完全去规划消融。

每次含告警最多6次工具、最多5次模型请求、120秒、40000累计token响应后停止阈值。模型配置冻结为本地ModelConfig的base_url/model/timeout_seconds/max_output_tokens，不保存密钥。本次实际为DeepSeek API、deepseek-flash、网络30秒、单次输出2048。API模型别名可能由供应商更新，本地配置冻结不是供应商权重版本锁定。

整个模型批次最多120次请求；不自动重试失败调查。40000不是每次预留token或费用硬上限，最后响应可能超额。未知token用量使当前调查停止，不能计为零。

## 冻结与重放

`freeze`先重建选中样本原始观测并核对内容哈希，派生来源缺失视图也按投影重算，再复制到独占运行目录；只接受development来源。存储输入manifest/plan摘要、规范化实验计划、计划摘要、源码哈希及源码ZIP，涵盖调查协议/提示、采集重建代码和依赖声明。执行和audit检查源码、参数、模型配置和快照；不支持悄悄修改提示后续跑同一批。

摘要是本地意外修改检测，不是数字签名或安全隔离。原采集代码历史指纹不同，本轮按当前重建函数检查选中原始观测，不冒充再次通过旧版采集audit。输入真值标签只用于选样和评分，不进入调查上下文。

每次调查在外发前独占写started标记，报告另用独占文件保存。续跑跳过已有报告，包括失败报告；有started但没有完整报告时视为费用未知，禁止自动重发。部分写入或基础设施异常同样需要人工核对，不能删除标记后假装没调用过。取消保存部分报告并中止批次。

```powershell
python -m evals.run_active_comparison freeze .snapshots/NEW_RUN --source .snapshots/paired-20261109-development-v1
python -m evals.run_active_comparison fixed .snapshots/NEW_RUN
python -m evals.run_active_comparison audit .snapshots/NEW_RUN
```

仅在获得本批明确授权后，才执行`models`模式并加`--authorized-model-calls`。这个开关只是执行边界，不替代用户授权。freeze读取配置但不发起API请求；audit不需密钥。

## 评分和解释边界

逐步重放已接纳Plan，核对历史、证据、状态、固定查询顺序、调用数量与token账本一致性。执行completed、领域已停止且code匹配预定标签才通过类别/协议验收。错误原因、实际工具/模型数、已知token、未知用量、耗时、重复查询拒绝和非法动作分开记录；未开始/中断费用未知单列，不删失败分母。

用量检查只能发现内部不一致，不能代替供应商账单。事件引用可追溯不等于语义或因果正确。不要把无效动作率等同于所有无效查询率，也不要把更早失败带来的少调用解释为效率提升。成功调查成本与全部尝试成本应分别查看；无模型自主主张，暂不报告“无支持主张比例”。

## 执行前准备记录

已冻结`.snapshots/active-comparison-20261112`，8份固定基线通过、24次模型调查尚未开始。6项新增专项覆盖冻结/重放、保留集拒绝、计划/源码/模型配置变化阻断、未知中断不重发、报告证据/用量篡改、失败不重试与标签不外发。无真实模型调用，保留集未访问。

359项全量测试通过（203.769秒，含PostgreSQL），6项专项通过；冻结批次离线audit再次确认固定8/8、模型24次未开始、未知中断0。本轮暂存差异检查通过。

2026-11-12核对[DeepSeek官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash高峰未缓存输入2元/百万token、输出8元/百万token，空闲半价。假设本批输入30万—100万、输出3万—10万token，高峰估算约0.84—2.8元；建议预留5元，非费用硬上限。此处是用量假设，不是已测成本，实际以用量和账单为准。

## 授权后的实际执行

2026-11-12完成全部24次模型调查，未重试4次协议失败，未改变冻结代码、提示、预算或输入。固定8/8、react 7/8、active 6/8、active_no_scoring 7/8通过类别/协议验收。实际71次请求、89,957 token，未知用量和未知中断均为0。32份报告全部通过独立逐步重建，审计前后报告和started标记哈希不变。结果与限制见 [实测记录](../experiments/active-query-comparison.md)。此前359项测试是执行准备验证；此次仅运行冻结实验与离线审计，未新增代码或重复全量测试。
# 结构化主张离线对照补充（2026-11-17）

正式入口增加`freeze-claims`、`claims`、`audit-claims`，不读取模型配置、不发起模型请求。与查询策略批次分目录冻结，沿用计划哈希、内容寻址快照、源码归档、逐条结果与只读重放核对；结果独占写入，不覆盖既有结果。源码或计划变化后不能续跑原批次。

固定选择原六类开发采集的repeat=1完整视图。每份快照对订单、库存、发送前耗时构造三种主张：正确样本P95、数值加1ms的错误P95、将样本统计推广到全部流量；共6次采集54条主张。所有主张均标为constructed，不是自然模型输出，也不是历史错误发生率。repeat=1此前参与过路径审计，因此不能称作独立保留集。

冻结阶段先从源窗口重建配对快照并核对内容，再在评测代码中独立排序各指标非空值、按ceil(0.95*n)取值形成标签；不调用生产statistics或验证器生成标签。该标签独立于验证器，但仍依赖共同的观测投影，不代表对原始遥测全链路的独立认证。运行时只向验证器传入主张和已获取证据，不传标签。

两种方案共享每份快照的告警与事件摘要（每份2次工具，共12次）：reference_only检查已获取的同窗口来源和指标存在；statistical额外检查统计值与样本范围。前者是明确受限的引用基线，不代表最强语义规则，也不是重跑旧自然语言报告验收。每条保存预期标签、接受结果及统计验证原因；分开报告矛盾主张放行、证据不足主张放行、正确主张误拒、正确主张保留、全部接受数，均为计数/分母。两种方案额外模型/工具调用均0，不由此声称CPU开销为0；本轮不作延迟比较或修正实验。

```powershell
.\.venv\Scripts\python.exe -m evals.run_active_comparison freeze-claims .snapshots/NEW_CLAIMS --source .snapshots/paired-20261109-development-v1
.\.venv\Scripts\python.exe -m evals.run_active_comparison claims .snapshots/NEW_CLAIMS
.\.venv\Scripts\python.exe -m evals.run_active_comparison audit-claims .snapshots/NEW_CLAIMS
```

以下为历史查询策略协议，不覆盖本节主张对照的范围。
