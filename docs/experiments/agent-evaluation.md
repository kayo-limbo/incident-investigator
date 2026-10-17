# 模型调查独立验收

`evals.run_baseline` 支持 `--strategy baseline|agent`，共用健康、500 ms 库存延迟、错误库存端口、库存停止和恢复五个案例。评测器持有场景标签和预期结果，调查子进程只接收 Trace ID、策略和调用次数上限；删除继承环境中的两个注入参数。此边界仍不是进程权限沙箱。

每个场景记录类别是否符合预期、端到端调查耗时、工具调用次数和完整调查报告。`termination=completed` 表示正常生成报告；其余终止类型不能得分，即使 finding.code 恰好是 insufficient_evidence。评分还要求引用存在且唯一，并至少引用一条成功的链路证据。它只能检查类别与引用完整性，不能自动证明自然语言结论的每个断言都正确。

模型配置在修改容器前校验。诊断错误记录失败后继续案例；子进程超时或退出失败保存固定错误类型并中止后续案例。finally 恢复正确地址与零延迟，并通过订单业务探针确认恢复。模型子进程总等待上限 300 秒；超时中断不意味着服务商已停止计费。

命令：

```powershell
.\.venv\Scripts\python.exe -m evals.run_baseline --strategy baseline --output docs/experiments/baseline-validation.private.json
.\.venv\Scripts\python.exe -m evals.run_baseline --strategy agent
```

结果默认分开存放，模型报告采用 private.json，避免意外公开观测和生成内容。两次运行遵循相同场景定义，但各自产生请求，不能称为完全相同快照或统计意义上的受控对照。模型与规则的对比还需统一诊断标准、重复运行及人工核对。

2026-10-17 验证：39 项本地测试通过；扩展后的固定流程在真实容器中五个案例全部通过，业务恢复验证成功，记录位于本地 baseline-validation.private.json。DeepSeek 真实验收尚待密钥在运行进程中可用；不将固定流程结果作为模型成绩。
