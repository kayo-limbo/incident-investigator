# Incident Investigator

面向微服务的故障调查 Agent 项目，重点研究预算约束下的主动调查、结论证据验证与竞争假设的多 Agent 协作。订单系统作为第一套实验环境；现有任务持久化、恢复和 API 为调查提供运行支撑。

## 当前推进：结论级证据验证

2026-11-18 双分支准入审计：在 3 次联合故障采集的 13 次普通 ReAct 调查中，12 份有效正文均写出慢库存与 PoolTimeout，另 1 次引用失败。初始摘要已提供两项证据，本批未复现证据齐全后的因素漏报，暂缓双分支实现。见[按采集分组的审计结果](docs/experiments/active-query-comparison.md#2026-11-18-双分支准入单-agent-是否漏报并存因素)。

2026-11-18 收尾验收：另取 4 次采集的三轮历史窗口报告，从原始 span 重算 24 条自然 P95 陈述，均与原文显示精度相符。此批没有可用于正文拦错对照的数值错误；联合故障的共现、因果判断仍超出 P95 核验范围。本轮以已验证的结构化核验能力收尾，未启动亮点三。见[材料范围和逐组核对](docs/experiments/bundle-comparison.md#2026-11-18-正文验收材料筛查)。

2026-11-17原文绑定：新增报告哈希/字符位置与受控独立句字段核对，防止结构化主张改动原文指标、窗口、数值、来源及近似精度，36项主线回归通过。旧13条自由文本片段绑定0/13，均保留为未核验；此接口约束新报告表达，不能宣称已实现旧报告自动拦错。见[接口边界](docs/decisions/035-query-policy-adapters.md#2026-11-17-主张原文绑定v1受限独立句)。

2026-11-17修复回放：统计验证v2支持原文近似小数精度与告警记录归属，区分客户端/服务端指标。相同人工提取开发主张中核对12条，正确保留11/11、误拒0/11，错误库存P95仍拒绝1/1；另1条不等式未支持。35项主线回归通过，无模型调用。这是已知问题回放，不是自动报告或独立泛化成绩。见[v2接口](docs/decisions/035-query-policy-adapters.md#2026-11-17-统计主张v2近似精度与告警归属)与[结果](docs/experiments/bundle-comparison.md#v2修复回放近似表达和告警归属)。

2026-11-17自然报告审计：冻结12份历史ReAct报告，核对13条定量P95主张，12条支持、1条矛盾；尚无旧报告原生验证适配。7条人工转换诊断中拦住1条真实错误，同时误拒1条合理舍入。已记录告警来源及不等式覆盖缺口，不能将构造对照成绩直接当真实报告收益。见[自然报告审计](docs/experiments/bundle-comparison.md#后续12份自然报告的定量p95审计)。

2026-11-17冻结对照：正式入口在6次开发采集上构造54条结构化主张，统计验证拦截18/18条数值错误与18/18条全量外推，正确主张误拒0/18；只读重放一致，33项主线回归通过，无模型调用。这是构造变体的机制验证，不是自然模型报告改善或独立保留集成绩。见[结果与限制](docs/experiments/active-query-comparison.md#2026-11-17-结构化统计主张首轮冻结对照)。

2026-11-17最小实现：active调查新增只读结构化统计主张验证接口，区分支持、矛盾、证据不足，检查P95/单请求值、指标、窗口和已查询引用；错误统计主张及正确对照的机制测试通过。尚未自动提取自然语言主张或执行独立效果对照，不宣称诊断质量提升。接口与边界见[统计主张验证](docs/decisions/035-query-policy-adapters.md#2026-11-17-结构化统计主张验证独立只读接口)。

2026-11-17：主线改为“先复现引用合法但解释错误的真实实例，再做最小确定性验证和冻结对照”。已从原始span复现历史报告的P95口径错误：把最慢订单对应的库存耗时352.213ms当成库存P95，独立排序为352.727ms；原类别/引用验收仍通过。本次只完成单例审计及正确对照，未实现验证器、未证明质量改善。见[复现与限制](docs/experiments/bundle-comparison.md#2026-11-17-结论验证准入审计)。

原文绑定边界已明确，暂不继续扩展历史句式正则。若接报告流程，先以结构化主张和可信渲染定义新输出契约，再独立验收候选主张；不把模板改写当作自然报告改善。双分支协作仅保留有界准入审计，第二拓扑后置。验收阶段、开源参考及止损条件以[当前路线](docs/decisions/019-agent-roadmap.md#2026-11-17-当前执行计划)为准。

## 历史路线：调查机制与迁移验证

2026-11-05 历史计划：以下不覆盖上方2026-11-17的当前优先级，所列计划能力不代表已经实现或验证。

1. **增强任务与评测基础**：先完成“订单变慢、不同解释”的任务组，覆盖下游慢、调用方等待与关键证据缺失；增加一种必要证据来源，与 Trace 一起冻结。规则、普通 ReAct 和现有假设策略获得相同证据权限。
2. **主动调查**：将查询目的深化为竞争假设、预期判别结果和后续更新；比较相同预算下的诊断质量、查询数与无效查询比例。
3. **结论证据验证**：区分观测事实与因果主张，检查支持、反证与证据缺口，验证失败后补查或拒答；衡量错误拦截、正确结论误拒和额外成本。
4. **有限多 Agent 实验**：在有可分解任务与单 Agent 对照后引入最多两个调查分支，验证共享证据、去重、全局预算与冲突补查；比较单 Agent、顺序委派、并行委派。
5. **第二套拓扑迁移**：三个机制分别完成首轮对照、协议稳定后冻结策略，接入结构不同的小型环境。分别记录无策略调优迁移和有限适配结果；换拓扑不等于验证未知故障泛化。

第 1 项已有首轮采集与三策略回放，失败诊断补充见下文；双窗口采集、冻结查询与离线证据验收已实现，任务可判别性仍需按开发结果逐项确认。实现与采集记录见 [双窗口开发场景](docs/experiments/paired-windows.md)，设计见 [前后窗口的竞争解释](docs/decisions/030-cross-source-investigation-tasks.md)，整体迁移边界见 [增强订单任务与迁移验证计划](docs/decisions/027-investigation-and-transfer-plan.md)。效果数字由独立实验给出，不预设复杂策略优于规则。

## 当前状态

2026-11-15收益空间审计：穷举全部38个已有开发视图（14次采集及来源消融）的合法查询路径，固定顺序、简单条件路由与最短成功路径合计均为132次调用；2,042个可继续状态中的候选分数全部为1。当前任务与停止协议下无查询数优化空间，暂停亮点一的增益实验，不再追加模型调用；保留工程成果，下一优先方向为结论证据验证。开源模块阅读、复现方法与边界见[审计记录](docs/experiments/active-query-comparison.md)。

2026-11-15混合场景真实模型对照：active与去评分版本均完成目标请求下钻并通过独立局部验收，查询路径及6次工具调用与fixed相同；react因下钻遗漏window被拒绝。共13次请求、19,386 token，四份报告离线审计通过，失败未重试。已验证真实模型下钻闭环，仍无查询效率增益或评分有效性证据。见[实测结果与限制](docs/experiments/active-query-comparison.md)。

2026-11-15真实混合场景：两次有界开发采集均触发mixed窗口及最慢请求混合延迟，准入、原始Trace重建、局部定位和业务恢复通过；固定策略均用6次工具完成。正式入口新增从原始Trace计算的独立局部预期评分，67项主线/paired测试通过。未调用真实模型，仍无主动策略收益证据；见[实验结果](docs/experiments/active-query-comparison.md)及[冻结套件](docs/decisions/036-frozen-query-comparison.md)。

2026-11-14局部下钻：v3对mixed事件摘要补查最慢采样请求，单列请求阶段与引用，避免将不同请求的P95视为同一请求的联合现象。固定与模型策略共享该充分性要求，预算不足保留缺口；29项主线测试及24次既有开发视图离线回放通过。真实开发视图尚未触发此分支，当前只有合成机制验收，不能宣称真实质量或效率增益。见[当前协议](docs/decisions/035-query-policy-adapters.md)。

2026-11-13分类充分性：主动调查v2按假设更新证据缺口，支持在已确认至少两种变化时提前完成复合分类，并显式保留未核查因素；单一变化仍检查并存因素。合成快照证明查询路径可影响调用数，但原8个开发视图的192次离线路径回放仍无优于固定顺序的查询数，未调用真实模型。实现边界与验证见[查询协议](docs/decisions/035-query-policy-adapters.md)及[实验记录](docs/experiments/active-query-comparison.md)。

2026-11-12 开发查询对照完成：8个开发视图中，固定基线8/8、react 7/8、active 6/8、active_no_scoring 7/8通过类别/协议验收。24次真实调查共71次模型请求、89,957 token，4次协议失败原样保留，32份报告离线重建通过。成功模型调查的工具数均与同样本固定基线相同，尚无主动查询增益；诊断仍由共同规则生成，不是模型自主诊断准确率。结果、失败与后续范围见 [查询对照实测](docs/experiments/active-query-comparison.md)，冻结约束见 [协议](docs/decisions/036-frozen-query-comparison.md)。

2026-11-11 查询策略接入：固定流程、普通查询模型、显式Plan模型已共享四类工具和自动停止协议，新增异步模型适配、调用/token/时间限制、取消报告及默认离线CLI。36份开发视图的三策略脚本回放状态/证据一致；尚无真实模型效果结果。本轮只比较查询选择，诊断由共同规则生成。协议与限制见 [查询策略适配](docs/decisions/035-query-policy-adapters.md)。

2026-11-10 主动调查参考闭环：新增摘要、单请求下钻、配置比较、入口比较四类受限工具，枚举查询预期、证据缺口、程序维护的假设状态和可检查停止原因。当前为确定性参考执行器，未接模型策略；12份完整开发样本类别符合预期，来源缺失条件保守拒答，不能据此宣称主动规划增益。接口、开发回放及与旧规则的拒答差异见 [参考闭环](docs/decisions/034-active-reference-loop.md)。下一步统一三策略工具/停止协议并接入模型规划适配器。

2026-11-09 实验基础：显式采集计划支持开发/保留集参数、复合容量与入口变化，采集和离线audit绑定同一份计划；开发回放拒绝保留集标签。保留集仅固定参数，尚未采集或评分。下一条开发主线为细粒度工具和“竞争假设—证据缺口—判别查询—状态更新—停止”闭环，随后再作三策略对照。实现、实验纪律及限制见 [主动调查实验基础](docs/decisions/033-active-investigation-foundation.md)。

本轮开发集六类各两次：12/12采集有效且准入通过，离线重建和默认业务恢复通过；规则完整证据12份类别符合预期，24份来源消融符合预设类别/拒答条件。337项全量测试通过，无模型调用；这些是开发集验收，不是语义准确率或泛化成绩。

2026-11-07 三策略接入：双窗口v2已有固定规则、普通ReAct与显式假设的统一CLI和窗口引用校验，报告分离阶段定位与变化解释。既有容量/入口4份开发快照规则输出对应解释；8份来源消融均保守拒答，Trace单源仍保留阶段定位。两种Agent仅脚本化验证，无真实模型调用；不是泛化或语义准确率结果。执行方式、限制与下一批独立参数见 [双窗口三策略](docs/decisions/032-paired-strategies.md)。下一步实现显式参数采集计划并冻结独立样本，再开展策略对照。

2026-11-07 截止读数修复：新增schema 4 / paired-bundle-v2，将发送结束send_end与排空结束after分开，保留旧协议速率含义。相同参数下容量/入口各两次复验4/4有效且准入通过；容量速率比0.9998/1.0008、入口约1.65，原阈值未放宽。离线audit及业务恢复通过，324项全量、24项专项通过，无模型调用。见 [入口速率契约与结果](docs/decisions/031-arrival-cutoff.md)。下一步将v2接入三策略同权限执行入口，建立强规则基线与独立测试参数；这不是Agent准确率成绩。

2026-11-07 后续：已实现双窗口证据协议、窗口级受限查询、有界开环采集与离线重建。五类场景第二轮10次尝试中9次采集有效、7次任务准入通过；两次容量场景受排空区间速率口径影响，仍未准入。计时与Windows文件占用修复后的最终重启复验2/2通过，离线重算及默认配置/业务恢复通过。全量318项回归通过，最后统一服务端时间区间后19项专项通过；未调用模型。详情和保留的失败见 [双窗口开发场景](docs/experiments/paired-windows.md)。下一步分离发送结束与排空结束读数，修正入口速率证据口径，再冻结策略对照。

2026-11-07：完成下一批任务设计，将问题推进到“相对参考窗口，为什么开始变慢”，规划稳定、下游变化、容量变化、入口变化和重启对照五类任务。保留Trace已有字段，通过实测入口计数与采样Trace的来源消融检查互补性；区分阶段定位、观测变化与因果解释。新双窗口协议和开环采集尚未实现，本次没有新模型调用或实验成绩。见 [设计与验收门槛](docs/decisions/030-cross-source-investigation-tasks.md)。

2026-11-05 后续：bundle报告新增有界失败候选、受控Schema路径和失败分类，保留已接纳状态与被拒绝动作的区别；23项bundle专项、299项全量测试通过无跳过，上轮30份历史报告只读评分/用量核对一致。未改变提示、校验或预算，未调用真实模型，不宣称诊断质量改善。设计、测试与限制见 [Bundle失败诊断](docs/decisions/029-bundle-failure-diagnostics.md)。

2026-11-05：已实现独立的多来源冻结证据实验入口：订单客户端运行观测、症状告警、Trace/运行观测两种受限查询、来源字段引用，以及固定规则/普通 ReAct/假设策略回放。四组真实环境采集及业务恢复通过，另有两份缺失 Trace 派生条件；两轮24次真实模型调查的类别/协议验收为规则6/6、普通11/12、假设2/12，全部离线重算一致。人工复核发现类别正确但P95解释错误的报告，不能将通过数视为完整语义准确率。新协议未接入 Worker 恢复，不代表主动调查或多 Agent 机制已完成。见 [实现与命令](docs/decisions/028-frozen-evidence-bundles.md)、[验收记录](docs/experiments/bundle-comparison.md)。

已实现两个最小 FastAPI 服务：订单预览通过 HTTP 查询固定库存，并提供 Docker Compose 配置、可控延迟、请求关联与耗时日志。已有 OpenTelemetry 显式埋点、Jaeger、固定调查流程和受限 Agent 执行器。DeepSeek 适配器与命令行入口已完成真实五场景验收，并确认业务恢复。调查任务已接入 PostgreSQL 持久化；故障实验服务本身没有数据库，尚未实现自建 Web 平台。以下计划能力不代表已完成。

进度按验收划分：

- [x] 双服务健康请求与 Docker Compose 可重置环境。
- [x] 延迟故障、结构化日志和跨服务调用链，已保存独立验收结果。
- [x] 第二类故障：订单的库存地址端口错误；库存自身保持可用，已验证配置修复后的恢复。
- [x] 首版固定排查流程与四个只读证据工具；已有结构化证据和接口边界，尚非进程级隔离。
- [x] 最小调查执行器：结构化动作、工具白名单、调用次数预算与证据引用校验。
- [x] DeepSeek V4.1 Flash 适配器与 CLI：已完成模拟 HTTP 测试和一次真实五场景验收，库存停止时合理拒答。
- [x] 真实 HTTP 连接池竞争实验：七组并发/低负载/慢下游对照，已验证发送前延迟、PoolTimeout 和恢复。
- [x] 冻结窗口接入规则与 Agent：绑定输入、字段引用校验、调用/token 用量及四场景对照入口。
- [x] 显式假设策略：版本历史、支持/反证字段、查询目的与结论关联；提供同快照三策略回放。
- [x] 普通与假设窗口 CLI 的异步调用、总时间预算与取消报告。
- [x] 两种异步窗口策略的可选累计 token 停止阈值与未知用量处理。
- [x] 冻结快照重复查询停止规则及允许重复的对照选项。
- [x] PostgreSQL 持久化任务、幂等提交、互斥领取与一次性 Worker，真实数据库集成验收通过。
- [x] 任务 HTTP API：幂等提交、状态/结果查询、排队取消，复用独立租约 Worker。
- [x] 常驻 Worker：连续消费、基础设施失败有限退避、停止信号排空退出。
- [x] 持久化任务状态事件与SSE断线续传：同事务写入、任务内编号、游标补读。
- [ ] 服务化工作台、审批处置、批量评测。

目前已完成最小故障实验环境、规则基线和最小模型调查闭环；五场景验收不代表完整评测体系或泛化能力。

## 可选的一次协议纠错

2026-11-04：新增解析失败、低负载重复查询及引用关联失败的六项离线回归；针对`claim_reference_not_linked`细化固定反馈，明确所选假设、支持/反证与完整字段引用的对应关系。原始失败候选离线重放仍被正确拒绝，不提交状态。未调用真实模型，效果尚待新样本验证，默认纠错仍关闭；见 [失败回归与引用反馈](docs/decisions/026-failure-regressions.md)。

2026-11-03已完成新参数五快照、两轮40次正式异步图对照：普通agent纠错关/开均8/10，假设策略关5/10、开9/10；规则5/5。实际只触发2次纠错，1次成功、1次仍失败，不能把组间差异全部归因于纠错，默认保持关闭。共126次模型调用、677,723 token，全部失败保留并离线重算；参数、成本、失败分类与限制见 [纠错开关对照](docs/experiments/correction-comparison.md)。下一步以解析失败、低负载重复查询及引用关联失败建立回归，再选一个小改动。

2026-11-03：两种异步窗口策略支持 `--max-corrections 1`（默认0关闭），仅对动作Schema、证据引用和假设关联校验失败提供一次修正机会。纠错占原模型调用、token及时间预算，失败动作不执行、不接纳；反馈、已用机会及失败历史进入检查点，重启不刷新额度。CLI、任务CLI及HTTP API均支持；开启使用独立恢复协议，关闭保留旧v1与幂等摘要。全量264项测试通过无跳过，包含真实PostgreSQL恢复及旧检查点升级烟测。未调用真实模型，效果仍需新样本对照。运行方式和恢复限制见 [一次协议纠错](docs/decisions/025-bounded-correction.md)。

## 调查失败诊断

2026-11-02：窗口 `agent` 与 `hypotheses` 报告新增结构化 `diagnostic`，记录失败阶段、分类、固定原因码、模型轮次及Schema错误类型；响应解析进一步区分响应JSON/结构与动作JSON等六类原因，用量仍保留在原调用账本。现有停止语义不变，未加入自动纠错；诊断误判仍由独立评测判断。全量246项测试通过、无跳过，含真实PostgreSQL失败报告恢复；未调用真实模型。字段、兼容与限制见 [失败诊断设计](docs/decisions/024-failure-diagnostics.md)。

## 持久化调查任务

2026-10-31：普通窗口 `agent` 的正式异步主循环已迁移到 LangGraph，覆盖模型决策、动作校验、工具执行与结束判断。CLI 和 Worker 共用图流程，CLI 不持久化，Worker 使用步骤检查点、租约隔离、跨重启预算和未知调用停止；报告标识为 `window_react_langgraph_v1`。旧异步循环仅保留为迁移测试对照，同步兼容入口保持现状。全量238项测试通过，无跳过，包含真实PostgreSQL与子进程恢复；未调用真实模型，不代表诊断质量提升。实现、升级边界与验收见 [普通 agent 串行图与恢复](docs/decisions/023-langgraph-agent.md)。下一步回到调查质量与独立样本，多 Agent 分支与协作仍后置。

2026-10-30：异步 `hypotheses` 已迁移到LangGraph的决策、校验、状态接纳、工具执行四个节点，CLI和Worker继续使用原策略名。先安装 `requirements-graph.txt`；报告标识为 `window_hypotheses_langgraph_v1`。共享原有领域校验和预算，旧循环冻结为回归参考。设计、安装及验证边界见 [LangGraph假设调查](docs/decisions/020-langgraph-hypotheses.md)。

Worker 的 `hypotheses` 和 `agent` 现已接入 PostgreSQL 步骤检查点：租约接管后恢复已提交状态，累计调用与 token 用量，停机时间计入调查时限；模型请求结果未可靠落库时停止自动重发并报告用量未知。升级前停止 API 和全部 Worker，执行 `python -m backend.tasks init` 迁移至 schema 4。独立调查 CLI 不持久化，`fixed` 策略仍从头执行；尚无多 Agent、人工审批或有副作用的处置。适配器仅支持当前串行图，详见 [调查恢复与租约一致性](docs/decisions/021-graph-recovery.md)。

新增 PostgreSQL 任务表与 CLI：幂等提交、短事务领取、一次性 Worker、结果查询和队列取消；复用 fixed、agent、hypotheses 三种策略。实现与运行命令见 [持久化任务设计](docs/decisions/014-persistent-tasks.md)。新增30秒默认租约与心跳，后续Worker可回收过期任务，最多三次尝试；旧令牌不能覆盖新结果。升级前停旧Worker并执行init，详见 [租约与恢复](docs/decisions/015-task-leases.md)。已有本地任务HTTP API和常驻消费者；两种异步模型策略现按检查点恢复，历史无检查点任务仍需从头开始。

2026-10-28 已在 PostgreSQL 17.11 上通过 11 项数据库集成测试及 6 项任务单元测试，覆盖并发提交、互斥领取、锁跳过、事务释放、结果落库及 CLI 流程。此前因 Docker 启动失败留下的验证缺口已补齐；后续租约阶段补充了进程退出与接管验证，范围见上述租约文档，不替代真实模型诊断评测。

HTTP入口使用 `python -m uvicorn backend.tasks.api:create_app --factory --host 127.0.0.1 --port 8010`，启动前设置 `TASK_DATABASE_URL`，API与Worker共享快照目录。提供 `POST /tasks`（必填 `Idempotency-Key`）、`GET /tasks/{id}` 和 `POST /tasks/{id}/cancel`。HTTP只提交持久化任务，仍由独立Worker执行；同键同参数重放返回同一任务，不重复调查。当前仅适用于本地开发，尚无认证或租户隔离。完整命令、错误码与并发语义见 [任务HTTP接口](docs/decisions/016-task-http-api.md)。

2026-10-29：API专项11项通过（含3项真实PostgreSQL测试），全量181项通过、无跳过；独立Uvicorn进程的真实HTTP OpenAPI与数据库查询烟测通过。首次回归暴露已有假设超时测试依赖50毫秒执行速度，改为第二轮进入后触发真实超时，再完成回归；未调用真实模型，未做性能基准。

常驻消费入口：`python -m backend.tasks --snapshots .snapshots work --worker-id worker-1`。每个进程串行消费，空队列等待；数据库连接异常与租约冲突有限抖动退避，连续失败默认5次退出。Ctrl+C或SIGTERM请求停止新轮次，等待当前轮次完成；调查失败不阻止后续任务。启动前同样设置数据库地址并完成init。设计、运行及退出限制见 [常驻Worker](docs/decisions/017-continuous-worker.md)。2026-10-29新增13项测试（含2项真实PostgreSQL），全量194项通过、无跳过；未验证Linux容器停止或做性能压测。

新增 `GET /tasks/{id}/events` SSE接口，支持 `Last-Event-ID` 和 `after` 游标补读。任务状态与事件通过数据库触发器同事务提交，重复提交和心跳不新增事件；终态补读完后重连返回204。升级前停止API/Worker并运行 `backend.tasks init`，现存任务只回填当前状态snapshot。2026-10-29专项12项、全量206项通过无跳过，包含真实HTTP断开后补读验收。仅记录生命周期事件，不包含逐步模型/工具执行或checkpoint，详见 [持久化事件与SSE](docs/decisions/018-task-events.md)。

## 窗口证据与冻结快照

窗口 `agent` 和 `hypotheses` CLI 均使用异步模型调用，总时间预算默认 120 秒，可通过 `window_cli investigate <snapshot_id> --strategy hypotheses --timeout-seconds 60` 调整。预算从读取快照前开始，多轮共享一个截止时间；到期取消本地网络等待，报告为 `deadline_exceeded`。调查期间第一次 Ctrl+C 会保留证据和用量，通过 `--output` 保存 `cancelled` 报告并返回退出码 130。假设策略还保留已提交的假设版本与查询记录，不接纳迟到更新。未取得的供应商用量记为未知。同步文件读取、解析和清理仍可能使实际返回晚于截止时间，本地取消不证明供应商停止生成或计费。

既有同步 Python 接口和评测入口继续保留：普通窗口同步接口仅在操作边界检查时间，假设同步接口没有总时间预算；固定规则与旧单 Trace 入口未接入异步取消。实现与测试见 [异步调查与取消](docs/decisions/010-async-investigation.md) 和 [假设策略的异步状态边界](docs/decisions/011-async-hypotheses.md)。

两种异步窗口策略可使用 `--max-total-tokens 20000` 设置累计停止阈值，默认不启用。统计供应商返回的各轮输入与输出总量；达到阈值后拒绝当前动作并停止，缺少完整总量时返回 `usage_unavailable`。报告 `token_budget` 区分已知小计、完整总量、剩余额度与超额数。该阈值在响应后检查，最后一次请求可能超额，不是费用硬上限；详见 [累计token预算](docs/decisions/012-observed-token-budget.md)。

两种异步策略默认 `--repeat-query-policy stop`：重复下钻本会话已经成功读取的冻结Trace时，返回 `repeated_query`，记录原证据ID并停止，不新增工具执行或提交该动作附带的假设更新。已发生的模型调用和token仍计入。可显式使用 `--repeat-query-policy allow` 对照原行为；同步入口默认allow。该规则可能降低完成率，尚无真实模型效果改善结论；范围与取舍见 [冻结查询重复控制](docs/decisions/013-repeated-snapshot-query.md)。

2026-10-26：新增时间预算测试，使用可控时钟验证快照读取计时、迟到动作拒绝、已完成证据保留及网络超时区分；未调用真实模型或重跑容器实验。

新增 `WindowEvidenceTools`：订单窗口查询、成功/失败样本摘要、连接池观测投影、本地内容寻址快照和离线下钻。窗口最长五分钟、最多 200 条，区分已观测、空结果、部分数据和不可用；任何查询都不保证全量。设计与运行方法见 [窗口证据与快照](docs/decisions/006-window-evidence.md)。

2026-10-18 新增 10 项测试，全部 55 项本地测试通过；真实 Jaeger 三组共 75 个窗口请求（含预热）验收通过，业务恢复已确认。验收入口为 `python -m evals.run_window_evidence`，独立检查正常、小池、慢下游叠加池超时三组请求及最终业务恢复，结果见 [窗口验收记录](docs/experiments/window-evidence.md)。实验独占本地 Compose 环境，会调整并恢复配置。

新增窗口规则与 Agent 入口，共享快照、摘要、请求索引和默认六次工具调用上限；确定性结论要求引用已下钻的具体字段。窗口报告记录模型/工具调用、token、耗时与终止原因，缺失 token 记为未知。原单 Trace 入口继续保留。设计见 [窗口调查协议](docs/decisions/007-window-investigation.md)，真实结果与失败复盘见 [四场景对照](docs/experiments/window-comparison.md)。

新增 `hypotheses` 策略，在同一快照上维护假设版本、支持与反证、待检查项及查询目的，最终结论关联具体假设版本和观测字段。非法更新整批拒绝，保留已有状态。结构校验不证明语义或因果正确；CLI 已支持总时限、取消与可选累计token停止阈值，Worker 的进程恢复见上文。设计与命令见 [显式假设协议](docs/decisions/008-hypothesis-investigation.md)，真实失败与结果见 [假设策略对照](docs/experiments/hypothesis-comparison.md)。

```powershell
# snapshot_id 来自 window_cli query 输出；默认 fixed 无需模型
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy fixed
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy agent --output window-agent.private.json
.\.venv\Scripts\python.exe -m backend.investigation.window_cli investigate <snapshot_id> --strategy hypotheses --output hypothesis.private.json
# 独占实验环境；both 使用真实模型，完整报告默认不提交
.\.venv\Scripts\python.exe -m evals.run_window_comparison --strategy both
```

固定协议的新参数验收入口已加入：采集前记录五组计划与代码指纹，独立检查真实观测及业务恢复，再对冻结快照执行预定次数的三策略回放。新增测试覆盖协议变化拒绝、场景未出现、失败轮保留和采集失败后的恢复；方法与命令见 [固定协议验收](docs/decisions/009-fixed-protocol-evaluation.md)。2026-10-23 已采集 95 条窗口请求，五组独立业务检查与规则验收通过、业务恢复通过，全量 88 项测试通过。

2026-10-25 在协议不变的条件下完成上述五组快照的两轮真实模型对照：规则 10/10、普通 ReAct 8/10、假设策略 5/10。失败包含类别与文字不一致、动作格式错误、证据关联失败及响应解析错误，均保留且没有补跑替换。假设策略的较低用量受到提前失败影响，尚不能宣称效率或可靠性提升；详见 [新参数验收记录](docs/experiments/holdout-evaluation.md)。

## 固定流程调查（无需模型）

模型无关的调查执行器已提供 `backend.investigation.agent.investigate_agent` Python 接口。命令行默认运行固定流程，`--strategy agent` 启用 DeepSeek。执行器的接口、预算与实际限制见 [最小调查执行循环](docs/decisions/003-bounded-agent-loop.md)；执行器测试通过不代表模型诊断能力已验证。

先产生一个请求，再将返回的 Trace ID 交给调查入口：

```powershell
$response = Invoke-WebRequest "http://127.0.0.1:8000/orders/preview?sku=demo"
$traceId = [string]($response.Headers['X-Trace-ID'] | Select-Object -First 1)
# SDK 批量导出需短暂等待
Start-Sleep -Seconds 2
.\.venv\Scripts\python.exe -m backend.investigation $traceId
```

程序输出结论、限制和带唯一 ID 的证据；需要保存时增加 `--output investigation.private.json`（报告可包含内部服务信息，按需决定是否提交）。工具仅查询真实链路、固定服务契约、一项运行配置和固定地址业务探针；不读取实验真值。详细规则与限制见 [设计笔记](docs/decisions/002-read-only-investigation.md)。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 评测会依次注入故障，结束时恢复正确地址与零延迟
.\.venv\Scripts\python.exe -m evals.run_baseline
```

真实容器下已验证健康、延迟、错误端口、库存停止、恢复五个案例。库存停止时返回“证据不足”，而不是凭空认定宕机。结果见 `docs/experiments/baseline-results.json`；这些是功能验收，不是模型准确率或泛化能力指标。

## DeepSeek 调查入口

默认模型 ID 为 `deepseek-flash`，对应 DeepSeek V4.1 Flash，官方地址为 `https://api.deepseek.com`。首版固定非思考模式，使用 JSON 动作协议。运行 Agent 会将所查询的投影证据发送到配置的模型服务。

在项目根目录 `.env` 中填写 `DEEPSEEK_API_KEY=你的密钥` 并保存，再使用上文新生成的 `$traceId`：

```powershell
.\.venv\Scripts\python.exe -m backend.investigation $traceId --strategy agent --max-tool-calls 6 --output investigation.private.json
```

可选配置见 `.env.example`；程序自动读取项目根目录 `.env`，进程环境变量优先。文件使用单行 KEY=VALUE，可加配对引号，不支持变量展开、多行值或行尾注释；`.env` 已被 Git 忽略。默认单次输出上限 2048 tokens、网络阶段超时 30 秒。模型调用无自动重试，最多调用模型 7 次、查询工具 6 次。这里只限制单次生成和调用次数，没有整个调查的 token/费用预算或严格总时限。

独立五场景验收也可选择模型策略（会临时修改本地实验服务，勿并行运行其他故障实验）：

```powershell
.\.venv\Scripts\python.exe -m evals.run_baseline --strategy agent
```

模型结果默认写入 `docs/experiments/agent-results.private.json`，固定流程仍默认写入原来的结果文件。报告包含耗时、工具次数和执行器 `termination`；模型调用失败、动作非法、证据引用非法、预算耗尽均不计为合理拒答。配置缺失在修改实验环境前报错；诊断类别不符会记录失败并继续其余案例，最终恢复服务。五个案例属于功能验收，不是泛化准确率。

独立五场景验收也可选择模型策略（会临时修改本地实验服务，勿并行运行其他故障实验）：

```powershell
.\.venv\Scripts\python.exe -m evals.run_baseline --strategy agent
```

模型结果默认写入 `docs/experiments/agent-results.private.json`，固定流程仍默认写入原来的结果文件。报告包含耗时、工具次数和执行器 `termination`；模型调用失败、动作非法、证据引用非法、预算耗尽均不计为合理拒答。配置缺失在修改实验环境前报错；诊断类别不符会记录失败并继续其余案例，最终恢复服务。五个案例属于功能验收，不是泛化准确率。

缺少密钥时在发请求前报错。鉴权失败、限流、超时、截断或非法输出会结束调查并保留已有证据；报告中的 `insufficient_evidence` 可能代表执行失败，需同时查看 termination 和 summary。当前 41 项本地测试通过。2026-10-17 使用真实 DeepSeek 完成健康、延迟、错误端口、库存停止、恢复五个案例，均符合预期并恢复业务；这是单次功能验收，不是泛化准确率。实际耗时、工具次数和重复查询问题见 [模型验收记录](docs/experiments/agent-evaluation.md)。接口来源和运行限制见 [DeepSeek 适配器设计](docs/decisions/004-deepseek-adapter.md)。

## 连接池竞争与慢下游对照

```powershell
docker compose build
.\.venv\Scripts\python.exe -m lab.run_pool_experiment
```

七组条件各采集 24 个请求，比较正常/慢库存、不同连接池上限及低负载。实测小池组库存 P95 约 83 ms、发送前 P95 约 572 ms；慢下游宽池组库存 P95 约 503 ms、发送前约 7 ms。慢下游叠加池竞争时 24 请求中 12 次 PoolTimeout，而小池在低负载下全部成功。

结果和限制见 [连接池实验](docs/experiments/pool-experiment.md)。`client.pre_send_ms` 包含排队、建连和调度，不能当成纯连接池等待时间。成功请求与超时请求须一起分析；新窗口策略已接入这些观测，原单 Trace 策略保持原有能力。

手动可设置 `INVENTORY_MAX_CONNECTIONS`（默认 100）与 `INVENTORY_POOL_TIMEOUT_MS`（默认 2000）。实验脚本会恢复默认池配置、正确地址和零延迟，并检查业务恢复；它们使用独占本地实验环境，不要并行执行。结果文件 `pool-results.json` 包含场景真值，不可作为 Agent 输入。

## 配置错误故障实验

```powershell
docker compose up --build -d --wait
.\.venv\Scripts\python.exe -m lab.run_config_experiment
```

脚本依次验证正确地址 `http://inventory:8001`、错误端口 `http://inventory:8002`、恢复正确地址，每组发送 3 次订单请求，并在订单容器内单独探测真实库存接口。实验验证订单返回 503 时库存依然可以返回 200，同时检查 CLIENT span 的 `server.address`、`server.port` 和 `error.type`。

结果写入 `docs/experiments/config-results.json`，每次运行覆盖。结束时恢复库存地址及零延迟。所有实验脚本都会显式设定服务地址，避免继承终端残留的错误地址；它们用于独占的本地实验环境，不应并发运行。

手动实验可以设置 `$env:INVENTORY_BASE_URL="http://inventory:8002"` 后执行 `docker compose up -d --wait`。恢复时将该变量改为 `http://inventory:8001` 并再次执行 `up`。脚本只为其子进程覆盖环境变量，不修改你所在 PowerShell 的变量；手动注入后也需要手动恢复终端变量。

延迟故障有完整的库存 SERVER span，且耗时显著升高；端口错误通常只有订单 SERVER 与 CLIENT span。缺少库存 span 本身不足以证明配置错误，还需要结合目标端口、实际运行配置以及同网络视角的库存探针。详见 [配置错误实验记录](docs/experiments/config-experiment.md)。

## 调用链可视化

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker compose up --build -d --wait
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
```

打开 [Jaeger](http://127.0.0.1:16686)，选择 `orders` 服务并点击 Find Traces。SDK 批量导出有短暂延迟，首次请求后稍等再搜索。也可以从响应头复制 `X-Trace-ID`，打开 `http://127.0.0.1:16686/trace/这里替换成TraceID`，查看订单 SERVER → 订单 CLIENT → 库存 SERVER 的父子关系。

复用下方 500 ms 延迟操作，重新发请求后，库存 span 应显著变长。日志的 `trace_id` 可关联到同一条链路。不要将三个嵌套 span 的耗时相加。

```powershell
# 不依赖 Docker 的上下文传播与错误处理测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
# 检查 Jaeger 中真实落地的正常、延迟、不可用、恢复链路
.\.venv\Scripts\python.exe -m lab.run_trace_experiment
```

端到端脚本输出 `docs/experiments/trace-results.json`，每次运行覆盖；包含实际 span、关联日志和实验真值，不应作为未来 Agent 的输入。它在结束时恢复库存与零延迟。Jaeger 内存数据会在容器重启后丢失。`docker compose up --wait` 只等待服务启动及已有健康检查；Jaeger 的实际接收能力由验收脚本确认。

本机运行 Python 服务也可启用导出：在两个服务启动终端分别设置 `$env:OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://127.0.0.1:4318/v1/traces"`。未设置时不导出。页面端口可用 `JAEGER_PORT` 覆盖，默认 16686；OTLP HTTP 宿主机端口为 4318。

设计、版本、传播过程和限制见 [调用链设计笔记](docs/decisions/001-tracing.md)。

本阶段验证状态：4 个本地 ASGI 测试通过（包括新增连接失败目标端口校验）；2026-10-15 完成容器构建和 Jaeger 端到端验收。正常、500 ms 延迟、库存不可用、恢复四种场景全部通过，已检查实际入库的 span 父子关系、错误标记和日志 Trace ID，并确认 Jaeger 页面能显示三层调用链。详见 [本次调用链实验记录](docs/experiments/trace-experiment.md)。

## 延迟故障实验

安装本地依赖后，在项目根目录执行：

```powershell
docker compose up --build -d --wait
.\.venv\Scripts\python.exe -m lab.run_delay_experiment
```

脚本依次设置 0、500、0 ms 库存延迟。每组预热一次，然后发送 10 次串行订单请求（并发为 1），检查 HTTP 200、业务响应和请求 ID，并收集同一请求的三条日志。结果写入 `docs/experiments/delay-results.json`；每次运行覆盖该文件。结束时恢复无延迟配置，保留容器运行。脚本会重建当前 Compose 项目的库存容器，请不要与其他故障实验同时运行。

也可以手动修改延迟：

```powershell
$env:INVENTORY_DELAY_MS="500"
docker compose up -d --wait
curl.exe -i -H "X-Request-ID: manual-demo" "http://127.0.0.1:8000/orders/preview?sku=demo"
docker compose logs --tail 50 orders inventory

# 恢复；仅 start/restart 不会更新容器环境变量，需要 up 重建。
$env:INVENTORY_DELAY_MS="0"
docker compose up -d --wait
```

库存服务启动时读取 `INVENTORY_DELAY_MS`（0–10000 的整数），使用 `asyncio.sleep` 模拟异步等待。订单客户端仍采用 2 秒网络阶段超时，因此超过该阈值的故障可能返回 503；500 ms 实验预期仍返回 200。`/health` 只检查进程存活，不受故障延迟影响，不代表业务已恢复。

订单服务接收或生成 `X-Request-ID`，传给库存服务，并在响应头中返回。日志包含 `service`、`event`、`request_id`、`duration_ms` 等字段，使用单调时钟测量耗时。三个观测分别为库存处理、订单等待库存、订单总处理时间；它们是包含关系，不能相加。客户端耗时还包含本机到容器的网络与协议开销。日志不输出延迟开关或场景真值，实验结果包含真值，未来不得作为 Agent 的诊断输入。

阅读顺序：`lab/inventory/main.py` 的异步延迟 → `lab/observability.py` 的请求 ID 与计时 → `lab/orders/main.py` 的下游计时 → `lab/run_delay_experiment.py` 的对照与日志匹配。

## Docker Compose 运行

在项目根目录执行（Docker Desktop 使用 Linux 容器）：

```powershell
docker compose up --build -d --wait
docker compose ps
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
```

预期订单接口返回 HTTP 200、`{"sku":"demo","can_order":true}`。如果之前本机启动的服务仍占用 8000/8001，可先在原终端按 Ctrl+C，或在当前 PowerShell 中设置 `$env:ORDERS_PORT="18000"`、`$env:INVENTORY_PORT="18001"` 后启动，并改用对应端口访问。

验证库存故障与恢复：

```powershell
docker compose stop inventory
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
# 预期 HTTP 503

docker compose start inventory
docker compose up -d --wait
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
# 预期恢复 HTTP 200

docker compose logs --tail 50 orders inventory
docker compose down
```

`Dockerfile` 打包依赖和代码，两个服务复用同一份构建定义，由 Compose 指定不同启动命令。容器内监听 `0.0.0.0`，宿主机端口只绑定 `127.0.0.1`。订单通过 `http://inventory:8001` 访问库存；`inventory` 是 Compose 服务名，容器里的 `127.0.0.1` 仅指向自身。

`depends_on` 等待库存健康后再启动订单，但不负责运行中的自动恢复。订单健康检查只检查 HTTP 服务存活；库存停止时订单仍可能显示 healthy，必须请求订单业务接口判断链路是否恢复。

`.dockerignore` 默认排除全部文件，只放行 Dockerfile、依赖清单和 lab 下的 Python 源码。私人文档、Git 历史、本地虚拟环境和 `.env` 不进入构建上下文。

基础镜像使用 `python:3.12-slim` 并在 Dockerfile 中固定已拉取的 SHA256 摘要；依赖版本见 `requirements.txt`。首次构建需要连接 Docker Hub 和 Python 包源。若拉取镜像超时，可单独运行 `docker pull python:3.12-slim` 检查网络。

验证记录：Compose 配置检查、镜像构建与容器启动通过；真实容器下已验证正常请求 HTTP 200 → 停止库存后 HTTP 503 → 重启库存后恢复 HTTP 200。库存停止时可能表现为连接失败或超时，两者均返回 503。本机 Python 进程下的对应验收也已通过。

参考：[Compose 网络与服务名](https://docs.docker.com/compose/how-tos/networking/)、[Docker 构建上下文与忽略规则](https://docs.docker.com/build/concepts/context/)。

## 本地运行

使用 Python 3.12，在项目根目录的 PowerShell 中安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

打开两个终端，工作目录均为项目根目录，分别启动：

```powershell
# 终端一：库存服务
.\.venv\Scripts\python.exe -m uvicorn lab.inventory.main:app --host 127.0.0.1 --port 8001
```

```powershell
# 终端二：订单服务
.\.venv\Scripts\python.exe -m uvicorn lab.orders.main:app --host 127.0.0.1 --port 8000
```

在第三个终端验收：

```powershell
curl.exe -i http://127.0.0.1:8001/inventory/demo
curl.exe -i "http://127.0.0.1:8000/orders/preview?sku=demo"
```

预期分别返回 HTTP 200 与 `{"sku":"demo","available":10}`、`{"sku":"demo","can_order":true}`。在库存终端按 Ctrl+C，再请求订单接口，预期 HTTP 503 和 `Inventory service unavailable`；重新启动库存服务后，应恢复 HTTP 200。

订单服务默认连接 `http://127.0.0.1:8001`，可通过启动前设置 `INVENTORY_BASE_URL` 环境变量覆盖。HTTP 客户端各网络阶段的超时为 2 秒，并非整个请求的严格总时限。连接失败、超时和下游非成功状态返回 503；库存响应格式错误返回 502。

阅读顺序：`lab/inventory/main.py` 的路由与响应模型 → `lab/orders/main.py` 的 `preview_order` → `lifespan` 中客户端的创建、复用与关闭。库存目前对任何商品都固定返回 10，订单接口只做预览，不创建订单。

实现参考：[FastAPI 生命周期](https://fastapi.tiangolo.com/advanced/events/)、[HTTPX 异步客户端](https://www.python-httpx.org/async/)。

## 计划能力

- 在隔离环境中运行小型微服务链路，复现故障并重置环境。
- 基于指标、日志、调用链与变更记录收集证据，管理候选根因。
- 在工具调用、时间与 token 预算内完成调查，允许证据不足时无法确定根因。
- 经审批执行预设的受限处置，通过独立业务探针和遥测验证恢复。
- 对比固定排查流程、普通 ReAct 与加入证据管理和预算控制的调查策略。

## 初步技术方向

- 业务 API 与调查服务：Python、FastAPI。
- 实验环境：Docker Compose。
- 可观测性：OpenTelemetry，按需接入指标、日志与链路后端。
- 数据存储：PostgreSQL。
- Web 工作台：React、TypeScript。

具体组件与版本将在实现阶段确认。

## 首个里程碑

搭建两个服务之间的请求链路，在相同负载下完成：

1. 运行健康业务请求。
2. 注入可关闭的延迟故障。
3. 从指标或调用链定位异常环节。
4. 移除故障并验证业务恢复。

这一阶段暂不接入模型或开发网页。当前具备基础请求链路、延迟注入、关联日志对照实验和 OpenTelemetry 调用链实现；更完整的负载实验仍待完成。

## 验证原则

诊断必须引用实际观测证据；故障真值与注入控制信息不向调查 Agent 暴露。处置执行成功不等于业务恢复，评测结果以独立检查为准。仅报告实际完成和测试过的能力。
