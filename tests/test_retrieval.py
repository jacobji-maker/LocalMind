import json
import unittest

from localmind.adapters import ReplayAdapter
from localmind.memory import CapturePaused, ForgottenSource, ScopeDenied, Store
from localmind.retrieval import MemoryReader
from localmind.runtime import Runtime


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        for project in ("p", "other"):
            self.store.register_project(project)
            for task in ("t", "other"):
                self.store.register_task(project, task)
        self.scope = {"project_id": "p", "task_id": "t", "logical_session_id": "s"}
        self.reader = MemoryReader(self.store, self.scope)

    def tearDown(self):
        self.store.close()

    def put(self, text, scope=None, **kwargs):
        return Runtime(self.store, ReplayAdapter(["模拟助手提议"])).run(
            scope or self.scope, text, **kwargs)

    def test_search_recalls_original_older_than_recent_200(self):
        first = self.put("古老项目暗号是蓝鲸-unique")
        for index in range(105):
            self.put("无关轮次%d" % index)
        result = self.reader.search("蓝鲸")
        self.assertEqual(first["packet"]["original_request"]["event_id"], result["results"][0]["event_id"])
        # Compiler must be able to recall this same old evidence, not only MCP.
        new = self.put("蓝鲸的项目暗号是什么？")
        self.assertTrue(any("蓝鲸-unique" in item["content"]
                            for item in new["packet"]["relevant_memories"]))

    def test_scope_filters_search_and_guessed_expansion(self):
        secret = self.put("别的项目秘密蓝鲸", dict(self.scope, project_id="other"))
        self.put("别的任务秘密蓝鲸", dict(self.scope, task_id="other"))
        self.assertEqual([], self.reader.search("蓝鲸")["results"])
        with self.assertRaises(ScopeDenied):
            self.reader.expand(secret["packet"]["original_request"]["event_id"])
        self.assertNotIn("秘密", json.dumps(self.reader.state(), ensure_ascii=False))

    def test_pause_delete_and_correction_are_visible_without_cache(self):
        first = self.put("缓存为24小时", changes=[{"subject_key": "ttl", "value": "24小时"}])
        event_id = first["packet"]["original_request"]["event_id"]
        self.put("缓存改为2小时", changes=[{"subject_key": "ttl", "value": "2小时"}])
        self.assertNotIn(event_id, [item["event_id"] for item in self.reader.search("24小时")["results"]])
        old = self.reader.expand(event_id)["source"]
        self.assertEqual("historical_source", old["status"])
        self.assertEqual([], old["effective_subjects"])
        self.assertEqual("2小时", self.reader.state()["subjects"][0]["value"])
        self.store.pause()
        for read in (self.reader.state, lambda: self.reader.search("缓存"), lambda: self.reader.expand(event_id)):
            with self.assertRaises(CapturePaused):
                read()
        self.store.resume()
        self.store.forget(self.scope, event_id)
        with self.assertRaises(ForgottenSource):
            self.reader.expand(event_id)

    def test_state_keeps_full_conditions_and_queries_create_no_events(self):
        self.put("只有备份恢复验证成功后才允许修改。", changes=[{
            "subject_key": "change", "value": "允许修改"}])
        before = self.store.status()["event_count"]
        state = self.reader.state()
        self.assertIn("只有备份恢复验证成功后", state["evidence"][0]["text"])
        self.reader.search("未采集查询秘密")
        self.assertEqual(before, self.store.status()["event_count"])
        self.assertNotIn("未采集查询秘密", "\n".join(self.store.db.iterdump()))

    def test_global_declaration_is_recallable_without_project_private_text(self):
        self.put("全部使用中文", scope=dict(self.scope, project_id="other"), changes=[{
            "subject_key": "language", "value": "全部使用中文", "scope_level": "global"}])
        self.assertEqual("全部使用中文", self.reader.search("中文")["results"][0]["preview"])
        self.assertEqual("全部使用中文", self.reader.state()["subjects"][0]["value"])

    def test_search_bounds_reject_bool_and_oversized_query(self):
        for query, limit in (("x", True), ("", 2), ("x" * 513, 1), ("x", 21)):
            with self.assertRaises(ValueError):
                self.reader.search(query, limit)
