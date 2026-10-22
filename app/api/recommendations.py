from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import app.main as _root
from app import (
    model_config,
    recommendation_engine,
    recommendation_stats,
)
from app import recommendation_feedback as feedback_store
from app import recommendations as rec_store

logger = logging.getLogger("personal_ai_os.api")

router = APIRouter(tags=["智能建议"])

class RecommendationGenerateIn(BaseModel):
    days: int = 7
    use_model: bool = False

    @field_validator("days")
    @classmethod
    def _days_allowed(cls, value: int) -> int:
        if isinstance(value, bool) or value not in recommendation_engine.SUPPORTED_WINDOWS:
            raise ValueError(f"days 只支持 {sorted(recommendation_engine.SUPPORTED_WINDOWS)}")
        return value

    @field_validator("use_model", mode="before")
    @classmethod
    def _use_model_bool(cls, value: Any) -> Any:
        if not isinstance(value, bool):
            raise ValueError("use_model 必须是布尔值 true/false")
        return value


def _raise_rec_error(exc: rec_store.RecommendationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _recommendation_model_enabled() -> bool:
    """Server-side gate for the real cloud model. Default OFF.

    A client flag can never enable model calls on its own; the operator must
    both set LLM_API_KEY and explicitly opt in via RECOMMENDATIONS_USE_MODEL=1.
    """
    return (
        os.getenv("RECOMMENDATIONS_USE_MODEL", "").strip() == "1"
        and model_config.load_model_config().enabled
    )


@router.get("/api/recommendation-statistics")
def get_recommendation_statistics_route(
    start_at: str | None = Query(default=None),
    end_at: str | None = Query(default=None),
) -> dict[str, Any]:
    try:
        with _root.db() as conn:
            return recommendation_stats.get_recommendation_statistics(
                conn, start_at=start_at, end_at=end_at,
            )
    except recommendation_stats.StatsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/api/recommendations")
def list_recommendations_route(
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    try:
        with _root.db() as conn:
            items = rec_store.list_recommendations(
                conn, status=status, category=category, priority=priority,
                limit=limit, offset=offset,
            )
            total = rec_store.count_recommendations(
                conn, status=status, category=category, priority=priority,
            )
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/api/recommendations/{rec_id}")
def get_recommendation_route(rec_id: int) -> dict[str, Any]:
    try:
        with _root.db() as conn:
            return rec_store.get_recommendation(conn, rec_id)
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)


@router.post("/api/recommendations/generate")
async def generate_recommendations_route(body: RecommendationGenerateIn) -> dict[str, Any]:
    requested_model = body.use_model
    effective_model = requested_model and _recommendation_model_enabled()
    try:
        with _root.db() as conn:
            result = await recommendation_engine.generate(
                conn, days=body.days, use_model=effective_model,
            )
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)
    return {
        "status": result.status,
        "counts": result.counts,
        "created": result.created,
        "window": result.window,
        "gaps": result.gaps,
        "run": result.run,
        "model_requested": requested_model,
        "model_used": effective_model,
        "model_disabled_by_server": bool(requested_model and not effective_model),
    }


@router.post("/api/recommendations/{rec_id}/accept")
def accept_recommendation_route(rec_id: int) -> dict[str, Any]:
    return _transition_recommendation(rec_id, "accept")


@router.post("/api/recommendations/{rec_id}/ignore")
def ignore_recommendation_route(rec_id: int) -> dict[str, Any]:
    return _transition_recommendation(rec_id, "ignore")


@router.post("/api/recommendations/{rec_id}/reject")
def reject_recommendation_route(rec_id: int) -> dict[str, Any]:
    return _transition_recommendation(rec_id, "reject")


@router.post("/api/recommendations/{rec_id}/reopen")
def reopen_recommendation_route(rec_id: int) -> dict[str, Any]:
    return _transition_recommendation(rec_id, "reopen")


def _transition_recommendation(rec_id: int, action: str) -> dict[str, Any]:
    try:
        with _root.db() as conn:
            return rec_store.transition_status(conn, rec_id, action)
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)


@router.delete("/api/recommendations/{rec_id}")
def delete_recommendation_route(rec_id: int) -> dict[str, bool]:
    try:
        with _root.db() as conn:
            rec_store.delete_recommendation(conn, rec_id)
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)
    return {"deleted": True}


class RecommendationFeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # All fields are optional at the HTTP boundary so partial updates retain
    # service-layer merge semantics. Creation completeness is checked below.
    vote: str | None = None
    reason_codes: list[str] | None = None
    note: str | None = None
    preference_scope: str | None = None
    resume_after_at: str | None = None


class RecommendationMetricObservationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    expected_value: int | float | None = None
    observed_value: int | float | None = None
    unit: str
    window: dict[str, Any] = Field(default_factory=dict)
    source: str
    captured_at: str
    attribution: str = "not_inferred"


class RecommendationOutcomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Partial updates use exclude_unset=True; the service merges values with
    # the existing outcome and enforces required fields on creation.
    execution_status: str | None = None
    result_rating: str | None = None
    actual_effect_note: str | None = None
    metric_observations: list[RecommendationMetricObservationIn] | None = None
    occurred_at: str | None = None


def _raise_feedback_validation_error(exc: ValidationError, *, kind: str) -> None:
    label = "反馈" if kind == "feedback" else "执行结果"
    raise HTTPException(
        status_code=400,
        detail=f"{label}参数不合法：请检查枚举、长度、时间格式、指标白名单和允许字段。",
    ) from exc


def _require_accepted_active_for_outcome_mutation(item: dict[str, Any]) -> None:
    if item.get("status") != "accepted":
        raise HTTPException(
            status_code=409,
            detail="只有当前已采纳的建议可以修改执行结果；撤销采纳后历史结果保留但不可修改。",
        )
    expires_at = item.get("expires_at")
    if expires_at and str(expires_at) < _root.now_iso():
        raise HTTPException(status_code=409, detail="建议已过期，历史结果保留但不可修改。")


@router.get("/api/recommendations/{rec_id}/feedback")
def get_recommendation_feedback_route(rec_id: int) -> dict[str, Any]:
    try:
        with _root.db() as conn:
            rec_store.get_recommendation(conn, rec_id)
            return {"feedback": feedback_store.get_feedback(conn, rec_id)}
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)


@router.put("/api/recommendations/{rec_id}/feedback")
def put_recommendation_feedback_route(rec_id: int, body: Any = Body(default=None)) -> dict[str, Any]:
    try:
        data = RecommendationFeedbackIn.model_validate(body)
        changes = data.model_dump(exclude_unset=True)
        provided = data.model_fields_set
        # Explicit null is a user-visible clear operation; omitted fields stay
        # unset and therefore retain existing values in the B-layer merge.
        if "note" in provided and changes.get("note") is None:
            changes["note"] = ""
        if "reason_codes" in provided and changes.get("reason_codes") is None:
            changes["reason_codes"] = []
        if "preference_scope" in provided and changes.get("preference_scope") is None:
            changes["preference_scope"] = "unspecified"
        with _root.db() as conn:
            return feedback_store.upsert_feedback(conn, rec_id, changes)
    except ValidationError as exc:
        _raise_feedback_validation_error(exc, kind="feedback")
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)


@router.delete("/api/recommendations/{rec_id}/feedback")
def delete_recommendation_feedback_route(rec_id: int) -> dict[str, bool]:
    try:
        with _root.db() as conn:
            feedback_store.delete_feedback(conn, rec_id)
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)
    return {"deleted": True}


@router.get("/api/recommendations/{rec_id}/outcome")
def get_recommendation_outcome_route(rec_id: int) -> dict[str, Any]:
    try:
        with _root.db() as conn:
            rec_store.get_recommendation(conn, rec_id)
            return {"outcome": feedback_store.get_outcome(conn, rec_id)}
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)


@router.put("/api/recommendations/{rec_id}/outcome")
def put_recommendation_outcome_route(rec_id: int, body: Any = Body(default=None)) -> dict[str, Any]:
    try:
        data = RecommendationOutcomeIn.model_validate(body)
        changes = data.model_dump(exclude_unset=True)
        provided = data.model_fields_set
        if "actual_effect_note" in provided and changes.get("actual_effect_note") is None:
            changes["actual_effect_note"] = ""
        if "metric_observations" in provided and changes.get("metric_observations") is None:
            changes["metric_observations"] = []
        if "result_rating" in provided and changes.get("result_rating") is None:
            changes["result_rating"] = "unclear"
        with _root.db() as conn:
            return feedback_store.upsert_outcome(conn, rec_id, changes)
    except ValidationError as exc:
        _raise_feedback_validation_error(exc, kind="outcome")
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)


@router.delete("/api/recommendations/{rec_id}/outcome")
def delete_recommendation_outcome_route(rec_id: int) -> dict[str, bool]:
    try:
        with _root.db() as conn:
            recommendation = rec_store.get_recommendation(conn, rec_id)
            _require_accepted_active_for_outcome_mutation(recommendation)
            feedback_store.delete_outcome(conn, rec_id)
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)
    return {"deleted": True}


@router.get("/api/recommendation-runs")
def list_recommendation_runs_route(
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    try:
        with _root.db() as conn:
            return rec_store.list_runs(conn, limit=limit, offset=offset)
    except rec_store.RecommendationError as exc:
        _raise_rec_error(exc)


# ===== Personal AI OS 1.3.3-D recommendation management API (end) =====



