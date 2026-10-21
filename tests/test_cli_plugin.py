"""Real subprocess capture and generated MCP launch configuration."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "tools" / "run_mcp.py"


class CLIPluginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = [sys.executable, str(LAUNCHER), "--db", str(Path(self.directory.name) / "state.sqlite"),
                     "--project", "synthetic", "--task", "task"]

    def tearDown(self):
        self.directory.cleanup()

    def run_cli(self, *args):
        return subprocess.run(self.base + list(args), cwd=self.directory.name,
                              capture_output=True, text=True, timeout=8)

    def test_remember_retries_and_pause_without_generating_fake_answers(self):
        self.assertEqual(0, self.run_cli("init").returncode)
        command = ("remember", "缓存有效期24小时", "--require", "ttl=24小时", "--dedupe-key", "synthetic-1")
        first = self.run_cli(*command)
        retry = self.run_cli(*command)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(retry.stdout))
        self.assertTrue(json.loads(first.stdout)["captured"])
        self.assertFalse(json.loads(first.stdout)["model_called"])
        state = json.loads(self.run_cli("inspect").stdout)
        self.assertEqual(1, len(state["events"]))
        self.assertEqual("user", state["events"][0]["actor"])
        self.assertIsNone(state["state"]["last_result"])
        self.assertEqual(0, self.run_cli("pause").returncode)
        denied = self.run_cli("remember", "暂停秘密", "--dedupe-key", "paused-1")
        self.assertEqual(2, denied.returncode)
        self.assertNotIn("暂停秘密", self.run_cli("inspect").stdout)

    def test_generated_config_launches_from_different_working_directory(self):
        self.assertEqual(0, self.run_cli("init").returncode)
        self.assertEqual(0, self.run_cli("remember", "只读记录", "--dedupe-key", "record-1").returncode)
        config = json.loads(self.run_cli("plugin-config", "--format", "json").stdout)["mcpServers"]["localmind"]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "synthetic", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "localmind_search", "arguments": {"query": "只读记录"}}},
        ]
        result = subprocess.run([config["command"]] + config["args"], cwd=self.directory.name,
                                input="".join(json.dumps(m) + "\n" for m in messages),
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertFalse(replies[-1]["result"]["isError"])
        self.assertEqual("只读记录", replies[-1]["result"]["structuredContent"]["results"][0]["preview"])
