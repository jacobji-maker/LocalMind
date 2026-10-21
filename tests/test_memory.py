"""Persistence invariants tested without invoking any model or external action."""
import json
import tempfile
import unittest
from pathlib import Path

from localmind.memory import (
    CapturePaused, DedupeConflict, ForgottenSource, ScopeDenied, StaleSnapshot,
    Store, TurnBusy,
)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "memory.sqlite3"
        self.store = Store(self.path)
        self.store.register_project("p", "/synthetic/project")
        self.store.register_task("p", "t")
        self.scope = {"project_id": "p", "task_id": "t", "logical_session_id": "s"}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def requirement(self, text="预算500元", key="budget", level="task", dedupe="input-1"):
        return self.store.receive(self.scope, text, dedupe, changes=[{
            "subject_key": key, "value": text, "scope_level": level, "kind": "requirement",
        }])

    def sent(self, turn):
        packet = {"context_id": "ctx-" + turn["turn_id"], "turn_id": turn["turn_id"],
                  "snapshot": turn["snapshot"], "source_event_ids": [turn["input_event_id"]]}
        self.store.record_context(turn["turn_id"], packet)
        self.store.mark_sent(turn["turn_id"], "req-" + turn["turn_id"], packet["context_id"],
                             "remote-" + turn["turn_id"], [{"role": "user", "content": "合成原话"}])
        return self.store.turn(turn["turn_id"])

    def complete(self, turn, text="合成回答"):
        turn = self.sent(turn)
        return self.store.complete(turn["turn_id"], text, turn["request_id"], "message-1")

    def test_retry_is_atomic_and_different_payload_is_rejected(self):
        first = self.requirement()
        second = self.requirement()
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(1, len(self.store.events(self.scope)))
        self.assertEqual(1, self.store.state(self.scope)["subjects"][0]["effective_revision"])
        with self.assertRaises(DedupeConflict):
            self.requirement("预算900元")
        self.assertEqual(1, self.store.status()["event_count"])

    def test_ordinary_input_queues_but_correction_supersedes_immediately(self):
        first = self.requirement()
        with self.assertRaises(TurnBusy):
            self.store.receive(self.scope, "继续", "ordinary")
        newer = self.requirement("预算800元", dedupe="correction")
        self.assertEqual("superseded", self.store.turn(first["turn_id"])["freshness"])
        self.assertTrue(self.store.valid(newer["snapshot"], self.scope))
        self.assertEqual("预算800元", self.store.state(self.scope)["subjects"][0]["value"])

    def test_correction_invalidates_inflight_and_background_summary(self):
        first = self.requirement()
        self.store.save_artifact(self.scope, "预算五百", [first["input_event_id"]],
                                 [{"subject_key": "budget", "scope_level": "task", "effective_revision": 1}],
                                 first["snapshot"])
        sent = self.sent(first)
        correction = self.requirement("预算800元", dedupe="correction")
        self.assertEqual([], self.store.artifacts(self.scope))
        with self.assertRaises(StaleSnapshot):
            self.store.save_artifact(self.scope, "晚回摘要", [first["input_event_id"]],
                                     [{"subject_key": "budget", "scope_level": "task", "effective_revision": 1}],
                                     first["snapshot"])
        result = self.store.complete(sent["turn_id"], "过时回答", sent["request_id"], "m")
        self.assertTrue(result["discarded"])
        self.assertIsNone(self.store.state(self.scope)["last_result"])
        self.assertNotIn("过时回答", json.dumps(self.store.events(self.scope), ensure_ascii=False))
        self.assertTrue(self.store.get_event(self.scope, correction["input_event_id"])["effective"])
        self.assertFalse(self.store.get_event(self.scope, first["input_event_id"])["effective"])

    def test_pause_does_not_capture_and_old_results_never_resurface(self):
        turn = self.sent(self.requirement())
        before = self.store.status()["event_count"]
        self.store.pause()
        with self.assertRaises(CapturePaused):
            self.store.receive(self.scope, "暂停秘密", "paused")
        self.store.resume()
        self.assertEqual(before, self.store.status()["event_count"])
        result = self.store.complete(turn["turn_id"], "晚到秘密", turn["request_id"], "m")
        self.assertTrue(result["discarded"])
        serialized = json.dumps(self.store.events(self.scope), ensure_ascii=False)
        self.assertNotIn("暂停秘密", serialized)
        self.assertNotIn("晚到秘密", serialized)

    def test_global_is_explicit_and_never_leaks_project_originals(self):
        local = self.requirement()
        self.complete(local)
        global_turn = self.requirement("请用中文", key="language", level="global", dedupe="global")
        self.store.register_project("other")
        self.store.register_task("other", "task")
        other = {"project_id": "other", "task_id": "task", "logical_session_id": "new"}
        self.assertEqual(["请用中文"], [s["value"] for s in self.store.state(other)["subjects"]])
        self.assertEqual("请用中文", self.store.get_event(other, global_turn["input_event_id"])["raw_content"])
        with self.assertRaises(ScopeDenied):
            self.store.get_event(other, local["input_event_id"])
        with self.assertRaises(ValueError):
            self.store.receive(self.scope, "项目机密，请用中文", "bad-global", changes=[{
                "subject_key": "language", "value": "请用中文", "scope_level": "global"}])
        self.assertFalse(self.store.valid(local["snapshot"], self.scope))

    def test_forget_cleans_derived_content_actual_requests_and_restart(self):
        turn = self.requirement("需要遗忘的合成秘密")
        artifact = self.store.save_artifact(self.scope, "派生合成秘密", [turn["input_event_id"]],
                                            [{"subject_key": "budget", "scope_level": "task", "effective_revision": 1}],
                                            turn["snapshot"])
        result = self.complete(turn, "回答中的合成秘密")
        self.store.save_checkpoint(self.scope, [turn["input_event_id"], result["event_id"]])
        forgotten = self.store.forget(self.scope, turn["input_event_id"])
        self.assertIn(result["event_id"], forgotten["forgotten_event_ids"])
        self.assertEqual([], self.store.artifacts(self.scope))
        self.assertEqual([], self.store.events(self.scope))
        self.assertIsNone(self.store.turn(turn["turn_id"])["messages"])
        self.assertIsNone(self.store.turn(turn["turn_id"])["packet"])
        with self.assertRaises(ForgottenSource):
            self.store.get_event(self.scope, turn["input_event_id"])
        # Database content and its locally held audit copies contain no raw or
        # derived forgotten text; filesystem backups remain a separate concern.
        dump = "\n".join(self.store.db.iterdump())
        self.assertNotIn("需要遗忘的合成秘密", dump)
        self.assertNotIn("回答中的合成秘密", dump)
        self.assertNotIn("派生合成秘密", dump)
        self.store.close()
        self.store = Store(self.path)
        with self.assertRaises(ForgottenSource):
            self.requirement("需要遗忘的合成秘密", dedupe="reimport")
        self.assertIsNone(self.store.checkpoint(self.scope))
        self.assertEqual([], self.store.state(self.scope)["subjects"])

    def test_forgotten_inflight_response_is_discarded_after_resume_and_reopen(self):
        turn = self.sent(self.requirement())
        self.store.forget(self.scope, turn["input_event_id"])
        self.store.close()
        self.store = Store(self.path)
        result = self.store.complete(turn["turn_id"], "不得复活", turn["request_id"], "m")
        self.assertTrue(result["discarded"])
        self.assertNotIn("不得复活", "\n".join(self.store.db.iterdump()))

    def test_restart_marks_sent_unknown_without_creating_another_attempt(self):
        turn = self.sent(self.requirement())
        self.store.close()
        self.store = Store(self.path)
        recovered = self.store.turn(turn["turn_id"])
        self.assertEqual("outcome_unknown", recovered["phase"])
        retry = self.requirement()
        self.assertEqual(recovered["attempt_id"], retry["attempt_id"])
        with self.assertRaises(ValueError):
            self.store.mark_sent(turn["turn_id"], "req-replay", recovered["context_id"], "new", [])

    def test_final_message_deduplicates_without_counting_echo_as_user_source(self):
        turn = self.sent(self.requirement())
        first = self.store.complete(turn["turn_id"], "助手预算复述", turn["request_id"], "m")
        second = self.store.complete(turn["turn_id"], "助手预算复述", turn["request_id"], "m")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual([turn["input_event_id"]], first["root_source_ids"])
        self.assertEqual(2, self.store.status()["event_count"])
        self.assertEqual(1, len(self.store.state(self.scope)["subjects"]))
        with self.assertRaises(DedupeConflict):
            self.store.complete(turn["turn_id"], "变更后的回答", turn["request_id"], "m")

    def test_unknown_conflict_preserves_original_without_suppressing_unrelated_state(self):
        first = self.requirement()
        self.complete(first)
        unknown = self.store.receive(self.scope, "不是那个意思", "unknown", pending_conflicts=[{
            "subject_keys": [], "reason": "CORRECTION_TARGET_UNKNOWN"}])
        state = self.store.state(self.scope)
        self.assertEqual("unknown", state["pending_conflicts"][0]["status"])
        self.assertEqual("预算500元", state["subjects"][0]["value"])
        self.assertEqual("不是那个意思", self.store.get_event(self.scope, unknown["input_event_id"])["text"])

    def test_unlocated_correction_invalidates_older_inflight_packet(self):
        first = self.sent(self.requirement())
        unknown = self.store.receive(self.scope, "不是那个意思", "unknown", pending_conflicts=[{
            "subject_keys": [], "reason": "CORRECTION_TARGET_UNKNOWN"}])
        self.assertFalse(self.store.valid(first["snapshot"], self.scope))
        self.assertTrue(self.store.valid(unknown["snapshot"], self.scope))
        late = self.store.complete(first["turn_id"], "忽视纠正的回答", first["request_id"], "m")
        self.assertTrue(late["discarded"])
        self.assertEqual("预算500元", self.store.state(self.scope)["subjects"][0]["value"])
        self.assertIsNone(self.store.state(self.scope)["last_result"])

    def test_located_conflict_suppresses_only_matching_subject_then_resolves(self):
        first = self.requirement()
        self.complete(first)
        self.store.receive(self.scope, "预算不对，稍后确认", "ambiguous", pending_conflicts=[{
            "subject_keys": ["budget"], "reason": "AMBIGUOUS_VALUE"}])
        self.assertEqual([], self.store.state(self.scope)["subjects"])
        self.assertEqual("pending", self.store.state(self.scope)["pending_conflicts"][0]["status"])
        self.requirement("预算800元", dedupe="resolved")
        self.assertEqual([], self.store.state(self.scope)["pending_conflicts"])
        self.assertEqual("预算800元", self.store.state(self.scope)["subjects"][0]["value"])

    def test_unrelated_correction_does_not_invalidate_other_subject_summary(self):
        original = self.requirement()
        self.store.save_artifact(self.scope, "只依赖预算的摘要", [original["input_event_id"]],
                                 [{"subject_key": "budget", "scope_level": "task", "effective_revision": 1}],
                                 original["snapshot"])
        self.requirement("只读操作", key="mode", dedupe="mode")
        self.assertEqual(1, len(self.store.artifacts(self.scope)))

    def test_summary_cannot_omit_subject_revision_dependency(self):
        original = self.requirement()
        with self.assertRaises(ValueError):
            self.store.save_artifact(self.scope, "只有event不够", [original["input_event_id"]], [], original["snapshot"])

    def test_forget_recalled_assistant_answer_erases_direct_dependents(self):
        first = self.store.receive(self.scope, "提供苹果参数", "first")
        first_answer = self.complete(first, "私密苹果参数=apple-secret")
        middle = self.store.receive(self.scope, "暂时换个话题", "middle")
        self.complete(middle, "无关回答")
        last = self.store.receive(self.scope, "苹果参数是什么", "last")
        packet = {
            "context_id": "ctx-recall", "turn_id": last["turn_id"], "snapshot": last["snapshot"],
            "relevant_memories": [{"memory_id": first_answer["event_id"], "kind": "event",
                                   "content": first_answer["raw_content"],
                                   "source_event_ids": first_answer["root_source_ids"]}],
        }
        self.store.record_context(last["turn_id"], packet)
        self.assertIn(first_answer["event_id"], self.store.turn(last["turn_id"])["source_event_ids"])
        self.store.mark_sent(last["turn_id"], "req-last", "ctx-recall", "remote-last", [
            {"role": "user", "content": "来自记忆：apple-secret"}])
        last_answer = self.store.complete(last["turn_id"], "沿用apple-secret", "req-last", "m")
        self.assertIn(first_answer["event_id"], last_answer["source_event_ids"])
        self.store.forget(self.scope, first_answer["event_id"])
        self.assertIsNone(self.store.turn(last["turn_id"])["packet"])
        self.assertIsNone(self.store.turn(last["turn_id"])["messages"])
        with self.assertRaises(ForgottenSource):
            self.store.get_event(self.scope, last_answer["event_id"])
        self.assertNotIn("apple-secret", "\n".join(self.store.db.iterdump()))
        self.assertEqual("提供苹果参数", self.store.get_event(self.scope, first["input_event_id"])["text"])

    def test_checkpoint_bridges_sessions_and_process_lock_prevents_second_writer(self):
        first = self.requirement()
        self.complete(first)
        saved = self.store.save_checkpoint(self.scope, [first["input_event_id"]])
        other_session = dict(self.scope, logical_session_id="another-session")
        self.assertEqual(saved["checkpoint_id"], self.store.checkpoint(other_session)["checkpoint_id"])
        with self.assertRaises(RuntimeError):
            Store(self.path)

    def test_newer_session_input_prevents_old_answer_overwriting_last_result(self):
        old = self.sent(self.store.receive(self.scope, "较早的问题", "old"))
        new_scope = dict(self.scope, logical_session_id="new-session")
        newer = self.store.receive(new_scope, "较新的问题", "new")
        new_answer = self.complete(newer, "较新的回答")
        late = self.store.complete(old["turn_id"], "迟到的旧回答", old["request_id"], "m")
        self.assertTrue(late["discarded"])
        self.assertEqual(new_answer["event_id"], self.store.state(new_scope)["last_result"]["event_id"])

    def test_discarded_result_keeps_only_allowlisted_accounting(self):
        old = self.sent(self.requirement())
        self.store.pause()
        result = self.store.complete(old["turn_id"], "暂停后不得保存的回答", old["request_id"], "m", usage={
            "answer": {"prompt_eval_count": 42, "eval_count": 7,
                       "raw_text": "不许保存的provider正文", "total_duration": 123},
            "answer_latency_ms": 2.5, "adapter": "ollama-local", "model": "qwen3.5:9b",
            "simulated": False, "provider_debug": "不许保存的调试正文",
        })
        self.assertTrue(result["discarded"])
        usage = self.store.turn(old["turn_id"])["usage"]
        self.assertEqual(42, usage["answer"]["prompt_eval_count"])
        self.assertEqual(2.5, usage["answer_latency_ms"])
        self.assertEqual("qwen3.5:9b", usage["model"])
        self.assertNotIn("raw_text", usage["answer"])
        self.assertNotIn("provider_debug", usage)
        self.assertNotIn("不许保存", "\n".join(self.store.db.iterdump()))

    def test_ordinal_decision_binds_original_proposal_and_is_forgotten_with_it(self):
        first = self.store.receive(self.scope, "给我三个方案", "propose")
        proposal = self.complete(first, "一、A；二、B；三、C")
        choice = self.store.receive(self.scope, "选第二个", "choose", changes=[{
            "subject_key": "selection", "kind": "decision", "value": "第二个"}])
        subject = self.store.state(self.scope)["subjects"][0]
        self.assertEqual(proposal["event_id"], subject["reference_event_id"])
        self.assertIn(proposal["event_id"], self.store.get_event(self.scope, choice["input_event_id"])["source_event_ids"])
        self.complete(choice, "已采用方案B")
        self.store.forget(self.scope, proposal["event_id"])
        self.assertEqual([], self.store.state(self.scope)["subjects"])
        with self.assertRaises(ForgottenSource):
            self.store.get_event(self.scope, choice["input_event_id"])

    def test_global_choice_cannot_promote_project_private_proposal(self):
        first = self.store.receive(self.scope, "给我私有方案", "private-propose")
        self.complete(first, "一、私有方案A；二、私有方案B")
        before = self.store.status()["event_count"]
        with self.assertRaises(ScopeDenied):
            self.store.receive(self.scope, "第二个", "global-choice", changes=[{
                "subject_key": "selection", "kind": "decision", "scope_level": "global", "value": "第二个"}])
        self.assertEqual(before, self.store.status()["event_count"])


if __name__ == "__main__":
    unittest.main()
