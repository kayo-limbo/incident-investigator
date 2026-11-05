# 订单多来源冻结证据与三策略回放

日期：2026-11-05。已实现实验入口并完成首轮真实三策略回放；独立于旧窗口 CLI/Worker 协议。实际验收见 [实验记录](../experiments/bundle-comparison.md)。

## 实现范围

新证据包 schema_version=2、projection_version=investigation-bundle-v1，由现有内容寻址 SnapshotStore 保存。初始上下文仅含订单症状和可用来源，查询 trace_window 获取采样请求分段，查询 client_runtime 获取实际订单进程的前后配置和请求计数。查询不接受 URL、命令、其他快照 ID 或实验参数。

新增 GET /observations/client 仅用于现有本地实验服务，读取进程实际使用的连接池容量、等待超时、进程标识、开始/观测时间、请求开始/结束计数及在途量。计数覆盖订单预览；结束计数包括失败/取消，不能当作成功数。累计峰值从进程启动计算，不是本窗口峰值；未实现跨进程汇总或指标存储。返回值无注入延迟、场景名、答案、目标 URL 或密钥。

采集器在负载前后读取端点，检查同一进程、时间覆盖和配置一致性，独立核对19次请求（18次正式请求及1次预热）。Trace 仍来自 Jaeger，只投影白名单字段。缺失条件派生包移除整个分段来源，保留告警症状与运行观测，不包含原始快照 ID。原始包和场景标签仅保留在评测 manifest，调查执行器无换包接口。这是工具接口隔离，不是进程级访问隔离。

每个来源返回可引用路径；引用包含 snapshot_id/source/field，只允许本调查已查询、当前包存在且非空的标量字段。重复字段或跨包引用拒绝。确定性分类要求已读到完整请求分段且至少引用其中一个请求字段；运行配置不能代替缺失分段。此约束只保证形式合法，尚未实现主张语义或因果验证。

## 策略和预算

- fixed：固定查询两个来源，沿用已有公开耗时/错误模式阈值分类，补充引用，不将小池或高并发直接当作根因。
- agent：普通 ReAct，自行选择两个来源与结束时机。
- hypotheses：复用已有假设账本的版本、支持/反证、身份不变与结论关联规则，新增来源引用适配；查询目的仍为文本，尚未实现判别性查询算法。

两个模型策略复用 AsyncWindowChatModel 传输和 SerialGraphRun 的调用计数、累计 token 停止、总时限和取消。新图有 decide/validate/tool 三个节点，候选假设在校验通过、执行前检查预算后才接纳；重复查询连同附带更新拒绝。独立协议默认无纠错，也没有 Worker、数据库检查点或跨重启恢复入口，不能把旧协议的恢复验收当成本协议的验证。

每调查最多6次工具调用（初始告警占1次）、6次模型调用、120秒、40000累计token停止阈值。重复读取同一来源停止；实际只有两个来源，尚无精细查询参数。token 阈值在响应后检查，可能超额；缺失供应商用量停止，不记零。同步快照读取与解析不能被严格抢占。

## 固定协议与运行

先按 README 安装现有 requirements.txt、requirements-graph.txt，并配置模型。无新增依赖；LangGraph 等版本沿用已锁定依赖。此次没有复制外部仓库源码，复用的是项目已有模块。

在项目根目录运行（目录每次使用新名称；collect 独占实验环境，退出时恢复零延迟/池100/等待2000ms）：

    docker compose up --build -d --wait
    .\.venv\Scripts\python.exe -m evals.run_bundle_comparison freeze --directory .snapshots/bundle-example
    .\.venv\Scripts\python.exe -m evals.run_bundle_comparison collect --directory .snapshots/bundle-example
    .\.venv\Scripts\python.exe -m evals.run_bundle_comparison fixed --directory .snapshots/bundle-example
    .\.venv\Scripts\python.exe -m evals.run_bundle_comparison evaluate --directory .snapshots/bundle-example
    .\.venv\Scripts\python.exe -m evals.summarize_bundle_comparison --directory .snapshots/bundle-example

freeze 读取模型配置但不调用供应商；fixed/collect 不调用模型。evaluate 会外发受限观测并产生模型费用，执行前须有相应授权。fixed 结果可用汇总命令增加 --fixed-only 离线重算。

计划在采集前记录工作区代码指纹、四组环境参数、两个缺失条件、预算、模型配置及两轮顺序。每轮对所有六份证据各执行两种模型策略，第二轮反转策略顺序，总计24次模型调查。未完成调用保持 started，结果目录不能覆盖，不自动重发未知请求；协议发生变化拒绝继续。完整工作区源文件应随实验私有归档，避免仅用 Git HEAD 掩盖已有未提交改动。

本批明确作为开发样本，四组真实环境运行与两份派生条件分开计数。评分校验冻结包来源、原始业务观测、类别、终止状态、字段引用及假设关联，离线重算不调用模型；自然语言中的错误因果陈述仍须单独人工复核，不能称为完整语义准确率。失败调用成本纳入汇总。

首轮实验版本仅保留已接纳的假设、查询、固定校验原因码与调用账本，未保存被拒绝的候选动作或细分 Schema 路径。因此该轮 invalid_schema 只能归类为协议失败，不能进一步推断具体错误字段。2026-11-05 后续已补充有界候选与受控路径，见 [029](029-bundle-failure-diagnostics.md)；不为历史报告补造内容，反馈改进仍须新样本验证。

## 当前实验能力限制

新增来源扩大了可观测范围，但两个来源均一次返回整窗数据，任务仍有较强阈值可解性。尚未完成无关变更、局部异常、传播或未知机制的独立测试；本轮不能证明 Agent 优于规则，也不能证明主动调查、多 Agent 或迁移收益。下一步应根据真实失败与查询轨迹设计可判别任务，而不是直接增加角色。
