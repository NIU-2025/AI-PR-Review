"""
Review API 路由

提供 PR Review 的核心 API 端点:
- POST /api/review  - 提交 PR URL 进行 Review
- GET  /api/health   - 健康检查
"""

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

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
    # Day 2 升级: 非 trivial 模式使用逐文件深度分析
    try:
        if mode.value == "trivial":
            analysis = llm.analyze(pr_data, context, mode)
        else:
            analysis = llm.analyze_per_file(pr_data, context, mode, context_builder)
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


# ──────────────────────────────────────────────
# Day 2 新增: SSE 流式端点
# ──────────────────────────────────────────────

@router.post("/review/stream")
async def review_pr_stream(
    request: PRReviewRequest,
    github: GitHubService = Depends(get_github_service),
    llm: LLMService = Depends(get_llm_service),
    context_builder: ContextBuilder = Depends(get_context_builder),
):
    """
    PR Review 流式端点 (SSE)

    与 /api/review 分析逻辑相同, 但通过 Server-Sent Events 逐事件推送,
    前端可以渐进渲染分析进度, 大幅改善用户体验。

    事件流格式:
      event: progress
      data: 进度文字

      event: summary
      data: {"overview": "...", "key_changes": [...], ...}

      event: risk
      data: {"severity": "P1", "title": "...", "file": "...", ...}

      event: done
      data: {"model": "...", "tokens": 12345, "duration_ms": 8000, "risk_count": 3}

      event: error
      data: 错误信息
    """

    # ── Step 1: 拉取 PR 数据 (非流式, 因为这一步本身就很快) ──
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

    # ── Step 3: SSE 生成器 ──
    async def sse_event_generator():
        """
        SSE 事件流生成器

        包装 LLM 分析生成器, 将每个 yield 转换为符合 SSE 协议格式的字符串。
        同时在流开始时发送 PR 元信息, 在发生异常时发送 error 事件。
        """
        try:
            # ── 首先发送 PR 元信息 (前端可以立即展示 PR 标题/作者) ──
            yield _format_sse(
                "meta",
                {
                    "title": pr_data.metadata.title,
                    "author": pr_data.metadata.author,
                    "base_branch": pr_data.metadata.base_branch,
                    "head_branch": pr_data.metadata.head_branch,
                    "total_files": pr_data.total_files,
                    "total_additions": pr_data.total_additions,
                    "total_deletions": pr_data.total_deletions,
                },
            )

            # ── 流式分析 ──
            if mode.value == "trivial":
                # trivial 模式不使用逐文件分析, 用旧的非流式方法
                yield _format_sse("progress", "正在分析变更总结...")
                analysis = llm.analyze(pr_data, context, mode)
                yield _format_sse("summary", analysis.summary.model_dump())
                yield _format_sse(
                    "done",
                    {
                        "model": llm.model,
                        "tokens": analysis.token_used,
                        "duration_ms": analysis.analysis_duration_ms,
                        "risk_count": len(analysis.risks),
                        "risk_level": analysis.summary.risk_level,
                    },
                )
            else:
                # 逐文件分析 + 流式推送
                for event_dict in llm.analyze_per_file_stream(
                    pr_data, context, mode, context_builder
                ):
                    yield _format_sse(
                        event_dict["event"], event_dict["data"]
                    )

        except RuntimeError as e:
            logger.error(f"SSE 流分析失败: {e}")
            yield _format_sse("error", str(e))
        except Exception as e:
            logger.error(f"SSE 流分析异常: {e}", exc_info=True)
            yield _format_sse("error", f"分析异常: {e}")

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 Nginx 缓冲
        },
    )


def _format_sse(event: str, data) -> str:
    """
    将事件和数据格式化为 SSE 协议字符串

    SSE 格式:
      event: {事件类型}\n
      data: {JSON数据}\n
      \n

    Args:
        event: 事件类型 (progress / summary / risk / done / meta / error)
        data: 数据内容, str 或 dict

    Returns:
        SSE 格式的字符串
    """
    if isinstance(data, str):
        data_str = data
    else:
        data_str = json.dumps(data, ensure_ascii=False)

    return f"event: {event}\ndata: {data_str}\n\n"
