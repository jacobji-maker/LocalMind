"""Bounded, scope-pinned reads for native clients; no model input becomes a fact.

The reader does not capture queries, construct turns, retain result caches, or
contact a model. SQLite rows are streamed so recall is not limited to the recent
dialogue window. This is a deterministic scan, not a vector or indexed search.
"""
from __future__ import annotations

import copy
import json

from .context import (
    BudgetInsufficient, _check_constraint, _constraint, _dependency_closure,
    _effective_subjects, _event_scope_allowed, _score, _terms, _usable_event,
)
from .memory import CapturePaused, StaleSnapshot


class MemoryReader:
    MAX_RESULT_BYTES = 49152

    def __init__(self, store, scope):
        self.store = store
        self.scope = copy.deepcopy(scope)

    def _begin(self):
        if self.store.status()["capture_state"] != "active":
            raise CapturePaused("Memory reads are paused")
        return self.store.snapshot(self.scope)

    def _finish(self, result, snapshot):
        if not self.store.valid(snapshot, self.scope):
            raise StaleSnapshot("Memory changed during retrieval")
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > self.MAX_RESULT_BYTES:
            raise BudgetInsufficient("Complete memory evidence exceeds the result budget")
        return result

    def _view(self, event):
        return {
            "event_id": event["event_id"], "actor": event["actor"],
            "origin_kind": event["origin_kind"], "scope_level": event["scope_level"],
            "status": "current_source" if event.get("effective", True) else "historical_source",
            "source_event_ids": event.get("source_event_ids", []),
            "root_source_ids": event.get("root_source_ids", []),
            "text": event["raw_content"],
        }

    def _matching(self, query, limit):
        suppressed = self.store.suppressed_event_ids(self.scope)
        terms = _terms(query) | {query.strip().lower()}
        best = []
        # Filter authorization before scoring or returning any source. Global
        # originals must have been explicitly declared standalone user inputs.
        for row in self.store.db.execute("SELECT seq,body FROM events ORDER BY seq DESC"):
            event = json.loads(row["body"])
            if not _event_scope_allowed(self.scope, event):
                continue
            event["effective"] = event["event_id"] not in suppressed
            if not _usable_event(self.scope, event):
                continue
            score = _score(event["raw_content"], terms)
            if score:
                event["seq"] = row["seq"]
                best.append((score, row["seq"], event))
                best.sort(key=lambda item: (-item[0], -item[1]))
                del best[limit:]
        return [item[2] for item in best]

    def matching_events(self, query, limit=24):
        """Internal compiler API: effective originals, never a caller scope."""
        # Public MCP queries remain <=512 chars. Internal compilation must not
        # silently discard the end of the actual user input at that boundary.
        if isinstance(query, str) and len(query) > 16384:
            raise BudgetInsufficient("Original query exceeds the internal retrieval bound")
        self._validate_query(query, limit, maximum=24, max_chars=16384)
        with self.store._lock:
            snapshot = self._begin()
            results = self._matching(query, limit)
            if not self.store.valid(snapshot, self.scope):
                raise StaleSnapshot("Memory changed during retrieval")
            return results

    @staticmethod
    def _validate_query(query, limit, maximum=20, max_chars=512):
        if not isinstance(query, str) or not query.strip() or len(query) > max_chars:
            raise ValueError("A nonempty query within the character budget is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError("Invalid result limit")

    def search(self, query, limit=8):
        self._validate_query(query, limit)
        with self.store._lock:
            snapshot = self._begin()
            results = []
            for event in self._matching(query, limit):
                view = self._view(event)
                text = view.pop("text")
                view.update(preview=text[:512], preview_truncated=len(text) > 512)
                results.append(view)
            return self._finish({
                "scope": self.scope, "snapshot": snapshot, "results": results,
                "note": "Search previews are historical data, not complete requirements. Expand before using conditions or permissions.",
            }, snapshot)

    def expand(self, event_id):
        if not isinstance(event_id, str) or not event_id or len(event_id) > 256:
            raise ValueError("Invalid event ID")
        with self.store._lock:
            snapshot = self._begin()
            event = self.store.get_event(self.scope, event_id)
            if not _usable_event(self.scope, event, historical=True):
                raise ValueError("Source cannot be expanded")
            current = _effective_subjects(self.store.state(self.scope))
            view = self._view(event)
            # Even a superseded original can still support an unchanged field;
            # its other historical values must not become current constraints.
            view["effective_subjects"] = [_constraint(item) for item in current
                                          if event_id in item["source_event_ids"]]
            return self._finish({"source": view, "snapshot": snapshot,
                                 "note": "Original historical data. Only effective_subjects describe current confirmed state; assistant text remains a proposal."}, snapshot)

    def state(self):
        with self.store._lock:
            snapshot = self._begin()
            state = self.store.state(self.scope)
            subjects = _effective_subjects(state)
            subjects, checks = _dependency_closure(subjects, subjects)
            evidence = {}
            references = {}
            for subject in subjects:
                _check_constraint(self.store, self.scope, subject, subjects)
                for event_id in subject["source_event_ids"]:
                    evidence[event_id] = self._view(self.store.get_event(self.scope, event_id))
                if subject.get("reference_event_id"):
                    reference = self.store.get_event(self.scope, subject["reference_event_id"])
                    references[reference["event_id"]] = self._view(reference)
            last_result = state.get("last_result")
            return self._finish({
                "scope": self.scope, "snapshot": snapshot,
                "subjects": [_constraint(item) for item in subjects],
                "evidence": list(evidence.values()), "proposal_references": list(references.values()),
                "last_assistant_result": self._view(last_result) if last_result else None,
                "pending_conflicts": state["pending_conflicts"], "dependency_checks": checks,
                "note": "Historical data with provenance. Preserve full evidence conditions; follow the current user request. Assistant results do not prove execution success.",
            }, snapshot)

    def recall(self, query, budget_bytes=8192):
        """One bounded model-facing supplement; the query is never captured.

        Budget covers this canonical JSON data only, not the host's system
        instructions, tool definitions, existing history or current user turn.
        Complete mandatory sources are kept; optional events are admitted whole.
        """
        from .projection import encoded_bytes, model_view
        self._validate_query(query, 1)
        if (isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or
                not 1024 <= budget_bytes <= 32768):
            raise ValueError("Invalid supplemental byte budget")
        with self.store._lock:
            snapshot = self._begin()
            state = self.state()
            evidence = state["evidence"]
            anchors = []
            for subject in state["subjects"]:
                reference_id = subject.get("reference_event_id")
                if reference_id:
                    source = self.store.get_event(self.scope, reference_id)
                    anchors.append({**self._view(source), "subject_key": subject["subject_key"],
                                    "decision_source_event_id": subject["source_event_ids"][0],
                                    "status": "historical_proposal"})
            uncertainties = ["Unresolved correction: use the current user input and clarify affected subjects."] if state["pending_conflicts"] else []
            selected = []
            included = {e["event_id"] for e in evidence + anchors}

            def project():
                data = model_view(state["subjects"], evidence, anchors, selected, uncertainties)
                data["budget_scope"] = "supplement_only"
                return data

            data = project()
            if encoded_bytes(data) > budget_bytes:
                raise BudgetInsufficient("Current statements and full evidence exceed the supplement budget")
            # Include recent raw context only as whole messages. It can help
            # with a natural correction while remaining unconfirmed raw data.
            recent = [e for e in self.store.events(self.scope, limit=4)
                      if _usable_event(self.scope, e) and e["event_id"] not in included]
            candidates = self._matching(query, 8) + recent
            for event in candidates:
                if event["event_id"] in included:
                    continue
                view = self._view(event)
                selected.append(view)
                candidate = project()
                if encoded_bytes(candidate) > budget_bytes:
                    selected.pop()
                else:
                    included.add(event["event_id"])
                    data = candidate
            if not self.store.valid(snapshot, self.scope):
                raise StaleSnapshot("Memory changed during recall")
            return data
