"""Verify compact recall preserves complete evidence and budget semantics."""
import json
import unittest

from localmind.adapters import ReplayAdapter
from localmind.context import BudgetInsufficient
from localmind.memory import Store
from localmind.projection import encoded_bytes
from localmind.retrieval import MemoryReader
from localmind.runtime import Runtime


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.register_project("synthetic")
        self.store.register_task("synthetic", "main")
        self.scope = {"project_id": "synthetic", "task_id": "main", "logical_session_id": "history"}
        self.reader = MemoryReader(self.store, self.scope)

    def tearDown(self):
        self.store.close()

    def capture(self, text, key, changes=None):
        turn = self.store.receive(self.scope, text, key, changes=changes)
        self.store.finish_capture(turn["turn_id"])
        return turn["input_event_id"]

    def test_many_filler_turns_keep_shared_evidence_once_and_omit_audit(self):
        original = "后端确定采用SQLite，回答使用中文；只有备份恢复验证通过才允许修改数据库，当前尚未验证。"
        source_id = self.capture(original, "source", [
            {"subject_key": "backend", "value": "SQLite", "kind": "decision"},
            {"subject_key": "language", "value": "中文", "kind": "requirement"},
        ])
        for index in range(15):
            self.capture("日报第%d页的标题先保留，正文以后再写。" % index, "filler-%d" % index)
        query = "换会话继续，已确认的存储后端和回答语言是什么？"
        recalled = self.reader.recall(query, budget_bytes=8192)
        serialized = json.dumps(recalled, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(encoded_bytes(recalled), 8192)
        self.assertEqual({"backend": "SQLite", "language": "中文"},
                         {item["key"]: item["value"] for item in recalled["statements"]})
        originals = [item for item in recalled["sources"] if item["event_id"] == source_id]
        self.assertEqual(1, len(originals))
        self.assertEqual(original, originals[0]["text"])
        self.assertEqual(1, serialized.count(original))
        self.assertEqual("user", originals[0]["role"])
        for audit_key in ("snapshot", "capture_epoch", "deletion_epoch", "scope_revisions",
                          "dependency_checks", "planner_usage", "logical_session_id"):
            self.assertNotIn('"' + audit_key + '"', serialized)
        self.assertNotIn(query, "\n".join(self.store.db.iterdump()))

    def test_mandatory_long_conditions_fail_instead_of_being_truncated(self):
        condition = "仅当备份校验完成才允许修改，否则拒绝；" + "附加前提均应逐项满足。" * 100
        self.capture(condition, "condition", [{"subject_key": "permission", "value": "允许修改"}])
        with self.assertRaises(BudgetInsufficient):
            self.reader.recall("现在可以修改吗？", budget_bytes=1024)
        recalled = self.reader.recall("现在可以修改吗？", budget_bytes=8192)
        self.assertIn(condition, [item.get("text") for item in recalled["sources"]])

    def test_explicit_choice_keeps_original_option_conditions_and_identity(self):
        proposal = "方案一：SQLite。方案二：仅当校验备份可恢复时采用文件，禁止先清空目录。方案三：内存。"
        runtime = Runtime(self.store, ReplayAdapter([proposal]))
        runtime.run(self.scope, "请给出三个存储方案。", "proposal")
        choice_id = self.capture("选第二个。", "choice", [
            {"subject_key": "backend", "value": "第二个", "kind": "decision"},
        ])
        recalled = self.reader.recall("最终选用什么？")
        self.assertEqual("第二个", recalled["statements"][0]["value"])
        selected = recalled["resolved_choices"][0]
        self.assertEqual("仅当校验备份可恢复时采用文件，禁止先清空目录", selected["selected_option"])
        sources = {item["ref"]: item for item in recalled["sources"]}
        self.assertEqual(choice_id, sources[selected["user_source"]]["event_id"])
        self.assertEqual("user", sources[selected["user_source"]]["role"])
        self.assertEqual("assistant", sources[selected["proposal_source"]]["role"])
        self.assertEqual(proposal, sources[selected["proposal_source"]]["text"])


if __name__ == "__main__":
    unittest.main()
