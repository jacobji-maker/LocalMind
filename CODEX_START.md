# 接手 LocalMind 0.5

先读 README.md、docs/ARCHITECTURE_0.5.md、docs/PROTOCOL.md、docs/DECISIONS.md、docs/capability-report.json、docs/validation-report.json 与 TASKS.md。原始需求在 HANDOFF.md、CONTEXT_FLOW.md 和 INTEGRATION.md；这些文件保留历史时点，不能覆盖最新测试和接入事实。

0.5 已实现共享模型投影、单次预算化 MCP recall 与严格编号解引用，本轮100项回归通过。真实复测同一11个开发场景：完整历史11/11、近期9/11、编译上下文10/11、MCP11/11；56次本地调用含暖机，MCP输入仍为完整历史1.475倍。证据在docs/contrast-*-0.5.json。0.4原始分数和失败记录保留，不能把已见短历史集合的改善当作未见或长历史验收。

运行 `python3 -m unittest discover -s tests -v` 和 `python3 -m localmind demo`。核心只有 Python 标准库依赖。不要把 Replay 的脚本回答记成真实模型输出，不把本地 Ollama 接通记成 Codex 云端接通；不要直接把旧 0.2 包导入为当前协议。

继续前核对 capability-report 的 unknown/阻塞。保持原话、范围、纠正、暂停/删除屏障、直接和根来源依赖、条件、幂等及未知远端状态。单库只允许一个进程持有。不改全局配置、不读凭证、不下载大型模型或静默切换付费 API。

Git 提交和推送按用户明确授权执行；用户本轮已授权开始提交，目标为 `jacobji-maker` 的公开 LocalMind 仓库。只提交项目源码、设计及合成测试证据，排除数据库、凭证和本机个人环境配置。认证或建仓仍阻塞时保留本地可审查提交，不能宣称已经上传；远端操作成功后核验分支和 commit。后续超出该项目或改变仓库可见性仍按新的授权范围处理。
