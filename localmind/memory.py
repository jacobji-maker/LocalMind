"""Transactional, source-aware local state. No model output can grant authority.

The store is intentionally synchronous: one process and one serialized SQLite
writer. Network calls must run outside its transactions. SQLite 3.9+ / Python
3.9+ are sufficient; no package or model is needed for the deterministic core.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "0.3"


class MemoryErrorBase(RuntimeError):
    pass


class ScopeDenied(MemoryErrorBase):
    pass


class CapturePaused(MemoryErrorBase):
    pass


class StaleSnapshot(MemoryErrorBase):
    pass


class DedupeConflict(MemoryErrorBase):
    pass


class TurnBusy(MemoryErrorBase):
    pass


class ForgottenSource(MemoryErrorBase):
    pass


def _id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _copy(value):
    return json.loads(_json(value))


def _safe_usage(value):
    """Keep accounting without copying arbitrary provider text into audit data."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    containers = {"answer", "planner", "token_usage", "details"}
    numbers = {"input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
               "prompt_tokens", "completion_tokens", "prompt_eval_count", "eval_count", "total_duration",
               "load_duration", "prompt_eval_duration", "eval_duration", "answer_latency_ms",
               "planner_latency_ms", "latency_ms", "actual_tokens", "estimated_tokens"}
    tags = {"adapter", "model", "backend", "counter"}
    result = {}
    for key, item in value.items():
        if key in containers:
            result[key] = _safe_usage(item)
        elif key in numbers and (item is None or
                isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) and item >= 0):
            result[key] = item
        elif key in tags and (item is None or isinstance(item, str) and
                              re.fullmatch(r"[A-Za-z0-9._:/+\-]{1,128}", item)):
            result[key] = item
        elif key == "simulated" and isinstance(item, bool):
            result[key] = item
    return result


def _collect_sources(value):
    """Collect declared references, never interpret arbitrary text as an ID."""
    found = set()
    if isinstance(value, dict):
        # A recalled event is itself a dependency, in addition to its root
        # evidence. Deleting an assistant answer must also erase later packets
        # that quoted it, even though its root user input still exists.
        if value.get("kind") == "event" and isinstance(value.get("memory_id"), str):
            found.add(value["memory_id"])
        for key, child in value.items():
            if key in ("source_event_ids", "root_source_ids") and isinstance(child, list):
                found.update(item for item in child if isinstance(item, str))
            elif key in ("event_id", "input_event_id") and isinstance(child, str):
                found.add(child)
            else:
                found.update(_collect_sources(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_sources(child))
    return found


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._process_lock = None
        if self.path != ":memory:" and os.name == "posix":
            import fcntl
            self._process_lock = open(self.path + ".lock", "a")
            try:
                fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._process_lock.close()
                self._process_lock = None
                raise MemoryErrorBase("Another LocalMind runtime already owns this database")
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA secure_delete=ON")
        # DELETE avoids leaving historical prompt text in a persistent WAL file.
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects (
          project_id TEXT PRIMARY KEY, workspace_path TEXT NOT NULL, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks (
          project_id TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
          last_result_event_id TEXT, PRIMARY KEY(project_id,task_id),
          FOREIGN KEY(project_id) REFERENCES projects(project_id));
        CREATE TABLE IF NOT EXISTS events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
          project_id TEXT NOT NULL, task_id TEXT NOT NULL, logical_session_id TEXT NOT NULL,
          body TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_scope ON events(project_id,task_id,seq);
        CREATE TABLE IF NOT EXISTS turns (
          turn_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
          logical_session_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, body TEXT NOT NULL,
          UNIQUE(project_id,task_id,logical_session_id,dedupe_key));
        CREATE TABLE IF NOT EXISTS subjects (
          scope_key TEXT NOT NULL, subject_key TEXT NOT NULL, revision INTEGER NOT NULL,
          body TEXT NOT NULL, PRIMARY KEY(scope_key,subject_key,revision));
        CREATE TABLE IF NOT EXISTS artifacts (
          artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
          body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS checkpoints (
          checkpoint_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT NOT NULL,
          logical_session_id TEXT NOT NULL, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tombstones (
          scope_key TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(scope_key,content_hash));
        """)
        with self._write():
            old_version = self._meta("schema_version")
            if old_version is not None and old_version != SCHEMA_VERSION:
                raise ValueError("Unsupported database protocol version: " + str(old_version))
            for key, value in {
                "schema_version": SCHEMA_VERSION, "capture_state": "active",
                "capture_epoch": 0, "deletion_epoch": 0, "policy_revision": 0,
                "global_revision": 0,
            }.items():
                if self._meta(key) is None:
                    self._set_meta(key, value)
        # Restart never silently resends requests whose remote outcome is unknown.
        self.recover()

    def close(self):
        with self._lock:
            self.db.close()
            if self._process_lock is not None:
                self._process_lock.close()
                self._process_lock = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _write(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", (key, _json(value)))

    def register_project(self, project_id, workspace_path=""):
        self._identifier(project_id)
        workspace_path = str(workspace_path)
        with self._write():
            row = self.db.execute("SELECT workspace_path FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if row and row[0] != workspace_path and workspace_path:
                raise ValueError("Project already registered to another workspace")
            self.db.execute("INSERT OR IGNORE INTO projects VALUES (?,?,0)", (project_id, workspace_path))
        return {"project_id": project_id, "workspace_path": row[0] if row else workspace_path}

    def register_task(self, project_id, task_id):
        self._identifier(task_id)
        with self._write():
            if not self.db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
                raise ScopeDenied("Project is not registered")
            self.db.execute("INSERT OR IGNORE INTO tasks VALUES (?,?,0,NULL)", (project_id, task_id))
        return {"project_id": project_id, "task_id": task_id}

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("Identifiers must be nonempty strings of at most 256 characters")

    def _scope(self, scope):
        if not isinstance(scope, dict):
            raise ScopeDenied("A registered scope is required")
        result = {}
        for key in ("project_id", "task_id", "logical_session_id"):
            self._identifier(scope.get(key))
            result[key] = scope[key]
        row = self.db.execute("SELECT 1 FROM tasks WHERE project_id=? AND task_id=?",
                              (result["project_id"], result["task_id"])).fetchone()
        if not row:
            raise ScopeDenied("Project/task is not registered")
        return result

    @staticmethod
    def _scope_key(scope, level):
        if level == "global":
            return _json(["global"])
        if level == "project":
            return _json(["project", scope["project_id"]])
        if level == "task":
            return _json(["task", scope["project_id"], scope["task_id"]])
        raise ValueError("Unsupported scope_level")

    def _subject_rows(self, scope):
        keys = [self._scope_key(scope, level) for level in ("global", "project", "task")]
        rows = self.db.execute("SELECT body FROM subjects WHERE scope_key IN (?,?,?) ORDER BY revision",
                               keys).fetchall()
        latest = {}
        for row in rows:
            subject = json.loads(row[0])
            latest[(subject["scope_key"], subject["subject_key"])] = subject
        return list(latest.values())

    def _snapshot(self, scope):
        scope = self._scope(scope)
        project_revision = self.db.execute("SELECT revision FROM projects WHERE project_id=?",
                                           (scope["project_id"],)).fetchone()[0]
        task_revision = self.db.execute("SELECT revision FROM tasks WHERE project_id=? AND task_id=?",
                                        (scope["project_id"], scope["task_id"])).fetchone()[0]
        return {
            "scope": scope,
            "scope_revisions": {"global": self._meta("global_revision"),
                                "project": project_revision, "task": task_revision},
            "subject_revisions": [{"subject_key": s["subject_key"], "scope_level": s["scope_level"],
                                   "effective_revision": s["effective_revision"]}
                                  for s in self._subject_rows(scope) if s["status"] == "active"],
            "policy_revision": self._meta("policy_revision"),
            "capture_epoch": self._meta("capture_epoch"),
            "deletion_epoch": self._meta("deletion_epoch"),
        }

    def snapshot(self, scope):
        with self._lock:
            return self._snapshot(scope)

    def _valid(self, snapshot, scope):
        return self._meta("capture_state") == "active" and snapshot == self._snapshot(scope)

    def valid(self, snapshot, scope):
        with self._lock:
            try:
                return self._valid(snapshot, scope)
            except (ValueError, ScopeDenied):
                return False

    def _bump(self, scope, level):
        if level == "global":
            self._set_meta("global_revision", self._meta("global_revision") + 1)
        elif level == "project":
            self.db.execute("UPDATE projects SET revision=revision+1 WHERE project_id=?", (scope["project_id"],))
        else:
            self.db.execute("UPDATE tasks SET revision=revision+1 WHERE project_id=? AND task_id=?",
                            (scope["project_id"], scope["task_id"]))

    def _event(self, event_id):
        row = self.db.execute("SELECT seq,body FROM events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            raise KeyError(event_id)
        result = json.loads(row["body"])
        result["seq"] = row["seq"]
        result["text"] = result["raw_content"]
        return result

    def _event_allowed(self, scope, event):
        if event["scope_level"] == "global":
            return True
        source_scope = event["scope"]
        if source_scope["project_id"] != scope["project_id"]:
            return False
        return event["scope_level"] == "project" or source_scope["task_id"] == scope["task_id"]

    def _get_event(self, scope, event_id):
        scope = self._scope(scope)
        result = self._event(event_id)
        if not self._event_allowed(scope, result):
            raise ScopeDenied("Event is outside this registered scope")
        if result["status"] == "deleted":
            raise ForgottenSource("Event has been forgotten")
        return result

    def get_event(self, scope, event_id):
        with self._lock:
            result = self._get_event(scope, event_id)
            result["effective"] = event_id not in self._suppressed(scope)
            return result

    def _insert_event(self, scope, content, actor, origin_kind, turn_id=None,
                      source_event_ids=None, parent_event_id=None, scope_level="task",
                      generated_context_id=None, dedupe_key=None):
        content_hash = _hash(content)
        tombstone_keys = (self._scope_key(scope, "project"), self._scope_key(scope, "global"))
        if self.db.execute("SELECT 1 FROM tombstones WHERE scope_key IN (?,?) AND content_hash=?",
                           (*tombstone_keys, content_hash)).fetchone():
            raise ForgottenSource("Content matches a deletion tombstone; explicit reauthorization is required")
        event_id = _id("evt")
        roots = set()
        for source_id in source_event_ids or []:
            source = self._get_event(scope, source_id)
            roots.update(source["root_source_ids"] or [source_id])
        if not roots:
            roots.add(event_id)
        event = {
            "schema_version": SCHEMA_VERSION, "event_id": event_id,
            "scope": scope, "scope_level": scope_level, "actor": actor,
            "origin_kind": origin_kind, "raw_content": content,
            "content_hash": content_hash, "root_source_ids": sorted(roots),
            "source_event_ids": sorted(set(source_event_ids or [])),
            "parent_event_id": parent_event_id, "turn_id": turn_id,
            "generated_context_id": generated_context_id, "dedupe_key": dedupe_key,
            "status": "active", "occurred_at": _now(), "ingested_at": _now(),
        }
        self.db.execute("INSERT INTO events(event_id,project_id,task_id,logical_session_id,body) VALUES (?,?,?,?,?)",
                        (event_id, scope["project_id"], scope["task_id"], scope["logical_session_id"], _json(event)))
        return self._event(event_id)

    def _turn(self, turn_id):
        row = self.db.execute("SELECT body FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        if not row:
            raise KeyError(turn_id)
        return json.loads(row[0])

    def turn(self, turn_id):
        with self._lock:
            return self._turn(turn_id)

    def _put_turn(self, turn):
        turn["lifecycle"] = turn["phase"]
        self.db.execute("UPDATE turns SET body=? WHERE turn_id=?", (_json(turn), turn["turn_id"]))

    def _stale_turns(self):
        for row in self.db.execute("SELECT body FROM turns").fetchall():
            turn = json.loads(row[0])
            if turn["freshness"] == "current" and not self._valid(turn["snapshot"], turn["scope"]):
                turn["freshness"] = "superseded"
                self._put_turn(turn)

    def receive(self, scope, text, dedupe_key, changes=None, pending_conflicts=None):
        if not isinstance(text, str) or not text:
            raise ValueError("User text cannot be empty")
        self._identifier(dedupe_key)
        changes = _copy(changes or [])
        conflicts = _copy(pending_conflicts or [])
        if not isinstance(changes, list) or not isinstance(conflicts, list):
            raise ValueError("changes and pending_conflicts must be lists")
        seen = set()
        for change in changes:
            key, value = change.get("subject_key"), change.get("value")
            self._identifier(key)
            level = change.get("scope_level", "task")
            if level not in ("global", "project", "task"):
                raise ValueError("Invalid scope_level")
            if change.get("kind", "requirement") not in ("goal", "requirement", "decision", "fact"):
                raise ValueError("Invalid subject kind")
            if not isinstance(value, str) or not value or value not in text:
                raise ValueError("Explicit subject value must quote the current user input")
            if change.get("authority", "user_stated") != "user_stated" or change.get("status", "active") != "active":
                raise ValueError("Candidate or inferred material cannot become a user requirement")
            if (level, key) in seen:
                raise ValueError("One update per subject/scope per input")
            seen.add((level, key))
            dependencies = change.get("dependencies", [])
            if not isinstance(dependencies, list) or any(not isinstance(k, str) or not k for k in dependencies):
                raise ValueError("dependencies must be subject keys")
        has_global = any(c.get("scope_level") == "global" for c in changes)
        if has_global and (len(changes) != 1 or changes[0]["value"] != text or conflicts):
            raise ValueError("Global statements must be a single standalone user input equal to their value")
        if len({c.get("scope_level", "task") for c in changes}) > 1:
            raise ValueError("One input may update only one scope level; split project and task statements")
        with self._write():
            scope = self._scope(scope)
            if self._meta("capture_state") != "active":
                raise CapturePaused("Capture is paused; input was not saved")
            old = self.db.execute("SELECT body FROM turns WHERE project_id=? AND task_id=? AND logical_session_id=? AND dedupe_key=?",
                                  (scope["project_id"], scope["task_id"], scope["logical_session_id"], dedupe_key)).fetchone()
            input_hash = _hash(_json({"text": text, "changes": changes, "pending_conflicts": conflicts}))
            if old:
                result = json.loads(old[0])
                if result["input_hash"] != input_hash:
                    raise DedupeConflict("Retry key was already used with different input")
                return result
            # Ordinary inputs serialize within a logical session. Explicit state
            # corrections are admitted immediately and supersede queued work.
            if not changes and not conflicts:
                for row in self.db.execute("SELECT body FROM turns WHERE project_id=? AND logical_session_id=?",
                                           (scope["project_id"], scope["logical_session_id"])).fetchall():
                    busy = json.loads(row[0])
                    if busy["phase"] in ("received", "compiled", "sent") and busy["freshness"] == "current":
                        raise TurnBusy("A foreground turn is already active; queue this input")
            turn_id = _id("turn")
            source_level = "global" if has_global else ("project" if changes and all(c.get("scope_level") == "project" for c in changes) else "task")
            event = self._insert_event(scope, text, "user", "user_input", turn_id=turn_id,
                                       scope_level=source_level, dedupe_key=dedupe_key)
            for change in changes:
                level = change.get("scope_level", "task")
                scope_key = self._scope_key(scope, level)
                rows = self.db.execute("SELECT revision,body FROM subjects WHERE scope_key=? AND subject_key=? ORDER BY revision DESC LIMIT 1",
                                       (scope_key, change["subject_key"])).fetchall()
                revision = rows[0]["revision"] + 1 if rows else 1
                if rows:
                    previous = json.loads(rows[0]["body"])
                    previous["status"] = "superseded"
                    self.db.execute("UPDATE subjects SET body=? WHERE scope_key=? AND subject_key=? AND revision=?",
                                    (_json(previous), scope_key, change["subject_key"], rows[0]["revision"]))
                reference_event_id = change.get("reference_event_id")
                is_reference = change.get("kind") == "decision" and re.search(
                    r"第(?:[一二三四五六七八九十]|[0-9])+个|刚才|那个", change["value"])
                if reference_event_id is None and is_reference:
                    for row in self.db.execute("SELECT event_id FROM events WHERE project_id=? AND task_id=? ORDER BY seq DESC",
                                               (scope["project_id"], scope["task_id"])):
                        possible = self._event(row[0])
                        if possible["status"] == "active" and possible["actor"] == "assistant":
                            reference_event_id = possible["event_id"]
                            break
                if reference_event_id is not None:
                    reference = self._get_event(scope, reference_event_id)
                    if reference["actor"] != "assistant" or reference["seq"] >= event["seq"]:
                        raise ValueError("Decision reference must be an earlier assistant proposal in this scope")
                    if (level == "global" and reference["scope_level"] != "global" or
                            level == "project" and reference["scope_level"] == "task"):
                        raise ScopeDenied("A shared decision cannot depend on a narrower private proposal")
                    # The user's choice is a new independent statement, with a
                    # direct semantic dependency on the proposal it selected.
                    event["source_event_ids"] = sorted(set(event["source_event_ids"] + [reference_event_id]))
                    saved_event = {k: v for k, v in event.items() if k not in ("seq", "text")}
                    self.db.execute("UPDATE events SET body=? WHERE event_id=?", (_json(saved_event), event["event_id"]))
                subject = {
                    "subject_key": change["subject_key"], "value": change["value"],
                    "scope_level": level, "scope_key": scope_key,
                    "kind": change.get("kind", "requirement"), "authority": "user_stated",
                    "status": "active", "effective_revision": revision,
                    "source_event_ids": [event["event_id"]],
                    "dependencies": change.get("dependencies", []),
                    "reference_event_id": reference_event_id,
                }
                self.db.execute("INSERT INTO subjects VALUES (?,?,?,?)",
                                (scope_key, change["subject_key"], revision, _json(subject)))
                self._bump(scope, level)
            for conflict in conflicts:
                candidate_keys = conflict.get("subject_keys", [])
                if conflict.get("subject_key"):
                    candidate_keys = list(set(candidate_keys + [conflict["subject_key"]]))
                # An unlocated correction is unknown, with no invented suppression.
                conflict["status"] = "pending" if candidate_keys else "unknown"
                conflict["source_event_ids"] = [event["event_id"]]
                conflict["subject_keys"] = candidate_keys
                for subject in self._subject_rows(scope):
                    if subject["subject_key"] in candidate_keys and subject["status"] == "active":
                        subject["status"] = "suppressed"
                        self.db.execute("UPDATE subjects SET body=? WHERE scope_key=? AND subject_key=? AND revision=?",
                                        (_json(subject), subject["scope_key"], subject["subject_key"], subject["effective_revision"]))
                        self._bump(scope, subject["scope_level"])
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                                ("conflict:" + _id("conflict"), _json({**conflict, "scope": scope})))
            # New input is itself a task-state change. This also prevents an
            # older response from another logical session of the same task from
            # winning a race and replacing the newer task result.
            self._bump(scope, "task")
            self._stale_turns()
            turn = {
                "schema_version": SCHEMA_VERSION, "turn_id": turn_id,
                "scope": scope, "input_event_id": event["event_id"], "input_hash": input_hash,
                "snapshot": self._snapshot(scope), "phase": "received", "lifecycle": "received",
                "freshness": "current", "request_id": None, "attempt_id": None,
                "context_id": None, "remote_thread_id": None, "packet": None,
                "messages": None, "source_event_ids": [event["event_id"]],
                "usage": None, "error": None, "outcome": None, "created_at": _now(),
                "completed_event_id": None,
            }
            self.db.execute("INSERT INTO turns VALUES (?,?,?,?,?,?)",
                            (turn_id, scope["project_id"], scope["task_id"], scope["logical_session_id"], dedupe_key, _json(turn)))
            return turn

    def _suppressed(self, scope):
        keys = [self._scope_key(scope, level) for level in ("global", "project", "task")]
        all_subjects = [json.loads(row[0]) for row in self.db.execute("SELECT body FROM subjects WHERE scope_key IN (?,?,?)", keys)]
        suppressed = {event_id for subject in all_subjects if subject["status"] != "active"
                      for event_id in subject["source_event_ids"]}
        # A source shared by an unchanged active subject still cannot safely be
        # replayed wholesale: the compiler must quote only that subject's value.
        changed = True
        rows = self.db.execute("SELECT event_id,body FROM events WHERE project_id=? AND task_id=?",
                               (scope["project_id"], scope["task_id"])).fetchall()
        while changed:
            changed = False
            for row in rows:
                event = json.loads(row["body"])
                if row["event_id"] not in suppressed and set(event["root_source_ids"]) & suppressed:
                    suppressed.add(row["event_id"])
                    changed = True
        return suppressed

    def suppressed_event_ids(self, scope):
        with self._lock:
            return self._suppressed(self._scope(scope))

    def events(self, scope, limit=20):
        if not isinstance(limit, int) or not 0 <= limit <= 1000:
            raise ValueError("Event limit must be between 0 and 1000")
        with self._lock:
            scope = self._scope(scope)
            suppressed = self._suppressed(scope)
            results = []
            for row in self.db.execute("SELECT event_id FROM events WHERE project_id=? AND task_id=? ORDER BY seq DESC",
                                       (scope["project_id"], scope["task_id"])):
                event = self._event(row[0])
                if event["status"] == "active":
                    event["effective"] = event["event_id"] not in suppressed
                    results.append(event)
                    if len(results) >= limit:
                        break
            return list(reversed(results)) if limit else []

    def search_events(self, scope, query, limit=24):
        """Optional full-history recall backend; scope and lifecycle stay in the reader."""
        from .retrieval import MemoryReader
        return MemoryReader(self, scope).matching_events(query, limit)

    def state(self, scope):
        with self._lock:
            scope = self._scope(scope)
            row = self.db.execute("SELECT revision,last_result_event_id FROM tasks WHERE project_id=? AND task_id=?",
                                  (scope["project_id"], scope["task_id"])).fetchone()
            subjects = [s for s in self._subject_rows(scope) if s["status"] == "active"]
            conflicts = []
            for conflict_row in self.db.execute("SELECT value FROM metadata WHERE key LIKE 'conflict:%'"):
                conflict = json.loads(conflict_row[0])
                conflict_scope = conflict["scope"]
                if conflict_scope["project_id"] == scope["project_id"] and conflict_scope["task_id"] == scope["task_id"]:
                    # A new explicit value resolves only its named subjects.
                    resolved = {s["subject_key"] for s in subjects}
                    outstanding = [k for k in conflict["subject_keys"] if k not in resolved]
                    if outstanding or conflict["status"] == "unknown":
                        conflicts.append(conflict)
            last_result = None
            if row["last_result_event_id"]:
                try:
                    last_result = self._get_event(scope, row["last_result_event_id"])
                    if last_result["event_id"] in self._suppressed(scope):
                        last_result = None
                except (KeyError, ForgottenSource):
                    pass
            return {"schema_version": SCHEMA_VERSION, "scope": scope, "revision": row["revision"],
                    "subjects": subjects, "pending_conflicts": conflicts, "last_result": last_result}

    def finish_capture(self, turn_id):
        """Finish a trusted input-only capture without fabricating a model request or answer."""
        with self._write():
            turn = self._turn(turn_id)
            if turn["phase"] == "completed" and turn.get("outcome") == "captured":
                return turn
            if turn["phase"] != "received":
                raise ValueError("Only a newly received input can finish as capture-only")
            if turn["freshness"] != "current" or not self._valid(turn["snapshot"], turn["scope"]):
                raise StaleSnapshot("Capture changed or was paused before completion")
            turn.update(phase="completed", outcome="captured", completed_at=_now())
            self._put_turn(turn)
            return turn

    def record_context(self, turn_id, packet):
        with self._write():
            turn = self._turn(turn_id)
            if turn["phase"] not in ("received", "compiled"):
                raise ValueError("Only an unsent turn can be compiled")
            if turn["freshness"] != "current" or not self._valid(turn["snapshot"], turn["scope"]):
                raise StaleSnapshot("Context changed before compilation committed")
            if packet.get("turn_id") not in (None, turn_id):
                raise ValueError("Packet belongs to another turn")
            if packet.get("snapshot", turn["snapshot"]) != turn["snapshot"]:
                raise StaleSnapshot("Packet snapshot differs from received input")
            sources = _collect_sources(packet) | {turn["input_event_id"]}
            for event_id in sources:
                self._get_event(turn["scope"], event_id)
            turn.update(packet=_copy(packet), context_id=packet["context_id"], phase="compiled",
                        source_event_ids=sorted(sources))
            self._put_turn(turn)
            return turn

    def mark_sent(self, turn_id, request_id, context_id, remote_thread_id, messages):
        with self._write():
            turn = self._turn(turn_id)
            if turn["phase"] != "compiled":
                raise ValueError("Only a compiled turn may be sent once")
            if turn["freshness"] != "current" or not self._valid(turn["snapshot"], turn["scope"]):
                raise StaleSnapshot("Context changed before send")
            if context_id != turn["context_id"]:
                raise ValueError("Mismatched context_id")
            self._identifier(request_id)
            for source_id in turn["source_event_ids"]:
                self._get_event(turn["scope"], source_id)
            turn.update(phase="sent", request_id=request_id, attempt_id=_id("attempt"),
                        remote_thread_id=remote_thread_id, messages=_copy(messages), sent_at=_now())
            self._put_turn(turn)
            return turn

    def record_remote_thread(self, turn_id, request_id, remote_thread_id):
        """Record only the transport's opaque identity, without changing state freshness."""
        self._identifier(remote_thread_id)
        if not re.fullmatch(r"[A-Za-z0-9._:/+\-]{1,256}", remote_thread_id):
            raise ValueError("Remote thread identity must be a bounded opaque identifier")
        with self._write():
            turn = self._turn(turn_id)
            if turn["request_id"] != request_id:
                raise ValueError("Remote thread request_id does not match")
            if turn["phase"] not in ("sent", "outcome_unknown", "completed"):
                raise ValueError("Only a sent request can have a remote thread identity")
            old = turn.get("remote_thread_id")
            if old is not None and old != remote_thread_id:
                raise DedupeConflict("A different remote thread was already recorded")
            turn["remote_thread_id"] = remote_thread_id
            self._put_turn(turn)
            return turn

    def complete(self, turn_id, text, request_id, message_id, usage=None):
        if not isinstance(text, str):
            raise ValueError("Final text must be a string")
        self._identifier(message_id)
        with self._write():
            turn = self._turn(turn_id)
            if turn["request_id"] != request_id:
                raise ValueError("Final result request_id does not match")
            if turn["phase"] == "completed":
                if turn.get("message_id") != message_id:
                    raise DedupeConflict("A different final message was already committed")
                if turn.get("result_hash") not in (None, _hash(text)):
                    raise DedupeConflict("A final message retry changed content")
                if turn.get("completed_event_id"):
                    try:
                        return self._get_event(turn["scope"], turn["completed_event_id"])
                    except ForgottenSource:
                        pass
                return {"discarded": True, "reason": turn.get("outcome", "superseded")}
            if turn["phase"] not in ("sent", "outcome_unknown"):
                raise ValueError("No matching sent request awaits this result")
            if turn["freshness"] != "current" or not self._valid(turn["snapshot"], turn["scope"]):
                turn["phase"] = "completed"
                turn["freshness"] = "forgotten" if turn["freshness"] == "forgotten" else "superseded"
                turn["outcome"] = "discarded_" + turn["freshness"]
                turn["message_id"] = message_id
                turn["result_hash"] = _hash(text)
                turn["usage"] = _safe_usage(usage)
                self._put_turn(turn)
                return {"discarded": True, "reason": turn["outcome"]}
            for source_id in turn["source_event_ids"]:
                self._get_event(turn["scope"], source_id)
            event = self._insert_event(turn["scope"], text, "assistant", "assistant_output",
                                       turn_id=turn_id, source_event_ids=turn["source_event_ids"],
                                       parent_event_id=turn["input_event_id"],
                                       generated_context_id=turn["context_id"],
                                       dedupe_key=request_id + ":" + message_id)
            turn.update(phase="completed", message_id=message_id, completed_event_id=event["event_id"],
                        result_hash=_hash(text), usage=_safe_usage(usage), outcome="completed", completed_at=_now())
            self._put_turn(turn)
            self.db.execute("UPDATE tasks SET last_result_event_id=? WHERE project_id=? AND task_id=?",
                            (event["event_id"], turn["scope"]["project_id"], turn["scope"]["task_id"]))
            return event

    def fail(self, turn_id, reason, outcome_unknown=False, usage=None):
        """Reasons are program error codes, not exception strings with raw prompts."""
        if not isinstance(reason, str) or not reason or len(reason) > 128:
            raise ValueError("Use a short non-sensitive error code")
        with self._write():
            turn = self._turn(turn_id)
            if turn["phase"] == "completed":
                return turn
            turn.update(phase="outcome_unknown" if outcome_unknown else "failed", error=reason,
                        outcome="unknown" if outcome_unknown else "failed")
            if usage is not None:
                turn["usage"] = _safe_usage(usage)
            self._put_turn(turn)
            return turn

    def recover(self):
        """Resolve interrupted ownership without automatically sending a turn.

        A reopened Store has exclusive database ownership. Its former owner
        cannot still be executing an unsent foreground turn, including when a
        short-lived MCP read opens the database. Preserve the committed user
        event, but terminate received/compiled work so it cannot block future
        input forever. An explicit new user request needs a new dedupe key;
        retrying the interrupted key returns its terminal cancellation.

        Sent work remains outcome_unknown because its remote effect cannot be
        inferred from a local restart. Neither path automatically resends.
        """
        changed = []
        with self._write():
            for row in self.db.execute("SELECT body FROM turns").fetchall():
                turn = json.loads(row[0])
                if turn["phase"] == "sent":
                    turn.update(phase="outcome_unknown", outcome="unknown", error="RESTART_REMOTE_OUTCOME_UNKNOWN")
                    self._put_turn(turn)
                    changed.append(turn["turn_id"])
                elif turn["phase"] in ("received", "compiled"):
                    turn.update(phase="cancelled", outcome="failed", error="RESTART_UNSENT_INTERRUPTED")
                    self._put_turn(turn)
                    changed.append(turn["turn_id"])
        return changed

    def pause(self):
        with self._write():
            if self._meta("capture_state") != "paused":
                self._set_meta("capture_state", "paused")
                self._set_meta("capture_epoch", self._meta("capture_epoch") + 1)
                self._stale_turns()
        return self.status()

    def resume(self):
        with self._write():
            if self._meta("capture_state") != "active":
                self._set_meta("capture_state", "active")
                self._set_meta("capture_epoch", self._meta("capture_epoch") + 1)
        return self.status()

    def status(self):
        with self._lock:
            return {"schema_version": self._meta("schema_version"),
                    "capture_state": self._meta("capture_state"),
                    "capture_epoch": self._meta("capture_epoch"),
                    "deletion_epoch": self._meta("deletion_epoch"),
                    "policy_revision": self._meta("policy_revision"),
                    "sqlite_version": sqlite3.sqlite_version,
                    "event_count": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
                    "turn_count": self.db.execute("SELECT count(*) FROM turns").fetchone()[0]}

    def _subject_dependencies_valid(self, scope, dependencies):
        current = {(s["scope_level"], s["subject_key"]): s for s in self._subject_rows(scope)}
        for dependency in dependencies:
            value = current.get((dependency.get("scope_level", "task"), dependency["subject_key"]))
            if not value or value["status"] != "active" or value["effective_revision"] != dependency["effective_revision"]:
                return False
        return True

    def save_artifact(self, scope, content, source_event_ids, subject_revisions, snapshot, kind="summary"):
        if not isinstance(content, str) or not source_event_ids:
            raise ValueError("Derived material needs text and real sources")
        if kind not in ("summary", "candidate", "experience", "index"):
            raise ValueError("Unsupported derived artifact kind")
        with self._write():
            scope = self._scope(scope)
            if not self._valid(snapshot, scope):
                raise StaleSnapshot("Background result is older than current state")
            roots = set()
            suppressed = self._suppressed(scope)
            for source_id in source_event_ids:
                source = self._get_event(scope, source_id)
                if source_id in suppressed:
                    raise StaleSnapshot("Background result depends on a superseded source")
                roots.update(source["root_source_ids"])
            if not self._subject_dependencies_valid(scope, subject_revisions):
                raise StaleSnapshot("Derived subject versions are stale")
            # Any state-bearing source must carry its subject version as well as
            # the immutable event ID; otherwise correction would not invalidate it.
            covered = {(d.get("scope_level", "task"), d["subject_key"], d["effective_revision"])
                       for d in subject_revisions}
            for subject in self._subject_rows(scope):
                if set(subject["source_event_ids"]) & (set(source_event_ids) | roots):
                    expected = (subject["scope_level"], subject["subject_key"], subject["effective_revision"])
                    if expected not in covered:
                        raise ValueError("State-bearing summary must declare effective subject dependencies")
            artifact = {
                "artifact_id": _id("artifact"), "scope": scope, "content": content,
                "kind": kind, "authority": "inferred", "status": "candidate",
                "source_event_ids": sorted(set(source_event_ids)), "root_source_ids": sorted(roots),
                "subject_revisions": _copy(subject_revisions), "snapshot": _copy(snapshot),
                "created_at": _now(),
            }
            self.db.execute("INSERT INTO artifacts VALUES (?,?,?,?)",
                            (artifact["artifact_id"], scope["project_id"], scope["task_id"], _json(artifact)))
            return artifact

    def artifacts(self, scope):
        with self._lock:
            scope = self._scope(scope)
            suppressed = self._suppressed(scope)
            results = []
            for row in self.db.execute("SELECT body FROM artifacts WHERE project_id=? AND task_id=?",
                                       (scope["project_id"], scope["task_id"])):
                artifact = json.loads(row[0])
                if artifact["status"] == "deleted" or not self._subject_dependencies_valid(scope, artifact["subject_revisions"]):
                    continue
                try:
                    for event_id in artifact["source_event_ids"]:
                        self._get_event(scope, event_id)
                        if event_id in suppressed:
                            raise StaleSnapshot("Suppressed source")
                except (ForgottenSource, StaleSnapshot, KeyError):
                    continue
                results.append(artifact)
            return results

    def append_observation(self, scope, text, source_event_ids, snapshot, dedupe_key=None):
        """Explicit observed data only. It never creates an active requirement."""
        if not isinstance(text, str) or not text or not source_event_ids:
            raise ValueError("Observation needs text and input sources")
        with self._write():
            scope = self._scope(scope)
            if not self._valid(snapshot, scope):
                raise StaleSnapshot("Observation is stale or capture is paused")
            if dedupe_key:
                for row in self.db.execute("SELECT event_id FROM events WHERE project_id=? AND task_id=?",
                                           (scope["project_id"], scope["task_id"])):
                    event = self._event(row[0])
                    if event["dedupe_key"] == dedupe_key and event["origin_kind"] == "tool_result":
                        if event["content_hash"] != _hash(text):
                            raise DedupeConflict("Observation retry changed content")
                        return event
            return self._insert_event(scope, text, "tool", "tool_result", source_event_ids=source_event_ids,
                                      dedupe_key=dedupe_key)

    def save_checkpoint(self, scope, recent_event_ids=None, workspace_snapshot=None):
        with self._write():
            scope = self._scope(scope)
            if self._meta("capture_state") != "active":
                raise CapturePaused("Checkpoint not saved while capture is paused")
            state = self.state(scope)
            recent = list(recent_event_ids or [])
            for event_id in recent:
                self._get_event(scope, event_id)
            source_ids = set(recent)
            source_ids.update(source_id for s in state["subjects"] for source_id in s["source_event_ids"])
            source_ids.update(_collect_sources(state["pending_conflicts"]))
            if state["last_result"]:
                source_ids.add(state["last_result"]["event_id"])
            checkpoint = {
                "schema_version": SCHEMA_VERSION, "checkpoint_id": _id("checkpoint"),
                "scope": scope, "snapshot": self._snapshot(scope),
                "subjects": state["subjects"], "last_result": state["last_result"],
                "pending_conflicts": state["pending_conflicts"], "recent_event_ids": recent,
                "source_event_ids": sorted(source_ids),
                "workspace_snapshot": workspace_snapshot, "created_at": _now(),
            }
            self.db.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?)",
                            (checkpoint["checkpoint_id"], scope["project_id"], scope["task_id"],
                             scope["logical_session_id"], _json(checkpoint)))
            return checkpoint

    def checkpoint(self, scope):
        with self._lock:
            scope = self._scope(scope)
            for row in self.db.execute("SELECT body FROM checkpoints WHERE project_id=? AND task_id=? ORDER BY rowid DESC",
                                       (scope["project_id"], scope["task_id"])):
                checkpoint = json.loads(row[0])
                # Task checkpoints may bridge logical sessions, while a TurnRun
                # snapshot remains strictly bound to its receiving session.
                comparison = _copy(checkpoint.get("snapshot", {}))
                comparison["scope"] = scope
                if not checkpoint.get("deleted") and self._valid(comparison, scope):
                    return checkpoint
            return None

    def forget(self, scope, event_id):
        with self._write():
            scope = self._scope(scope)
            original = self._get_event(scope, event_id)
            forgotten = {event_id}
            # Include derived event roots transitively; never rewrite an unrelated
            # user's newer explicit correction into a dependency of its old value.
            all_events = [self._event(row[0]) for row in self.db.execute("SELECT event_id FROM events")]
            changed = True
            while changed:
                changed = False
                for event in all_events:
                    dependencies = set(event["root_source_ids"]) | set(event.get("source_event_ids", []))
                    if event["event_id"] not in forgotten and dependencies & forgotten:
                        forgotten.add(event["event_id"])
                        changed = True
            for event in all_events:
                if event["event_id"] not in forgotten:
                    continue
                tombstone_level = "global" if event["scope_level"] == "global" else "project"
                self.db.execute("INSERT OR IGNORE INTO tombstones VALUES (?,?,?)",
                                (self._scope_key(event["scope"], tombstone_level), event["content_hash"], _now()))
                event["raw_content"] = ""
                event.pop("text", None)
                event.pop("seq", None)
                event["status"] = "deleted"
                self.db.execute("UPDATE events SET body=? WHERE event_id=?", (_json(event), event["event_id"]))
            for row in self.db.execute("SELECT scope_key,subject_key,revision,body FROM subjects").fetchall():
                subject = json.loads(row["body"])
                if set(subject["source_event_ids"]) & forgotten or subject.get("reference_event_id") in forgotten:
                    subject.update(value="", status="deleted")
                    self.db.execute("UPDATE subjects SET body=? WHERE scope_key=? AND subject_key=? AND revision=?",
                                    (_json(subject), row["scope_key"], row["subject_key"], row["revision"]))
            for row in self.db.execute("SELECT body FROM turns").fetchall():
                turn = json.loads(row[0])
                dependencies = set(turn["source_event_ids"]) | _collect_sources(turn.get("packet"))
                if dependencies & forgotten or turn.get("completed_event_id") in forgotten:
                    turn.update(packet=None, messages=None, freshness="forgotten", outcome="forgotten")
                    self._put_turn(turn)
            for row in self.db.execute("SELECT artifact_id,body FROM artifacts").fetchall():
                artifact = json.loads(row["body"])
                if (set(artifact["source_event_ids"]) | set(artifact["root_source_ids"])) & forgotten:
                    artifact.update(content="", status="deleted")
                    self.db.execute("UPDATE artifacts SET body=? WHERE artifact_id=?", (_json(artifact), row["artifact_id"]))
            for row in self.db.execute("SELECT checkpoint_id,body FROM checkpoints").fetchall():
                checkpoint = json.loads(row["body"])
                if set(checkpoint["source_event_ids"]) & forgotten:
                    safe = {k: checkpoint[k] for k in ("schema_version", "checkpoint_id", "scope", "created_at")}
                    safe["deleted"] = True
                    self.db.execute("UPDATE checkpoints SET body=? WHERE checkpoint_id=?", (_json(safe), row["checkpoint_id"]))
            for row in self.db.execute("SELECT key,value FROM metadata WHERE key LIKE 'conflict:%'").fetchall():
                conflict = json.loads(row["value"])
                if set(conflict.get("source_event_ids", [])) & forgotten:
                    self.db.execute("DELETE FROM metadata WHERE key=?", (row["key"],))
            self.db.execute("UPDATE tasks SET last_result_event_id=NULL WHERE last_result_event_id IN (" +
                            ",".join("?" for _ in forgotten) + ")", sorted(forgotten))
            self._set_meta("deletion_epoch", self._meta("deletion_epoch") + 1)
            self._stale_turns()
            return {"forgotten_event_ids": sorted(forgotten), "deletion_epoch": self._meta("deletion_epoch"),
                    "physical_erasure": "sqlite_secure_delete; filesystem snapshots and external copies unverified"}
