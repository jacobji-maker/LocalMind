"""Deterministic scoring against frozen synthetic ground truth.

This module never calls a model, changes fixtures, or infers missing answers.
Scoring is deliberately strict: a successful task also needs the requested
JSON shape and types. Raw parsed values are retained for failure inspection.
"""
from __future__ import annotations

import json
import math


class _DuplicateMember(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember("Duplicate JSON member")
        result[key] = value
    return result


def _nonfinite_constant(_value):
    raise ValueError("Non-finite JSON number")


def _is_json_value(value):
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _is_json_value(item) for key, item in value.items())
    return False


def _same_value(actual, expected):
    # bool is a subclass of int, and 2 == 2.0 in Python. Neither satisfies an
    # integer response contract when the frozen answer is the JSON integer 2.
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return set(actual) == set(expected) and all(
            _same_value(actual[key], value) for key, value in expected.items())
    if type(expected) is list:
        return len(actual) == len(expected) and all(
            _same_value(left, right) for left, right in zip(actual, expected))
    return actual == expected


def evaluate_answer(text, expected, accepted_values=None, truncated=False):
    """Score one response without normalizing away content or shape errors.

    ``accepted_values`` maps an expected field to explicit equivalent values
    from the frozen fixture. Aliases must have the expected value's JSON type.
    Invalid fixture configuration raises ValueError; invalid model output is
    returned as a failed result with short error codes.
    """
    if type(expected) is not dict or not _is_json_value(expected):
        raise ValueError("Expected answer must be a JSON object")
    if accepted_values is None:
        accepted_values = {}
    if type(accepted_values) is not dict or set(accepted_values) - set(expected):
        raise ValueError("Accepted values must refer to expected fields")
    for key, aliases in accepted_values.items():
        if (type(aliases) is not list or not _is_json_value(aliases) or
                any(type(alias) is not type(expected[key]) for alias in aliases)):
            raise ValueError("Accepted values must retain the expected JSON type")
    if type(truncated) is not bool:
        raise ValueError("Truncation status must be boolean")

    errors = ["OUTPUT_TRUNCATED"] if truncated else []
    if not isinstance(text, str):
        return {"passed": False, "parsed": None, "errors": errors + ["OUTPUT_NOT_TEXT"]}
    try:
        parsed = json.loads(text, object_pairs_hook=_unique_object,
                            parse_constant=_nonfinite_constant)
    except _DuplicateMember:
        return {"passed": False, "parsed": None, "errors": errors + ["DUPLICATE_JSON_MEMBER"]}
    except (ValueError, TypeError, RecursionError):
        return {"passed": False, "parsed": None, "errors": errors + ["INVALID_JSON"]}
    if type(parsed) is not dict:
        return {"passed": False, "parsed": parsed, "errors": errors + ["OUTPUT_NOT_OBJECT"]}
    # JSON permits a finite lexical number such as 1e999 whose conversion can
    # overflow to infinity. Reject it after parsing as well as NaN/Infinity.
    try:
        valid_json = _is_json_value(parsed)
    except RecursionError:
        valid_json = False
    if not valid_json:
        return {"passed": False, "parsed": None, "errors": errors + ["NONFINITE_OR_INVALID_JSON_VALUE"]}
    if set(parsed) != set(expected):
        errors.append("WRONG_KEYS")
    for key, wanted in expected.items():
        if key not in parsed:
            errors.append("MISSING_FIELD:" + key)
        elif type(parsed[key]) is not type(wanted):
            errors.append("WRONG_TYPE:" + key)
        elif not any(_same_value(parsed[key], alternative)
                     for alternative in [wanted] + accepted_values.get(key, [])):
            errors.append("WRONG_VALUE:" + key)
    return {"passed": not errors, "parsed": parsed, "errors": errors}
