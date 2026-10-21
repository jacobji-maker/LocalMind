import copy
import unittest

from localmind.choices import affirmative_ordinal, parse_numbered_options, resolve_choice


class ChoiceTests(unittest.TestCase):
    def sources(self, user_text="选第二个。", proposal_text="方案一：SQLite。方案二：文件。方案三：内存。"):
        scope = {"project_id": "p", "task_id": "t", "logical_session_id": "old"}
        proposal = {"event_id": "proposal", "actor": "assistant", "origin_kind": "assistant_output",
                    "status": "active", "seq": 2, "scope": scope, "raw_content": proposal_text}
        user = {"event_id": "user", "actor": "user", "origin_kind": "user_input", "status": "active",
                "seq": 3, "scope": dict(scope, logical_session_id="new"), "raw_content": user_text,
                "source_event_ids": ["proposal"]}
        subject = {"subject_key": "backend", "value": "第二个", "kind": "decision", "status": "active",
                   "authority": "user_stated", "source_event_ids": ["user"], "reference_event_id": "proposal"}
        return subject, user, proposal

    def test_explicit_selection_resolves_original_and_does_not_mutate(self):
        values = self.sources()
        unchanged = copy.deepcopy(values)
        self.assertEqual({"value": "第二个", "resolved_value": "文件", "ordinal": 2,
                          "method": "explicit_ordinal_and_numbered_proposal",
                          "source_event_ids": ["user", "proposal"]}, resolve_choice(*values))
        self.assertEqual(unchanged, values)

    def test_numbered_forms_and_complete_conditions(self):
        for text in ("1. SQLite\n2. 文件\n3. 内存", "一、SQLite\n二、文件\n三、内存",
                     "方案1：SQLite；方案2：文件；方案3：内存"):
            with self.subTest(text=text):
                self.assertEqual(["SQLite", "文件", "内存"], parse_numbered_options(text))
        condition = "文件，但必须先验证备份，未验证时不得修改"
        self.assertEqual(["SQLite", condition], parse_numbered_options("1. SQLite\n2. " + condition))

    def test_ambiguous_lists_missing_and_mixed_markers_are_rejected(self):
        for text in ("普通正文没有编号", "1. A\n3. C", "1. A\n2. B\n1. C\n2. D",
                     "方案一：A。方案一：B。", "方案一：A。方案2：B。", "1. A\n二、B",
                     "1. A\n2、B", "介绍如下\n1. A\n2. B", "1. A\n2. B\n全部都需先备份",
                     "方案一：A。", "1. A\n2. ", "1. A\n2. B\n方案一：C。方案二：D。"):
            with self.subTest(text=text):
                self.assertIsNone(parse_numbered_options(text))

    def test_only_unambiguous_affirmative_choice_is_supported(self):
        for text in ("选第二个", "我选择第2个方案。", "采用第二个方案", "就选二"):
            self.assertEqual(2, affirmative_ordinal(text))
        for text in ("不选第二个", "不要选第二个", "选第二个？", "是否选第二个", "如果通过就选第二个",
                     "可能选第二个", "选第二个或第三个", "先别选第二个", "选第二个，但要先验证", "选第21个"):
            with self.subTest(text=text):
                self.assertIsNone(affirmative_ordinal(text))

    def test_untrusted_or_mismatched_sources_and_deleted_proposals_fail_closed(self):
        mutations = [
            (0, "authority", "inferred"), (0, "status", "candidate"), (0, "kind", "requirement"),
            (0, "reference_event_id", "unrelated"), (0, "source_event_ids", ["other"]),
            (1, "source_event_ids", []), (1, "actor", "assistant"), (1, "seq", 1),
            (1, "status", "deleted"), (2, "actor", "user"), (2, "status", "deleted"),
            (2, "seq", True), (2, "scope", {"project_id": "other", "task_id": "t"}),
        ]
        for index, key, value in mutations:
            values = self.sources()
            values[index][key] = value
            with self.subTest(index=index, key=key, value=value):
                self.assertIsNone(resolve_choice(*values))
        self.assertIsNone(resolve_choice(None, {}, {}))

    def test_condition_negation_and_missing_selection_do_not_become_confirmations(self):
        for text in ("不选第二个。", "选第二个？", "如果验证完成就选第二个。"):
            self.assertIsNone(resolve_choice(*self.sources(user_text=text)))
        subject, user, proposal = self.sources(user_text="选第五个。")
        subject["value"] = "第五个"
        self.assertIsNone(resolve_choice(subject, user, proposal))


if __name__ == "__main__":
    unittest.main()
