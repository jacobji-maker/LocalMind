# LocalMind 0.5 运行与开发说明

给开发者和想深入试用的朋友：这里保留完整命令、模块分工和接入边界。项目介绍见 [首页](../README.md)。

本地保存真实经历和当前事项，每轮重建有来源的必要上下文。已实现 SQLite 状态核心、规则编译器、命令行、回放、Ollama 适配器和只读 MCP 插件入口。真实接入状态见 [能力报告](capability-report.json)，测试证据见 [验证记录](validation-report.json)。

0.5 将内部审计与模型输入分开，自控入口和 MCP 使用共享投影；新增一次预算化 `localmind_recall`，按来源去重原文，并为明确编号选择提供有双来源依据的具体选项。详见 [架构修订](ARCHITECTURE_0.5.md)。自然纠正、同义召回及原生 Codex 自动捕获仍待完善和验收。

同一 11 个开发场景已真实复测：MCP 从 8/11 改善到 11/11，编译上下文从 9/11 到 10/11，完整有效历史仍为 11/11。MCP 输入仍为本轮完整历史的 1.475 倍；核心仍在同义问法 C09 失败。详见 [0.5 对照报告](contrast-report-0.5.json)。这是已见短历史开发集合，尚未证明未见场景、长历史或原生宿主中的总体收益。

## 插件入口

0.4 首轮真实本地对照作为历史证据保留（2026-10-21）：11个合成开发探针，55次推理含暖机。完整有效历史11/11、近期历史9/11、LocalMind编译上下文9/11、当时固定MCP补充策略8/11。该轮没有显示总体收益，并暴露方案指代、语义召回和材料膨胀问题。逐条原文和用量见 [0.4 对照报告](contrast-report.json)，运行方法见 [评测说明](../eval/README.md)。该轮修复了3个核心问题，88项开发回归通过；0.5 回归数为100。

```bash
python3 -m localmind --project my-project init
python3 -m localmind --project my-project remember '缓存有效期2小时' --require ttl=2小时 --dedupe-key ttl-1
python3 -m localmind --project my-project plugin-config --format codex
```

配置生成器输出固定项目范围的 MCP 配置，不修改宿主设置。正常补充优先调用一次预算化 recall，另保留当前状态、历史搜索和原文展开；采集使用用户 CLI，不由模型自行声明为用户要求。宿主尚未安装验收，也没有自动采集每轮聊天。完整步骤与边界见 [插件使用说明](PLUGIN.md)。

## 直接运行

需要 Python 3.9+，核心无第三方依赖。在仓库根目录执行：

```bash
python3 -m localmind demo
python3 -m unittest discover -s tests -v
python3 -m localmind --project my-project init
python3 -m localmind --project my-project chat --model qwen3.5:9b
```

前两个命令不调用真实模型。最后一个使用本机 Ollama，不自动下载模型。真实推理可用性与延迟以能力报告为准。也可用 `demo --backend ollama` 跑真实本地演示，它仍不属于云端验收。

聊天中可以正常输入，以下命令把明确要求同步提交为当前状态：

```text
/goal 做一个本地缓存
/require cache_ttl=24小时
/correct cache_ttl=2小时
/decide storage=文件
/inspect
/pause
/resume
/forget evt_实际事件ID
/quit
```

普通输入保留原话；当前版本不保证自动定位任意自然语言中的修订目标。显式事项命令保证已定位纠正立即生效，不把未知目标假称为已经正确修订。`inspect` 可查看事件 ID 和当前事项。暂停期间不保存新输入，也不启动增强回答。遗忘清理本地数据库内相关原文、直接及间接派生物、请求副本和回答；不宣称撤回云端数据或擦除外部备份。

逐次调用：

```bash
python3 -m localmind --project my-project ask '缓存有效期为2小时。' --require cache_ttl=2小时
python3 -m localmind --project my-project --session next-session ask '继续，当前有效期是多少？'
```

`--db`、`--project`、`--task`、`--session` 放在子命令之前。默认数据位于运行命令时的工作目录 `.localmind/`。同一数据库只允许一个进程持有；聊天运行时不要另开进程改同库。跨会话保持 project/task，session 可以变化。

需要本地模型辅助选择时显式加 `--planner-model 模型名`；每轮最多一次候选选择，默认超时 5 秒，失败退回规则。现有模型是否足够快须看实测，不默认启用。回答与规划的用量分别记录，不自动增加云端调用。仅已核验的具体模型可被报告为本地推理；本机服务地址或模型别名本身不构成离线证明。

## 当前实现

| 模块 | 职责 |
|---|---|
| `memory.py` | 幂等事件、事项版本、范围、暂停与删除代次、来源依赖、检查点、恢复 |
| `context.py` | 规则召回、必要问答与选项锚点、来源验证、条件闭包、消息字节预算 |
| `runtime.py` | 接收、编译、发送复核与回答提交，未知远端结果不自动重发 |
| `adapters.py` | 明确标记的 Replay，以及无工具执行器的 Ollama 请求 |
| `models.py` | 一次有界候选选择；模型不能修改用户权威或授权范围 |
| `cli.py` | 注册、聊天、查看、修订、暂停、恢复、遗忘及合成演示 |
| `retrieval.py` | 固定授权范围的历史扫描、来源展开和完整条件校验 |
| `projection.py` / `choices.py` | 共享模型视图、完整来源去重、严格编号选择解释 |
| `mcp_server.py` / `plugin.py` | 四个只读工具（含 recall）、stdio 协议和本机配置生成 |
| `codex_adapter.py` | 限时客户端诊断；未核验执行边界时拒绝云端发送 |

正式内部协议是 [0.3 运行契约](PROTOCOL.md)、[关键架构决策](DECISIONS.md) 和 [runtime.schema.json](../contracts/runtime.schema.json)。旧 `context_packet.schema.json` 是 0.2 历史草案，不能直接导入为当前要求。结构通过仍须程序语义验证。

当前没有自主电脑执行、原生 Hooks、向量后端、持续后台推理、备份导入、多设备同步；Handoff 仅预留。检查点支持有版本的存取，不重放外部动作。公开的 42 条旧场景仍是开发种子，当前测试单独报告，不能混称隔离验收通过。

预算精确限制受控应用消息的 UTF-8 序列化字节数，token 是估算；供应商隐式内容和完整模型窗口另验。每轮不携完整历史，保留必要问答；已确认的“第二个”绑定历史选项原文。历史提议和已修订参数不自动恢复为当前要求。

## 接续开发

先读 [TASKS.md](../TASKS.md)、能力与验证报告，再按需要读原始 [HANDOFF.md](../HANDOFF.md)、[CONTEXT_FLOW.md](../CONTEXT_FLOW.md)。原交接文件保留历史状态，最新可执行行为以 0.3 协议及验证记录为准。

下一优先级是补齐受支持 Codex 入口的实际消息、认证与工具边界验证，完成真正云端闭环，再比较规则基线与本地小模型净收益。不静默换成付费 API，不读取或复制认证文件，不修改全局配置。
