# 项目 MCP 配置

在当前机器的源码目录运行 `python3 -m localmind --project my-project init`，再运行 `python3 -m localmind --project my-project plugin-config --format codex`。通用客户端使用 `--format json`。生成器依照当前 Python、源码和数据库位置输出绝对路径；移动目录后重新生成。宿主加载后才算启用。

0.5 优先使用 localmind_recall(query,budget_bytes=8192)，state/search/expand 用于完整状态、诊断或定点展开。预算仅包括补充，不包含宿主历史、工具定义和当前输入。

两份机器生成配置被 Git 忽略，不提交个人路径、数据库或凭证。
