"""Conservative interpretation of an explicit choice from a numbered proposal.

Resolution is derived navigation data. It never changes the confirmed original
statement, grants permission, or replaces the complete cited user/proposal
sources. Unrecognized prose and ambiguous selections intentionally remain
unresolved instead of falling back to another proposal.
"""
from __future__ import annotations

import re


_NUM = r"(?:[1-9][0-9]?|[一二三四五六七八九十]{1,3})"
_SELECT = re.compile(
    r"\s*(?:(?:我|我们)(?:决定|最终决定)?)?"
    r"(?:选择|就选|选|采用|选用|确定采用|确定选)\s*"
    r"第?(?P<number>" + _NUM + r")(?:个)?(?:方案|选项)?\s*[。.!！]?\s*"
)
_SCHEME = re.compile(r"(?:^|(?<=[。；;\n]))[ \t]*方案(?P<number>" + _NUM + r")[：:][ \t]*")
_LINES = re.compile(r"^[ \t]*(?P<number>" + _NUM + r")(?P<separator>[.、）)])(?:[ \t]+|(?=[^0-9]))", re.MULTILINE)


def _ordinal(value):
    if value.isascii() and value.isdigit():
        number = int(value)
        return number if 1 <= number <= 20 else None
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if value in digits:
        return digits[value]
    if value == "十":
        return 10
    if len(value) == 2 and value[0] == "十" and value[1] in digits:
        return 10 + digits[value[1]]
    if value == "二十":
        return 20
    return None


def affirmative_ordinal(text):
    """Return 1..20 only for a complete, unambiguous affirmative selection.

    Questions, negations, conditions, alternatives and additional clauses do
    not match this deliberately narrow grammar.
    """
    if not isinstance(text, str) or len(text) > 256:
        return None
    match = _SELECT.fullmatch(text)
    return _ordinal(match.group("number")) if match else None


def parse_numbered_options(text):
    """Read one clear, sequential list, returning its complete option strings.

    Supported forms are ``方案一：A。方案二：B。``, ``1. A\n2. B`` and
    ``一、A\n二、B``. Mixing schemes/scripts/separators, repeated or missing
    numbers, introductory prose and unnumbered continuation paragraphs fail
    closed. Complete option conditions are kept verbatim.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 16384:
        return None
    text = text.strip()
    scheme = list(_SCHEME.finditer(text))
    lines = list(_LINES.finditer(text))
    if bool(scheme) == bool(lines):
        return None
    matches = scheme or lines
    if not 2 <= len(matches) <= 20 or matches[0].start() != 0:
        return None
    scripts = {"ascii" if match.group("number").isascii() else "chinese" for match in matches}
    if len(scripts) != 1:
        return None
    if lines and len({match.group("separator") for match in matches}) != 1:
        return None
    numbers = [_ordinal(match.group("number")) for match in matches]
    if numbers != list(range(1, len(matches) + 1)):
        return None
    options = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # Remove the list delimiter/final punctuation, retaining all words,
        # negation, prerequisite and exception text inside the option.
        option = text[match.end():end].strip().rstrip("。；; ").strip()
        if not option or len(option) > 4096 or "\n" in option or "\r" in option:
            return None
        # A different marker type inside a segment would otherwise conceal a
        # second option set as ordinary content.
        if re.search(r"(?:^|[。；;])\s*方案" + _NUM + r"[：:]", option):
            return None
        options.append(option)
    return options


def _event_text(event):
    value = event.get("raw_content", event.get("text"))
    return value if isinstance(value, str) else None


def resolve_choice(subject, user_event, proposal_event):
    """Resolve a current statement against its fixed, earlier real proposal.

    This raw-Store API requires identity/order/source checks. Callers holding
    already-validated projected excerpts can instead use the two pure parsing
    helpers and keep the original references in their own derived projection.
    """
    if not all(isinstance(value, dict) for value in (subject, user_event, proposal_event)):
        return None
    if (subject.get("kind") != "decision" or subject.get("authority") != "user_stated"
            or subject.get("status") != "active"):
        return None
    user_id, proposal_id = user_event.get("event_id"), proposal_event.get("event_id")
    if (not isinstance(user_id, str) or not user_id or not isinstance(proposal_id, str) or not proposal_id
            or user_id not in subject.get("source_event_ids", [])
            or subject.get("reference_event_id") != proposal_id
            or proposal_id not in user_event.get("source_event_ids", [])):
        return None
    if (user_event.get("actor") != "user" or user_event.get("origin_kind") != "user_input"
            or proposal_event.get("actor") != "assistant"
            or proposal_event.get("origin_kind") != "assistant_output"
            or user_event.get("status") != "active" or proposal_event.get("status") != "active"):
        return None
    user_seq, proposal_seq = user_event.get("seq"), proposal_event.get("seq")
    if (type(user_seq) is not int or type(proposal_seq) is not int or not 0 < proposal_seq < user_seq):
        return None
    user_scope, proposal_scope = user_event.get("scope"), proposal_event.get("scope")
    if (not isinstance(user_scope, dict) or not isinstance(proposal_scope, dict)
            or any(not user_scope.get(key) or user_scope.get(key) != proposal_scope.get(key)
                   for key in ("project_id", "task_id"))):
        return None
    text = _event_text(user_event)
    value = subject.get("value")
    if not isinstance(value, str) or not value or text is None or value not in text:
        return None
    ordinal = affirmative_ordinal(text)
    options = parse_numbered_options(_event_text(proposal_event))
    if ordinal is None or options is None or ordinal > len(options):
        return None
    return {"value": value, "resolved_value": options[ordinal - 1], "ordinal": ordinal,
            "method": "explicit_ordinal_and_numbered_proposal",
            "source_event_ids": [user_id, proposal_id]}
