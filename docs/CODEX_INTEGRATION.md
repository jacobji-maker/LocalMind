# Codex 接入核验记录

日期：2026-10-21。可执行客户端已确认；真实云端回答仍未通过验收。只读 MCP 插件是独立接入路径，不能替代下面的自控云端验收。

## 本次纠正了此前的路径错误

`/Applications/ChatGPT.app/Contents/Resources/codex-cli` 是目录。此前对这个目录执行 `--version` 得到 EACCES，不能证明实际 CLI 不可调用。

根据目录内 `codex-package.json` 的公开入口，正确启动器为：

```text
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex
```

该启动器实际返回 `codex-cli 0.163.0-alpha.10.2`；`app-server --help`、`app-server generate-json-schema --out <临时目录> --experimental` 均成功。没有修改应用权限、安装客户端或读取认证文件。

## 真实启动边界

以独立子进程启动公开 App Server stdio 接口，先只发 `initialize`，成功后才允许 `account/read(refreshToken=false)`。本机两个启动模式都在 initialize 返回前退出：

- 默认 SQLite 状态目录不在当前可写范围内，初始化失败。
- 用官方配置项 `sqlite_home`、`log_dir` 将该次调用的状态与日志放进临时目录后，仍以 EPERM、exit code 1 退出。
- 普通模式和 `--no-daemon` 都各自设定四秒上限，并回收子进程。没有绕过沙箱、修改全局配置或复制登录状态。

因此认证类别、干净线程、真实模型请求和工具执行边界仍为 unknown。没有发送 `thread/start` 或 `turn/start`，云端模型调用数为零。这只是当前执行环境的结果，不能推断用户所有安装方式均不可用。

## 已核验的协议字段及限制

从实际版本生成的 schema 可确认：`thread/start` 支持 `ephemeral`、`dynamicTools`、`environments`、`config` 和 `developerInstructions`；`turn/start` 支持 `input`、`additionalContext` 与环境覆盖。这些字段为后续实现提供了明确契约。

但空动态工具列表、空环境列表、禁用 shell/apps 等配置，均不能单独证明所有 action 工具被关闭。协议中 `disabledPluginIds` 的说明还明确写着它尚不用于过滤插件能力。本次没有找到可返回最终模型工具目录的 RPC 或全局工具白名单，故没有为通过演示而放开真实云端请求。

新增 `localmind.codex_adapter.probe_codex()` 提供可重复的有界诊断：只返回版本、初始化状态、认证类别白名单及失败标签；不返回 stderr、邮箱或账户载荷。诊断从不创建线程、不刷新 token、不代理服务端动作请求。

`CodexAppServerAdapter.send()` 当前明确拒绝 `CODEX_TOOL_BOUNDARY_UNVERIFIED`。即使模拟诊断通过，也不能设置一个布尔开关就启用云端发送。后续要先实现并验证对应版本的工具边界，再接入回答运输。

## 原生 MCP 配置依据

官方 [Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference) 确认 `mcp_servers.<id>.command`、`args`、`cwd`、`enabled` 与 `enabled_tools` 为受支持配置项。生成项目配置片段可以独立完成；配置格式核验不代表当前宿主已加载插件。是否自动在每轮读取、能否改写已有历史，需要分别验收。

## 后续云端退出条件

先解决受支持 App Server 在当前沙箱中的启动；仅通过 `account/read` 记录认证类别。随后在隔离合成工作区验证新线程与消息映射，并取得 shell、搜索、MCP、Apps/插件、动态工具的实际限制证据。LocalMind 不向模型转发 `command/exec`、`process/spawn`、`thread/shellCommand` 等任意宿主 RPC。完成后才开始合成云端回合，分别记录应用消息、宿主历史及最终模型请求的可观测范围。
