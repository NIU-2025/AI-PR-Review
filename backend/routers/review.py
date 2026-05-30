"""
Review API 路由

提供 PR Review 的核心 API 端点:
- POST /api/review  - 提交 PR URL 进行 Review
- GET  /api/health   - 健康检查
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from config import AppConfig, load_config
from models.schemas import PRReviewRequest, PRReviewResponse
from services.github_service import GitHubAPIError, GitHubService
from services.llm_service import LLMService
from utils.context_builder import ContextBuilder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review"])


# ──────────────────────────────────────────────
# 依赖注入: 全局服务实例 (简单单例)
# ──────────────────────────────────────────────

_app_config: AppConfig | None = None
_github_service: GitHubService | None = None
_llm_service: LLMService | None = None
_context_builder: ContextBuilder | None = None


def _init_services():
    """延迟初始化服务实例"""
    global _app_config, _github_service, _llm_service, _context_builder
    if _app_config is None:
        _app_config = load_config()
        _github_service = GitHubService(
            token=_app_config.github.api_token,
            base_url=_app_config.github.api_base_url,
            timeout=_app_config.github.request_timeout,
            max_retries=_app_config.github.max_retries,
        )
        _llm_service = LLMService(config=_app_config.llm)
        _context_builder = ContextBuilder(config=_app_config.context)
        logger.info("服务实例初始化完成")


def get_github_service() -> GitHubService:
    _init_services()
    return _github_service  # type: ignore


def get_llm_service() -> LLMService:
    _init_services()
    return _llm_service  # type: ignore


def get_context_builder() -> ContextBuilder:
    _init_services()
    return _context_builder  # type: ignore


# ──────────────────────────────────────────────
# API 端点
# ──────────────────────────────────────────────

@router.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "timestamp": time.time()}


@router.post("/review", response_model=PRReviewResponse)
async def review_pr(
    request: PRReviewRequest,
    github: GitHubService = Depends(get_github_service),
    llm: LLMService = Depends(get_llm_service),
    context_builder: ContextBuilder = Depends(get_context_builder),
):
    """
    PR Review 核心端点

    流程:
    1. 解析 PR URL
    2. 从 GitHub 拉取 PR 数据
    3. 构建分析上下文
    4. 调用 LLM 进行多阶段分析
    5. 返回结构化 Review 结果
    """
    start_time = time.time()

    # ── Step 1: 拉取 PR 数据 ──
    try:
        pr_data = github.fetch_pr_data(request.pr_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except GitHubAPIError as e:
        raise HTTPException(
            status_code=e.status_code or 502,
            detail=str(e),
        )

    # ── Step 2: 判定分析模式 & 构建上下文 ──
    mode = context_builder.determine_mode(pr_data)

    if mode.value == "trivial":
        context = context_builder.build_trivial_context(pr_data)
    else:
        context = context_builder.build_context(pr_data)

    # ── Step 3: LLM 分析 ──
    try:
        analysis = llm.analyze(pr_data, context, mode)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"LLM 分析失败: {e}")

    total_ms = int((time.time() - start_time) * 1000)
    logger.info(f"PR Review 完成, 总耗时: {total_ms}ms")

    return PRReviewResponse(
        success=True,
        pr_url=request.pr_url,
        pr_metadata=pr_data.metadata,
        analysis=analysis,
    )
