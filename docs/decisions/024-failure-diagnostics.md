# 调查失败分层与诊断记录

日期：2026-11-02。范围：普通窗口与假设策略的失败可观测性。保持现有停止行为，不增加模型重试、协议纠错或新工具。

## 报告与分类

`WindowReport` 新增可空 `diagnostic`，假设报告继承该字段。结构包含 `stage`、`category`、固定 `code`、`turn` 和最多24项 `issue_types`。一轮对应一次模型调用，0表示调用前失败；累计用量仍读取同报告 `usage` / `usage_totals`，不复制第二份计数。具体失败调用的用量可按 `turn` 找到 `usage.calls` 中第几个模型条目（工具条目不计入轮次）。

| 分类 | 例子 | 阶段 |
| --- | --- | --- |
| `model_request` | 鉴权、限流、HTTP/传输错误、网络超时、上下文过大 | decide |
| `model_response` | 响应解析、拒答、截断、响应过大 | decide |
| `action_schema` | 动作缺字段、多余字段、类型错误 | validate |
| `evidence_reference` | 未下钻的引用、非法字段、观测不足 | validate |
| `hypothesis_state` | 版本/身份冲突、支持或反证缺失 | validate |
| `hypothesis_link` | 未选择假设、结论未关联具体支持证据 | validate |
| `tool_arguments` | 请求的Trace不属于绑定快照 | validate（执行阶段检查失败时以实际阶段为准） |
| `source` | 快照不可用 | source |
| `internal` | 未分类适配器或领域错误 | 实际失败阶段 |

保留 `termination`、调用 `error_code` 及既有假设 `validation_error` / `validation_issues`。新字段描述失败发生位置，不判断业务故障根因。成功报告的 `diagnostic` 为null；预算停止、取消和重复查询继续用原终止原因表达，不伪装成协议失败。

## 解析细分与用量

共享模型解析器保留 `invalid_response` 错误码，新增六个固定子原因：

- `response_json_invalid`：供应商响应不是有效JSON。
- `response_shape_invalid`：缺少必需结构或choices/message形状错误。
- `response_finish_invalid`：不支持的结束原因或返回了tool_calls。
- `action_content_invalid`：消息content不是字符串。
- `action_json_invalid`：content无法解析为JSON。
- `action_not_object`：content是JSON，但不是动作对象。

`ModelFailure.detail` 仅接受上述白名单，调用账本的 `failure_detail` 保存子原因，报告从失败调用生成诊断。已读取的供应商token即使动作解析失败也保留；无用量仍为未知。每轮清空细分原因，不能串用上一轮错误。`invalid_response` 没有detail时保持宽分类，不能为历史两次未保存原响应的失败补猜根因。

新增Schema诊断只存Pydantic错误类型，不保存任意字段名、输入值、异常文本、请求头或响应原文。此约束仅针对新增诊断字段；既有 `rejected_action`、`rejected_finding` 和图检查点的原始动作仍遵循原来的内部数据保留边界。

## 状态与兼容

诊断附加在失败报告上，领域校验、工具执行与假设提交顺序不变。失败动作不触发新查询、不接纳候选假设；无自动纠错。同步窗口及冻结异步对照也复用诊断生成逻辑，以保持同一动作的报告可比较。

报告与账本均已有检查点保存路径，不新增表或图通道，保持schema 4和既有图协议。两个新增字段默认null，旧报告和旧调用记录可读取；不会追溯生成历史诊断。完整失败报告已落检查点后，Worker可以补交任务终态且不再调用模型；未确认调用的 `usage_unavailable` 恢复规则保持原样。

诊断误判只由独立评测器判断：结构、引用合法且 `completed` 的报告仍可能类别错误。专项测试展示这种报告 `diagnostic=null`，但 `evals.run_window_comparison.score` 判分失败。故障真值没有进入模型或执行器；本阶段没有修改历史评测结果或接入自动语义审判。

## 验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_failure_diagnostics.py -v
# 设置专用 TASK_TEST_DATABASE_URL 后运行恢复与全量测试
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

新增7项诊断专项覆盖两种异步策略的六类解析失败、用量保留、请求和未知异常、动作与证据校验、假设状态/关联失败、非法动作不执行与不提交、旧报告兼容及独立误判判分。新增真实PostgreSQL/子进程测试验证失败诊断在检查点终态补交后仍保留，用量不丢失且模型不重发。

未增加依赖，未运行真实模型或新故障场景。此里程碑提供失败定位能力，不代表已提高诊断准确率；下一步再据具体错误设计预算内有限纠错，并用新样本评估。

最终验收：全量246项测试通过，无跳过，包含新增7项诊断测试和1项真实数据库恢复测试；既有图/旧循环对照、取消、预算、任务/API/SSE回归均通过。`pip check` 无依赖冲突。测试只使用随机schema并清理，未迁移默认业务schema。
