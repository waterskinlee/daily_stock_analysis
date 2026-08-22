# -*- coding: utf-8 -*-
"""SkillOpinion outcome API endpoints."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Security
from fastapi.security import APIKeyCookie
from pydantic import BaseModel, Field

from api.v1.schemas.common import ErrorResponse
from src.auth import COOKIE_NAME
from src.services.skill_opinion_outcome_service import SkillOpinionOutcomeService


logger = logging.getLogger(__name__)

admin_session_cookie = APIKeyCookie(
    name=COOKIE_NAME,
    scheme_name="AdminSessionCookie",
    auto_error=False,
)
router = APIRouter(dependencies=[Security(admin_session_cookie)])

AUTH_RESPONSE = {
    401: {
        "model": ErrorResponse,
        "description": "未登录或管理员会话无效（ADMIN_AUTH_ENABLED=true 时）",
    },
}

SkillOpinionOutcomeHorizon = Literal["1d", "3d", "5d", "10d"]


class SkillOpinionOutcomeRunRequest(BaseModel):
    sample_id: Optional[int] = Field(None, gt=0)
    analysis_history_id: Optional[int] = Field(None, gt=0)
    skill_id: Optional[str] = Field(None, min_length=1, max_length=128)
    stock_code: Optional[str] = Field(None, min_length=1, max_length=16)
    horizons: Optional[List[SkillOpinionOutcomeHorizon]] = None
    limit: int = Field(100, ge=1, le=500)


class SkillOpinionOutcomeItem(BaseModel):
    id: int
    skill_opinion_sample_id: int
    analysis_history_id: int
    stock_code: str
    skill_id: str
    signal: str
    horizon: str
    engine_version: str
    eval_status: str
    outcome: Optional[str] = None
    direction_correct: Optional[bool] = None
    unable_reason: Optional[str] = None
    analysis_date: Optional[str] = None
    start_trade_date: Optional[str] = None
    end_trade_date: Optional[str] = None
    start_price: Optional[float] = None
    end_close: Optional[float] = None
    stock_return_pct: Optional[float] = None
    directional_return_pct: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SkillOpinionOutcomeRunResponse(BaseModel):
    items: List[SkillOpinionOutcomeItem] = Field(default_factory=list)
    processed_keys: int
    created: int
    updated: int
    skipped: int
    failed: int
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    limit_unit: str
    engine_version: str


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": "validation_error", "message": str(exc)},
    )


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error("%s: %s", message, exc, exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"error": "internal_error", "message": message},
    )


@router.post(
    "/outcomes/run",
    response_model=SkillOpinionOutcomeRunResponse,
    responses={
        **AUTH_RESPONSE,
        400: {"model": ErrorResponse, "description": "请求字段非法"},
        422: {"model": ErrorResponse, "description": "请求体校验失败"},
        500: {"model": ErrorResponse, "description": "后验计算失败"},
    },
    summary="触发技能观点后验评估",
    description=(
        "显式触发 skill-opinion 样本级后验计算；只处理缺失或 pending 的 "
        "sample+horizon 键，不覆盖终态结果。全部过滤条件可选：不传则按 "
        "engine 固定顺序批量回填，最多处理 limit 个键。"
    ),
    operation_id="runSkillOpinionOutcomes",
)
def run_outcomes(request: SkillOpinionOutcomeRunRequest) -> SkillOpinionOutcomeRunResponse:
    service = SkillOpinionOutcomeService()
    try:
        return SkillOpinionOutcomeRunResponse(
            **service.run_outcomes(
                sample_id=request.sample_id,
                analysis_history_id=request.analysis_history_id,
                skill_id=request.skill_id,
                stock_code=request.stock_code,
                horizons=request.horizons,
                limit=request.limit,
            )
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Run skill opinion outcomes failed", exc)
