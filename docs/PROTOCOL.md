# LocalMind 运行协议 0.3

冻结日期：2026-10-21。本文规定内部数据语义及适配器必须遵守的边界；它不是厂商请求协议。实现覆盖、真实接入及性能只以测试记录和 `capability-report.json` 为准，不能因某字段已定义便宣称该功能已接通。

`contracts/runtime.schema.json` 的 `$defs` 提供结构契约。JSON Schema 只能验证形状；来源、范围、当前版本、依赖闭包和生命周期必须由存储层与编译器联合验证。未通过语义校验的包不得发送。

## 1. 身份与范围

| 字段 | 含义 |
|---|---|
| `project_id` | 预先注册的授权项目，不能由模型输出扩大 |
| `task_id` | 项目内持续目标，跨会话保留 |
| `logical_session_id` | 可见会话，同任务下可以更换 |
| `turn_id` | 一次已提交的用户输入 |
| `request_id` | 一次实际发送尝试；结果去重需要它与 `message_id` |
| `context_id` | 本轮编译结果身份，绑定原话、来源与 snapshot |
| `remote_thread_id` | 供应商线程，与本地任务和会话分别管理 |
| `event_id` | 原始事件身份；摘要、回声不能产生独立用户依据 |

`scope` 固定为 `{project_id, task_id, logical_session_id}`。读权限来自调用端已绑定的 scope，不来自模型提交的字段。默认检索同一项目和任务；跨会话的同任务必要问答可读。项目要求可以适用于项目内各任务；全局偏好必须经过显式全局授权。P0 只接受单独一项 global 更改，且其 value 必须等于本轮完整原话；该事件显式标记 global 才能跨项目读取。混有其他项目信息的原话不能借此整体升为全局。把 `scope_level` 改成 `global` 不会自动授权读取其他项目完整原文。

工作区路径仅由注册信息关联项目。当前目录、文本中的项目名称或模型猜测均不能代替授权绑定。P0 没有多用户共享库、跨项目联合检索或隐式扩权接口。

## 2. 六个契约

| 契约 | 最小责任 |
|---|---|
| `Event` | 原话、真实 actor/origin、幂等键、接收顺序、时间与根来源 |
| `TaskState` | 当前有效事项、未决冲突和最后可用回答；不是世界事实全集 |
| `TurnRun` | 输入事件、编译 snapshot、请求/线程身份、phase、freshness 与结果状态 |
| `ContextPacket` | 当前原话、适用要求、必要近期问答、证据、未知项和预算 |
| `Checkpoint` | 有来源的本地稳定边界快照；当前可保存和读取，不假定已实现备份导入 |
| `Handoff` | 只读子范围、目标、证据和预期产物；预留，不包含调度或工具授权 |

当前可执行 API 使用普通 JSON 兼容字典，事件或表行不必重复包裹 envelope；Event、TaskState、TurnRun、ContextPacket 和 Checkpoint 使用 `schema_version: "0.3"`。预留 Handoff 导出契约声明 `protocol_version: "0.3"` 和 `implementation_status: "reserved"`。数据库迁移编号与协议版本是两个概念。

| 契约 | 结构字段与约定 |
|---|---|
| Event | `event_id/seq/dedupe_key/scope/scope_level/actor/origin_kind/raw_content/content_hash/source_event_ids/root_source_ids`；直接依赖与独立根来源分别保存，不能只保留根来源而漏掉中间事件删除；`parent_event_id/turn_id/generated_context_id` 关联来源；发生与导入时间分别记录 |
| TaskState | `scope/revision/subjects/pending_conflicts/last_result`；subject revision 按事项递增，不靠文本相似度猜是否新版本 |
| TurnRun | `input_event_id/snapshot/phase/freshness/request_id/attempt_id/context_id/remote_thread_id`；`packet/messages` 属于遗忘范围；`usage/error/outcome` 不填造未知统计 |
| ContextPacket | `original_request/active_constraints/recent_key_messages/reference_anchors/relevant_memories/evidence_excerpts/uncertainties/budget/dependency_checks`；完整审计字段不全部外发 |
| Checkpoint | `scope/snapshot/source_event_ids/subjects/last_result/pending_conflicts/recent_event_ids/workspace_snapshot/created_at`；goal 包含在 subjects，近期问答及有序方案通过原始事件引用保留，不合成用户选择 |
| Handoff | `parent_turn_id/goal/scope/allowed_read_scopes/checkpoint_id/checkpoint_snapshot/constraints/source_event_ids/expected_artifact`；capabilities 固定 `memory.read`，子范围不得超过父授权 |

有效事项固定携带 `subject_key`、`value`、`scope_level`、`kind`、`source_event_ids`、`effective_revision`、`authority` 和 `status`。`kind` 为 goal / requirement / decision / fact；这只是本地分类。`fact` 的用户来源只证明用户曾陈述，不证明外部事实已验证。模型输出不能自行指定 `user_stated` 来获得用户权威。

## 3. 提交、纠正与状态屏障

原话只在采集 active 时提交。一次接收事务完成以下操作：检查幂等键、持久化用户 Event、建立 TurnRun、应用明确事项更新、增加受影响范围/事项版本、屏蔽旧结论及依赖物。每个新用户输入也增加 task revision，使较早 snapshot 不能在跨会话并发或迟到时写回当前状态。事务成功后才可报告“已保存”。同键同请求返回原 TurnRun；同键不同原文或更改不能悄悄覆盖，应拒绝为幂等冲突。

显式更改接口接收已定位的 `subject_key`，来源固定为本轮真实用户事件。P0 同一接收事务的全部更改必须处于同一个 scope_level，不将包含任务私有信息的事件整段升为项目或全局。自然语言解析不能假定总能确定修订目标：可定位冲突集合时记录 `pending_conflicts` 并抑制该集合；完全无法定位时保留原话并记录 unknown。不得声称完成修订。影响答案的歧义由回答入口要求澄清。

`snapshot` 绑定：

- `scope_revisions`：global、project、task 三类当前版本；
- `policy_revision`：授权/应用策略版本；
- `capture_epoch`：暂停、恢复屏障；
- `deletion_epoch`：删除屏障；
- 必要来源及 `subject_key` 的 `effective_revision` 依赖。

发送入队、实际调用适配器、回答写入、后台派生物提交与展开读取复用同一有效性判定，不能只比较 task 版本。范围版本用于阻断过时运行；派生物有效性还要逐项检查来源及事项依赖，以免无关修订使所有长期材料永久失效。

本地事务不能覆盖远端网络。最终检查到远端接收之间仍存在纠正竞态；已经发送的运行标记 superseded，并按适配器实际能力中断或等待丢弃，不能宣称撤回远端材料。

## 4. phase 与 freshness 分开

| 维度 | 值 | 规则 |
|---|---|---|
| phase | received、compiled、sent、completed、failed、cancelled、outcome_unknown | 描述发送生命周期；received 表示接收事务已提交 |
| freshness | current、superseded、forgotten | 描述结果是否还适用于当前状态 |

正常路径是 received → compiled → sent → completed。发送前失败可进入 failed/cancelled；发送后无法判断远端结果时进入 outcome_unknown。superseded 的回答不能更新 `last_result`；暂停或删除屏障后的回传不再保存原文或派生物。

0.4 开发版补充输入采集路径：`remember` 使用 received → completed，`outcome=captured`，不生成 request、packet、message 或 assistant Event，也不更新 last_result。相同幂等键重复采集返回已有结果。这是开发中 0.3 契约的扩展；SQLite 表结构不变，已有 0.3 数据无需迁移，新导出须使用配套 Schema 验证。

P0 单进程串行处理普通前台回合，存储层只允许单写入者；不在数据库事务内等待模型。明确纠正先提交，可以使旧回合过时。流式片段是展示缓存，只有最终消息可按 `(request_id, message_id)` 幂等落库；同一最终消息身份还须核对 `result_hash`，不能接受内容不同的重试。丢弃的旧回传至多保留白名单数值用量与结果 hash，不保留正文。

重启保留已提交输入；sent 且没有可靠最终状态的运行恢复为 outcome_unknown。received/compiled 的未发送回合结束为 cancelled，错误码 RESTART_UNSENT_INTERRUPTED，以免锁死后续输入；同一幂等键不自动重新发送，需要继续执行时使用明确的新键。不自动重发，不把恢复、fork 或重新加载误称为干净线程。检查点不包含隐藏思维、待执行外部动作或孤立 tool_call_id。

`last_result` 仅表示本轮仍新鲜的最终助手回答，必须保留其 assistant 身份。它不等于工具验证成功。实际观察结果需要 tool_result 来源、命令/条件及工作区版本，不能从助手“测试通过”自动产生。

## 5. 编译及真实消息映射

编译顺序为：授权和生命周期过滤 → 当前有效状态 → 必要近期问答 → 有界规则召回 → 最多一次可选本地模型建议 → 一次确定性补检索 → 依赖校验 → 预算 → 再次检查 snapshot。

当前用户原话必须作为最后一个 user 消息逐字保留。固定应用规则可以进入 system；历史要求、文件、摘要、网页与检索证据作为有边界的数据传递，不提升为 system/developer 指令。近期真实 user/assistant 问答保持身份，助手提议在用户确认之前仍是提议。原生 Hook 将来采用 additive 模式时不能重复注入宿主已有的当前用户输入。

`active_constraints` 只允许有可读真实用户来源、active 状态、当前事项版本及适用范围的条目。inferred、candidate、过时版本、generated_context 或无源条目必须拒绝。每条证据要能重新展开和验证，标签/转义不构成提示注入已解决的证明。

硬要求证据保留来源事件全文，不能靠关键词启发式截取“允许修改”等子串而丢掉前提。`evidence_excerpts` 的 status 为 current_source / historical_source，并用 `effective_subjects` 精确列出它仍支持的当前事项及版本。同一旧事件部分事项被纠正后，其旧文字只作历史依据；不在 effective_subjects 的旧结论不能重新变成当前要求。

可选 `relevant_memories` 分为原始事件摘录与派生候选。前者保留 actor / origin_kind，后者明确携带 inferred / candidate；检索命中或来源有效不意味着候选结论已确认。适配器必须连同这些身份字段传递，不能只留下没有来源身份的正文。

“选第二个”必须保留最近有关方案的原文与顺序。确认序数决定时同步固定事项的 `reference_event_id`；不能在每次编译时重新选择当时之前最近一个仍可读的助手回答。`reference_anchors` 绑定该原始助手方案、选择事项与用户选择来源；锚点标记 `historical_proposal` / `assistant_proposal`，不能因保留历史方案而恢复其已被纠正参数。近期窗口有数量及字节界限，不把完整历史链传到下一轮。当前请求无法找到指代目标时标记 unknown；已确认为决定却缺失固定提议锚时编译失败，不回退到更早的其他方案，也不能根据选项编号编造内容。

每个实际请求内按来源去重；下一干净回合重新携带仍必要的约束。捕获回声依据真实 origin、已登记 context_id 及内容 hash；用户文本恰好包含同名标签不能被当回声删除。

## 6. 依赖和预算

派生物必须绑定原始 event 来源，以及相关事项的 effective_revision。纠正保留旧原话用于明确历史查询，同时屏蔽旧结论和依赖摘要进入当前输入。删除则屏蔽原文及全部依赖。

`requires` / `qualifies` 表达前提和例外；P0 将两者共同编码为事项 `dependencies: [subject_key, ...]`，都视为不可省略条件，不实现不同关系的自动推理。展开必须保留完整闭包，设置深度/数量上限并检测环路。例如“允许修改”不能离开“备份已验证可恢复”。硬要求缺依赖、闭包超限或装不下时失败；可选材料整体舍弃，不能只保留结论。

字节预算按实际受控消息的 UTF-8 序列化结果计数；tokens 没有供应商 tokenizer 时明确标估算。优先级为当前原话、有效关键要求、必要近期问答、证据、可选概览。必需内容超过预算返回 `BUDGET_INSUFFICIENT`，不能静默截断原话、数字、否定或条件。供应商完整窗口、隐式系统指令和真实 token 使用量未暴露时均为 unknown。

实测修订：内部原话检索不再只取开头512字符；内部检索上限16384字符，公共MCP搜索仍为512字符。旧完整问答单独造成超预算时，明确不依赖前文的新问题可省去该问答；包含指代提示的续问继续保留完整前件并明确失败，不截断成可能误解的片段。该判断为词面启发式，未承诺完整自然语言理解。

本地规划器只可建议已有候选 ID、查询词或 unknown；不能修改来源、硬约束、范围或原话。超时、非法 JSON、虚构引用及结构正确但含义错误时回退规则基线，不增加第二次模型调用。

## 7. 暂停、遗忘与备份

pause 原子提交 paused 状态和 capture_epoch 屏障，阻止新输入、回答和后台写入；P0 持久态仅 active / paused，不保存异步 pausing 中态。恢复增加 epoch，不补采暂停期间内容。P0 没有悄悄旁路的无记忆直通模式。暂停只控制 LocalMind，不代表宿主或云端删除记录。

forget 先提交删除标记与 deletion_epoch，使原文读取、候选、派生、缓存、待发包和晚到回传立即不可用，再清理数据库中的内容副本。范围包括事件原文、摘要、检查点、审计原文、实际请求副本和关联回答。保留必要 ID/hash/状态，用墓碑阻止同内容未授权再导入。已经外发的材料无法本地撤回，下一回合使用干净线程。

逻辑遗忘不等于 SSD、SQLite 旧页、云端或外部备份的物理擦除。物理清理能力需独立报告，不因逻辑回放通过而声称彻底擦除。

备份恢复必须先取得并合并比备份更新的删除清单，再导入存活内容。没有当前墓碑时恢复应阻塞或进入隔离检查，不允许旧快照复活已删除材料。P0 未实现的备份导入接口不因本条存在而变为可用。

## 8. 0.2 → 0.3 迁移

旧 `context_packet.schema.json` 和示例保留为 0.2 设计资料，不能直接复用为当前外发包或硬要求来源。0.3 不提供仅改版本号的迁移。

原始历史只有保留可追溯 actor、消息身份、scope 和真实来源才可导入为对应 Event；普通文件中的“用户同意”保持 imported_source。重新核对当前授权、墓碑、事项修订链及所有依赖后，才可重建 TaskState 和新包。无法证明的旧摘要降为 candidate/unknown。缺字段与 provenance 的旧包不能补造证据。

协议新增必需字段或改变语义时升级版本；读取端对未知版本拒绝写入及外发。适配器根据锁定客户端版本单独映射，不把内部 schema 发给供应商当作原生请求。
