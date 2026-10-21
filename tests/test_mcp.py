"""Actual stdio framing and source boundaries, without calling any model."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from localmind.adapters import ReplayAdapter
from localmind.mcp_server import MAX_INPUT_BYTES, MCPServer, PROTOCOL_VERSION, serve
from localmind.memory import Store
from localmind.runtime import Runtime


def initialize(request_id=1, protocol=PROTOCOL_VERSION):
    return {"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {
        "protocolVersion": protocol, "capabilities": {},
        "clientInfo": {"name": "synthetic-test-client", "version": "1"}}}


def call(name, arguments=None, request_id=2):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
        "name": name, "arguments": arguments if arguments is not None else {}}}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "memory.sqlite"
        self.scope = {"project_id": "project-a", "task_id": "task", "logical_session_id": "s"}
        with Store(self.database) as store:
            store.register_project("project-a")
            store.register_task("project-a", "task")
            original = Runtime(store, ReplayAdapter(["合成回答"])).run(
                self.scope, "缓存有效期24小时", "source", changes=[{
                    "subject_key": "ttl", "value": "24小时", "kind": "requirement"}])
            self.source_id = original["packet"]["original_request"]["event_id"]
            store.register_project("project-b")
            store.register_task("project-b", "task")
            foreign_scope = dict(self.scope, project_id="project-b")
            foreign = store.receive(foreign_scope, "外项目机密unique", "foreign")
            self.foreign_id = foreign["input_event_id"]
        self.server = MCPServer(self.database, self.scope)
        self.server.dispatch(initialize())
        self.server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tearDown(self):
        self.directory.cleanup()

    def test_lifecycle_negotiation_and_only_read_tools(self):
        fresh = MCPServer(self.database, self.scope)
        early = fresh.dispatch(call("localmind_state"))
        self.assertEqual(-32000, early["error"]["code"])
        result = fresh.dispatch(initialize(protocol="future-protocol"))["result"]
        self.assertEqual(PROTOCOL_VERSION, result["protocolVersion"])
        self.assertEqual({"tools": {"listChanged": False}}, result["capabilities"])
        self.assertIn("error", fresh.dispatch(initialize()))
        fresh.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = fresh.dispatch({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
        tools = response["result"]["tools"]
        self.assertEqual({"localmind_recall", "localmind_state", "localmind_search", "localmind_expand"}, {t["name"] for t in tools})
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))
        self.assertTrue(all(not t["inputSchema"]["additionalProperties"] for t in tools))
        empty_id = fresh.dispatch({"jsonrpc": "2.0", "id": "", "method": "ping"})
        self.assertEqual({"jsonrpc": "2.0", "id": "", "result": {}}, empty_id)
        invalid = fresh.dispatch({"jsonrpc": "2.0", "id": "correlate", "method": "ping", "extra": 1})
        self.assertEqual("correlate", invalid["id"])
        self.assertEqual(-32600, invalid["error"]["code"])

    def test_reject_model_scope_authority_and_malformed_arguments(self):
        for args in ({"project_id": "project-b"}, {"actor": "user"}, {"authority": "user_stated"},
                     {"database": "/private"}, {"_meta": {"scope": "global"}}):
            with self.subTest(arguments=args):
                result = self.server.dispatch(call("localmind_state", args))
                self.assertEqual(-32602, result["error"]["code"])
        for args in ({"query": "q", "limit": True}, {"query": "q", "limit": 0},
                     {"query": "q", "limit": 21}, {"query": ""}, {"query": "q" * 513},
                     {"query": ["q"]}, {"query": "q", "path": "/"}):
            self.assertIn("error", self.server.dispatch(call("localmind_search", args)))
        for bad in (None, [], "text"):
            self.assertIn("error", self.server.dispatch({"jsonrpc": "2.0", "id": 4,
                "method": "tools/call", "params": {"name": "localmind_state", "arguments": bad}}))

    def test_recall_rejects_budget_scope_and_query_authority(self):
        invalid = [
            {"query": "ttl", "budget_bytes": True},
            {"query": "ttl", "budget_bytes": 0},
            {"query": "ttl", "budget_bytes": 1023},
            {"query": "ttl", "budget_bytes": 32769},
            {"query": "ttl", "budget_bytes": 4096.0},
            {"query": "ttl", "budget_bytes": "4096"},
            {"query": "ttl", "budget_bytes": None},
            {"query": ""}, {"query": "q" * 513},
            {"query": "\ud800"},
            {"query": "ttl", "project_id": "project-b"},
            {"query": "ttl", "actor": "user"},
            {"query": "ttl", "authority": "user_stated"},
            {"query": "ttl", "event_id": self.foreign_id},
            {"query": "ttl", "_meta": {"scope": "global"}},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                response = self.server.dispatch(call("localmind_recall", arguments))
                self.assertEqual(-32602, response["error"]["code"])

    def test_recall_is_one_budgeted_read_without_capturing_query_or_foreign_sources(self):
        result = self.server.dispatch(call("localmind_recall", {"query": "缓存有效期是多少？"}))["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        body = result["content"][0]["text"]
        self.assertIn("24小时", body)
        self.assertNotIn("外项目机密", body)
        self.assertLessEqual(len(body.encode("utf-8")), 8192)
        empty = self.server.dispatch(call("localmind_recall", {"query": "unique-query-never-captured"}))["result"]
        self.assertFalse(empty["isError"])
        with Store(self.database) as store:
            self.assertNotIn("unique-query-never-captured", "\n".join(store.db.iterdump()))

    def test_recall_obeys_pause_and_forget_between_calls(self):
        with Store(self.database) as store:
            store.pause()
        paused = self.server.dispatch(call("localmind_recall", {"query": "缓存"}))["result"]
        self.assertTrue(paused["isError"])
        with Store(self.database) as store:
            store.resume()
            store.forget(self.scope, self.source_id)
        forgotten = self.server.dispatch(call("localmind_recall", {"query": "缓存"}))["result"]
        self.assertFalse(forgotten["isError"])
        self.assertNotIn("24小时", json.dumps(forgotten, ensure_ascii=False))

    def test_tool_reads_do_not_persist_query_or_grant_cross_project_access(self):
        result = self.server.dispatch(call("localmind_state"))["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])
        foreign = self.server.dispatch(call("localmind_expand", {"event_id": self.foreign_id}))["result"]
        self.assertTrue(foreign["isError"])
        self.assertNotIn(self.foreign_id, json.dumps(foreign))
        self.assertNotIn("外项目机密", json.dumps(result, ensure_ascii=False))
        response = self.server.dispatch(call("localmind_search", {"query": "unique-query-not-an-event"}))["result"]
        self.assertFalse(response["isError"])
        with Store(self.database) as store:
            self.assertNotIn("unique-query-not-an-event", "\n".join(store.db.iterdump()))

    def test_pause_and_forget_take_effect_on_the_next_call_without_restarting_server(self):
        before = self.server.dispatch(call("localmind_expand", {"event_id": self.source_id}))["result"]
        self.assertFalse(before["isError"])
        with Store(self.database) as store:
            store.pause()
        paused = self.server.dispatch(call("localmind_expand", {"event_id": self.source_id}))["result"]
        self.assertTrue(paused["isError"])
        with Store(self.database) as store:
            store.resume()
            store.forget(self.scope, self.source_id)
        deleted = self.server.dispatch(call("localmind_expand", {"event_id": self.source_id}))["result"]
        self.assertTrue(deleted["isError"])
        searched = self.server.dispatch(call("localmind_search", {"query": "24小时"}))["result"]
        self.assertNotIn("24小时", json.dumps(searched, ensure_ascii=False))

    def test_missing_database_is_not_created_and_errors_do_not_echo_arguments(self):
        path = Path(self.directory.name) / "missing.sqlite"
        server = MCPServer(path, self.scope)
        server.dispatch(initialize())
        server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = server.dispatch(call("localmind_search", {"query": "secret-query"}))["result"]
        self.assertTrue(response["isError"])
        self.assertFalse(path.exists())
        self.assertNotIn("secret-query", json.dumps(response))

    def test_correction_is_visible_without_promoting_old_original_to_current_state(self):
        with Store(self.database) as store:
            Runtime(store, ReplayAdapter(["按2小时"])).run(self.scope, "有效期改为2小时", "correction",
                changes=[{"subject_key": "ttl", "value": "2小时", "kind": "requirement"}])
        state = self.server.dispatch(call("localmind_state"))["result"]["structuredContent"]
        self.assertEqual(["2小时"], [subject["value"] for subject in state["subjects"]])
        old = self.server.dispatch(call("localmind_expand", {"event_id": self.source_id}))["result"]
        self.assertFalse(old["isError"])
        source = old["structuredContent"]["source"]
        self.assertEqual("historical_source", source["status"])
        self.assertEqual([], source["effective_subjects"])
        results = self.server.dispatch(call("localmind_search", {"query": "24小时"}))["result"]
        self.assertNotIn("24小时", json.dumps(results, ensure_ascii=False))

    def test_stdio_frames_notifications_parse_errors_and_bounded_input(self):
        sequence = [initialize(), {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "method": "notifications/unknown"},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
        encoded = "\n".join(json.dumps(message) for message in sequence) + "\n"
        output = io.StringIO()
        self.assertEqual(0, serve(self.database, self.scope, io.BytesIO(encoded.encode()), output))
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([1, 2, 3], [message["id"] for message in responses])
        output = io.StringIO()
        malformed = b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n{}\n[1,2]\n{"a":1,"a":2}\n{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
        serve(self.database, self.scope, io.BytesIO(malformed), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([-32600, -32600, -32600, -32700, -32600], [message["error"]["code"] for message in responses])
        output = io.BytesIO()
        self.assertEqual(2, serve(self.database, self.scope, io.BytesIO(b"x" * (MAX_INPUT_BYTES + 1)), output))
        self.assertEqual(-32700, json.loads(output.getvalue())["error"]["code"])
        output = io.StringIO()
        self.assertEqual(2, serve(self.database, self.scope, io.StringIO("中" * 24000), output))
        self.assertEqual(-32700, json.loads(output.getvalue())["error"]["code"])

    def test_actual_subprocess_has_only_protocol_output_and_exits_on_eof(self):
        wire = "\n".join(json.dumps(item) for item in [initialize(),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            call("localmind_expand", {"event_id": self.source_id}),
            call("localmind_recall", {"query": "缓存有效期", "budget_bytes": 4096}, request_id=3)]) + "\n"
        completed = subprocess.run([sys.executable, "-m", "localmind", "--db", str(self.database),
            "--project", self.scope["project_id"], "--task", self.scope["task_id"],
            "--session", self.scope["logical_session_id"], "mcp"],
            input=wire, text=True, capture_output=True, timeout=5,
            cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(3, len(responses))
        self.assertFalse(responses[1]["result"]["isError"])
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertLessEqual(len(responses[2]["result"]["content"][0]["text"].encode("utf-8")), 4096)
        self.assertIn("24小时", completed.stdout)
