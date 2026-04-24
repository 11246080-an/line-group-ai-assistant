from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ExtractedInfo:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExtractedInfo":
        return cls(
            time=list(data.get("time", [])),
            location=list(data.get("location", [])),
            people_count=list(data.get("people_count", [])),
            budget=list(data.get("budget", [])),
            constraints=list(data.get("constraints", [])),
            activity_types=list(data.get("activity_types", [])),
            options=list(data.get("options", [])),
            decision_state=str(data.get("decision_state", "未定")),
            risk_info=list(data.get("risk_info", [])),
            need_type=data.get("need_type"),
        )


@dataclass
class ScenarioDefinition:
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
    scenario_code: str
    scenario_name: str
    stage: str
    should_intervene: bool
    intervention_type: str
    confidence_score: float
    evidence: list[str]
    system_behavior: list[str]
    requires_external_search: bool
    intermediate_reply: str
    suggested_reply: str
    extracted_info: ExtractedInfo

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_code": self.scenario_code,
            "scenario_name": self.scenario_name,
            "stage": self.stage,
            "should_intervene": self.should_intervene,
            "intervention_type": self.intervention_type,
            "confidence_score": self.confidence_score,
            "evidence": self.evidence,
            "system_behavior": self.system_behavior,
            "requires_external_search": self.requires_external_search,
            "intermediate_reply": self.intermediate_reply,
            "suggested_reply": self.suggested_reply,
            "extracted_info": self.extracted_info.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisResult":
        return cls(
            scenario_code=str(data["scenario_code"]),
            scenario_name=str(data["scenario_name"]),
            stage=str(data["stage"]),
            should_intervene=bool(data["should_intervene"]),
            intervention_type=str(data["intervention_type"]),
            confidence_score=float(data["confidence_score"]),
            evidence=list(data["evidence"]),
            system_behavior=list(data["system_behavior"]),
            requires_external_search=bool(data.get("requires_external_search", False)),
            intermediate_reply=str(data.get("intermediate_reply", "")),
            suggested_reply=str(data.get("suggested_reply", "")),
            extracted_info=ExtractedInfo.from_dict(data["extracted_info"]),
        )
