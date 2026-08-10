from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field


CANONICAL_CATEGORIES = {
    "temporary_obstacle",
    "shop_step",
    "construction",
    "construction_obstacle",
    "road_damage",
    "vehicle",
    "non_motor_vehicle",
    "motor_vehicle",
    "other",
}


class AnalysisDecision(BaseModel):
    valid: bool
    canonical_category: str = Field(alias="canonicalCategory")
    priority: Literal["low", "normal", "high", "urgent"]
    quality_score: float = Field(ge=0, le=1, alias="qualityScore")
    duplicate_risk: float = Field(ge=0, le=1, alias="duplicateRisk")
    needs_manual_review: bool = Field(alias="needsManualReview")
    summary: str = Field(max_length=500)
    quality_flags: list[str] = Field(default_factory=list, alias="qualityFlags")

    if hasattr(BaseModel, "model_validate"):
        model_config = {"populate_by_name": True, "extra": "ignore"}
    else:
        class Config:
            allow_population_by_field_name = True
            extra = "ignore"

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> "AnalysisDecision":
        validator = getattr(cls, "model_validate", None)
        return validator(data) if validator else cls.parse_obj(data)

    def as_dict(self) -> dict[str, Any]:
        dumper = getattr(self, "model_dump", None)
        return dumper(by_alias=True) if dumper else self.dict(by_alias=True)

    def with_updates(self, updates: dict[str, Any]) -> "AnalysisDecision":
        copier = getattr(self, "model_copy", None)
        return copier(update=updates) if copier else self.copy(update=updates)


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def local_quality_analysis(inputs: dict[str, Any]) -> AnalysisDecision:
    flags: list[str] = []
    category = str(inputs.get("category") or "other").strip().lower()
    if category not in CANONICAL_CATEGORIES:
        flags.append("unknown_category")
        category = "other"

    description = str(inputs.get("description") or "").strip()
    if len(description) < 8:
        flags.append("description_too_short")

    try:
        lat = float(inputs.get("lat"))
        lng = float(inputs.get("lng"))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            flags.append("invalid_coordinates")
    except (TypeError, ValueError):
        flags.append("missing_coordinates")

    confidence = float(inputs.get("confidence") or 0.0)
    if confidence > 1:
        confidence /= 100.0
    confidence = _clamp(confidence)
    if str(inputs.get("source") or "") == "edge" and confidence < 0.55:
        flags.append("low_model_confidence")

    duplicate_risk = _clamp(float(inputs.get("duplicateRisk") or 0.0))
    if duplicate_risk >= 0.8:
        flags.append("probable_duplicate")

    duration = max(0, int(float(inputs.get("durationSec") or 0)))
    quality = 1.0 - min(0.75, len(flags) * 0.16)
    if str(inputs.get("source") or "") == "edge":
        quality = min(quality, 0.45 + confidence * 0.55)
    quality = round(_clamp(quality), 3)
    valid = not {"invalid_coordinates", "missing_coordinates"}.intersection(flags)

    if category in {"construction", "construction_obstacle", "road_damage"}:
        priority = "high"
    elif confidence >= 0.85 or duration >= 30:
        priority = "high"
    elif quality < 0.6:
        priority = "low"
    else:
        priority = "normal"

    source_label = "边缘设备" if str(inputs.get("source")) == "edge" else "志愿者"
    summary = description or f"{source_label}上报的{category}候选事件"
    return AnalysisDecision.from_data(
        {
            "valid": valid,
            "canonicalCategory": category,
            "priority": priority,
            "qualityScore": quality,
            "duplicateRisk": round(duplicate_risk, 3),
            "needsManualReview": bool(flags) or str(inputs.get("source")) == "volunteer",
            "summary": summary[:500],
            "qualityFlags": flags,
        }
    )


@dataclass(frozen=True)
class AnalysisProviderConfig:
    mode: Literal["advantech", "advantech_dify", "dify", "disabled"]
    endpoint: str
    api_key: str
    workflow: str
    timeout_sec: float

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled" and bool(self.endpoint and self.api_key)

    @classmethod
    def from_env(cls) -> "AnalysisProviderConfig":
        advantech_dify_base = os.getenv("VISIONBRIDGE_ADVANTECH_DIFY_API_BASE", "").strip().rstrip("/")
        advantech_dify_key = os.getenv("VISIONBRIDGE_ADVANTECH_DIFY_API_KEY", "").strip()
        advantech_url = os.getenv("VISIONBRIDGE_ADVANTECH_AGENT_URL", "").strip().rstrip("/")
        advantech_key = os.getenv("VISIONBRIDGE_ADVANTECH_AGENT_KEY", "").strip()
        dify_base = os.getenv("VISIONBRIDGE_DIFY_API_BASE", "").strip().rstrip("/")
        dify_key = os.getenv("VISIONBRIDGE_DIFY_API_KEY", "").strip()
        if advantech_dify_base and advantech_dify_key:
            return cls(
                mode="advantech_dify",
                endpoint=f"{advantech_dify_base}/workflows/run",
                api_key=advantech_dify_key,
                workflow=os.getenv("VISIONBRIDGE_ADVANTECH_WORKFLOW", "visionbridge-data-cleaning"),
                timeout_sec=float(os.getenv("VISIONBRIDGE_ANALYSIS_TIMEOUT_SEC", "25")),
            )
        if advantech_url and advantech_key:
            return cls(
                mode="advantech",
                endpoint=advantech_url,
                api_key=advantech_key,
                workflow=os.getenv("VISIONBRIDGE_ADVANTECH_WORKFLOW", "visionbridge-data-cleaning"),
                timeout_sec=float(os.getenv("VISIONBRIDGE_ANALYSIS_TIMEOUT_SEC", "25")),
            )
        if dify_base and dify_key:
            return cls(
                mode="dify",
                endpoint=f"{dify_base}/workflows/run",
                api_key=dify_key,
                workflow=os.getenv("VISIONBRIDGE_DIFY_WORKFLOW", "visionbridge-data-cleaning"),
                timeout_sec=float(os.getenv("VISIONBRIDGE_ANALYSIS_TIMEOUT_SEC", "25")),
            )
        return cls(mode="disabled", endpoint="", api_key="", workflow="", timeout_sec=25)


class ExternalAnalysisClient:
    def __init__(self, config: AnalysisProviderConfig):
        self.config = config

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "VisionBridge-Analysis/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_sec) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", "replace")
            raise RuntimeError(f"analysis provider HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"analysis provider unavailable: {exc}") from exc

    @staticmethod
    def _extract_decision(response: dict[str, Any]) -> tuple[dict[str, Any], str]:
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else data
        raw = (
            outputs.get("analysisResult")
            or outputs.get("cleaningResult")
            or outputs.get("dispatchResult")
            or outputs.get("result")
            or outputs.get("decision")
        )
        if raw is None and all(key in outputs for key in ("valid", "canonicalCategory", "qualityScore")):
            raw = outputs
        if isinstance(raw, str):
            text = raw.strip()
            if "</think>" in text:
                text = text.split("</think>", 1)[1].strip()
            if text.startswith("```"):
                text = "\n".join(text.splitlines()[1:-1]).strip()
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise RuntimeError("analysis result is not JSON")
            raw = json.loads(text[start : end + 1])
        if not isinstance(raw, dict):
            raise RuntimeError("analysis provider output is missing a structured result")
        run_id = str(data.get("id") or data.get("workflow_run_id") or response.get("id") or "")
        return raw, run_id

    def run(self, inputs: dict[str, Any]) -> tuple[AnalysisDecision, str, dict[str, Any]]:
        if not self.config.enabled:
            raise RuntimeError("analysis provider is not configured")
        if self.config.mode == "advantech":
            payload = {
                "workflow": self.config.workflow,
                "tool": "dify.workflow.run",
                "inputs": inputs,
                "responseMode": "blocking",
                "user": str(inputs.get("sourceId") or "visionbridge-cloud"),
            }
        else:
            confidence = float(inputs.get("confidence") or 0)
            if confidence > 1:
                confidence /= 100
            provider_inputs = {
                # Compatibility contract used by the existing Advantech-hosted
                # Dify workflow. A newer cleaning workflow may additionally
                # consume rawDataJson without changing this cloud adapter.
                "deviceId": str(inputs.get("deviceId") or inputs.get("reporterId") or "visionbridge-cloud"),
                "timestamp": str(inputs.get("timestamp") or ""),
                "gpsLat": str(inputs.get("lat") or ""),
                "gpsLng": str(inputs.get("lng") or ""),
                "eventActive": "1",
                "obstacleConfidence": f"{confidence:.2f}",
                "alertLevelCode": str(inputs.get("priority") or "normal"),
                "triggerStreak": str(inputs.get("triggerStreak") or 0),
                "snapshotUrl": str(inputs.get("snapshotUrl") or ""),
            }
            payload = {
                "inputs": provider_inputs,
                "response_mode": "blocking",
                "user": str(inputs.get("sourceId") or "visionbridge-cloud"),
            }
        response = self._request(payload)
        raw, run_id = self._extract_decision(response)
        try:
            decision = AnalysisDecision.from_data(raw)
        except Exception:
            local = local_quality_analysis(inputs)
            provider_priority = str(raw.get("priority") or "").lower()
            priority = {"high": "high", "urgent": "urgent", "medium": "normal", "normal": "normal", "low": "low"}.get(
                provider_priority, local.priority
            )
            summary = str(raw.get("summary") or raw.get("suggestion") or local.summary).strip()[:500]
            decision = local.with_updates({"priority": priority, "summary": summary or local.summary})
        return decision, run_id, response
