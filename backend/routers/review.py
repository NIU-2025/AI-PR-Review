"""
Review API 路由

提供 PR Review 的核心 API 端点:
- POST /api/review           - 提交 PR URL 进行 Review
- POST /api/review/stream    - SSE 流式 Review
- POST /api/review/check-cache - 缓存预检 (Day 3)
- GET  /api/reviews          - 历史列表 (Day 3)
- GET  /api/review/{id}      - Review 详情 (Day 3)
- GET  /api/health           - 健康检查
"""

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from config import AppConfig, load_config
from models.schemas import PRReviewRequest, PRReviewResponse, ChangeSummary, RiskItem, AnalysisResult, CacheCheckResponse
from services.github_service import GitHubAPIError, GitHubService
from services.llm_service import LLMService
from utils.context_builder import ContextBuilder
from utils.file_storage import ResultStorage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["review"])


_app_config: AppConfig | None = None
_github_service: GitHubService | None = None
_llm_service: LLMService | None = None
_context_builder: ContextBuilder | None = None
_result_storage: ResultStorage | None = None


def _init_services():
    global _app_config, _github_service, _llm_service, _context_builder, _result_storage
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
        _result_storage = ResultStorage(config=_app_config.storage)
        logger.info("服务实例初始化完成")


def get_github_service() -> GitHubService:
    _init_services()
    return _github_service


def get_llm_service() -> LLMService:
    _init_services()
    return _llm_service


def get_context_builder() -> ContextBuilder:
    _init_services()
    return _context_builder


def get_result_storage() -> ResultStorage:
    _init_services()
    return _result_storage


@router.get("/health")
async def health_check():
    return {"status": "ok", "timestamp": time.time()}


# ──────────────────────────────────────────────
# Day 3: 缓存预检端点
# ──────────────────────────────────────────────

@router.post("/review/check-cache", response_model=CacheCheckResponse)
async def check_cache(
    request: PRReviewRequest,
    github: GitHubService = Depends(get_github_service),
    storage: ResultStorage = Depends(get_result_storage),
):
    try:
        owner, repo, pr_number = github.parse_pr_url(request.pr_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cached_meta = storage.find_cached(request.pr_url)
    logger.info(f"[check-cache] PR={request.pr_url}, cached_meta={'found' if cached_meta else 'NOT found'}")
    if cached_meta:
        logger.info(f"[check-cache] etag={cached_meta.get('etag', 'EMPTY')[:20]}..., review_id={cached_meta.get('review_id')}")

    if cached_meta and cached_meta.get("etag"):
        try:
            is_unchanged, _ = github.check_pr_updated(owner, repo, pr_number, cached_meta["etag"])
        except GitHubAPIError as e:
            logger.warning(f"缓存校验 API 调用失败: {e}, 降级为重新分析")
            is_unchanged = False

        if is_unchanged:
            cached_result = storage.load_result(cached_meta["review_id"])
            if cached_result:
                logger.info(f"缓存命中: {request.pr_url} → {cached_meta['review_id']}")
                pr_meta = cached_result.get("pr_metadata", {})
                analysis = cached_result.get("analysis", {})
                return CacheCheckResponse(
                    cached=True,
                    from_cache=True,
                    cached_at=cached_meta.get("saved_at"),
                    review_id=cached_meta["review_id"],
                    pr_metadata=pr_meta,
                    analysis=analysis,
                )

    logger.info(f"[check-cache] 缓存未命中: has_meta={'yes' if cached_meta else 'no'}, has_etag={'yes' if (cached_meta and cached_meta.get('etag')) else 'no'}")
    return CacheCheckResponse(cached=False)


# ──────────────────────────────────────────────
# POST /api/review (非流式)
# ──────────────────────────────────────────────

@router.post("/review", response_model=PRReviewResponse)
async def review_pr(
    request: PRReviewRequest,
    github: GitHubService = Depends(get_github_service),
    llm: LLMService = Depends(get_llm_service),
    context_builder: ContextBuilder = Depends(get_context_builder),
    storage: ResultStorage = Depends(get_result_storage),
):
    logger.info(f"[review_pr] >>> 入口, URL={request.pr_url}")
    start_time = time.time()

    try:
        pr_data = github.fetch_pr_data(request.pr_url)
        logger.info(f"[review_pr] Step1 OK: {pr_data.total_files} files")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except GitHubAPIError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e))

    has_changes = context_builder.has_meaningful_changes(pr_data)
    if not has_changes:
        logger.info(f"PR 无有效代码变更, 跳过分析")
        return PRReviewResponse(
            success=True,
            pr_url=request.pr_url,
            pr_metadata=pr_data.metadata,
            analysis=None,
            error="该 PR 不包含有效的代码变更（可能仅包含二进制文件、图片或空文件变更），无需分析。",
        )

    mode = context_builder.determine_mode(pr_data)

    if mode.value == "trivial":
        context = context_builder.build_trivial_context(pr_data)
    else:
        context = context_builder.build_context(pr_data)

    try:
        if mode.value == "trivial":
            analysis = llm.analyze(pr_data, context, mode)
        else:
            analysis = llm.analyze_per_file(pr_data, context, mode, context_builder)
    except RuntimeError as e:
        error_msg = _friendly_error(str(e))
        raise HTTPException(status_code=502, detail=error_msg)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"LLM 分析异常: {e}")

    total_ms = int((time.time() - start_time) * 1000)
    logger.info(f"PR Review 完成, 总耗时: {total_ms}ms")

    logger.info(f"[review_pr] Step4 准备保存: enabled={storage.enabled}, dir={storage.results_dir}")
    try:
        saved_id = storage.save_result(
            pr_url=request.pr_url,
            analysis=analysis,
            pr_metadata=pr_data.metadata,
            llm_model=llm.model,
            token_used=analysis.token_used,
            duration_ms=total_ms,
            etag=github.last_etag,
        )
        logger.info(f"[review_pr] Step4 保存完成: {saved_id}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(f"[review_pr] Step4 保存异常: {e}", exc_info=True)

    return PRReviewResponse(
        success=True,
        pr_url=request.pr_url,
        pr_metadata=pr_data.metadata,
        analysis=analysis,
    )


# ──────────────────────────────────────────────
# POST /api/review/stream (SSE 流式)
# ──────────────────────────────────────────────

@router.post("/review/stream")
async def review_pr_stream(
    request: PRReviewRequest,
    github: GitHubService = Depends(get_github_service),
    llm: LLMService = Depends(get_llm_service),
    context_builder: ContextBuilder = Depends(get_context_builder),
    storage: ResultStorage = Depends(get_result_storage),
):
    try:
        pr_data = github.fetch_pr_data(request.pr_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except GitHubAPIError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e))

    if not context_builder.has_meaningful_changes(pr_data):
        logger.info(f"流式端点: PR 无有效代码变更, 跳过分析")

        async def empty_pr_generator():
            yield _format_sse("meta", {
                "title": pr_data.metadata.title,
                "author": pr_data.metadata.author,
                "base_branch": pr_data.metadata.base_branch,
                "head_branch": pr_data.metadata.head_branch,
                "total_files": pr_data.total_files,
                "total_additions": pr_data.total_additions,
                "total_deletions": pr_data.total_deletions,
            })
            yield _format_sse("progress", "该 PR 不包含有效的代码变更（可能仅包含二进制文件、图片或空文件变更），无需分析。")
            yield _format_sse("done", {"model": "", "tokens": 0, "duration_ms": 0, "risk_count": 0, "risk_level": "trivial"})

        return StreamingResponse(
            empty_pr_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    mode = context_builder.determine_mode(pr_data)

    if mode.value == "trivial":
        context = context_builder.build_trivial_context(pr_data)
    else:
        context = context_builder.build_context(pr_data)

    async def sse_event_generator():
        try:
            yield _format_sse("meta", {
                "title": pr_data.metadata.title,
                "author": pr_data.metadata.author,
                "base_branch": pr_data.metadata.base_branch,
                "head_branch": pr_data.metadata.head_branch,
                "total_files": pr_data.total_files,
                "total_additions": pr_data.total_additions,
                "total_deletions": pr_data.total_deletions,
            })

            if mode.value == "trivial":
                yield _format_sse("progress", "正在分析变更总结...")
                analysis = llm.analyze(pr_data, context, mode)
                yield _format_sse("summary", analysis.summary.model_dump())
                yield _format_sse("done", {
                    "model": llm.model,
                    "tokens": analysis.token_used,
                    "duration_ms": analysis.analysis_duration_ms,
                    "risk_count": len(analysis.risks),
                    "risk_level": analysis.summary.risk_level,
                })

                storage.save_result(
                    pr_url=request.pr_url,
                    analysis=analysis,
                    pr_metadata=pr_data.metadata,
                    llm_model=llm.model,
                    token_used=analysis.token_used,
                    duration_ms=analysis.analysis_duration_ms,
                    etag=github.last_etag,
                )
            else:
                captured_summary = None
                captured_risks = []
                captured_done = None

                for event_dict in llm.analyze_per_file_stream(pr_data, context, mode, context_builder):
                    if event_dict["event"] == "summary":
                        captured_summary = event_dict["data"]
                    elif event_dict["event"] == "risk":
                        captured_risks.append(event_dict["data"])
                    elif event_dict["event"] == "done":
                        captured_done = event_dict["data"]
                    yield _format_sse(event_dict["event"], event_dict["data"])

                if captured_summary is not None and captured_done is not None:
                    try:
                        summary_obj = ChangeSummary(**captured_summary)
                        risk_objs = [RiskItem(**r) for r in captured_risks]
                        analysis_result = AnalysisResult(
                            summary=summary_obj,
                            risks=risk_objs,
                            llm_model=captured_done.get("model", llm.model),
                            token_used=captured_done.get("tokens", 0),
                            analysis_duration_ms=captured_done.get("duration_ms", 0),
                        )
                        logger.info(f"[sse_stream] 流式分析完成, 正在保存结果: risks={len(risk_objs)}, tokens={analysis_result.token_used}")
                        saved = storage.save_result(
                            pr_url=request.pr_url,
                            analysis=analysis_result,
                            pr_metadata=pr_data.metadata,
                            llm_model=analysis_result.llm_model,
                            token_used=analysis_result.token_used,
                            duration_ms=analysis_result.analysis_duration_ms,
                            etag=github.last_etag,
                        )
                        logger.info(f"[sse_stream] 保存完成: {saved}")
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        logger.error(f"[sse_stream] 保存异常: {e}", exc_info=True)

        except RuntimeError as e:
            logger.error(f"SSE 流分析失败: {e}")
            yield _format_sse("error", _friendly_error(str(e)))
        except Exception as e:
            logger.error(f"SSE 流分析异常: {e}", exc_info=True)
            yield _format_sse("error", f"系统内部异常: {e}")

    return StreamingResponse(
        sse_event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ──────────────────────────────────────────────
# Day 3: 历史记录 API
# ──────────────────────────────────────────────

@router.get("/reviews")
async def list_reviews(storage: ResultStorage = Depends(get_result_storage)):
    items = storage.list_all()
    return {"success": True, "count": len(items), "items": items}


@router.get("/review/{review_id}")
async def get_review_detail(review_id: str, storage: ResultStorage = Depends(get_result_storage)):
    result = storage.load_result(review_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Review 记录不存在: {review_id}")
    return {"success": True, **result}


def _friendly_error(raw_error: str) -> str:
    error_lower = raw_error.lower()
    if "connection" in error_lower or "connect" in error_lower:
        return "无法连接到 LLM API 服务。请检查: 1) 网络连接是否正常 2) .env 中的 LLM_API_BASE 地址是否正确 3) API 服务是否可用"
    if "auth" in error_lower or "401" in raw_error or "unauthorized" in error_lower:
        return "LLM API 认证失败。请检查 .env 中的 LLM_API_KEY 是否正确且未过期"
    if "rate" in error_lower or "429" in raw_error or "limit" in error_lower:
        return "LLM API 调用次数已达上限。请稍后重试或更换 API Key"
    if "timeout" in error_lower or "timed out" in error_lower:
        return "LLM API 响应超时。PR 文件较多或网络较慢时可能出现，请稍后重试"
    if "404" in raw_error:
        return "LLM API 端点不存在。请检查 .env 中的 LLM_API_BASE 和 LLM_MODEL 是否正确"
    return f"LLM 分析失败: {raw_error}"


def _format_sse(event: str, data) -> str:
    if isinstance(data, str):
        data_str = data
    else:
        data_str = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {data_str}\n\n"
