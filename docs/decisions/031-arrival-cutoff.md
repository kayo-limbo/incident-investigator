# 入口速率按发送截止读数计算

日期：2026-11-07。承接 [双窗口开发场景](../experiments/paired-windows.md)。本次修正观测区间，未改变负载参数、任务准入阈值或模型策略。

## 问题与契约

旧协议用before→after计数和时间计算速率，after在全部请求排空后读取。容量下降会使排空变长，即使发送节奏保持17次/秒，仍得到0.896/0.887的前后速率比。该计算对完整区间本身正确，但不能用来证明发送阶段流量保持不变。

新增schema 4 / paired-bundle-v2，旧schema 3 / paired-bundle-v1保持原义，不从旧数据推测或补造截止读数。PairedSession与来源消融支持显式版本解析；新采集计划标识为paired-development-v2，旧冻结计划不继续使用新代码。

| 读数 | 时机 | 用途 |
| --- | --- | --- |
| before | 预热结束、开始发送之前 | 初始计数与服务端单调时间；要求无在途 |
| send_end | 预定发送区间结束后立即查询 | 与before计算到达速率，允许仍有在途 |
| after | 驱动任务全部结束后 | 核对全部到达/完成计数及最终排空，要求无在途 |

新入口速率 = (send_end.requests_started − before.requests_started) / 服务端单调时间差。保留before→after的drained_interval_rate用于比较，不将两者混称为同一指标。send_end的在途数只是一次瞬时读数，不是窗口峰值。

截止观测使用单独的HTTP客户端，在窗口开始前建立客户端对象，避免观测请求排在负载驱动的连接池中。它仍与业务共用订单进程，不能保证不会被服务端调度延迟。因此同时限制：

- 从预定发送结束到观测返回不超过100ms，超过或请求失败即拒收。
- 服务端before→send_end区间不少于预定发送时长，额外耗时不超过100ms。
- before→send_end→after内进程及配置一致、墙钟/单调时间一致、计数单调且状态守恒。
- send_end和after的requests_started必须相等；仍有迟到请求则拒收，不悄悄移动边界。
- after到达数与完成数增量仍须等于独立业务记录数，并通过原排空与预算检查。

所以它是“实际发送结束观测区间速率”，并非精确到指定微秒的固定窗口计数；误差由实际读数与100ms上限约束。Trace仍使用服务端before/after完整区间，包括排空中的请求。场景名称、预期原因、发送计划只保留在评测端，不进入证据包。

## 实现与验证入口

- [证据协议](../../backend/investigation/paired_evidence.py)：ArrivalRuntime、PairedWindowV2、PairedBundleV2和版本分派；原v1计算不变。
- [采集入口](../../evals/collect_paired_windows.py)：截止回调、独立观测连接、延迟预算、三次读数重建与离线audit；未调用模型。
- [协议测试](../../tests/test_paired_evidence.py) 与 [采集测试](../../tests/test_paired_collection.py)：在途读数、迟到到达、截止失败/延迟、版本隔离、引用和原始数据篡改。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_paired*.py" -v
.\.venv\Scripts\python.exe -m evals.collect_paired_windows collect .snapshots/paired-cutoff-new --cases capacity arrival --repeats 2
.\.venv\Scripts\python.exe -m evals.collect_paired_windows audit .snapshots/paired-cutoff-new
```

新目录必须不存在。容量仍为60ms、17次/秒、池2→1；入口仍为60ms、池2、20→33次/秒。非入口场景速率比0.9–1.1、入口场景至少1.5的门槛均不变。开发实采结果另记，不回填旧轮次，不视为Agent准确率或未知故障泛化成绩。

## 实际验收

`.snapshots/paired-20261107-cutoff-v2` 四次独立开发采集全部有效且任务准入通过，当前源码audit重算为valid=4、attempted=4、task_ready=4、recovery=true。配置与订单业务恢复通过，未调用模型。

| 场景 / 重复 | 新区间速率（参考→事件，次/秒） | 新速率比 | 事件含排空速率（次/秒） | 事件截止在途数 |
| --- | --- | --- | --- | --- |
| 容量 / 1 | 16.9842→16.9807 | 0.9998 | 15.4701 | 23 |
| 容量 / 2 | 16.9722→16.9861 | 1.0008 | 15.3780 | 24 |
| 入口 / 1 | 19.9665→32.9841 | 1.6520 | 30.5529 | 36 |
| 入口 / 2 | 19.9591→32.9427 | 1.6505 | 30.1248 | 43 |

新结果支持“发送区间到达速率受控/上升”的观测判断，尾部不会再稀释该分母。它没有证明发送前延迟全部等于池等待，也没有证明未知故障泛化。新旧轮次是独立采集、运行条件会有波动；不能把上轮0/2与本轮2/2解释为Agent准确率提升。本轮同一原始记录同时保存两种区间速率，可直接审查测量差异。

验证：24项paired专项及324项全量测试通过，无跳过；全量128.884秒，包含真实PostgreSQL恢复，日志为paired-cutoff-tests.private.log、paired-cutoff-full.private.log。pip check通过。27份旧v1真实视图的内容与历史速率计算保持一致；12份新v2完整/来源消融视图均完成5次只读查询，真实send_end引用可解析。测试覆盖截止读数缺失/迟到、回调超时、截止时仍有在途、截止后新增到达、计数/时钟/进程异常、版本隔离与原始数据篡改。

下一小里程碑回到调查策略：为v2接入固定规则、普通ReAct和显式假设的同权限执行入口，先完成离线动作/引用验证及强规则基线，再冻结独立参数任务与模型对照规模。五类全套v2正式对照、独立测试集、主动查询增益及多Agent均尚未验证。
