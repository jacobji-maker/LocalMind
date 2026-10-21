"""Model-facing historical data, separated from the full local audit packet.

Callers validate authority and effective sources before projecting. This module
deduplicates originals but never summarizes, truncates or rewrites evidence.
"""
from __future__ import annotations

import json

from .choices import affirmative_ordinal, parse_numbered_options


def encoded_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def model_view(subjects, evidence, proposals=(), memories=(), uncertainties=(), locations=None):
    """Project already-validated state into a source table and compact references."""
    locations = locations or {}
    sources, index = [], {}

    def add_source(item):
        event_id = item.get("event_id", item.get("memory_id"))
        if event_id in index:
            return index[event_id]
        ref = "s%d" % len(sources)
        index[event_id] = ref
        source = {"ref": ref, "event_id": event_id, "role": item.get("actor", "unknown")}
        if event_id in locations:
            source["text_location"] = locations[event_id]
        else:
            source["text"] = item.get("text", item.get("content", ""))
        status = item.get("status")
        if status in ("historical_source", "historical_proposal"):
            source["status"] = status
        applicable = item.get("effective_subjects", [])
        if status == "historical_source":
            source["current_for"] = ([s["subject_key"] for s in applicable] if applicable else
                                     [s["subject_key"] for s in subjects if event_id in s["source_event_ids"]])
        if item.get("authority") == "inferred" or item.get("kind") not in (None, "event"):
            source["authority"] = item.get("authority", "inferred")
            source["status"] = item.get("status", "candidate")
        sources.append(source)
        return ref

    for item in list(evidence) + list(proposals):
        add_source(item)
    states = []
    for subject in subjects:
        states.append({"key": subject["subject_key"], "kind": subject.get("kind", "requirement"),
                       "value": subject["value"],
                       "from": [index[s] for s in subject["source_event_ids"]]})
    choices = []
    by_event = {e["event_id"]: e for e in evidence}
    for subject in subjects:
        if subject.get("kind") != "decision":
            continue
        anchor = next((p for p in proposals if p.get("subject_key") == subject["subject_key"] and
                       p.get("event_id") == subject.get("reference_event_id")), None)
        if not anchor:
            continue
        user = by_event.get(anchor.get("decision_source_event_id"))
        if not user or user.get("actor") != "user" or anchor.get("actor") != "assistant":
            continue
        ordinal = affirmative_ordinal(user["text"])
        options = parse_numbered_options(anchor["text"])
        if ordinal is None or options is None or ordinal > len(options):
            continue
        choices.append({"key": subject["subject_key"], "selected_option": options[ordinal - 1],
                        "method": "explicit_ordinal_and_numbered_proposal",
                        "user_source": index[user["event_id"]], "proposal_source": index[anchor["event_id"]]})
    for item in memories:
        add_source(item)
    result = {"type": "LocalMind historical data", "statements": states, "sources": sources}
    if choices:
        result["resolved_choices"] = choices
    if uncertainties:
        result["unknown_or_conflicting"] = list(uncertainties)
    return result


def packet_view(packet):
    original_id = packet["original_request"]["event_id"]
    locations = {original_id: "current_user_message"}
    locations.update({item["event_id"]: "recent_original_message" for item in packet.get("recent_key_messages", [])})
    # The current input is already sent verbatim, so its explicit state does not
    # repeat the original text in a separate high-salience field.
    subjects = [s for s in packet.get("active_constraints", []) if s.get("source_event_ids") != [original_id]]
    return model_view(subjects, packet.get("evidence_excerpts", []), packet.get("reference_anchors", []),
                      packet.get("relevant_memories", []), packet.get("uncertainties", []), locations)
