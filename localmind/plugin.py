"""Generate reviewable local MCP configuration without editing host settings."""
from __future__ import annotations

import json
from pathlib import Path
import sys


def configuration(database, scope, format="codex"):
    launcher = Path(__file__).resolve().parents[1] / "tools" / "run_mcp.py"
    database = str(Path(database).expanduser().resolve())
    args = [str(launcher), "--db", database, "--project", scope["project_id"],
            "--task", scope["task_id"], "--session", scope["logical_session_id"], "mcp"]
    command = str(Path(sys.executable).resolve())
    if format == "json":
        return json.dumps({"mcpServers": {"localmind": {"command": command, "args": args}}},
                          ensure_ascii=False, indent=2)
    if format != "codex":
        raise ValueError("Unsupported configuration format")
    return ("[mcp_servers.localmind]\ncommand = " + json.dumps(command, ensure_ascii=False) +
            "\nargs = " + json.dumps(args, ensure_ascii=False) +
            '\nenabled = true\nenabled_tools = ["localmind_recall", "localmind_state", "localmind_search", "localmind_expand"]\n')
