import unittest

from localmind.adapters import AdapterError, OllamaAdapter, ReplayAdapter
from localmind.models import OllamaPlanner


class AdapterTests(unittest.TestCase):
    def test_actual_body_contains_only_explicit_messages_and_no_tools(self):
        captured = []
        def transport(url, body, timeout):
            captured.append(body)
            return {"done": True, "message": {"content": "答复"}, "prompt_eval_count": 21, "eval_count": 3}
        messages = [{"role": "user", "content": "  原话\n不改写！  "}]
        result = OllamaAdapter("installed:1b", transport=transport).send(messages, "request")
        self.assertEqual(captured[0]["messages"][-1]["content"], messages[-1]["content"])
        self.assertNotIn("tools", captured[0])
        self.assertNotIn("context", captured[0])
        self.assertNotIn("previous_response_id", captured[0])
        self.assertEqual(result["usage"]["prompt_eval_count"], 21)

    def test_model_tool_call_never_executed(self):
        def transport(*args):
            return {"done": True, "message": {"content": "", "tool_calls": [{"name": "shell"}]}}
        with self.assertRaises(AdapterError):
            OllamaAdapter("installed:1b", transport=transport).send([{"role": "user", "content": "x"}], "r")

    def test_cloud_or_remote_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            OllamaAdapter("model-cloud")
        for url in ("https://elsewhere.example", "http://user:pass@localhost:11434", "http://localhost:11434/path"):
            with self.assertRaises(ValueError):
                OllamaAdapter("local:1b", base_url=url)

    def test_planner_fabricated_reference_is_rejected_after_one_call(self):
        observed = []
        def transport(url, body, timeout):
            observed.append(body)
            return {"done": True, "message": {"content": '{"selected_ids":["foreign"],"query_terms":[]}'}}
        planner = OllamaPlanner("local:1b", transport=transport)
        with self.assertRaises(ValueError):
            planner("第二个", [{"id": "allowed", "text": "方案"}])
        self.assertEqual(len(observed), 1)
        self.assertEqual(planner.calls, 1)

    def test_replay_is_labeled_simulated(self):
        self.assertTrue(ReplayAdapter(["fixture"]).send([{"role": "user", "content": "q"}], "r")["simulated"])
