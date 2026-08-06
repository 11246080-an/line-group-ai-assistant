import unittest
from unittest.mock import patch
import sys
import os
from types import SimpleNamespace

from ai_linebot_core.app.llm_judge import (
    PROCESSING_REQUIRED_SIGNAL,
    judge_with_llm,
)
from ai_linebot_core.app.models import ExtractedInfo


class _FakeOpenAI:
    def __init__(self, *args, **kwargs):
        pass


sys.modules.setdefault("openai", SimpleNamespace(OpenAI=_FakeOpenAI))


class ProcessingHintTests(unittest.TestCase):
    def setUp(self):
        self._old_api_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-key"

    def tearDown(self):
        if self._old_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._old_api_key

    @patch("ai_linebot_core.app.llm_judge._build_generation_messages", return_value=[])
    @patch("ai_linebot_core.app.llm_judge._call_openai_json")
    def test_hint_is_sent_between_judgment_and_generation(self, call_openai, build_generation):
        call_openai.side_effect = [
            {
                "scenario_code": "劇本五",
                "scenario_name": "路線順序最佳化",
                "stage": "行程規劃階段",
                "should_intervene": True,
                "reply_trigger": "explicit_request",
                "intervention_type": "顯性介入",
                "confidence_score": 0.9,
                "evidence": ["需要排序多個景點"],
                "system_behavior": ["重新排列景點"],
                "requires_external_search": False,
                "intermediate_reply": PROCESSING_REQUIRED_SIGNAL,
                "suggested_reply": "",
                "extracted_info": {"location": ["台北101", "故宮"]},
            },
            {"intermediate_reply": "", "suggested_reply": "已完成排序"},
        ]
        events = []

        def notify(signal):
            events.append(("hint", signal, call_openai.call_count))

        result = judge_with_llm("幫我順一下行程", ExtractedInfo(), on_processing_required=notify)

        self.assertEqual(events, [("hint", PROCESSING_REQUIRED_SIGNAL, 1)])
        self.assertEqual(call_openai.call_count, 2)
        self.assertEqual(result.suggested_reply, "已完成排序")
        self.assertTrue(build_generation.call_args.kwargs["user_was_notified"])

    @patch("ai_linebot_core.app.llm_judge._call_openai_json")
    def test_fast_no_reply_does_not_send_hint_or_second_request(self, call_openai):
        call_openai.return_value = {
            "scenario_code": "劇本一",
            "scenario_name": "初步時間討論",
            "stage": "需求發散階段",
            "should_intervene": False,
            "reply_trigger": "no_reply",
            "intervention_type": "不介入",
            "confidence_score": 0.8,
            "evidence": ["只是一般討論"],
            "system_behavior": ["持續觀察"],
            "requires_external_search": False,
            "intermediate_reply": "",
            "suggested_reply": "",
            "extracted_info": {},
        }
        notifications = []
        judge_with_llm("週末有空", ExtractedInfo(), on_processing_required=notifications.append)
        self.assertEqual(notifications, [])
        self.assertEqual(call_openai.call_count, 1)


if __name__ == "__main__":
    unittest.main()
