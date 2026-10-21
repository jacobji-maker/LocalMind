"""Failure responses remain failures; a model does not judge its own answer."""
import json
from pathlib import Path
import unittest

from localmind.evaluation import evaluate_answer


class ContrastScoringTests(unittest.TestCase):
    def test_exact_values_and_explicit_aliases(self):
        self.assertTrue(evaluate_answer(' {"channel":"电子邮件"}\n', {"channel": "电子邮件"})["passed"])
        self.assertTrue(evaluate_answer('{"channel":"email"}', {"channel": "电子邮件"},
                                        {"channel": ["邮件", "email"]})["passed"])
        self.assertFalse(evaluate_answer('{"channel":"短信"}', {"channel": "电子邮件"},
                                         {"channel": ["邮件", "email"]})["passed"])

    def test_bool_integer_float_and_string_cannot_substitute_for_each_other(self):
        for text, expected in (("{\"count\":true}", {"count": 1}),
                               ("{\"count\":1.0}", {"count": 1}),
                               ("{\"count\":\"1\"}", {"count": 1}),
                               ("{\"allowed\":0}", {"allowed": False})):
            with self.subTest(text=text):
                result = evaluate_answer(text, expected)
                self.assertFalse(result["passed"])
                self.assertTrue(any(error.startswith("WRONG_TYPE:") for error in result["errors"]))

    def test_null_is_unknown_not_a_string_or_guessed_default(self):
        self.assertTrue(evaluate_answer('{"backend":null}', {"backend": None})["passed"])
        for value in ("null", "unknown", "SQLite", ""):
            self.assertFalse(evaluate_answer(json.dumps({"backend": value}), {"backend": None})["passed"])

    def test_shape_extra_fields_missing_fields_and_nested_types(self):
        for text in ('{"backend":"SQLite","extra":1}', '{}', '[]', 'null', '"SQLite"'):
            with self.subTest(text=text):
                self.assertFalse(evaluate_answer(text, {"backend": "SQLite"})["passed"])
        result = evaluate_answer('{"values":[true]}', {"values": [1]})
        self.assertFalse(result["passed"])
        self.assertEqual(["WRONG_VALUE:values"], result["errors"])

    def test_reject_markdown_trailing_chatter_duplicates_and_nonfinite_numbers(self):
        expected = {"value": 1}
        for text in ('```json\n{"value":1}\n```', '{"value":1} 完成',
                     '{"value":1}\n{"value":1}', '{"value":1,"value":1}',
                     '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}'):
            with self.subTest(text=text):
                self.assertFalse(evaluate_answer(text, expected)["passed"])
        duplicate = evaluate_answer('{"value":{"k":1,"k":2}}', {"value": {"k": 1}})
        self.assertEqual(["DUPLICATE_JSON_MEMBER"], duplicate["errors"])

    def test_truncation_is_failure_even_when_visible_json_looks_complete(self):
        result = evaluate_answer('{"retries":2}', {"retries": 2}, truncated=True)
        self.assertFalse(result["passed"])
        self.assertEqual({"retries": 2}, result["parsed"])
        self.assertEqual(["OUTPUT_TRUNCATED"], result["errors"])
        self.assertFalse(evaluate_answer(None, {"retries": 2})["passed"])

    def test_invalid_ground_truth_is_a_driver_error_not_a_model_failure(self):
        for expected, aliases in (([], None), ({"n": float("nan")}, None),
                                  ({"n": 1}, {"other": [1]}),
                                  ({"n": 1}, {"n": [True]}),
                                  ({"n": None}, {"n": ["SQLite"]})):
            with self.subTest(expected=expected, aliases=aliases):
                with self.assertRaises(ValueError):
                    evaluate_answer('{}', expected, aliases)

    def test_every_frozen_fixture_can_be_scored_without_mutation(self):
        path = Path(__file__).resolve().parents[1] / 'eval' / 'contrast_cases.json'
        original = path.read_bytes()
        fixtures = json.loads(original)
        self.assertEqual(11, len(fixtures["cases"]))
        for case in fixtures["cases"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(evaluate_answer(json.dumps(case["expected"], ensure_ascii=False),
                                                case["expected"], case.get("accepted_values"))["passed"])
        self.assertEqual(original, path.read_bytes())
