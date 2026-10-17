# DeepSeek 模型适配器

## 决策与范围

用户指定 DeepSeek API 与 DeepSeek V4.1 Flash。2026-10-17 查阅官方文档确认 API 模型 ID 为 `deepseek-flash`，默认基址为 `https://api.deepseek.com`。模型 ID 是供应商别名，无法据此保证未来底层版本不变。

`model.py` 实现 DecisionModel 接口，复用已锁定的 HTTPX 0.28.1，不新增 SDK。`ChatCompletionsModel` 当前按 DeepSeek 参数实现，不宣称兼容所有同类供应商。可覆盖基址用于本机模拟或明确选择的兼容服务。

调用 `POST /chat/completions`，设置 `response_format=json_object`、`max_tokens`、`thinking.type=disabled`、`stream=false`。模型返回动作对象，由现有执行器继续校验并分发工具。这里使用 JSON 动作协议，没有使用供应商原生 function calling；未来应通过对照实验决定是否切换。

## 上下文与失败处理

系统提示包含工具语义、动作 Schema、格式示例及证据纪律，不包含实验真值、注入开关或固定流程给出的诊断。每轮发送当前 Trace ID、已有证据快照和剩余调用次数。提示模型把观测内容视为数据，但提示词本身不能保证完全抵御提示注入；实际执行范围仍由白名单约束。

模型请求从 `DEEPSEEK_API_KEY` 配置读取凭据（进程环境变量优先，其次是项目根目录 `.env`），放入 Authorization 请求头。配置 repr 隐藏密钥；错误报告仅使用固定错误码，不记录请求头、响应原文或推理内容。CLI 先验证输入和配置，再创建 HTTP 客户端，通过上下文管理关闭连接。

HTTPX 禁用环境代理继承与重定向，无自动重试。远端基址须 HTTPS；本机可 HTTP，URL 不接受用户名、密码、查询串或片段。上下文上限 256,000 UTF-8 字节，响应上限 1,000,000 解压后字节，读取期间检查经过时间。

默认网络阶段超时 30 秒，单次生成上限 2048 tokens，最多 6 次工具调用和 7 次模型调用。网络超时与读循环检查不等于严格总截止时间；输入 token、累计 token、费用和供应商 usage 暂未统计或限制。每轮全量发送已有证据，会重复消耗输入 token。

HTTP 鉴权、限流、网络异常、输出截断、拒答、空输出和响应格式错误转换为 ModelFailure 固定码。执行器返回 insufficient_evidence 并保留证据。JSON mode 只协助产生 JSON，动作结构与证据引用仍以本地校验为准。确定性结论的语义正确性需要独立评测。

## 验收状态与下一步

2026-10-17：34 项 unittest 全部通过，包含 10 项新增 HTTP 模拟/配置/CLI 测试。覆盖一次完整的“模型响应→工具证据→下一轮模型→报告”离线流程，以及超时、限流、鉴权、重定向拒绝、截断、空响应、超限和密钥隐藏。

后续更新（2026-10-17）：本地 .env 已配置密钥；用户明确授权将实验观测数据发送至 DeepSeek 后，已完成真实五场景验收，五项通过且业务恢复确认成功。完整报告保存在被 Git 忽略的 agent-results.private.json；可公开的结果摘要见 [模型验收记录](../experiments/agent-evaluation.md)。当前本地测试总计 41 项通过。下一步针对实际重复查询建立去重策略并记录模型用量；不能将单次五场景功能验收作为泛化准确率。

## 官方接口来源

本实现按接口文档编写，没有复制外部项目源码；截至 2026-10-17 核对：

- [DeepSeek 首次 API 调用](https://api-docs.deepseek.com/)：模型 ID、基址、鉴权及端点。
- [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)：JSON 模式、提示要求、max_tokens 与空输出情况。
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)：请求/响应参数。
- [DeepSeek 思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)：thinking 开关。首版明确关闭，之后可单独评估其收益与开销。
