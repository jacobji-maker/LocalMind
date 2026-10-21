# LocalMind 0.5 插件开发版

提供固定项目范围的本地 MCP 记忆入口，Python 3.9+，无第三方运行依赖。它可接入支持本地 stdio MCP 的宿主，尚未在当前 Codex 客户端内安装或验收。

## 启动步骤

在 LocalMind 目录执行：

```bash
python3 -m localmind --project my-project init
python3 -m localmind --project my-project remember '缓存有效期24小时' --require ttl=24小时 --dedupe-key ttl-1
python3 -m localmind --project my-project remember '缓存有效期改为2小时' --require ttl=2小时 --dedupe-key ttl-2
python3 -m localmind --project my-project inspect
python3 -m localmind --project my-project plugin-config --format codex
```

`remember` 保存实际传入的用户原话与显式事项，不调用模型。重试相同输入使用同一幂等键；新输入使用新键。`--decide key=value`、`--goal 内容` 同样可用，值必须出现在原话中。普通自然语言也可采集和搜索，但任意纠正尚不能自动解析成事项修订。MCP 不提供采集工具，模型不能把自己的提议写成用户确认。

`plugin-config` 输出绝对路径配置：Codex 用 `--format codex`，通用 MCP 用 `--format json`。生成器不修改宿主设置。移动源码或更换 Python 后重新生成。接入其他项目时使用不同 project/task 或独立数据库。

手动启动：`python3 -m localmind --project my-project mcp`。该命令仅输出 MCP JSON-RPC，由宿主从标准输入发送请求，EOF 后退出。未初始化数据库时工具返回不可用，不创建空项目。

## 可调用的工具

| 工具 | 参数 | 返回内容 |
|---|---|---|
| `localmind_recall` | `query`，可选 `budget_bytes` 1024–32768，默认8192 | 推荐单次补充：去重原文、有效事项和编号解析；预算仅覆盖补充 |
| `localmind_state` | 无 | 当前有效事项、完整用户依据、历史方案引用和未决冲突 |
| `localmind_search` | `query`，可选 `limit` 1–20 | 当前可用历史的预览与来源 ID，超过最近 200 条的记录也可召回 |
| `localmind_expand` | `event_id` | 完整原文、角色、来源及仍有效事项；旧依据标为 historical_source |

搜索预览最多 512 字符，可能省略前提。依赖条件或许可时先展开；展开和状态返回超出预算会失败，不截断关键原文。搜索使用中文双字片段和字面关键词、流式扫描数据库；没有向量索引或大规模性能保证。

服务启动时固定 database/project/task/session，模型参数不能改范围、路径、角色或权威。显式全局声明按既有协议可读，普通项目消息不跨项目共享。来源文本是历史资料，助手回答不等于已验证执行结果。

## 暂停与遗忘

```bash
python3 -m localmind pause
python3 -m localmind resume
python3 -m localmind --project my-project forget evt_实际来源ID
```

暂停作用于该数据库。服务每次工具调用后释放数据库，CLI 可在两次读取之间控制状态，下一次调用即生效。不缓存结果、不保存查询词；数据库繁忙时返回不可用，宿主稍后重读。已返回宿主的内容无法由本地遗忘撤回。

## 验证与接入边界

测试使用真实 Python 子进程验证 initialize、initialized、tools/list/call 和 EOF，覆盖同一服务中的暂停、纠正与遗忘。生成的实际命令也从不同工作目录启动。它们是本地协议集成测试，未冒称真实 Codex 插件验收。

MCP 调用时机由宿主控制。当前没有每轮自动捕获 Hook，也不能删除宿主已有历史。自控 CLI 的 Ollama 回答入口仍可用。Codex App Server 正确可执行路径已确认，但初始化仍在当前沙箱中失败，见 [接入核验](CODEX_INTEGRATION.md)。`codex-check` 提供限时诊断，不启动云端模型回合。

协议依据：[MCP stdio](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)、[生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)、[Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)；Codex 字段依据 [Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)。官方格式核验不代表当前宿主已启用插件。

0.5 以 recall 为默认读取入口，state/search/expand 保留作诊断和定点展开。recall 不输出 snapshot/epoch 等内部审计，不采集 query；完整条件装不下时失败，不裁掉前提。此预算不包含宿主历史、工具定义或当前输入。
