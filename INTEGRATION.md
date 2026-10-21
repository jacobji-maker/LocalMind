# 接入路线与开源复用

> 历史核验说明：下文记录方案与早期探测时点，尤其第 4 节“未发起推理、模型未知”不再代表最新状态。已有本地真实推理对照与只读 MCP；0.5新增单次预算recall并完成同一开发集合的真实复测。当前接入事实见 [docs/capability-report.json](docs/capability-report.json)、[docs/CODEX_INTEGRATION.md](docs/CODEX_INTEGRATION.md) 和 [docs/PLUGIN.md](docs/PLUGIN.md)；0.4与0.5真实结果分别见 [旧报告](docs/contrast-report.json)、[新报告](docs/contrast-report-0.5.json)。这仍不替代未见场景、原生宿主或云端验收。

文档核验日期：2026-10-21。以下“已核验”均指官方资料；本机完整集成尚未验证。

## 1 接入矩阵

| 路线 | 每轮自动补充 | 控制已有历史压缩 | 对首版的决定 |
|---|---|---|---|
| 自控命令行 + Codex SDK / App Server | 本地组包及 SDK/RPC 入参可观测；宿主最终请求另验 | 新线程与显式检查点，或受支持的 compact | P0 完整闭环的基准入口 |
| 支持的本地 Codex Hooks | 可在发送前补充 | 宿主管理，LocalMind 恢复关键状态 | M0 提前验证，P1 原生体验集成 |
| 仅暴露 MCP 工具 | 取决于宿主/模型是否调用 | 不控制宿主历史 | 作为原文回查能力 |
| 任意现有 ChatGPT 网页聊天 | 未验证有等价受支持入口 | 未验证可控制 | 不作透明接管承诺 |
| 通用模型 API 客户端 | 自控请求时可以 | 客户端自行组织历史 | 后续独立适配，明确费用与授权 |

先实现一个入口，不同时维护多套前端。自控命令行让用户正常输入，并展示流式回答和“本轮用了哪些记忆”；不要求用户每次手动复制上下文。

## 2 已确认的官方能力

[S1] Codex 官方 Hooks 文档描述 UserPromptSubmit 的补充上下文能力，以及 SessionStart 在 compact 后恢复上下文；PostToolUse / Stop 可提供部分结果。每个模型可见 Hook 输出消息的近似默认阈值约 2,500 tokens，可按处理器配置；超限可能落盘并提供预览，落盘失败仍截断。该值不是完整请求窗口。仅支持相应本地编排路径；云端编排不能假定支持本地 command hooks。Hook 还需要宿主的信任配置。不能把这些文档能力当作当前设备已启用。

[S2] App Server 提供逐轮线程接口、事件与状态。thread/start 创建新对话；同线程续传、resume 和 fork 分别延续、恢复或复制历史。excludeTurns 只影响返回数据，不代表排除模型历史。thread/inject_items 是持久追加；thread/compact/start 的 ACK 不代表压缩完成，要等待实际版本的完成事件。完整重组仍需要自控入口的检查点与线程管理。

Python SDK 当前为 openai-codex，文档要求 Python ≥3.10，发布包带锁定 Codex CLI runtime 依赖；TypeScript SDK 要求 Node ≥18。本项目仍建议独立 Python 3.11+ 环境。PATH 缺少 codex/node 不足以判定 Python SDK 路线不可用；M0 锁定实际包、runtime 和协议版本。

[S3] ChatGPT 登录的 Codex 订阅访问与 API key 按量访问是不同路径。SDK/App Server 是 Codex 控制入口，不把订阅登录变成通用 OpenAI API 授权。优先评估现有登录支持的本地 Codex 入口，不读取或复制认证文件。不将订阅解释为通用 API 额度，也不静默切换计费方式。

[S4] Ollama 本地接口支持 JSON Schema 结构化输出和 embeddings。首版只接一个后端；模型名称、量化、中文表现、上下文长度和性能通过当前电脑实测选择。

[S5] MCP 工具由客户端发起调用，不能单靠“提供了 memory.search”保证每轮先检索。自动入口由适配器或生命周期回调触发。

## 3 M0 接入验证清单

输出 capability-report.json，至少记录：OS、运行时、实际客户端版本、编排模式、认证类型标签、可用事件、模型服务状态、窗口/usage 可观测性、context_ownership（应用历史 full/additive/opaque）、history_usage_observable、fresh_thread_supported、工具关闭/只读执行边界、验证步骤与失败原因。

1. 只读确认可用客户端及版本，避免把 PATH 缺少命令等同于应用未安装。
2. 核验本地模型是否可访问、有哪些已安装模型；不自动下载。
3. 在隔离工作区做一个受支持、可撤回的入口试验，验证原始请求不变和前置补充被实际接收；同时检查旧线程历史是否仍参与请求，不能只捕获本次补充。
4. 做一次真实本地模型结构化请求，校验来源与中文任务效果。
5. 做一次跨会话或压缩后的记忆恢复；真实请求与 mock 分开标记。
6. 核验故障、超时、输出溢写、Hook 信任配置和暂停行为。Hooks 不足只标记原生集成阻塞；自控后端不足则明确阻塞 P0 真实云端验收。
7. 有安装、配置或额外计费依赖时列明，不用未验证推断填满报告。

验证记录不能包含密钥、token、认证文件内容或私人原文。

## 4 当前环境的可见性

本次只读检查可见 macOS 26.7.1、系统 Python 3.9.6、SQLite 3.51.0，FTS5 unicode61 / trigram 可创建。PATH 可见 Ollama 0.40.3；未找到 codex 或 node 命令。访问本地模型端口被沙箱阻止，因此模型服务是否运行、已安装哪些模型均未知。

这些结果只反映本次工具运行环境，不推断用户电脑配置完整情况。没有安装组件、修改全局配置、下载模型或发起本地/云端模型推理。开发建议使用项目独立 Python 3.11+ 环境，实际可用运行时在 M0 决定。

## 5 开源复用策略（本次扩充）

已核验九个项目，详见 OPEN_SOURCE_REVIEW.md。借鉴优先级：OpenViking 的分层上下文与本地检索规划、claude-mem 的索引/时间线/原文展开、Hindsight 的事实/归纳/证据分离；Letta 的上下文版本化、Graphiti 的双时间、Memobase 的画像槽位与 Acontext 的经验层按需吸收。

继续保留轻量状态核心和 MemoryBackend 接口。外部后端每次只评估一个：先按实际瓶颈选择 OpenViking；需要带证据的经验整合时再考虑 Hindsight，Mem0 OSS 作为更轻量的候选。当前均未实际接入。

### 接入事实不能互相替代

- OpenViking 的 Codex 文档已有 UserPromptSubmit 等自动召回入口；M0 可以参考其适配实现，但不能据此声称当前设备已支持。[S11]
- mcp-memory-service 的 Codex 文档列出 SessionStart/compact 的召回；Stop 等负责捕获，不等于每轮按当前新输入重组。其标签过滤边界不满足本项目要求时，必须独立项目库/索引隔离或停用该适配。[S14]
- claude-mem 当前已扩展多个客户端，不能继续描述成仅支持 Claude Code；默认托管观察服务也不能当作离线推理。[S12]
- 三者都不能作为“可任意删除另一个客户端已有对话”的证据。完整压缩仍在自控入口捕获真实请求验证。

### 本地运行要分别记录

capability-report.json 应将 storage_local、embedding_local、planner_local、consolidation_local、cloud_answer_backend 分开。服务在本机运行、命令在本机执行、模型在本机推理是不同事实；任何 provider 的默认云端配置都不能被记作本地完成。

引入后端先检查严格范围、原文证据、导出、版本屏障、遗忘传播和来源映射，不能仅看 search 成功。当前没有复制上游代码。许可按组件记录于 OPEN_SOURCE_REVIEW.md，不再笼统声称所有候选项目均为 Apache-2.0。


## 6 交接补充的 M0 关键证据

本节为官方文档事实与待执行验证计划，尚未在本机实际接通。

| 检查 | 验收边界 |
|---|---|
| 请求可观测性 | 分开记录本地 packet、SDK/RPC 入参、宿主线程内容、最终模型请求；未暴露的项是 unknown |
| 新线程 | 不使用 resume/fork/previous response 隐式续传；模型没有复述合成旧标记不能单独证明历史已排除 |
| 其他隐式输入 | 核验工作区指令、宿主 memory、Hooks 与 MCP instructions；新线程仍可能包含这些内容 |
| 工具暴露 | 分开记录 shell、hosted search、MCP、Apps/插件、动态工具和宿主直接执行 RPC |
| 权限边界 | readOnly 限制本地命令文件系统，不等于禁用 MCP/连接器/浏览器等；空 dynamicTools 也不是全局禁用 |
| Hooks | UserPromptSubmit 的补充是额外 developer context；SessionStart(source=compact) 在下一请求前恢复；多个配置层 Hook 会累加 |
| 编排位置 | 本地 command Hooks 不支持所有云端编排路径，不能只因工具在本机就判定支持 |
| 本地推理 | 存储、embedding、规划、后台整理、云端回答分别核验；本地进程不是离线推理证明 |

配置文档提供 shell_tool、web_search、按 MCP server 的 enabled_tools/disabled_tools 和 Apps 独立配置。它们需按锁定版本组合并验证；未核验到一项即可全局只开放 memory 工具的保证。PreToolUse 也不能替代全面执行边界。拒绝验证只在合成文件与隔离范围中进行。

适配器不向模型转发任意宿主 RPC，memory 实现不代理 shellCommand 或 process/spawn 等直接执行入口。无法限制为 P0 所需的受控读取时，该后端的真实集成标记阻塞；不静默降成可自主操作的运行时。

模板见 contracts/capability-report.template.json。检查 SDK 状态或受支持认证接口时只记录认证类别，避免将返回的邮箱和 token 等写入报告。

官方来源：

- SDK：https://developers.openai.com/codex/sdk （当前重定向 https://learn.chatgpt.com/docs/codex-sdk）
- App Server：https://developers.openai.com/codex/app-server （当前重定向 https://learn.chatgpt.com/docs/app-server）
- Hooks：https://developers.openai.com/codex/hooks （当前重定向 https://learn.chatgpt.com/docs/hooks）
- 配置：https://developers.openai.com/codex/config-reference （当前重定向 https://learn.chatgpt.com/docs/config-file/config-reference）
- 权限：https://learn.chatgpt.com/docs/permissions
- 认证：https://developers.openai.com/codex/auth （当前重定向 https://learn.chatgpt.com/docs/auth）
