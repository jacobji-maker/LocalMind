"""New adversarial regressions discovered during the first effect experiment.

No real model is used here: these cases examine actual state and application
messages, independently of whether a generated answer happens to guess right.
"""
import tempfile
from pathlib import Path
import unittest

from localmind.adapters import ReplayAdapter
from localmind.context import BudgetInsufficient, Compiler
from localmind.memory import CapturePaused, Store
from localmind.retrieval import MemoryReader
from localmind.runtime import Runtime


SCOPE = {"project_id": "synthetic", "task_id": "main", "logical_session_id": "session"}


def setup_store(path=":memory:"):
    store = Store(path)
    store.register_project(SCOPE["project_id"])
    store.register_task(SCOPE["project_id"], SCOPE["task_id"])
    return store


def capture(store, text, key, changes=None):
    turn = store.receive(SCOPE, text, key, changes=changes)
    store.finish_capture(turn["turn_id"])
    return turn


class AdversarialTests(unittest.TestCase):
    def test_long_previous_answer_does_not_block_unrelated_new_question(self):
        with setup_store() as store:
            long_answer = "历史长回答。" * 1000
            self.assertGreater(len(long_answer.encode("utf-8")), 12288)
            adapter = ReplayAdapter([long_answer, "4"])
            runtime = Runtime(store, adapter)
            first = runtime.run(SCOPE, "撰写历史说明长文", "long-answer", budget_bytes=40000)
            self.assertEqual(long_answer, first["answer"])
            current = "现在仅回答独立算式：2+2等于几？"
            result = runtime.run(SCOPE, current, "independent-question")
            self.assertEqual("4", result["answer"])
            self.assertEqual([], result["packet"]["recent_key_messages"])
            self.assertIn("unreferenced_recent_dialogue_omitted_for_budget", result["packet"]["selection_reasons"])
            self.assertEqual(current, adapter.captured[-1][-1]["content"])
            self.assertFalse(any(long_answer in item["content"] for item in adapter.captured[-1]))

    def test_long_referenced_answer_fails_budget_instead_of_dropping_antecedent(self):
        with setup_store() as store:
            long_answer = "历史长回答。" * 1000
            adapter = ReplayAdapter([long_answer, "不得发送的模拟回答"])
            runtime = Runtime(store, adapter)
            runtime.run(SCOPE, "给出方案的详细说明", "long-proposal", budget_bytes=40000)
            with self.assertRaises(BudgetInsufficient):
                runtime.run(SCOPE, "继续刚才那个方案", "referenced-question")
            self.assertEqual(1, len(adapter.captured))

    def test_lookup_keyword_after_long_background_can_recall_old_original(self):
        with setup_store() as store:
            capture(store, "玄鲸服务的恢复口令为AZ-481。", "old-original")
            for index in range(205):
                capture(store, "无关项目进展编号%03d" % index, "filler-%d" % index)
            text = "这是冗长背景。" * 80 + "请问玄鲸服务的恢复口令是什么？"
            self.assertGreater(text.index("玄鲸"), 512)
            turn = store.receive(SCOPE, text, "lookup")
            packet = Compiler(store).compile(SCOPE, turn["turn_id"])
            self.assertEqual(text, packet["original_request"]["text"])
            self.assertTrue(any("AZ-481" in item["content"]
                                for item in packet["relevant_memories"]))

    def test_restart_terminates_unsent_work_without_replaying_and_unblocks_new_input(self):
        for phase in ("received", "compiled"):
            with self.subTest(interrupted_phase=phase), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "state.sqlite"
                with setup_store(database) as store:
                    turn = store.receive(SCOPE, "继续合成任务", "same-request")
                    if phase == "compiled":
                        packet = Compiler(store).compile(SCOPE, turn["turn_id"])
                        store.record_context(turn["turn_id"], packet)
                with Store(database) as store:
                    interrupted = store.turn(turn["turn_id"])
                    self.assertEqual("cancelled", interrupted["phase"])
                    self.assertEqual("RESTART_UNSENT_INTERRUPTED", interrupted["error"])
                    self.assertIsNone(interrupted["request_id"])
                    self.assertEqual("继续合成任务", store.get_event(SCOPE, turn["input_event_id"])["raw_content"])
                    adapter = ReplayAdapter(["新的模拟回答"])
                    runtime = Runtime(store, adapter)
                    for _ in range(2):
                        retry = runtime.run(SCOPE, "继续合成任务", "same-request")
                        self.assertFalse(retry["sent"])
                        self.assertEqual("cancelled", retry["turn"]["phase"])
                    self.assertEqual([], adapter.captured)
                    result = runtime.run(SCOPE, "用户明确开始新的问题", "new-request")
                    self.assertTrue(result["sent"])
                    self.assertEqual("新的模拟回答", result["answer"])
                    self.assertEqual(1, len(adapter.captured))

    def test_reopening_interrupted_input_does_not_resume_paused_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            with setup_store(database) as store:
                original = store.receive(SCOPE, "暂停前已保存的原话", "before-pause")
                store.pause()
            with Store(database) as store:
                self.assertEqual("paused", store.status()["capture_state"])
                self.assertEqual("cancelled", store.turn(original["turn_id"])["phase"])
                before = store.status()["event_count"]
                adapter = ReplayAdapter(["不应调用的模拟回答"])
                with self.assertRaises(CapturePaused):
                    Runtime(store, adapter).run(SCOPE, "暂停期间的新秘密", "paused-input")
                self.assertEqual(before, store.status()["event_count"])
                self.assertEqual([], adapter.captured)
                self.assertNotIn("暂停期间的新秘密", "\n".join(store.db.iterdump()))

    def test_shared_source_partial_correction_preserves_roles_and_current_values(self):
        with setup_store() as store:
            original = capture(store, "缓存24小时，后端使用文件。", "initial", changes=[
                {"subject_key": "ttl", "value": "24小时"},
                {"subject_key": "backend", "value": "文件"},
            ])
            capture(store, "缓存改为2小时。", "correction", changes=[
                {"subject_key": "ttl", "value": "2小时"},
            ])
            reader = MemoryReader(store, SCOPE)
            state = reader.state()
            self.assertEqual({"ttl": "2小时", "backend": "文件"},
                             {item["subject_key"]: item["value"] for item in state["subjects"]})
            expanded = reader.expand(original["input_event_id"])["source"]
            self.assertEqual("historical_source", expanded["status"])
            self.assertEqual(["backend"], [item["subject_key"] for item in expanded["effective_subjects"]])


if __name__ == "__main__":
    unittest.main()
