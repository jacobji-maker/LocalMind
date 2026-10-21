# 回放评测方案

cases.jsonl 含 30 条合成种子场景：跨会话恢复 10 条、纠正与变化 10 条、长历史与预算 10 条。全部是测试夹具，不是真实用户的行为记录。它们定义预期语义，尚未执行。

实现者应将 seed_events 转为测试事件，input 转为当前请求，operation 转为故障/删除等测试动作；给定 expected 检查上下文、存储和最终行为三个层面。must_include / must_exclude 是语义约束，不要求模型逐字输出，原始请求等明示“精确保留”的字段除外。

## 公平比较

- A 无主动编排：固定近期窗口；仍可用同样的历史查询工具。
- B 规则基线：相同工具与数据，确定性范围过滤/检索/组包。
- C 小模型编排：与 B 相同原始数据和入口，增加本地意图、选择与候选压缩。
- 可选 C-no-compress：保留候选选择，取消模型压缩，区分收益来源。

保持相同云端模型/版本、原始历史可访问范围、工具权限、总输入预算和云端推理预算。各组分别克隆数据库，禁止 C 的派生记忆泄漏到 A/B。若工具额外读取资料，token、延迟、调用次数计入该组；本地推理开销单独和合计报告。

用程序校验来源、范围、原话、修订、预算和实际发送内容；用人工标注或独立评审检查语义、任务产物和用户补充次数。不能仅依赖回答模型给自己的分数。主观评审隐藏组别。模型有随机性时重复试验，保存 seed/参数和逐例结果。

## 指标记录

case_id、arm、run_id、model_version、local_model_version、revision、actual_context_ref、source_refs、constraint_pass、scope_pass、task_success、clarification_count、latency_ms、cloud_tokens_actual/estimated、local_tokens、retry_count、degraded_reason。

未知 usage 留空并标注 unavailable，不用字符数冒充实际 token。编排额外延迟从发送事件到云端请求就绪计算；云端整体耗时另报。报告均值、p50/p95、样本数量和失败分布，不只报平均分。

## 验收隔离

这些夹具公开给开发者，适合回归。进入 M4 前另冻结 ≥30 条未见验收用例，三类各 ≥10 条，包含措辞变化、无关干扰、冲突、条件变化和不能回答的情况。留出集合不参与提示词调参。

指标门槛见 PRODUCT.md。零串线、原始请求不改写、纠正立即生效和有来源是发布不变量；任务成功率和延迟仍需真实数据验证。

LongMemEval 的抽取、跨会话、时间推理、更新和证据不足时不臆测等维度可作补充参考 [S7]；不能将其公开分数替代本产品场景验收。


## 开源调研新增场景

research_cases.jsonl 新增 12 条，与 cases.jsonl 的 30 条合计 42 条开发种子。operations 仍是待实现回放驱动的语义描述，不是现成命令或已运行测试。覆盖回声、重复证据、干净线程、摘要失效、条件、未知、无关记忆、后端延迟/隔离、经验版本、遗忘传播和依赖预算。评测组与成本边界见 ../EXPERIMENT_PLAN.md。
# 首轮真实效果对照

新增冻结开发探针 `contrast_cases.json`，11例。范围、遗忘操作、模型参数、输出结构及消息预算在各组保持一致；无记忆组仅作为负对照。本轮结果不属于隔离发布验收。

```bash
python3 tools/contrast_eval.py prepare
python3 tools/run_contrast_ollama.py --ollama-url http://127.0.0.1:11435
python3 tools/contrast_eval.py score
```

先在本机启动已安装模型的 Ollama 服务，并确认本地推理与禁云设置。脚本不会下载模型。prepare 只加载明确标记的合成历史并实际调用MCP子进程；run 才做真实推理。请求和结果默认放在 docs/contrast-*.json；开始新的试验应使用新的输出路径，已存在结果仅允许在请求哈希、模型和参数一致时续跑，未知结果不自动重发。输出Schema没有答案枚举，评分严格区分null、数值、布尔和字符串。

当前实测用工具的本地运行环境执行了与可复现runner相同的模型payload，原始响应记录执行方式，未声称使用当前对话或原生Codex作为后端。评分结果与运行证据见 docs/LIVE_REVIEW.md。
