from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest

from localmind.adapters import AdapterError, ReplayAdapter
from localmind.cli import run_demo
from localmind.memory import CapturePaused, DedupeConflict, StaleSnapshot, Store
from localmind.runtime import Runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "state.sqlite")
        self.store = Store(self.db)
        self.scope = {"project_id": "A", "task_id": "work", "logical_session_id": "s1"}
        self.store.register_project("A")
        self.store.register_task("A", "work")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_completed_dedup_never_sends_twice(self):
        adapter = ReplayAdapter(["answer"])
        runtime = Runtime(self.store, adapter)
        first = runtime.run(self.scope, "  保留\n原话  ", "same")
        second = runtime.run(self.scope, "  保留\n原话  ", "same")
        self.assertTrue(second["reused"])
        self.assertFalse(second["sent"])
        self.assertEqual(len(adapter.captured), 1)
        self.assertEqual(first["messages"][-1], {"role": "user", "content": "  保留\n原话  "})

    def test_input_only_capture_is_idempotent_without_a_request_or_assistant_event(self):
        changes = [{"subject_key": "ttl", "value": "2小时", "kind": "requirement"}]
        turn = self.store.receive(self.scope, "缓存有效期2小时", "capture", changes=changes)
        captured = self.store.finish_capture(turn["turn_id"])
        retry = self.store.receive(self.scope, "缓存有效期2小时", "capture", changes=changes)
        self.assertEqual(captured, self.store.finish_capture(retry["turn_id"]))
        self.assertEqual(captured["phase"], "completed")
        self.assertEqual(captured["outcome"], "captured")
        self.assertIsNone(captured["request_id"])
        self.assertIsNone(captured["completed_event_id"])
        self.assertIsNone(captured["packet"])
        self.assertEqual([event["actor"] for event in self.store.events(self.scope)], ["user"])
        self.assertIsNone(self.store.state(self.scope)["last_result"])
        self.assertEqual(self.store.state(self.scope)["subjects"][0]["value"], "2小时")

    def test_input_only_capture_rejects_pause_and_superseded_inputs(self):
        old = self.store.receive(self.scope, "24小时", "old", changes=[
            {"subject_key": "ttl", "value": "24小时"}])
        newer = self.store.receive(self.scope, "2小时", "new", changes=[
            {"subject_key": "ttl", "value": "2小时"}])
        with self.assertRaises(StaleSnapshot):
            self.store.finish_capture(old["turn_id"])
        self.store.pause()
        with self.assertRaises(StaleSnapshot):
            self.store.finish_capture(newer["turn_id"])

    def test_correction_arriving_during_send_discards_old_answer(self):
        store, scope = self.store, self.scope
        class CorrectingAdapter(ReplayAdapter):
            def send(self, messages, request_id):
                store.receive(scope, "改为2小时", "correction", changes=[
                    {"subject_key": "ttl", "value": "2小时", "scope_level": "task", "kind": "requirement"}])
                return super().send(messages, request_id)
        result = Runtime(store, CorrectingAdapter(["仍然24小时"])).run(scope, "24小时", "original", changes=[
            {"subject_key": "ttl", "value": "24小时", "scope_level": "task", "kind": "requirement"}])
        self.assertTrue(result["discarded"])
        self.assertIsNone(result["answer"])
        self.assertEqual(store.state(scope)["subjects"][0]["value"], "2小时")
        self.assertFalse(any(e["raw_content"] == "仍然24小时" for e in store.events(scope)))

    def test_runtime_correction_commits_before_send_finishes_and_ordinary_work_waits(self):
        started, release = threading.Event(), threading.Event()
        correction_received, ordinary_started = threading.Event(), threading.Event()
        receive = self.store.receive
        def observed_receive(scope, text, dedupe_key, **kwargs):
            turn = receive(scope, text, dedupe_key, **kwargs)
            if dedupe_key == "correction":
                correction_received.set()
            return turn
        self.store.receive = observed_receive

        class BlockingAdapter(ReplayAdapter):
            def send(self, messages, request_id):
                if not started.is_set():
                    started.set()
                    if not release.wait(5):
                        raise AssertionError("Test did not release the first model call")
                return super().send(messages, request_id)
        adapter = BlockingAdapter(["旧回答24小时", "新回答2小时", "后续回答2小时"])
        runtime = Runtime(self.store, adapter)
        def ordinary():
            ordinary_started.set()
            return runtime.run(self.scope, "继续", "ordinary")
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(runtime.run, self.scope, "24小时", "original", changes=[
                {"subject_key": "ttl", "value": "24小时", "kind": "requirement"}])
            try:
                self.assertTrue(started.wait(3))
                later = pool.submit(ordinary)
                self.assertTrue(ordinary_started.wait(3))
                corrected = pool.submit(runtime.run, self.scope, "改为2小时", "correction", changes=[
                    {"subject_key": "ttl", "value": "2小时", "kind": "requirement"}])
                self.assertTrue(correction_received.wait(3), "Correction was blocked by model execution")
                self.assertEqual(self.store.state(self.scope)["subjects"][0]["value"], "2小时")
                self.assertFalse(first.done())
            finally:
                release.set()
            old_result, correction_result, ordinary_result = (
                first.result(timeout=3), corrected.result(timeout=3), later.result(timeout=3))
        self.assertTrue(old_result["discarded"])
        self.assertEqual(correction_result["answer"], "新回答2小时")
        self.assertEqual(ordinary_result["answer"], "后续回答2小时")
        self.assertEqual([messages[-1]["content"] for messages in adapter.captured],
                         ["24小时", "改为2小时", "继续"])
        self.assertNotIn("旧回答24小时", str(self.store.events(self.scope)))

    def test_remote_thread_identity_is_persisted_and_cannot_be_rebound(self):
        result = Runtime(self.store, ReplayAdapter(["answer"])).run(self.scope, "request", "thread")
        turn = self.store.turn(result["turn_id"])
        remote = "replay-clean-" + turn["request_id"]
        self.assertEqual(turn["remote_thread_id"], remote)
        self.assertEqual(self.store.record_remote_thread(turn["turn_id"], turn["request_id"], remote)
                         ["remote_thread_id"], remote)
        with self.assertRaises(ValueError):
            self.store.record_remote_thread(turn["turn_id"], "wrong-request", remote)
        with self.assertRaises(DedupeConflict):
            self.store.record_remote_thread(turn["turn_id"], turn["request_id"], "another-thread")
        for invalid in ("private text\nthread", "a" * 257, "", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.record_remote_thread(turn["turn_id"], turn["request_id"], invalid)
        self.store.close()
        self.store = Store(self.db)
        self.assertEqual(self.store.turn(turn["turn_id"])["remote_thread_id"], remote)

    def test_control_during_send_scrubs_returned_context_as_well_as_stored_content(self):
        for control in ("pause", "forget"):
            with self.subTest(control=control):
                started, release = threading.Event(), threading.Event()
                class BlockingAdapter(ReplayAdapter):
                    def send(self, messages, request_id):
                        started.set()
                        if not release.wait(5):
                            raise AssertionError("Test did not release the model call")
                        return super().send(messages, request_id)
                marker = "私密上下文-" + control
                runtime = Runtime(self.store, BlockingAdapter(["私密晚到回答-" + control]))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(runtime.run, self.scope, marker, control)
                    try:
                        self.assertTrue(started.wait(3))
                        if control == "pause":
                            self.store.pause()
                        else:
                            event = next(event for event in self.store.events(self.scope)
                                         if event["raw_content"] == marker)
                            self.store.forget(self.scope, event["event_id"])
                    finally:
                        release.set()
                    result = future.result(timeout=3)
                self.assertTrue(result["discarded"])
                self.assertIsNone(result["packet"])
                self.assertIsNone(result["messages"])
                self.assertNotIn(marker, str(result))
                self.assertNotIn("私密晚到回答", str(result))
                self.assertNotIn("私密晚到回答", str(self.store.events(self.scope)))
                self.store.resume()

    def test_pause_during_send_prevents_persistence_and_new_capture(self):
        store = self.store
        class PausingAdapter(ReplayAdapter):
            def send(self, messages, request_id):
                store.pause()
                return super().send(messages, request_id)
        result = Runtime(store, PausingAdapter(["paused response secret"])).run(self.scope, "before pause", "p")
        self.assertTrue(result["discarded"])
        with self.assertRaises(CapturePaused):
            Runtime(store, ReplayAdapter(["should not send"])).run(self.scope, "paused input secret", "p2")
        self.assertFalse(any("secret" in e["raw_content"] for e in store.events(self.scope)))

    def test_network_uncertainty_is_not_automatically_retried_after_restart(self):
        class FailingAdapter:
            def send(self, messages, request_id):
                raise AdapterError("timeout")
        with self.assertRaises(AdapterError):
            Runtime(self.store, FailingAdapter()).run(self.scope, "request", "unknown")
        self.store.close()
        self.store = Store(self.db)
        adapter = ReplayAdapter(["must not send"])
        result = Runtime(self.store, adapter).run(self.scope, "request", "unknown")
        self.assertEqual(result["turn"]["phase"], "outcome_unknown")
        self.assertEqual(len(adapter.captured), 0)

    def test_synthetic_demo_spans_selection_correction_restart_and_forgetting(self):
        result = run_demo()
        self.assertEqual(result["kind"], "simulated-replay")
        self.assertTrue(result["correction_visible"])
        self.assertTrue(result["decision_removed"])
        self.assertEqual(len(result["turns"]), 5)
        self.assertIn("2小时", str(result["turns"][3]["messages"]))

    def test_token_limited_output_does_not_become_completed_state(self):
        class TruncatingAdapter:
            def send(self, messages, request_id):
                return {"text": "partial secret reply", "message_id": "partial", "output_truncated": True,
                        "usage": {"eval_count": 192}, "latency_ms": 10,
                        "adapter": "ollama-local", "model": "local:1b"}
        result = Runtime(self.store, TruncatingAdapter()).run(self.scope, "request", "limited")
        self.assertTrue(result["truncated"])
        self.assertIsNone(result["answer"])
        turn = self.store.turn(result["turn_id"])
        self.assertEqual(turn["phase"], "failed")
        self.assertEqual(turn["usage"]["answer"]["eval_count"], 192)
        self.assertIsNone(self.store.state(self.scope)["last_result"])
        self.assertNotIn("partial secret reply", str(self.store.events(self.scope)))
