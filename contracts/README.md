# 协议草案说明

context_packet.schema.json 使用 JSON Schema Draft 2020-12，定义 LocalMind 内部编译结果，不是任何云端厂商的直接请求协议。context_packet.example.json 是合成示例，来源 ID 和用量均为演示值；policy.example.json 是待实测调整的设计默认值，不能直接当作可运行配置。

适配器负责把用户原话作为 user message 原样发送；原生 Hook 已持有原话时不重复追加。预算和审计字段默认只留本地。

Schema 只能验证结构。程序仍需校验：来源确实存在、摘录匹配、授权范围允许、原始请求无改写、状态与 capture_epoch 有效、删除清单未命中、未决冲突对应旧约束已被抑制、适用条件与预算满足。

active_constraints 的 subject_key 是事项标识，effective_revision 是当前生效版本；source_event_ids 是依据。authority 不能由外部文本自行提升。推测不得进入有效硬约束。pending_conflicts 保留尚未解决的纠正，不靠“新消息放在最后”消解冲突。

调用本地模型时不要求它生成整个上下文包；只让它返回候选引用、查询词、相关性和候选压缩，最终包由程序编译。设计阶段未进行真实模型请求，也未运行完整 JSON Schema 引擎验证；文件解析和必要字段一致性已检查，M1 应补标准验证器与语义校验。

本次内部草案扩充 information_needs：记录本轮所需信息的满足/未知/不适用状态，不输出隐式思维轨迹；unknown 不得生成事实补齐。excerpt 可携带 origin_kind、root_source_ids 与 depends_on_ids；这些字段必须与真实 Event/SourceDependency 匹配，模型不可自行声明更高权威。重复助手陈述不能增加 root source 数量。字段存在仍不等于语义正确。协议尚未稳定，真实开发时冻结正式版本。
