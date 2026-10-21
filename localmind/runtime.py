"""Synchronous foreground loop. The database owns all durable state barriers."""
from __future__ import annotations

from contextlib import contextmanager
import threading
import uuid

from .adapters import AdapterError
from .context import Compiler, to_messages, validate_packet


class Runtime:
    def __init__(self, store, adapter, planner=None):
        self.store, self.adapter, self.planner = store, adapter, planner
        self.compiler = Compiler(store)
        self._foreground = threading.Condition()
        self._foreground_busy = False
        self._pending_corrections = 0

    @contextmanager
    def _accept_turn(self, scope, text, dedupe_key, changes, pending_conflicts):
        """Commit corrections promptly; serialize their model calls before ordinary work."""
        with self._foreground:
            if changes or pending_conflicts:
                turn = self.store.receive(scope, text, dedupe_key, changes=changes,
                                          pending_conflicts=pending_conflicts)
                self._pending_corrections += 1
                try:
                    self._foreground.wait_for(lambda: not self._foreground_busy)
                finally:
                    self._pending_corrections -= 1
                    self._foreground.notify_all()
                # A duplicate may have completed, or a newer correction may
                # have superseded this accepted input while it was waiting.
                turn = self.store.turn(turn["turn_id"])
            else:
                self._foreground.wait_for(
                    lambda: not self._foreground_busy and not self._pending_corrections)
                turn = self.store.receive(scope, text, dedupe_key)
            self._foreground_busy = True
        try:
            yield turn
        finally:
            with self._foreground:
                self._foreground_busy = False
                self._foreground.notify_all()

    def run(self, scope, text, dedupe_key=None, changes=None, pending_conflicts=None,
            budget_bytes=12288):
        """No hidden retries. A duplicate/unknown/completed run is returned without sending."""
        with self._accept_turn(scope, text, dedupe_key or uuid.uuid4().hex,
                               changes, pending_conflicts) as turn:
            turn_id = turn["turn_id"]
            if turn["phase"] != "received":
                return {"reused": True, "turn": turn, "sent": False}
            try:
                packet = self.compiler.compile(scope, turn_id, budget_bytes=budget_bytes,
                                               planner=self.planner)
                self.store.record_context(turn_id, packet)
                validate_packet(self.store, scope, packet)
                request_id = "req-" + uuid.uuid4().hex
                messages = to_messages(packet)
                self.store.mark_sent(turn_id, request_id, packet["context_id"],
                                     None, messages)
                result = self.adapter.send(messages, request_id)
                if result.get("remote_thread_id") is not None:
                    self.store.record_remote_thread(turn_id, request_id, result["remote_thread_id"])
                usage = {"answer": result.get("usage"), "answer_latency_ms": result.get("latency_ms"),
                         "planner": packet.get("planner_usage"),
                         "planner_latency_ms": packet.get("planner_latency_ms"),
                         "adapter": result.get("adapter"), "model": result.get("model"),
                         "simulated": bool(result.get("simulated"))}
                if result.get("output_truncated"):
                    self.store.fail(turn_id, "MODEL_OUTPUT_TRUNCATED", usage=usage)
                    stale = not self.store.valid(packet["snapshot"], scope)
                    return {"sent": True, "turn_id": turn_id, "context_id": packet["context_id"],
                            "answer": None, "truncated": True, "usage": usage,
                            "packet": None if stale else packet,
                            "messages": None if stale else messages, "simulated": usage["simulated"],
                            **({"discarded": True} if stale else {})}
                completion = self.store.complete(turn_id, result["text"], request_id,
                                                 result["message_id"], usage=usage)
                discarded = bool(completion.get("discarded"))
                return {"sent": True, "turn_id": turn_id, "context_id": packet["context_id"],
                        "answer": None if discarded else result["text"],
                        "discarded": discarded, "completion": completion, "usage": usage,
                        "packet": None if discarded else packet,
                        "messages": None if discarded else messages, "simulated": usage["simulated"]}
            except AdapterError:
                self.store.fail(turn_id, "ADAPTER_OUTCOME_UNKNOWN", outcome_unknown=True)
                raise
            except Exception as exc:
                current = self.store.turn(turn_id)
                self.store.fail(turn_id, type(exc).__name__, outcome_unknown=current["phase"] == "sent")
                raise
