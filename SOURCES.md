# 资料与核验范围

核验日期：2026-10-21。技术能力以官方文档当前描述为依据；实现时仍需锁定并实测版本。以下来源只支持相邻的接口/项目事实；LocalMind 的范围、预算、状态机、优先级和指标是本项目设计决定。

## 官方与一手来源

- [S1] Codex Hooks：https://developers.openai.com/codex/hooks 。前置补充、压缩后恢复、信任与本地编排限制。该页可能重定向到 learn.chatgpt.com/docs/hooks。
- [S2] Codex App Server：https://developers.openai.com/codex/app-server ；SDK：https://developers.openai.com/codex/sdk 。会话、逐轮请求、追加与压缩的能力边界。
- [S3] Codex Authentication：https://developers.openai.com/codex/auth 。ChatGPT 登录与 API key 是不同访问与计费路径。
- [S4] Ollama Structured Outputs：https://docs.ollama.com/capabilities/structured-outputs ；Embeddings：https://docs.ollama.com/capabilities/embeddings 。结构化格式及本地检索接口。
- [S5] MCP Tools 规范（固定参考版本 2025-06-18）：https://modelcontextprotocol.io/specification/2025-06-18/server/tools 。客户端调用语义；实际接入时协商宿主版本。
- [S6] SQLite FTS5：https://sqlite.org/fts5.html 。文本检索、trigram、短词限制和索引一致性。
- [S7] LongMemEval 原论文：https://arxiv.org/abs/2410.10813 。仅借鉴评测维度，不采用其分数作为本产品预期。
- [S8] Mem0 官方源码：https://github.com/mem0ai/mem0 ；许可证：https://github.com/mem0ai/mem0/blob/main/LICENSE 。
- [S9] Letta 官方入口：https://github.com/letta-ai/letta ；当前代码入口：https://github.com/letta-ai/letta-code ；许可证：https://github.com/letta-ai/letta-code/blob/main/LICENSE 。
- [S10] Graphiti 官方源码：https://github.com/getzep/graphiti ；许可证：https://github.com/getzep/graphiti/blob/main/LICENSE 。

## 原始项目资料

阅读了用户提供项目对应的 README.md、CODEX_START.md、TASKS.md、AGENTS.md 四份当前设计。原始目标为本地认知运行时及“观察—判断—操作—验证”闭环。本版本根据当前会话中的需求，把每轮上下文编排设为 P0，自主执行顺延。

原始文件未被覆盖。本包不重新打包原始对话、认证数据或不相关项目资料。

## 完成状态

已完成：产品与架构设计、官方资料核验、受限环境只读探测、协议草案和合成回放定义。

尚未完成：软件实现、实际本地推理、实际云端接入、Codex Hooks 配置和运行、端到端质量/延迟/用量评测。环境可见性不等于实机兼容性结论。


## 本次开源调研补充（同日核验）

- [S8 补充] Mem0 OSS/Platform 对比：https://docs.mem0.ai/platform/platform-vs-oss ；Ollama：https://docs.mem0.ai/components/llms/models/ollama 。区分 OSS 与平台，以及本地模型配置。
- [S9 补充] Letta Context Repositories：https://www.letta.com/blog/context-repositories/ 。Git 版本管理、MemFS 与渐进展开；不是 LocalMind 来源权限策略的证明。
- [S10 补充] Graphiti episodes：https://help.getzep.com/graphiti/core-concepts/adding-episodes ；Graphiti/Zep：https://help.getzep.com/zep-vs-graphiti 。来源、时间与平台边界。
- [S11] OpenViking：https://github.com/volcengine/OpenViking ；分层：https://docs.openviking.ai/en/concepts/03-context-layers ；轻量 query_planner：https://docs.openviking.ai/en/guides/01-configuration ；Codex：https://docs.openviking.ai/en/agent-integrations/04-codex ；能力参考：https://docs.openviking.ai/en/agent-integrations/16-capability-reference 。主项目及组件许可见 README / LICENSE。
- [S12] claude-mem：https://github.com/thedotmack/claude-mem ；配置：https://github.com/thedotmack/claude-mem/blob/main/docs/public/configuration.mdx ；许可边界：https://github.com/thedotmack/claude-mem/blob/main/docs/license.md 。当前多客户端、渐进检索、本地 provider 与托管服务边界。
- [S13] Hindsight：https://github.com/vectorize-io/hindsight ；配置：https://hindsight.vectorize.io/developer/configuration ；recall：https://hindsight.vectorize.io/developer/api/recall 。三类操作、支持证据、后台归纳与模型分工；MIT 许可。
- [S14] mcp-memory-service：https://github.com/doobidoo/mcp-memory-service ；Codex：https://github.com/doobidoo/mcp-memory-service/blob/main/codex/README.md 。生命周期和项目过滤位置。
- [S15] Memobase：https://github.com/memodb-io/memobase ；服务器：https://github.com/memodb-io/memobase/blob/main/src/server/readme.md 。画像/事件、缓冲整理、原始 blobs 与本地部署。
- [S16] Acontext：https://github.com/memodb-io/Acontext ；快速学习：https://docs.acontext.io/learn/quick ；本地：https://docs.acontext.io/settings/local 。任务经验、skill 与自托管边界。
- 设计判断与事实边界：上述机制已有上游先例。本项目新增的具体优先级、状态屏障、依赖规则和评测阈值属于工程设计，未实装、未复现任何上游性能或节省数据。

新增完成项：九项目比较、架构取舍、评测计划、12 条合成边界场景。软件实现、真实后端/模型/客户端集成状态仍为未完成。开发时需冻结版本；本次没有以 GitHub 星数、营销分数或默认 provider 推断可用性。

## 开发交接核验补充

同日再次核验 Codex 官方 SDK、App Server、Hooks、配置、权限和认证文档；关键修订见 INTEGRATION.md 第6节。CONTEXT_FLOW.md 的身份、状态机、snapshot、校验点和启动顺序为 LocalMind 工程设计，不是上游现成功能承诺。产品实现与实际模型调用状态仍为未完成。
