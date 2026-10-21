# 开源调研与架构取舍

核验日期：2026-10-21。只使用官方仓库、官方文档和原论文。下文“文档事实”不代表本机已安装运行；“选择/借鉴”是 LocalMind 的工程判断。未复制上游代码，也未采用托管服务。主分支未锁定 commit，正式引入时需要重新记录版本和模块许可。

## 1 对产品方向的判断

用户需要的是：在继续正常工作时，每轮都有人维护“现在要做什么、哪些要求有效、哪些经历相关”，并把必要信息交给云端模型。检索出相似句子只是其中一步。保存、召回、当前状态、上下文取舍、真实客户端接入必须分别验收。

现有项目已提供很多基础设施。“小模型辅助检索”“分层记忆”“后台整理”都有先例，不作为 LocalMind 的原创性主张。本项目的价值在于把这些机制组合成适合个人长期工作的产品，并验证纠正及时性、证据可靠性和任务结果。

## 2 九个项目的比较

| 项目与官方源码 | 文档可核验的能力 | 对 LocalMind 的选择 |
|---|---|---|
| [OpenViking](https://github.com/volcengine/OpenViking) | 按目录/项目检索；L0 摘要、L1 概览、L2 原始内容；本地 Ollama query planner；Codex Hooks 与 MCP | 整体方向最贴近需求。借鉴分层与入口，作为首个可选对照后端；当前纠正另走本地同步状态 |
| [claude-mem](https://github.com/thedotmack/claude-mem) | 多客户端观察/捕获；本地 worker、SQLite/Chroma；search → timeline → get_observations | 借鉴精简索引到原文的渐进检索和适配器设计；不把所有工具输出都交模型整理 |
| [Hindsight](https://github.com/vectorize-io/hindsight) | retain / recall / reflect；事实、经历、带支持证据的 observations、后台 mental models | 借鉴证据与归纳分离；作为需要更强记忆后端时的候选，先通过同一适配器比较 |
| [Mem0](https://github.com/mem0ai/mem0) | OSS 记忆提取/检索/管理接口、可配置模型与向量库；有 history | 适合轻量 SDK 对照。不能把托管平台的时序、图与 Dream 整合算入 OSS |
| [Letta / Letta Code](https://github.com/letta-ai/letta-code) | 当前源码采用完整 agent harness；Context Repositories/MemFS、渐进展开与 Git 版本管理 | 参考上下文分层和可追溯修改；整套采用会改变工作入口，暂不作为底座 |
| [Graphiti](https://github.com/getzep/graphiti) | episode 来源、双时间、事实失效、关系与混合检索 | 借鉴时间与失效语义，先在 SQLite 实现；关系推理成为瓶颈再引入图引擎 |
| [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) | SQLite、本地 ONNX embedding、REST/MCP 与生命周期适配 | 参考本地部署和入口。Codex 文档的召回以 SessionStart/compact 为主，不能代替逐轮重组 |
| [Memobase](https://github.com/memodb-io/memobase) | 用户 profile 与事件分离；buffer/flush 异步整理；带预算的 context API | 借鉴稳定画像槽位。默认处理后移除原始 blobs，与本项目的原文证据策略不同 |
| [Acontext](https://github.com/memodb-io/Acontext) | 从任务成败后台整理经验，形成可读 Markdown skill 并按需读取 | 后续经验层参考；工作经验要带适用条件与验证记录，不直接变为用户要求 |

每项结论对应的额外证据列在 SOURCES.md 的 S11–S16，以及 S8–S10 的补充文档。

## 3 需要纠正的选型假设

### OpenViking：最相关，但后台学习与即时纠正不同

[配置文档](https://docs.openviking.ai/en/guides/01-configuration) 已说明轻量 query planner 可使用 Ollama，因此本地小模型路线有可参考实现。[接入能力文档](https://docs.openviking.ai/en/agent-integrations/16-capability-reference) 同时区分会话归档与后台记忆抽取；提交成功不能证明新约束已经进入长期记忆。

其 client/local compressor 可以运行本机 Codex 或 Claude 命令，不能据名称推断为离线模型推理。Codex Hook 负责补充，已有宿主历史的压缩仍需宿主支持。来源统计只能说明内容提供给了模型，不能证明模型采用。

### Mem0：开源 SDK 与云平台分开评估

[官方对比页](https://docs.mem0.ai/platform/platform-vs-oss) 当前将 Graph Memory、Temporal Reasoning、Memory Decay、Dream 列为平台功能；OSS 的旧外部图集成已移除。当前 README 描述提取阶段为 ADD-only，不代表 update/delete 管理 API 不存在。LocalMind 的纠正、来源权威与有效版本不能交由其默认提取流程决定。

### Letta：参考当前实现

[旧仓库入口](https://github.com/letta-ai/letta) 已指向 letta-code；旧 V1 API 在 archive 分支。不能按早期 MemGPT 教程假定现行服务结构。[Context Repositories](https://www.letta.com/blog/context-repositories/) 值得借鉴，但模型可编辑的记忆文件不是用户原始陈述的替代证据；LocalMind 不允许 agent 自行修改当前用户硬约束。

当前默认 backend 为 Letta Cloud；需要显式选择 local backend。客户端位置、记忆存储位置和推理模型位置分别配置，本地运行客户端不能证明全部推理离线。

### 本地部署、逐轮注入、历史缩短是三个独立问题

Hindsight 支持本地模型与 PostgreSQL，Memobase 可自托管；二者都需要验证模型配置和后台资源开销。claude-mem 默认安装引导的托管观察服务需要与本地 provider 配置区分。Acontext 的本地 quick start 要求模型服务凭据，本轮未验证全离线链路。

[mcp-memory-service Codex 文档](https://github.com/doobidoo/mcp-memory-service/blob/main/codex/README.md) 的事件表没有列出逐次 UserPromptSubmit 检索；标签过滤还存在取回后处理的路径。LocalMind 必须在检索前绑定项目范围，不能用末端过滤替代强制隔离。

## 4 对架构的六项实质加强

| 机制 | 用户收益 | 本项目的具体设计 |
|---|---|---|
| 同步纠正与当前状态 | 刚说过的新要求不会等待后台整理 | 已确定的修订先事务提交；不明确的冲突抑制旧硬约束；用户原话必达 |
| 分层材料与按需展开 | 历史很长也不用整段塞入 | 短索引 → 当前状态/主题概览 → 关键证据；关键否定与参数直接携带原文 |
| 任务所需信息检查 | 不因相似度高而补错背景 | 针对本轮检查目标、适用约束、相关决定或结果；缺失项保留 unknown，不生成假记忆 |
| 来源依赖与失效传播 | 改一条要求时，旧摘要也会失效 | 记录摘要依赖的事件/记忆及版本；修订、删除后派生物失效并按当前来源重建 |
| 注入与原始证据分开 | 旧记忆不会自我复制、越说越真 | generated_context 标记与来源 ID；助手复述不计为独立佐证；只在同一请求内去重 |
| 工作结果评测 | 证明它帮助理解，而不仅是能搜到 | 量测纠正后行为、指代命中、证据覆盖、串线、实际请求、端到端耗时和用量 |

这些是针对本产品的架构取舍，不宣称为独创算法。具体协议和失效流程见 ARCHITECTURE.md 第 13–16 节。

## 5 复用路线与停止条件

保留 Python + SQLite 的事件/状态核心和 Context Compiler。建立一个 MemoryBackend 接口，后端只接收允许写入的事件并返回带来源的候选，不能接管用户纠正和权限范围。

顺序是：先跑规则基线，再评估本地小模型；如仍存在难以解决的检索或整理问题，先试一个 OpenViking 适配器。若瓶颈是带证据的经验整合，再试 Hindsight。Mem0 SDK 是更轻量的候选；不要求首版集成所有后端。

后端缺少严格前置过滤时，用独立项目索引/库隔离，并验证全流程不会接触其他项目数据；做不到就不进入该项目的真实运行。后端返回没有来源的摘要，可以作为检索线索，不能直接成为有效要求或已验证事实。不可局部遗忘或导出时，明确阻塞相应产品能力，不能用“接口有 delete”代替删除传播验证。

如果现成后端在一致评测中没有改善质量或开发成本，继续采用轻量实现。不是为了使用开源框架而增加运行服务。

## 6 当前许可记录

| 项目/组件 | 当前官方声明 |
|---|---|
| Mem0、Letta Code、Graphiti、claude-mem、mcp-memory-service、Memobase、Acontext 核心仓库 | Apache-2.0；托管功能、品牌或附属资源需按实际组件判断 |
| Hindsight | MIT |
| OpenViking 主项目/框架适配器 | 主项目 AGPLv3；适配器包有各自声明 |
| OpenViking crates/ov_cli、examples | Apache-2.0；Hermes 示例保留 MIT |

OpenViking README 还列有整体重写及大规模跨语言翻译的声明，因此不能仅凭仓库标签判断全部复用范围。此表用于选型记录；引入时保存锁定源码与完整许可。本次只参考公开机制，未做代码复制或许可证适用性法律判断。

## 7 进入开发的目标

用一个真实可控入口证明：继续项目时少解释；更改要求后跟得上；相关证据可找回；无关历史不持续增长。详细任务已合并 TASKS.md，评测计划见 EXPERIMENT_PLAN.md。当前仍是设计交付，未完成产品实现。
