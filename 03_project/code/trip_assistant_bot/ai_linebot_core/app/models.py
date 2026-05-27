from __future__ import annotations

# 這個檔案專門放「資料格式」。
# 你可以把它想成：先規定系統裡的資料應該長什麼樣子，
# 這樣每個模組交換資料時才不會亂掉。

from dataclasses import asdict, dataclass, field
from typing import Any


def _normalize_list_field(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, (tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value] if value else []
    return [str(value)]


@dataclass
class ExtractedInfo:
    # 這裡放的是從原始對話中抽取出的重點資訊。
    time: list[str] = field(default_factory=list)
    location: list[str] = field(default_factory=list)
    people_count: list[str] = field(default_factory=list)
    budget: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    activity_types: list[str] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    decision_state: str = "未定"
    risk_info: list[str] = field(default_factory=list)
    need_type: str | None = None

    # 把 dataclass 轉成字典，方便輸出成 JSON。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # 如果收到的是字典格式，也可以再轉回 ExtractedInfo 物件。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedInfo":
        return cls(
            time=_normalize_list_field(data.get("time", [])),
            location=_normalize_list_field(data.get("location", [])),
            people_count=_normalize_list_field(data.get("people_count", [])),
            budget=_normalize_list_field(data.get("budget", [])),
            constraints=_normalize_list_field(data.get("constraints", [])),
            activity_types=_normalize_list_field(data.get("activity_types", [])),
            options=_normalize_list_field(data.get("options", [])),
            decision_state=str(data.get("decision_state", "未定")),
            risk_info=_normalize_list_field(data.get("risk_info", [])),
            need_type=data.get("need_type"),
        )


@dataclass
class ScenarioDefinition:
    # 這是單一劇本的基本資料。
    code: str
    name: str
    stage: str
    should_intervene: bool
    intervention_type: str
    system_behavior: list[str]
    suggested_reply: str
    keywords: list[str]
    feature_hints: list[str]


@dataclass
class AnalysisResult:
    # 這是整個系統最後要輸出的標準結果。
    # LINE Bot 同學之後最常接到的就是這個資料。
    scenario_code: str
    scenario_name: str
    stage: str
    should_intervene: bool
    reply_trigger: str
    intervention_type: str
    confidence_score: float
    evidence: list[str]
    system_behavior: list[str]
    requires_external_search: bool
    intermediate_reply: str
    suggested_reply: str
    extracted_info: ExtractedInfo

    # 把結果轉成固定字典格式，方便轉成 JSON。
    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_code": self.scenario_code,
            "scenario_name": self.scenario_name,
            "stage": self.stage,
            "should_intervene": self.should_intervene,
            "reply_trigger": self.reply_trigger,
            "intervention_type": self.intervention_type,
            "confidence_score": self.confidence_score,
            "evidence": self.evidence,
            "system_behavior": self.system_behavior,
            "requires_external_search": self.requires_external_search,
            "intermediate_reply": self.intermediate_reply,
            "suggested_reply": self.suggested_reply,
            "extracted_info": self.extracted_info.to_dict(),
        }

    # 如果結果來自字典，也可以重新組回 AnalysisResult。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisResult":
        return cls(
            scenario_code=str(data["scenario_code"]),
            scenario_name=str(data["scenario_name"]),
            stage=str(data["stage"]),
            should_intervene=bool(data["should_intervene"]),
            reply_trigger=str(data["reply_trigger"]),
            intervention_type=str(data["intervention_type"]),
            confidence_score=float(data["confidence_score"]),
            evidence=list(data["evidence"]),
            system_behavior=list(data["system_behavior"]),
            requires_external_search=bool(data.get("requires_external_search", False)),
            intermediate_reply=str(data.get("intermediate_reply", "")),
            suggested_reply=str(data.get("suggested_reply", "")),
            extracted_info=ExtractedInfo.from_dict(data["extracted_info"]),
        )
