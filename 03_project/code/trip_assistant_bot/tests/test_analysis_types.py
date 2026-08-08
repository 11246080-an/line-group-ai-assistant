import unittest

from ai_linebot_core.app.llm_judge import (
    LLMJudgeError,
    _extract_json,
    _normalize_result,
)
from ai_linebot_core.app.models import ExtractedInfo


def _analysis_payload(**overrides):
    payload = {
        "scenario_code": "劇本一",
        "scenario_name": "初步時間討論",
        "stage": "需求發散階段",
        "should_intervene": False,
        "reply_trigger": "no_reply",
        "intervention_type": "不介入",
        "confidence_score": 0.8,
        "evidence": ["一般聚餐討論"],
        "system_behavior": ["持續觀察"],
        "requires_external_search": False,
        "intermediate_reply": "",
        "suggested_reply": "",
        "extracted_info": {"time": ["這週六晚上"], "activity_types": ["聚餐"]},
    }
    payload.update(overrides)
    return payload


class AnalysisTypeTests(unittest.TestCase):
    def test_null_confidence_is_contract_error_not_type_error(self):
        with self.assertRaises(LLMJudgeError) as caught:
            _normalize_result(
                _analysis_payload(confidence_score=None),
                ExtractedInfo(),
                "這週六晚上要不要一起吃飯？",
            )
        self.assertIn("confidence_score", str(caught.exception))

    def test_analysis_result_is_normalized_model_with_dictionary_output(self):
        result = _normalize_result(
            _analysis_payload(),
            ExtractedInfo(),
            "這週六晚上要不要一起吃飯？",
        )
        self.assertFalse(result.should_intervene)
        self.assertIsInstance(result.to_dict(), dict)
        self.assertEqual(result.extracted_info.activity_types, ["聚餐"])

    def test_decided_restaurant_keeps_original_no_reply_rule(self):
        result = _normalize_result(
            _analysis_payload(
                evidence=["群組已完成決策"],
                extracted_info={"location": ["鼎泰豐"], "decision_state": "已決定"},
            ),
            ExtractedInfo(),
            "我們已經決定吃鼎泰豐了",
        )
        self.assertFalse(result.should_intervene)

    def test_non_string_sdk_content_is_rejected_explicitly(self):
        with self.assertRaises(LLMJudgeError) as caught:
            _extract_json({"should_intervene": False})
        self.assertIn("預期 str", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
