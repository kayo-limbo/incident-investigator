# 006：窗口证据与冻结快照

## 本轮问题与取舍

连接池竞争不能仅凭一条成功 Trace 判断：必须同时保留成功、失败及缺失观测。本轮完成第①项的首个可验收切片：查询 → 受限投影 → 冻结 → 摘要/下钻 → 字段引用验证。没有改写既有 Agent 循环，也没有把新的统计量直接包装成根因规则。

新增思路是把“查询执行成功”与“证据覆盖完整”分开。`quality` 为 `observed`、`empty`、`partial` 或 `unavailable`；`observed` 只说明获得有效样本，不等于业务正常。`coverage` 始终为 `unknown`，因为未实现采样覆盖率或遥测水位线。缺少库存 span 不补零、不单独判定库存不可用。

## 文件与接口

- `evidence_models.py`：不可变窗口参数和字段级 `ObservationRef`。
- `window_queries.py`：`WindowEvidenceTools(EvidenceTools)`，共享既有错误边界；`query_window` 和 `get_snapshot_trace` 可由后续规则/Agent 策略共同调用。
- `snapshot_store.py`：规范 JSON 的 SHA256 标识、同目录临时文件与原子发布、读取校验和字段解析。
- `window_cli.py`：独立入口，不调用模型。
- `evals/run_window_evidence.py`：独立请求结果与窗口证据对照；真值仅存在于验证器。

窗口以订单 SERVER 的开始时间定义 `[start_us,end_us)`，UTC epoch 微秒，最多五分钟/200 条。Jaeger 查询固定 `orders` 和 `GET /orders/preview`，本地再次过滤边界。响应最多 8 MB，每条最多 50 spans，网络阶段超时 3 秒，流读取时检查累计 5 秒；这不是严格的可取消总 deadline。

响应畸形条目被拒绝并计数；来源报告错误、坏条目或达到 limit 标为 partial。成功与失败请求一起进入摘要；每个耗时统计附带自己的样本数，缺失不填零。发送前耗时还含建连、调度，不能等同纯连接池等待。快照中的配置值来自请求 span，不当作当前配置或完整历史配置版本。

快照包含数据源、固定权限范围描述、参数、采集时间、投影版本、质量和所有接受的投影。每次实时查询都会重新采集，采集时间进入哈希，因此不把相同参数视为永久缓存命中；相同已冻结 payload 重复写入幂等。下钻只读指定快照，不访问实时 Jaeger。迟到数据进入新快照，旧快照不改变。

这属于应用层不可变存储，不能阻止外部人员删除/修改文件；读取哈希检测篡改。当前没有数据库事务、保留策略、多租户授权或进程崩溃临时文件清理。`scope` 是固定来源说明，不是已实现的权限系统。完整结果指受限投影，不是 Jaeger 原始响应；任意标签、日志、进程环境不落盘。

字段引用绑定 snapshot、trace、span 和字段；解析器检查成员关系及字段存在，不能证明语义上支持某个根因。总模型调用/token/终止原因汇总尚未实现；窗口调用失败也记录一次调用与耗时，尚未引入跨工具统一账本。

## 运行与验收

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
docker compose up --build -d --wait
.\.venv\Scripts\python.exe -m evals.run_window_evidence
```

独立实验三组各 24 个请求及一次预热，核对实际业务状态码、全部请求 ID、请求时连接池配置字段及快照下钻，最后恢复零延迟、默认池配置和正确地址，并通过业务接口验证。测试结果不证明 Agent 定位准确率。

手动查询近一分钟（先生成业务请求并留出导出等待时间）：

```powershell
$endUs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000
$startUs = $endUs - 60000000
.\.venv\Scripts\python.exe -m backend.investigation.window_cli query --start-us $startUs --end-us $endUs
# 用上一步返回的 snapshot_id 和 summary.trace_ids 替换下面两个值
.\.venv\Scripts\python.exe -m backend.investigation.window_cli trace <snapshot_id> <trace_id>
```

默认存储 `.snapshots/`，Git 忽略该目录及 `*.private.json`。不要把快照当作可公开的无敏感业务信息文件。

## 来源与后续路线

已读取与 Compose 镜像一致的 [Jaeger v2.9.0 query_parser.go](https://github.com/jaegertracing/jaeger/blob/v2.9.0/cmd/query/app/query_parser.go)，核对 service/operation/start/end/limit 以及微秒单位。此次自行实现 Python 投影与快照逻辑，未复制上游源码、未新增第三方依赖。尚未阅读固定版本 HolmesGPT/AIOpsLab 核心模块，不宣称已借鉴其实现。

总体方向采用“证据可追溯、调查可恢复、处置可验证”，分步推进：

1. 补齐第①项：把窗口工具接入公平的规则与 Agent 调查入口，统一调用/token/耗时与终止记录；先核对当前模型 usage 响应，不把缺失 token 当作零。
2. 显式假设版本与支持/反证、查询目的、Claim 字段引用；共同作用的解释不强制单选。此时固定 HolmesGPT 版本研究工具协议。
3. 在冻结输入上比较去重、有限并发及上下文策略；保持相同权限与预算。
4. 引入 PostgreSQL、Worker、LangGraph；先定义权威任务状态、租约代次与 checkpoint 对账，再实现恢复。
5. 审批绑定资源/参数/版本/有效期；执行后独立验收，分别记录定位正确、缓解有效与持续恢复。
6. 工作台展示结构化决策记录，扩展有新诊断难点的场景，并以回放和真实环境评测区分策略收益与恢复能力。

当前不提前创建空的假设、Worker 或审批框架，下一轮围绕已有窗口证据继续推进。
